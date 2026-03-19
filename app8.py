import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from nicegui import app, ui

from chat_utils3 import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    EXTRACT_ADD_ON,
    MODELS,
    REASONING_LEVELS,
    AssistantTurn,
    Attachment,
    ChatClient,
    ConversationState,
    CouncilEntry,
    EditItem,
    EditRound,
    ExchangeEntry,
    PendingEdit,
    PromptBuilder,
    ReasoningEvent,
    UserTurn,
    search_files,
)

P_PROPS = 'dark outlined dense color=white'
MD_CLASSES = 'chat7-md max-w-none break-words'
STATIC_DIR = Path(__file__).with_name('static')
STATIC_V = max((p.stat().st_mtime_ns for p in STATIC_DIR.glob('chat7.*')), default=0)
HEAD_ASSETS = f'''
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/markdown-it-texmath/css/texmath.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.11.1/build/styles/github-dark.min.css">
<link rel="stylesheet" href="/chat7-static/chat7.css?v={STATIC_V}">
<script defer src="https://cdn.jsdelivr.net/npm/markdown-it@14.1.0/dist/markdown-it.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.11.1/build/highlight.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/mermaid@11.6.0/dist/mermaid.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/markdown-it-texmath/texmath.min.js"></script>
<script defer src="/chat7-static/chat7.js?v={STATIC_V}"></script>
'''
if STATIC_DIR.is_dir():
    try: app.add_static_files('/chat7-static', str(STATIC_DIR))
    except (RuntimeError, ValueError): pass


class Phase(StrEnum):
    IDLE = 'idle'
    STREAMING = 'streaming'
    COUNCIL_STREAMING = 'council_streaming'
    COUNCIL_SYNTHESIZING = 'council_synthesizing'
    AWAITING_EDIT = 'awaiting_edit'


@dataclass(slots=True)
class PageState:
    draft: str = ''
    model: str = DEFAULT_MODEL
    reasoning: str = DEFAULT_REASONING
    mode: Literal['chat+edit', 'chat', 'extract'] = 'chat+edit'
    file_attachments: list[str] = field(default_factory=list)
    url_attachments: list[Attachment] = field(default_factory=list)
    council_counts: dict[str, int] = field(default_factory=dict)
    search_results: list[str] = field(default_factory=list)
    search_idx: int = -1
    last_edit_status: str | None = None


@dataclass(slots=True)
class LiveRun:
    id: str
    kind: Literal['exchange', 'council_member', 'council_synthesis']
    entry_id: str
    target_id: str
    task: asyncio.Task | None = None
    renderer: Any = None
    started_at: float = 0.0
    reasoning: str = ''
    reasoning_delta: str = ''
    display_delta: str = ''
    raw_buffer: str = ''
    has_answer: bool = False
    done: bool = False
    error: str | None = None
    interrupted: bool = False
    reset_display: bool = False


@dataclass(slots=True)
class RunCoordinator:
    exchange_run: LiveRun | None = None
    member_runs: dict[str, LiveRun] = field(default_factory=dict)
    synthesis_run: LiveRun | None = None


@dataclass(slots=True)
class UiRefs:
    container: Any = None
    attachments: Any = None
    input_field: Any = None
    file_search: Any = None
    file_results_container: Any = None
    model_button: Any = None
    model_menu: Any = None
    model_menu_body: Any = None
    reasoning_select: Any = None
    mode_select: Any = None
    stop_btn: Any = None
    back_btn: Any = None
    send_btn: Any = None
    nodes: dict[str, Any] = field(default_factory=dict)
    content_ids: dict[str, str] = field(default_factory=dict)
    edit_slots: dict[str, Any] = field(default_factory=dict)
    timer_labels: dict[str, Any] = field(default_factory=dict)
    status_chips: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def new_id() -> str: return uuid4().hex
def int_or(x: Any, d: int = 0) -> int:
    try: return int(x)
    except Exception: return d


class ChatPageView:
    def __init__(self, page: 'ChatPageController'): self.page = page

    def js_call(self, method: str, *args: Any):
        s = ', '.join(json.dumps(a) for a in args)
        ui.run_javascript(f'(() => {{ const f = n => window.chat7 ? window.chat7.{method}({s}) : n > 0 && setTimeout(() => f(n - 1), 50); f(40); }})();')

    def set_markdown(self, content_id: str, text: str, now: bool = False): self.js_call('setMarkdown', content_id, text, now)
    def append_markdown(self, content_id: str, chunk: str): self.js_call('appendMarkdown', content_id, chunk)
    def focus_input(self): ui.run_javascript('document.getElementById("input-field")?.querySelector("textarea")?.focus()')
    def focus_file_search(self): ui.run_javascript('document.querySelector("#file-search")?.focus()')
    def scroll_bottom(self): self.js_call('scrollBottom')

    def scroll_active_into_view(self):
        p = self.page
        if p.page.search_idx >= 0: ui.run_javascript(f'document.getElementById("file-opt-{p.page.search_idx}")?.scrollIntoView({{block:"nearest"}});')

    def clear_search_results(self):
        p = self.page
        p.page.search_results, p.page.search_idx = [], -1
        if p.refs.file_results_container: p.refs.file_results_container.clear()

    def clear_rendered_messages(self):
        p = self.page
        if p.refs.container: p.refs.container.clear()
        p.refs.nodes.clear(), p.refs.content_ids.clear(), p.refs.edit_slots.clear(), p.refs.timer_labels.clear(), p.refs.status_chips.clear(), p.refs.order.clear()

    def update_controls(self):
        p, phase = self.page, self.page.phase()
        streaming, can_send, can_back = phase in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}, phase in {Phase.IDLE, Phase.AWAITING_EDIT}, phase not in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}
        if p.refs.stop_btn: p.refs.stop_btn.set_visibility(streaming)
        if p.refs.back_btn: p.refs.back_btn.set_visibility(can_back)
        if p.refs.send_btn: p.refs.send_btn.set_visibility(can_send)

    def refresh_model_picker(self):
        p = self.page
        if p.refs.model_button:
            p.refs.model_button.text = p.model_button_text()
            p.refs.model_button.update()
        if not p.refs.model_menu_body: return
        p.refs.model_menu_body.clear()
        with p.refs.model_menu_body:
            ui.label('Click to select · shift+click to add council').classes('text-xs text-gray-400 px-3 pt-2')
            for m in MODELS:
                row = ui.row().classes('w-96 items-center justify-between gap-3 px-3 py-2 rounded cursor-pointer hover:bg-gray-800')
                row.on('click', lambda e, x=m: p.on_model_pick(e, x))
                with row:
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('check', size='16px').classes('text-blue-300' if m == p.page.model else 'opacity-0')
                        ui.label(m).classes('text-sm text-gray-200')
                    n = p.page.council_counts.get(m, 0)
                    if n > 0: ui.label(f'×{n}').classes('text-xs text-blue-200 bg-blue-900/50 rounded px-2 py-0.5')
            if p.council_total() > 0:
                ui.separator().classes('w-full my-1')
                ui.button('Clear council selection', on_click=p.clear_council_selection).props('flat dense size=sm color=grey').classes('mx-3 mb-2')

    def render_pending_attachments(self):
        p = self.page
        if not p.refs.attachments: return
        p.refs.attachments.clear()
        with p.refs.attachments:
            for path in p.page.file_attachments:
                with ui.element('div').classes('pending-att'):
                    ui.icon('attach_file').classes('text-green-400')
                    ui.label(Path(path).name).classes('text-green-300 text-sm ellipsis')
                    ui.button(icon='close', on_click=lambda x=path: p.remove_file(x)).props('flat dense size=sm').classes('text-green-400')
            for a in p.page.url_attachments:
                with ui.element('div').classes('pending-att'):
                    ui.icon('link').classes('text-purple-300')
                    ui.label(a.url).classes('text-purple-200 text-sm ellipsis')
                    ui.button(icon='close', on_click=lambda x=a.url: p.remove_url(x)).props('flat dense size=sm').classes('text-purple-300')

    def render_edit_round_slot(self, slot: Any, assistant_id: str):
        p = self.page
        if not slot: return
        slot.clear()
        r = p.conversation.edit_rounds.get(assistant_id)
        if not r: return
        icon, cls, label = p.edit_round_meta(r.status)
        with slot:
            with ui.element('div').classes('tool-att'):
                ui.icon(icon).classes(cls)
                ui.label(label).classes('tool-att-label text-gray-300')
                if r.status == 'pending' and p.conversation.pending_edit and p.conversation.pending_edit.assistant_id == assistant_id:
                    ui.button('Apply', on_click=lambda i=assistant_id: asyncio.create_task(p.apply_pending_edits(i))).props('flat dense size=sm color=positive').classes('ml-1')
            if r.status != 'pending':
                for it in r.items:
                    icon, cls = ('check', 'text-green-400') if it.status == 'success' else ('warning', 'text-amber-400') if it.status == 'partial' else ('close', 'text-red-400')
                    with ui.element('div').classes('tool-att'):
                        ui.icon(icon).classes(cls)
                        ui.label(Path(it.label).name or it.label).classes('tool-att-label text-gray-300')

    def robot_chip(self, kind: str):
        chip = ui.element('div').classes(f'robot-chip robot-chip-{kind}')
        with chip:
            ui.element('div').classes('robot-chip-halo')
            with ui.element('div').classes('robot-chip-head'):
                ui.element('div').classes('robot-chip-antenna')
                with ui.element('div').classes('robot-chip-eyes'):
                    ui.element('div').classes('robot-chip-eye')
                    ui.element('div').classes('robot-chip-eye')
                ui.element('div').classes('robot-chip-mouth')
        return chip

    def set_assistant_status(self, assistant_id: str, status: str | None):
        if not (chips := self.page.refs.status_chips.get(assistant_id)): return
        chips[0].set_visibility(status == 'thinking')
        chips[1].set_visibility(status == 'answering')
    def build_tools(self, content_id: str, atts: list[Attachment] | None = None, assistant_id: str | None = None, timer_id: str | None = None, timer_value: int | None = None, status_id: str | None = None):
        p = self.page
        with ui.element('div').classes('answer-tools flex items-center gap-2 flex-wrap'):
            if status_id:
                t, a = self.robot_chip('thinking'), self.robot_chip('answering'); t.set_visibility(False); a.set_visibility(False); p.refs.status_chips[status_id] = (t, a)
            ui.button('', on_click=lambda i=content_id, b=f'{content_id}-copy': self.js_call('copyMarkdown', i, b)).props(f'icon=content_copy flat dense size=sm id={content_id}-copy').classes('tool-btn copy-icon')
            for a in atts or []:
                text = Path(a.path).name if a.kind == 'file' else a.url
                if not text: continue
                with ui.element('div').classes('tool-att'):
                    ui.icon('attach_file' if a.kind == 'file' else 'link').classes('text-gray-400')
                    ui.label(text).classes('tool-att-label text-gray-300')
            if assistant_id:
                p.refs.edit_slots[assistant_id] = ui.element('div').classes('inline-flex items-center gap-2 flex-wrap')
                self.render_edit_round_slot(p.refs.edit_slots[assistant_id], assistant_id)
            if timer_id:
                timer = ui.label(p.timer_text(timer_value)).classes('timer')
                p.refs.timer_labels[timer_id] = timer

    def render_message(self, token: str, role: str, content: str, label: str, atts: list[Attachment] | None = None, assistant_id: str | None = None, timer_id: str | None = None, timer_value: int | None = None):
        p = self.page
        if token in p.refs.nodes: return
        content_id = f'msg8-{len(p.refs.content_ids)}-{time.time_ns()}'
        p.refs.content_ids[token] = content_id
        with p.refs.container:
            wrap = ui.column().classes('w-full gap-1')
            p.refs.nodes[token], p.refs.order = wrap, p.refs.order + [token]
            with wrap:
                ui.label(label).classes('text-[11px] text-gray-500 px-1')
                if role == 'user':
                    with ui.element('div').classes('flex justify-start mb-1'):
                        with ui.element('div').classes('rounded-lg px-3 py-2 w-full min-w-0 user-bubble'):
                            ui.element('div').props(f'id={content_id}').classes(f'{MD_CLASSES} text-white')
                    with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                        self.build_tools(content_id, atts=atts)
                else:
                    with ui.element('div').classes('flex justify-start mb-1'):
                        with ui.element('div').classes('rounded-lg px-3 py-2 w-full min-w-0 answer-bubble'):
                            ui.element('div').props(f'id={content_id}').classes(f'{MD_CLASSES} text-white')
                    with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                        self.build_tools(content_id, assistant_id=assistant_id, timer_id=timer_id, timer_value=timer_value, status_id=timer_id)
        self.set_markdown(content_id, content, True)

    def render_history(self):
        p = self.page
        self.clear_rendered_messages()
        for e in p.conversation.entries:
            if isinstance(e, ExchangeEntry):
                self.render_message(p.exchange_user_token(e), 'user', e.user.display_text, 'You', e.user.attachments)
                self.render_message(p.exchange_assistant_token(e), 'assistant', p.assistant_display(e.assistant), e.assistant.label or e.assistant.model, assistant_id=e.assistant.id, timer_id=e.assistant.id, timer_value=p.assistant_timer_value(e.assistant.id))
                continue
            self.render_message(p.council_user_token(e), 'user', e.query.display_text, 'You · council', e.query.attachments)
            for m in e.members: self.render_message(p.council_member_token(e, m), 'assistant', p.assistant_display(m), m.label or m.model, timer_id=m.id, timer_value=p.assistant_timer_value(m.id))
            if e.synthesis: self.render_message(p.council_synthesis_token(e), 'assistant', p.assistant_display(e.synthesis), e.synthesis.label or e.synthesis.model, assistant_id=e.synthesis.id, timer_id=e.synthesis.id, timer_value=p.assistant_timer_value(e.synthesis.id))
        for aid in list(p.refs.status_chips): self.set_assistant_status(aid, p.assistant_status(aid))
        self.update_controls()
        self.scroll_bottom()

    def render_search_results(self):
        p = self.page
        p.refs.file_results_container.clear()
        with p.refs.file_results_container:
            if not p.page.search_results:
                ui.label('No files found').classes('text-gray-500 p-2')
                return
            for i, path in enumerate(p.page.search_results):
                active = ' active' if i == p.page.search_idx else ''
                row = ui.row().classes(f'w-full cursor-pointer p-2 rounded text-gray-300 file-option{active}').props(f'data-idx={i} id=file-opt-{i}')
                row.on('click', lambda _=None, x=path: p.select_file(x))
                with row:
                    ui.icon('description').classes('text-gray-500')
                    ui.label(Path(path).name).classes('flex-grow')
                    ui.label(str(Path(path).parent)).classes('text-xs text-gray-500')

    def build_header(self):
        p = self.page
        with ui.element('div').classes('fixed-header'):
            with ui.row().classes('gap-2 p-2 w-full items-start'):
                with ui.element('div').classes('relative'):
                    p.refs.model_button = ui.button(p.model_button_text(), icon='smart_toy').props('dark outline dense color=white').classes('text-white w-72 header-control header-model-btn')
                    with ui.menu() as p.refs.model_menu:
                        p.refs.model_menu_body = ui.column().classes('gap-0 p-1')
                p.refs.reasoning_select = ui.select(REASONING_LEVELS, label='Reasoning').props(P_PROPS).classes('text-white w-32 header-control').bind_value(p.page, 'reasoning')
                with ui.element('div').classes('flex-grow relative'):
                    p.refs.file_search = ui.input(placeholder='Search files or paste URL...').props(f'{P_PROPS} debounce=250 id=file-search').classes('w-full header-control')
                    p.refs.file_results_container = ui.column().classes('file-results')
                    p.refs.file_search.on_value_change(p.on_search)
                    p.refs.file_search.on('keydown', p.on_file_search_keydown)
        self.refresh_model_picker()

    def build_chat(self):
        with ui.element('div').classes('chat-stack'):
            self.page.refs.container = ui.column().classes('chat-container').props(f'id=chat8-{time.time_ns()}')

    def build_footer(self):
        p = self.page
        with ui.element('div').classes('chat-footer'):
            with ui.column().classes('w-full gap-0'):
                p.refs.attachments = ui.row().classes('pending-atts w-full px-3 gap-1 flex-wrap')
                with ui.row().classes('w-full p-2 pt-2 gap-2 items-start'):
                    p.refs.input_field = ui.textarea(placeholder='Type your message...').props(f'{P_PROPS} autogrow input-class="min-h-22 max-h-100" id=input-field').classes('flex-grow text-white').bind_value(p.page, 'draft')
                    p.refs.input_field.on('keydown', p.on_input_keydown)
                    with ui.element('div').classes('ctrl-grid'):
                        p.refs.mode_select = ui.select(['chat+edit', 'chat', 'extract'], label='Mode').props(P_PROPS).classes('ctrl-tile text-white').bind_value(p.page, 'mode')
                        with ui.element('div').classes('ctrl-stack'):
                            p.refs.send_btn = ui.button('Send', on_click=lambda: p.send(), icon='send').props('color=primary').classes('ctrl-tile')
                            p.refs.stop_btn = ui.button('Stop', on_click=p.stop_streaming, icon='stop').props('color=red').classes('ctrl-tile absolute inset-0')
                        p.refs.back_btn = ui.button('Back', on_click=p.undo, icon='undo').props('color=orange').classes('ctrl-tile')
                        ui.button('Clear', on_click=p.clear_chat, icon='delete').props('color=grey').classes('ctrl-tile')


@dataclass(slots=True)
class ChatPageController:
    storage: Any
    conversation: ConversationState
    page: PageState
    chat: ChatClient = field(default_factory=ChatClient)
    prompts: PromptBuilder = field(default_factory=PromptBuilder)
    refs: UiRefs = field(default_factory=UiRefs)
    runs: RunCoordinator = field(default_factory=RunCoordinator)
    view: Any = field(init=False, repr=False)

    def __post_init__(self): self.view = ChatPageView(self)

    @classmethod
    def load(cls, storage: Any):
        c, p = storage.get('conversation9'), storage.get('page9')
        x = cls(storage, c if isinstance(c, ConversationState) else ConversationState(), p if isinstance(p, PageState) else PageState())
        x.normalize_state()
        storage['conversation9'], storage['page9'] = x.conversation, x.page
        return x

    def normalize_state(self):
        if self.page.model not in MODELS: self.page.model = DEFAULT_MODEL
        if self.page.reasoning not in REASONING_LEVELS: self.page.reasoning = DEFAULT_REASONING
        if self.page.mode not in {'chat+edit', 'chat', 'extract'}: self.page.mode = 'chat+edit'
        self.page.file_attachments = [str(p).strip().replace('\\', '/') for p in (self.page.file_attachments or []) if str(p).strip()]
        self.page.url_attachments = [a for a in (self.page.url_attachments or []) if isinstance(a, Attachment) and a.kind == 'url' and a.url.strip()]
        self.page.council_counts = {m: max(1, int_or(n, 0)) for m, n in (self.page.council_counts or {}).items() if m in MODELS and int_or(n, 0) > 0}
        self.page.search_results, self.page.search_idx = [str(x) for x in (self.page.search_results or []) if str(x).strip()], int_or(self.page.search_idx, -1)
        self.prune_state()
        self.reconcile_entries()

    def phase(self) -> Phase:
        return Phase.STREAMING if self.runs.exchange_run else Phase.COUNCIL_STREAMING if self.runs.member_runs else Phase.COUNCIL_SYNTHESIZING if self.runs.synthesis_run else Phase.AWAITING_EDIT if self.conversation.pending_edit else Phase.IDLE

    def timer_text(self, seconds: int | None = None) -> str:
        x = 0 if seconds is None else max(0, int(seconds))
        return f'{x // 60}:{x % 60:02d}'

    def run_elapsed(self, r: LiveRun) -> int: return max(0, int(time.monotonic() - r.started_at)) if r.started_at > 0 else 0
    def council_total(self) -> int: return sum(self.page.council_counts.values())
    def model_button_text(self) -> str: return self.page.model if self.council_total() <= 0 else f'{self.page.model} +{self.council_total()} council'
    def set_draft_text(self, value: str): self.page.draft = value; self.refs.input_field and setattr(self.refs.input_field, 'value', value)

    def exchange_user_token(self, e: ExchangeEntry) -> str: return f'x:{e.id}:u'
    def exchange_assistant_token(self, e: ExchangeEntry) -> str: return f'x:{e.id}:a'
    def council_user_token(self, c: CouncilEntry) -> str: return f'c:{c.id}:u'
    def council_member_token(self, c: CouncilEntry, m: AssistantTurn) -> str: return f'c:{c.id}:m:{m.id}'
    def council_synthesis_token(self, c: CouncilEntry) -> str: return f'c:{c.id}:s'

    def assistant_display(self, a: AssistantTurn) -> str:
        return (a.display_text or a.raw_text).rstrip() or ('Response stopped.' if a.finalized else '')

    def assistant_timer_value(self, assistant_id: str) -> int:
        return self.run_elapsed(r) if (r := self.run_for_assistant(assistant_id)) else max(0, int((self.locate_assistant(assistant_id)[1] or AssistantTurn('', '', '')).elapsed))
    
    def assistant_status(self, assistant_id: str) -> str | None: return 'answering' if (r := self.run_for_assistant(assistant_id)) and r.has_answer else 'thinking' if r else None

    def file_ctx(self, atts: list[Attachment]) -> list[str]:
        return list(dict.fromkeys(a.path.strip().replace('\\', '/') for a in atts if a.kind == 'file' and a.path.strip()))

    def current_attachments(self) -> list[Attachment]:
        return [Attachment('file', path=p) for p in self.page.file_attachments] + [Attachment('url', url=a.url, content=a.content) for a in self.page.url_attachments]

    def restore_attachments(self, atts: list[Attachment]):
        self.page.file_attachments = [a.path.strip().replace('\\', '/') for a in atts if a.kind == 'file' and a.path.strip()]
        self.page.url_attachments = [Attachment('url', url=a.url, content=a.content) for a in atts if a.kind == 'url' and a.url.strip()]

    def edit_targets(self, text: str) -> list[str]:
        out = []
        for d in self.chat.parse_edit_markdown(text or '') or []:
            x = (d.filename or '').strip().replace('\\', '/')
            if x and x not in out: out.append(x)
        return out

    def edit_round_meta(self, status: str) -> tuple[str, str, str]:
        if status == 'pending': return 'tips_and_updates', 'text-blue-300', 'Edits available'
        if status == 'success': return 'check_circle', 'text-green-400', 'All edits applied'
        if status == 'partial': return 'warning', 'text-amber-400', 'Some edits applied'
        if status == 'rejected': return 'cancel', 'text-red-400', 'Edits rejected'
        return 'cancel', 'text-red-400', 'Edits not applied'

    def locate_entry(self, entry_id: str) -> ExchangeEntry | CouncilEntry | None:
        return next((e for e in self.conversation.entries if e.id == entry_id), None)

    def locate_assistant(self, assistant_id: str) -> tuple[ExchangeEntry | CouncilEntry | None, AssistantTurn | None, str | None, bool]:
        for e in self.conversation.entries:
            if isinstance(e, ExchangeEntry) and e.assistant.id == assistant_id: return e, e.assistant, self.exchange_assistant_token(e), False
            if isinstance(e, CouncilEntry):
                for m in e.members:
                    if m.id == assistant_id: return e, m, self.council_member_token(e, m), True
                if e.synthesis and e.synthesis.id == assistant_id: return e, e.synthesis, self.council_synthesis_token(e), False
        return None, None, None, False

    def valid_editable_assistant_ids(self) -> set[str]:
        out = set()
        for e in self.conversation.entries:
            if isinstance(e, ExchangeEntry): out.add(e.assistant.id)
            elif e.synthesis: out.add(e.synthesis.id)
        return out

    def prune_state(self):
        valid = self.valid_editable_assistant_ids()
        self.conversation.edit_rounds = {k: v for k, v in (self.conversation.edit_rounds or {}).items() if k in valid}
        if not isinstance(self.conversation.pending_edit, PendingEdit) or self.conversation.pending_edit.assistant_id not in valid or not self.conversation.pending_edit.text.strip(): self.conversation.pending_edit = None

    def settle_assistant(self, a: AssistantTurn):
        if a.finalized: return
        raw = (a.raw_text or '').rstrip()
        a.has_answer = a.has_answer or bool(raw)
        a.raw_text = raw or 'Response stopped.'
        a.display_text = (a.display_text or '').rstrip() or (self.chat.render_for_display(a.raw_text, a.ctx_files) if a.has_answer else a.raw_text)
        a.finalized, a.interrupted = True, True

    def reconcile_entries(self):
        for e in self.conversation.entries:
            if isinstance(e, ExchangeEntry):
                self.settle_assistant(e.assistant)
                continue
            for m in e.members: self.settle_assistant(m)
            if e.synthesis: self.settle_assistant(e.synthesis)
            if e.status in {'streaming_members', 'streaming_synthesis'}: e.status = 'interrupted'
        self.prune_state()

    def run_for_assistant(self, assistant_id: str) -> LiveRun | None:
        return self.runs.exchange_run if self.runs.exchange_run and self.runs.exchange_run.target_id == assistant_id else self.runs.member_runs.get(assistant_id) or (self.runs.synthesis_run if self.runs.synthesis_run and self.runs.synthesis_run.target_id == assistant_id else None)

    def is_live(self, r: LiveRun) -> bool:
        return self.runs.exchange_run is r or self.runs.member_runs.get(r.target_id) is r or self.runs.synthesis_run is r

    def drop_run(self, r: LiveRun):
        if self.runs.exchange_run is r: self.runs.exchange_run = None
        if self.runs.synthesis_run is r: self.runs.synthesis_run = None
        if self.runs.member_runs.get(r.target_id) is r: self.runs.member_runs.pop(r.target_id, None)

    def start_run(self, kind: Literal['exchange', 'council_member', 'council_synthesis'], entry_id: str, assistant: AssistantTurn, stream: Any):
        r = LiveRun(new_id(), kind, entry_id, assistant.id, started_at=time.monotonic())
        if kind == 'exchange': self.runs.exchange_run = r
        elif kind == 'council_synthesis': self.runs.synthesis_run = r
        else: self.runs.member_runs[assistant.id] = r
        r.task = asyncio.create_task(self.run_stream(r, stream))
        return r

    async def run_stream(self, r: LiveRun, stream: Any):
        err = None
        try:
            async for chunk in stream:
                if not self.is_live(r): return
                if isinstance(chunk, ReasoningEvent):
                    if not r.has_answer and chunk.text: r.reasoning, r.reasoning_delta = r.reasoning + chunk.text, r.reasoning_delta + chunk.text
                    continue
                if not isinstance(chunk, str) or not chunk: continue
                if not r.has_answer:
                    _, a, _, _ = self.locate_assistant(r.target_id)
                    r.has_answer, r.renderer, r.reset_display = True, self.chat.new_display_renderer(a.ctx_files if a else []), True
                r.raw_buffer += chunk
                if r.renderer and (delta := r.renderer.feed(chunk)): r.display_delta += delta
        except asyncio.CancelledError:
            return
        except Exception as e:
            err = str(e)
        finally:
            if self.is_live(r): r.error, r.done = err, True

    def finalize_run(self, r: LiveRun):
        if not self.is_live(r): return
        e, a, token, is_member = self.locate_assistant(r.target_id)
        if not a:
            self.drop_run(r)
            return
        if r.has_answer and r.renderer and hasattr(r.renderer, 'finish') and (delta := r.renderer.finish() or ''):
            a.display_text += delta
            if token in self.refs.content_ids: self.view.append_markdown(self.refs.content_ids[token], delta)
        raw = (r.raw_buffer or a.raw_text or '').rstrip() or 'Response stopped.'
        display = (a.display_text or '').rstrip() or (self.chat.render_for_display(raw, a.ctx_files) if r.has_answer else raw)
        a.raw_text, a.display_text, a.has_answer = raw, display, r.has_answer or bool((a.raw_text or '').strip())
        a.elapsed, a.finalized, a.interrupted, a.error = self.run_elapsed(r), True, r.interrupted, r.error
        if token in self.refs.content_ids and not r.has_answer: self.view.set_markdown(self.refs.content_ids[token], display, True)
        if a.id in self.refs.timer_labels: self.refs.timer_labels[a.id].text = self.timer_text(a.elapsed)
        self.view.set_assistant_status(a.id, None)
        self.drop_run(r)
        if r.error: ui.notify(f'{a.label or a.model}: {r.error}', type='negative')
        if isinstance(e, ExchangeEntry):
            if a.has_answer and not r.error and not r.interrupted and self.chat.parse_edit_markdown(raw): self.set_pending_edits(raw, a.id)
            self.view.update_controls()
            return
        if not isinstance(e, CouncilEntry): return
        if is_member:
            if e.status == 'streaming_members' and not self.runs.member_runs and all(m.finalized for m in e.members): self.start_council_synthesis(e)
            self.view.update_controls()
            return
        e.status = 'completed' if not (r.error or r.interrupted) else 'interrupted'
        if a.has_answer and not r.error and not r.interrupted and self.chat.parse_edit_markdown(raw): self.set_pending_edits(raw, a.id)
        self.view.update_controls()

    def build_council_prompt(self, c: CouncilEntry) -> str:
        parts = ['Following the above conversation, I decided to elicit multiple opinions for the following query:', c.query.display_text, 'Analyze the following responses critically, consider their respective merits, and combine their insights with your own reasoning to create the best overall answer to my original query:']
        for i, m in enumerate(c.members, start=1): parts += [f'### {i}. {m.label}', (m.raw_text or m.display_text).rstrip() or 'Response stopped.']
        parts.append('Provide the answer directly without talking about the individual responses.')
        return '\n\n'.join(parts).rstrip()

    def on_model_pick(self, event, model: str):
        if event.args.get('shiftKey'):
            if self.phase() in {Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}:
                ui.notify('Finish the current council first', type='warning')
                return
            self.page.council_counts[model] = self.page.council_counts.get(model, 0) + 1
            self.view.refresh_model_picker()
            return
        self.page.model = model
        self.view.refresh_model_picker()
        with contextlib.suppress(Exception): self.refs.model_menu.close()

    def clear_council_selection(self):
        self.page.council_counts = {}
        self.view.refresh_model_picker()

    def note_text(self, status: str | None) -> str | None:
        return {
            'applied': 'I have accepted and implemented your latest round of edits above.',
            'partial': 'I have applied the uniquely matchable parts of your latest round of edits above; some commands failed.',
            'failed': 'I could not apply your latest round of edits above.',
            'rejected': 'I rejected your latest round of edits above.',
        }.get(status)

    def set_pending_edits(self, text: str, assistant_id: str):
        text, targets = (text or '').rstrip(), self.edit_targets(text) or ['edits']
        self.conversation.pending_edit = PendingEdit(assistant_id, text, targets)
        self.conversation.edit_rounds[assistant_id] = EditRound(status='pending', items=[EditItem(t, 'pending') for t in targets], text=text)
        self.page.last_edit_status = 'pending'
        self.view.render_edit_round_slot(self.refs.edit_slots.get(assistant_id), assistant_id)
        self.view.update_controls()

    def reopen_edit_round(self, assistant_id: str) -> bool:
        r, text = self.conversation.edit_rounds.get(assistant_id), (self.conversation.edit_rounds.get(assistant_id).text if self.conversation.edit_rounds.get(assistant_id) else '').rstrip()
        if not r or r.status != 'rejected' or not text or self.conversation.pending_edit: return False
        targets = [it.label.strip() for it in r.items if it.label.strip()] or self.edit_targets(text) or ['edits']
        self.conversation.pending_edit, self.page.last_edit_status = PendingEdit(assistant_id, text, targets), None
        self.conversation.edit_rounds[assistant_id] = EditRound(status='pending', items=[EditItem(t, 'pending') for t in targets], text=text)
        self.view.render_edit_round_slot(self.refs.edit_slots.get(assistant_id), assistant_id)
        self.view.update_controls()
        return True

    def clear_pending_edits(self, reject: bool = False):
        p = self.conversation.pending_edit
        if not p: return
        if reject:
            self.conversation.edit_rounds[p.assistant_id] = EditRound(status='rejected', items=[EditItem(t, 'error') for t in (p.targets or ['edits'])], text=p.text)
            self.page.last_edit_status = 'rejected'
        else:
            self.conversation.edit_rounds.pop(p.assistant_id, None)
        self.view.render_edit_round_slot(self.refs.edit_slots.get(p.assistant_id), p.assistant_id)
        self.conversation.pending_edit = None
        self.view.update_controls()

    def clear_edit_round_state(self, before_send: bool = False) -> str | None:
        if before_send and self.conversation.pending_edit: self.clear_pending_edits(reject=True)
        elif not before_send: self.clear_pending_edits(reject=False)
        note, self.page.last_edit_status = self.note_text(self.page.last_edit_status) if before_send else None, None
        return note

    async def apply_pending_edits(self, assistant_id: str):
        p = self.conversation.pending_edit
        if not p or p.assistant_id != assistant_id or not p.text.strip(): return
        text, targets = p.text.rstrip(), p.targets[:] or self.edit_targets(p.text) or ['edits']
        self.conversation.pending_edit = None
        try:
            events = self.chat.apply_markdown_edits(text, assistant_id, (self.locate_assistant(assistant_id)[1] or AssistantTurn('', '', '')).ctx_files) or []
        except Exception as e:
            self.conversation.edit_rounds[assistant_id], self.page.last_edit_status = EditRound(status='error', items=[EditItem(t, 'error') for t in targets], text=text), 'failed'
            self.view.render_edit_round_slot(self.refs.edit_slots.get(assistant_id), assistant_id)
            ui.notify(f'Edit error: {e}', type='negative')
            self.view.update_controls()
            return
        kind_map, rank = {'complete': 'success', 'partial': 'partial', 'error': 'error'}, {'success': 0, 'partial': 1, 'error': 2}
        items = [EditItem(str(ev.path or ev.filename or 'edit').strip().replace('\\', '/'), kind_map[ev.kind]) for ev in events if ev.kind in kind_map] or [EditItem(t, 'error') for t in targets]
        merged = {}
        for it in items: merged[it.label] = it.status if it.label not in merged or rank[it.status] > rank[merged[it.label]] else merged[it.label]
        items = [EditItem(k, merged[k]) for k in merged]
        n_ok, n_partial, n_err = sum(x.status == 'success' for x in items), sum(x.status == 'partial' for x in items), sum(x.status == 'error' for x in items)
        status = 'error' if n_err and not (n_ok or n_partial) else 'partial' if n_partial or (n_ok and n_err) else 'success'
        self.conversation.edit_rounds[assistant_id], self.page.last_edit_status = EditRound(status=status, items=items, text=text), ('applied' if status == 'success' else 'partial' if status == 'partial' else 'failed')
        self.view.render_edit_round_slot(self.refs.edit_slots.get(assistant_id), assistant_id)
        self.view.update_controls()
        if x := self.chat.consume_user_input_prefill():
            self.set_draft_text(x)
            self.view.focus_input()

    def try_attach_file(self, path: str) -> bool:
        if err := self.chat.validate_file_attachment(path):
            ui.notify(err, type='negative')
            return False
        if path not in self.page.file_attachments: self.page.file_attachments.append(path)
        return True

    def select_file(self, path: str):
        if self.try_attach_file(path): self.refresh_file_picker()

    def attach_multiple(self, paths: list[str]):
        any_added = False
        for p in paths: any_added = self.try_attach_file(p) or any_added
        if any_added or paths: self.refresh_file_picker()

    async def attach_url(self, url: str):
        try:
            u, content = self.chat.normalize_url(url), await self.chat.fetch_url_content(self.chat.normalize_url(url))
            if not any(a.url == u for a in self.page.url_attachments): self.page.url_attachments.append(Attachment('url', url=u, content=content))
            self.refresh_file_picker()
        except Exception as e:
            ui.notify(f'URL error: {e}', type='negative')

    def refresh_file_picker(self):
        self.refs.file_search.value = ''
        self.view.clear_search_results()
        self.view.render_pending_attachments()
        self.view.focus_file_search()

    async def on_search(self):
        q = (self.refs.file_search.value or '').strip()
        self.view.clear_search_results()
        if len(q) < 2: return
        results = await asyncio.to_thread(search_files, q)
        if (self.refs.file_search.value or '').strip() != q: return
        self.page.search_results = results or []
        self.view.render_search_results()

    async def on_file_search_keydown(self, event):
        key, results, n = event.args.get('key'), self.page.search_results, len(self.page.search_results)
        if key in {'ArrowDown', 'Down'}:
            if n == 0: return
            self.page.search_idx = (self.page.search_idx + 1) % n
            self.view.render_search_results()
            self.view.scroll_active_into_view()
            return
        if key in {'ArrowUp', 'Up'}:
            if n == 0: return
            self.page.search_idx = (self.page.search_idx - 1) % n
            self.view.render_search_results()
            self.view.scroll_active_into_view()
            return
        if key == 'Escape':
            self.refs.file_search.value = ''
            self.view.clear_search_results()
            return
        if key != 'Enter': return
        q = (self.refs.file_search.value or '').strip()
        if q and self.chat.looks_like_url(q):
            await self.attach_url(q)
            return
        if '*' in q:
            self.attach_multiple(await asyncio.to_thread(search_files, q) or [])
            return
        if n == 0: return
        self.select_file(results[self.page.search_idx if 0 <= self.page.search_idx < n else 0])

    async def on_input_keydown(self, event):
        if event.args.get('key') != 'Enter' or event.args.get('shiftKey'): return
        prevent = getattr(event, 'prevent_default', None)
        if callable(prevent):
            result = prevent()
            if asyncio.iscoroutine(result): await result
        self.send()

    def remove_file(self, path: str):
        with contextlib.suppress(ValueError): self.page.file_attachments.remove(path)
        self.view.render_pending_attachments()
        if self.phase() in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}: ui.notify('File removed; changes will reflect after the current response finishes.', type='info')

    def remove_url(self, url: str):
        self.page.url_attachments = [a for a in self.page.url_attachments if a.url != url]
        self.view.render_pending_attachments()
        if self.phase() in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}: ui.notify('URL removed; changes will reflect after the current response finishes.', type='info')

    def start_exchange(self, msg: str, note: str | None, atts: list[Attachment]):
        display, history, force_edit = (f'{note}\n\n{msg}' if note else msg), (f'{(f"{note}\n\n{msg}" if note else msg)}\n\n{EXTRACT_ADD_ON}' if self.page.mode == 'extract' else (f'{note}\n\n{msg}' if note else msg)), self.page.mode == 'chat+edit'
        e = ExchangeEntry(new_id(), UserTurn(new_id(), display, msg, history, [Attachment(a.kind, a.path, a.url, a.content) for a in atts], force_edit), AssistantTurn(new_id(), self.page.model, self.page.model, ctx_files=self.file_ctx(atts)))
        self.conversation.entries.append(e)
        self.page.file_attachments, self.page.url_attachments = [], []
        self.set_draft_text('')
        self.start_run('exchange', e.id, e.assistant, self.chat.stream(self.prompts.normal_request_messages(self.conversation, e), self.page.model, self.page.reasoning))
        self.view.render_history()
        self.view.render_pending_attachments()

    def start_council(self, msg: str, note: str | None, atts: list[Attachment]):
        counts, display, force_edit = self.page.council_counts.copy(), (f'{note}\n\n{msg}' if note else msg), self.page.mode == 'chat+edit'
        member_prompt = f'{display}\n\n{EXTRACT_ADD_ON}' if self.page.mode == 'extract' else display
        members = [AssistantTurn(new_id(), model, model if n == 1 else f'{model} #{i}', ctx_files=self.file_ctx(atts)) for model in MODELS for n in [counts.get(model, 0)] for i in range(1, n + 1)]
        c = CouncilEntry(new_id(), UserTurn(new_id(), display, msg, display, [Attachment(a.kind, a.path, a.url, a.content) for a in atts], force_edit), member_prompt, members=members, status='streaming_members')
        self.conversation.entries.append(c)
        self.page.file_attachments, self.page.url_attachments, self.page.council_counts = [], [], {}
        for m in c.members: self.start_run('council_member', c.id, m, self.chat.stream(self.prompts.member_request_messages(self.conversation, c), m.model, self.page.reasoning))
        self.set_draft_text('')
        self.view.refresh_model_picker()
        self.view.render_history()
        self.view.render_pending_attachments()

    def start_council_synthesis(self, c: CouncilEntry):
        if c.synthesis or c.status != 'streaming_members': return
        c.synthesis, c.status = AssistantTurn(new_id(), self.page.model, f'Synthesis · {self.page.model}', ctx_files=self.file_ctx(c.query.attachments)), 'streaming_synthesis'
        self.start_run('council_synthesis', c.id, c.synthesis, self.chat.stream(self.prompts.synthesis_request_messages(self.conversation, c, self.build_council_prompt(c)), self.page.model, self.page.reasoning))
        self.view.render_history()

    def cancel_run(self, r: LiveRun):
        if isinstance(r.task, asyncio.Task) and not r.task.done(): r.task.cancel()
        self.drop_run(r)

    def cancel_entry_runs(self, entry_id: str):
        if self.runs.exchange_run and self.runs.exchange_run.entry_id == entry_id: self.cancel_run(self.runs.exchange_run)
        if self.runs.synthesis_run and self.runs.synthesis_run.entry_id == entry_id: self.cancel_run(self.runs.synthesis_run)
        for r in [r for r in self.runs.member_runs.values() if r.entry_id == entry_id]: self.cancel_run(r)

    def active_council(self) -> CouncilEntry | None:
        entry_id = self.runs.synthesis_run.entry_id if self.runs.synthesis_run else next(iter(self.runs.member_runs.values())).entry_id if self.runs.member_runs else None
        e = self.locate_entry(entry_id) if entry_id else None
        return e if isinstance(e, CouncilEntry) else None

    def send(self):
        msg = (self.refs.input_field.value or '').strip()
        if self.phase() in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING} or not msg: return
        note, atts = self.clear_edit_round_state(before_send=True), self.current_attachments()
        if self.council_total() > 0:
            self.start_council(msg, note, atts)
            return
        self.start_exchange(msg, note, atts)

    def stop_streaming(self):
        if self.runs.exchange_run:
            r = self.runs.exchange_run
            r.interrupted, r.done = True, True
            if isinstance(r.task, asyncio.Task) and not r.task.done(): r.task.cancel()
            self.finalize_run(r)
            ui.notify('Response stopped', type='info')
            return
        if self.runs.member_runs:
            c = self.active_council()
            if not c:
                ui.notify('No active response to stop', type='warning')
                return
            c.status = 'interrupted'
            for r in list(self.runs.member_runs.values()):
                r.interrupted, r.done = True, True
                if isinstance(r.task, asyncio.Task) and not r.task.done(): r.task.cancel()
                self.finalize_run(r)
            ui.notify('Council stopped', type='info')
            return
        if self.runs.synthesis_run:
            c, r = self.active_council(), self.runs.synthesis_run
            if c: c.status = 'interrupted'
            r.interrupted, r.done = True, True
            if isinstance(r.task, asyncio.Task) and not r.task.done(): r.task.cancel()
            self.finalize_run(r)
            ui.notify('Synthesis stopped', type='info')
            return
        ui.notify('No active response to stop', type='warning')

    def undo(self):
        if not self.conversation.entries:
            ui.notify('No messages to undo', type='warning')
            return
        self.clear_edit_round_state()
        e = self.conversation.entries[-1]
        self.cancel_entry_runs(e.id)
        try:
            if isinstance(e, ExchangeEntry):
                if not self.chat.rollback_edits_for_assistant(e.assistant.id): raise RuntimeError(f'Failed to rollback edits for assistant {e.assistant.id}')
                restore, atts = e.user.restore_text, e.user.attachments
            else:
                if e.synthesis and not self.chat.rollback_edits_for_assistant(e.synthesis.id): raise RuntimeError(f'Failed to rollback edits for assistant {e.synthesis.id}')
                restore, atts = e.query.restore_text, e.query.attachments
        except Exception as x:
            ui.notify(f'Undo failed: {x}', type='negative')
            return
        self.conversation.entries.pop()
        self.prune_state()
        self.restore_attachments(atts)
        self.set_draft_text(restore)
        if (aid := next((x for x in [y.assistant.id for y in reversed(self.conversation.entries) if isinstance(y, ExchangeEntry)] + [y.synthesis.id for y in reversed(self.conversation.entries) if isinstance(y, CouncilEntry) and y.synthesis] if x), None)): self.reopen_edit_round(aid)
        self.view.render_history()
        self.view.render_pending_attachments()
        self.view.focus_input()

    def clear_chat(self):
        self.clear_edit_round_state()
        self.cancel_entry_runs(self.runs.exchange_run.entry_id) if self.runs.exchange_run else None
        self.cancel_entry_runs(self.runs.synthesis_run.entry_id) if self.runs.synthesis_run else None
        for r in list(self.runs.member_runs.values()): self.cancel_run(r)
        self.conversation.entries, self.conversation.pending_edit, self.conversation.edit_rounds = [], None, {}
        self.page.file_attachments, self.page.url_attachments, self.page.council_counts, self.page.search_results, self.page.search_idx = [], [], {}, [], -1
        self.set_draft_text('')
        self.view.clear_search_results()
        self.view.render_history()
        self.view.render_pending_attachments()
        self.view.refresh_model_picker()
        ui.notify('Chat cleared', type='positive')

    def flush_updates(self):
        for r in [x for x in [self.runs.exchange_run, self.runs.synthesis_run, *self.runs.member_runs.values()] if x]:
            _, a, token, _ = self.locate_assistant(r.target_id)
            self.view.set_assistant_status(r.target_id, 'answering' if r.has_answer else 'thinking')
            if token in self.refs.content_ids:
                content_id = self.refs.content_ids[token]
                if r.reset_display:
                    self.view.set_markdown(content_id, '', True)
                    a and setattr(a, 'display_text', '')
                    r.reset_display = False
                if not r.has_answer and r.reasoning_delta:
                    delta, r.reasoning_delta = r.reasoning_delta, ''
                    self.view.append_markdown(content_id, delta)
                if r.has_answer and r.display_delta:
                    delta, r.display_delta = r.display_delta, ''
                    if a: a.display_text, a.raw_text = a.display_text + delta, r.raw_buffer
                    self.view.append_markdown(content_id, delta)
            if r.done: self.finalize_run(r)

    def tick_timer(self):
        for aid, label in list(self.refs.timer_labels.items()):
            if aid in self.refs.timer_labels: self.refs.timer_labels[aid].text = self.timer_text(self.assistant_timer_value(aid))

    def mount(self):
        self.view.build_header()
        self.view.build_chat()
        self.view.build_footer()
        self.view.render_history()
        self.view.render_pending_attachments()
        if self.page.search_results: self.view.render_search_results()
        self.view.update_controls()
        ui.timer(0.05, self.flush_updates)
        ui.timer(1.0, self.tick_timer)
        if not (self.refs.input_field.value or '').strip() and (p := self.chat.consume_user_input_prefill()): self.set_draft_text(p)


@ui.page('/')
async def main_page():
    ui.add_head_html(HEAD_ASSETS)
    await ui.context.client.connected()
    ChatPageController.load(app.storage.tab).mount()


if __name__ in {'__main__', '__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8888)
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False, reconnect_timeout=300, ssl_certfile='cert.pem', ssl_keyfile='key.pem')
