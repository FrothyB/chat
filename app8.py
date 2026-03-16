import argparse
import contextlib
import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from nicegui import app, ui

from chat_utils3 import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    EXTRACT_ADD_ON,
    MODELS,
    REASONING_LEVELS,
    STYLE_CSS,
    ChatClient,
    ReasoningEvent,
    search_files,
)

P_PROPS = 'dark outlined dense color=white'
MD_CLASSES = 'chat7-md max-w-none break-words'
STATIC_DIR = Path(__file__).with_name('static')
STATIC_V = max((p.stat().st_mtime_ns for p in STATIC_DIR.glob('chat7.*')), default=0)
HEAD_ASSETS = f'''
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
    AWAITING_EDIT = 'awaiting_edit_decision'


@dataclass(slots=True)
class UrlAttachment:
    url: str
    content: str = ''


@dataclass(slots=True)
class EditItem:
    label: str
    status: Literal['pending', 'success', 'partial', 'error']


@dataclass(slots=True)
class EditRound:
    status: Literal['pending', 'success', 'partial', 'rejected', 'error']
    items: list[EditItem] = field(default_factory=list)
    text: str = ''


@dataclass(slots=True)
class CouncilMember:
    token: str
    model: str
    label: str
    ordinal: int = 1
    ctx_files: list[str] = field(default_factory=list)
    raw: str = ''
    display: str = ''
    display_delta: str = ''
    reasoning: str = ''
    reasoning_delta: str = ''
    renderer: Any = None
    reset_display: bool = False
    task: asyncio.Task | None = None
    started_at: float = 0.0
    elapsed: int = 0
    done: bool = False
    finalized: bool = False
    error: str | None = None
    has_answer: bool = False


@dataclass(slots=True)
class CouncilRound:
    round_id: int
    query: str
    display_query: str
    attachments: list[dict[str, str]] = field(default_factory=list)
    members: list[CouncilMember] = field(default_factory=list)
    synthesis: CouncilMember | None = None
    hidden_user_index: int | None = None
    hidden_assistant_index: int | None = None


@dataclass(slots=True)
class UiState:
    phase: Phase = Phase.IDLE
    draft: str = ''
    model: str = DEFAULT_MODEL
    reasoning: str = DEFAULT_REASONING
    mode: Literal['chat+edit', 'chat', 'extract'] = 'chat+edit'
    url_attachments: list[UrlAttachment] = field(default_factory=list)
    pending_edits_text: str | None = None
    last_edit_status: str | None = None
    pending_edit_assistant: int | None = None
    pending_edit_targets: list[str] = field(default_factory=list)
    edit_rounds: dict[int, EditRound] = field(default_factory=dict)
    search_results: list[str] = field(default_factory=list)
    search_idx: int = -1
    answer_timers: dict[int, int] = field(default_factory=dict)
    council_counts: dict[str, int] = field(default_factory=dict)
    councils: list[CouncilRound] = field(default_factory=list)
    next_council_id: int = 1


@dataclass(slots=True)
class StreamState:
    raw: str = ''
    display: str = ''
    display_delta: str = ''
    reasoning: str = ''
    reasoning_delta: str = ''
    renderer: Any = None
    reset_display: bool = False
    task: asyncio.Task | None = None
    started_at: float = 0.0
    done: bool = False
    error: str | None = None
    has_answer: bool = False
    assistant: int | None = None
    run_id: int = 0

    def reset(self):
        self.raw, self.display, self.display_delta = '', '', ''
        self.reasoning, self.reasoning_delta = '', ''
        self.renderer, self.reset_display, self.task = None, False, None
        self.started_at, self.done, self.error, self.has_answer, self.assistant = 0.0, False, None, False, None


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
    edit_slots: dict[int, Any] = field(default_factory=dict)
    timer_labels: dict[Any, Any] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def item_status(x: Any) -> Literal['pending', 'success', 'partial', 'error']: return x if x in {'pending', 'success', 'partial', 'error'} else 'error'
def round_status(x: Any) -> Literal['pending', 'success', 'partial', 'rejected', 'error']: return x if x in {'pending', 'success', 'partial', 'rejected', 'error'} else 'error'


def coerce_url_attachment(x: Any) -> UrlAttachment | None:
    if isinstance(x, UrlAttachment): return x if x.url.strip() else None
    if not isinstance(x, dict): return None
    u = str(x.get('url') or '').strip()
    return UrlAttachment(u, str(x.get('content') or '')) if u else None


def coerce_edit_round(x: Any) -> EditRound:
    raw = {'status': x.status, 'items': x.items, 'text': x.text} if isinstance(x, EditRound) else x if isinstance(x, dict) else {}
    items = []
    for it in raw.get('items') or []:
        if isinstance(it, EditItem):
            if it.label.strip(): items.append(EditItem(it.label.strip(), item_status(it.status)))
            continue
        if not isinstance(it, dict): continue
        label = str(it.get('label') or '').strip()
        if label: items.append(EditItem(label, item_status(it.get('status'))))
    return EditRound(status=round_status(raw.get('status')), items=items, text=str(raw.get('text') or '').rstrip())


def coerce_council_member(x: Any) -> CouncilMember | None:
    if isinstance(x, CouncilMember): return x if x.token and x.model and x.label else None
    if not isinstance(x, dict): return None
    token, model, label = str(x.get('token') or '').strip(), str(x.get('model') or '').strip(), str(x.get('label') or '').strip()
    if not token or not model or not label: return None
    return CouncilMember(
        token=token,
        model=model,
        label=label,
        ordinal=max(1, int(x.get('ordinal') or 1)),
        ctx_files=[str(p) for p in (x.get('ctx_files') or []) if str(p).strip()],
        raw=str(x.get('raw') or ''),
        display=str(x.get('display') or ''),
        reasoning=str(x.get('reasoning') or ''),
        elapsed=max(0, int(x.get('elapsed') or 0)),
        done=bool(x.get('done')),
        finalized=bool(x.get('finalized')),
        error=(str(x.get('error')) if x.get('error') is not None else None),
        has_answer=bool(x.get('has_answer')),
    )


def coerce_council_round(x: Any) -> CouncilRound | None:
    if isinstance(x, CouncilRound): return x if x.members else None
    if not isinstance(x, dict): return None
    members = [m for y in (x.get('members') or []) if (m := coerce_council_member(y))]
    if not members: return None
    return CouncilRound(
        round_id=max(1, int(x.get('round_id') or 1)),
        query=str(x.get('query') or ''),
        display_query=str(x.get('display_query') or ''),
        attachments=[dict(a) for a in (x.get('attachments') or []) if isinstance(a, dict)],
        members=members,
        synthesis=coerce_council_member(x.get('synthesis')),
        hidden_user_index=(max(1, int(x.get('hidden_user_index'))) if x.get('hidden_user_index') is not None else None),
        hidden_assistant_index=(max(1, int(x.get('hidden_assistant_index'))) if x.get('hidden_assistant_index') is not None else None),
    )


@dataclass(slots=True)
class ChatPageController:
    storage: Any
    state: UiState
    chat: ChatClient
    refs: UiRefs = field(default_factory=UiRefs)
    stream: StreamState = field(default_factory=StreamState)

    @classmethod
    def load(cls, storage: Any):
        state, chat = storage.get('ui_state8'), storage.get('chat8')
        page = cls(storage, state if isinstance(state, UiState) else UiState(), chat if isinstance(chat, ChatClient) else ChatClient())
        page.normalize_state()
        storage['ui_state8'], storage['chat8'] = page.state, page.chat
        return page

    def normalize_state(self):
        try: self.state.phase = Phase(self.state.phase)
        except ValueError: self.state.phase = Phase.IDLE
        if self.state.model not in MODELS: self.state.model = DEFAULT_MODEL
        if self.state.reasoning not in REASONING_LEVELS: self.state.reasoning = DEFAULT_REASONING
        if self.state.mode not in {'chat+edit', 'chat', 'extract'}: self.state.mode = 'chat+edit'
        self.state.url_attachments = [a for x in (self.state.url_attachments or []) if (a := coerce_url_attachment(x))]
        self.state.edit_rounds = {i: coerce_edit_round(r) for i, r in (self.state.edit_rounds or {}).items() if isinstance(i, int) and i >= 0}
        self.state.answer_timers = {i: max(0, int(v)) for i, v in (self.state.answer_timers or {}).items() if isinstance(i, int) and i >= 0}
        self.state.council_counts = {m: max(1, int(n)) for m, n in (self.state.council_counts or {}).items() if m in MODELS and int(n) > 0}
        self.state.councils = [c for x in (self.state.councils or []) if (c := coerce_council_round(x))]
        if self.state.phase in {Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING} and not self.state.councils: self.state.phase = Phase.IDLE

    def js_call(self, method: str, *args: Any):
        s = ', '.join(json.dumps(a) for a in args)
        ui.run_javascript(f'(() => {{ const f = n => window.chat7 ? window.chat7.{method}({s}) : n > 0 && setTimeout(() => f(n - 1), 50); f(40); }})();')

    def set_markdown(self, content_id: str, text: str, now: bool = False): self.js_call('setMarkdown', content_id, text, now)
    def append_markdown(self, content_id: str, chunk: str): self.js_call('appendMarkdown', content_id, chunk)
    def focus_input(self): ui.run_javascript('document.getElementById("input-field")?.querySelector("textarea")?.focus()')
    def focus_file_search(self): ui.run_javascript('document.querySelector("#file-search")?.focus()')
    def scroll_active_into_view(self): ui.run_javascript(f'document.getElementById("file-opt-{self.state.search_idx}")?.scrollIntoView({{block:"nearest"}});') if self.state.search_idx >= 0 else None
    def stream_elapsed(self) -> int: return max(0, int(time.monotonic() - self.stream.started_at)) if self.stream.started_at > 0 else 0
    def timer_text(self, seconds: int | None = None) -> str: x = self.stream_elapsed() if seconds is None else max(0, int(seconds)); return f'{x // 60}:{x % 60:02d}'
    def council_elapsed(self, m: CouncilMember) -> int: return max(0, int(time.monotonic() - m.started_at)) if m.started_at > 0 and not m.finalized else m.elapsed
    def set_draft_text(self, value: str): self.state.draft, self.refs.input_field.value = value, value

    def normal_token(self, idx: int) -> str: return f'm-{idx}'
    def council_user_token(self, c: CouncilRound) -> str: return f'c-{c.round_id}-u'
    def council_total(self) -> int: return sum(self.state.council_counts.values())
    def latest_council(self) -> CouncilRound | None: return self.state.councils[-1] if self.state.councils else None
    def active_council(self) -> CouncilRound | None: return self.latest_council() if self.state.phase in {Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING} else None
    def council_query_text(self, c: CouncilRound) -> str: return (c.display_query or c.query).rstrip() or c.query
    def council_prompt_text(self, m: CouncilMember) -> str: return (m.raw or m.display or m.reasoning).rstrip() or ('Response stopped.' if m.finalized else '')
    def council_display_text(self, m: CouncilMember) -> str: return (m.display or m.raw or m.reasoning).rstrip() or ('Response stopped.' if m.finalized else '')
    def hidden_council_indices(self) -> set[int]: return {i for c in self.state.councils for i in (c.hidden_user_index, c.hidden_assistant_index) if isinstance(i, int) and i >= 0}
    def council_members(self, c: CouncilRound) -> list[CouncilMember]: return c.members + ([c.synthesis] if c.synthesis else [])
    def active_council_matches(self, c: CouncilRound) -> bool: return bool(self.active_council() and self.active_council().round_id == c.round_id)

    def build_council_display(self, c: CouncilRound) -> str:
        parts = ['Following the above conversation, I decided to elicit multiple opinions for the following query:', self.council_query_text(c), 'Analyze the following responses critically, consider their respective merits, and combine their insights with your own reasoning to create the best overall answer to my original query:']
        for i, m in enumerate(c.members, start=1): parts += [f'### {i}. {m.label}', self.council_prompt_text(m)]
        parts.append('Provide the answer directly without talking about the individual responses.')
        return '\n\n'.join(parts).rstrip()

    def clear_search_results(self):
        self.state.search_results, self.state.search_idx = [], -1
        if self.refs.file_results_container: self.refs.file_results_container.clear()

    def clear_rendered_messages(self):
        if self.refs.container: self.refs.container.clear()
        self.refs.nodes.clear(), self.refs.content_ids.clear(), self.refs.edit_slots.clear(), self.refs.timer_labels.clear(), self.refs.order.clear()

    def refresh_file_picker(self):
        self.refs.file_search.value = ''
        self.clear_search_results()
        self.render_pending_attachments()
        self.focus_file_search()

    def prune_edit_rounds(self): self.state.edit_rounds = {i: v for i, v in self.state.edit_rounds.items() if 0 <= i < len(self.chat.messages)}
    def prune_answer_timers(self): self.state.answer_timers = {i: max(0, int(v)) for i, v in self.state.answer_timers.items() if 0 <= i < len(self.chat.messages) and self.chat.messages[i].get('role') == 'assistant'}

    def current_stream_assistant(self) -> int | None:
        ai = self.stream.assistant
        if ai is not None and 0 <= ai < len(self.chat.messages) and self.chat.messages[ai].get('role') == 'assistant': return ai
        ai = len(self.chat.messages) - 1
        return ai if ai >= 0 and self.chat.messages[ai].get('role') == 'assistant' else None

    def update_controls(self):
        streaming = self.state.phase in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}
        can_send = self.state.phase in {Phase.IDLE, Phase.AWAITING_EDIT}
        can_back = not streaming
        if self.refs.stop_btn: self.refs.stop_btn.set_visibility(streaming)
        if self.refs.back_btn: self.refs.back_btn.set_visibility(can_back)
        if self.refs.send_btn: self.refs.send_btn.set_visibility(can_send)

    def set_phase(self, phase: Phase):
        self.state.phase = phase
        self.update_controls()

    def model_button_text(self) -> str:
        n = self.council_total()
        return self.state.model if n <= 0 else f'{self.state.model} +{n} council'

    def refresh_model_picker(self):
        if self.refs.model_button:
            self.refs.model_button.text = self.model_button_text()
            self.refs.model_button.update()
        if not self.refs.model_menu_body: return
        self.refs.model_menu_body.clear()
        with self.refs.model_menu_body:
            ui.label('Click to select · shift+click to add council').classes('text-xs text-gray-400 px-3 pt-2')
            for m in MODELS:
                row = ui.row().classes('w-96 items-center justify-between gap-3 px-3 py-2 rounded cursor-pointer hover:bg-gray-800')
                row.on('click', lambda e, x=m: self.on_model_pick(e, x))
                with row:
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('check', size='16px').classes('text-blue-300' if m == self.state.model else 'opacity-0')
                        ui.label(m).classes('text-sm text-gray-200')
                    n = self.state.council_counts.get(m, 0)
                    if n > 0: ui.label(f'×{n}').classes('text-xs text-blue-200 bg-blue-900/50 rounded px-2 py-0.5')
            if self.council_total() > 0:
                ui.separator().classes('w-full my-1')
                ui.button('Clear council selection', on_click=self.clear_council_selection).props('flat dense size=sm color=grey').classes('mx-3 mb-2')

    def on_model_pick(self, event, model: str):
        if event.args.get('shiftKey'):
            if self.active_council():
                ui.notify('Finish the current council first', type='warning')
                return
            self.state.council_counts[model] = self.state.council_counts.get(model, 0) + 1
            self.refresh_model_picker()
            return
        self.state.model = model
        self.refresh_model_picker()
        with contextlib.suppress(Exception): self.refs.model_menu.close()

    def clear_council_selection(self):
        self.state.council_counts = {}
        self.refresh_model_picker()

    def reset_stream(self, invalidate: bool = False):
        if invalidate: self.stream.run_id += 1
        self.stream.reset()
        if self.state.phase == Phase.STREAMING: self.set_phase(Phase.IDLE)

    def stream_begin(self, assistant_index: int):
        self.stream.run_id += 1
        self.stream.reset()
        self.stream.assistant, self.stream.started_at = assistant_index, time.monotonic()
        self.set_phase(Phase.STREAMING)

    def stream_start_answer(self):
        self.stream.has_answer = True
        self.stream.raw, self.stream.display, self.stream.display_delta = '', '', ''
        self.stream.reasoning, self.stream.reasoning_delta = '', ''
        self.stream.renderer, self.stream.reset_display = self.chat.new_display_renderer(), True

    def stream_flush_renderer(self) -> str:
        if not (self.stream.has_answer and self.stream.renderer and hasattr(self.stream.renderer, 'finish')): return ''
        delta = self.stream.renderer.finish() or ''
        if delta: self.stream.display += delta
        return delta

    def stream_snapshot(self) -> tuple[int | None, str, str]:
        ai = self.current_stream_assistant()
        raw_answer = (self.chat.messages[ai].get('content') or '') if ai is not None and 0 <= ai < len(self.chat.messages) else ''
        raw = (raw_answer or self.stream.raw).rstrip() if self.stream.has_answer else (self.stream.reasoning or raw_answer or self.stream.raw).rstrip()
        raw = raw or 'Response stopped.'
        display = self.stream.display.rstrip() if self.stream.has_answer else raw
        if not display: display = self.chat.render_for_display(raw)
        return ai, raw, display

    def record_stream_timer(self, ai: int | None):
        if ai is not None: self.state.answer_timers[ai] = self.stream_elapsed()

    def stream_commit(self, err: str | None = None, interrupted: bool = False):
        if self.state.phase != Phase.STREAMING: return
        if interrupted and isinstance(self.stream.task, asyncio.Task) and not self.stream.task.done(): self.stream.task.cancel()
        ai = self.current_stream_assistant()
        if self.stream.has_answer:
            delta = self.stream_flush_renderer()
            if delta and ai is not None and (token := self.normal_token(ai)) in self.refs.content_ids: self.append_markdown(self.refs.content_ids[token], delta)
        ai, raw, display = self.stream_snapshot()
        had_answer = self.stream.has_answer
        if ai is not None:
            self.chat.ensure_assistant_nonempty(ai, raw)
            self.chat.set_assistant_display(ai, display)
            self.record_stream_timer(ai)
            token = self.normal_token(ai)
            if token in self.refs.content_ids and not had_answer: self.set_markdown(self.refs.content_ids[token], display, True)
            if ai in self.refs.timer_labels: self.refs.timer_labels[ai].text = self.timer_text(self.state.answer_timers.get(ai))
        self.reset_stream(invalidate=True)
        if err: ui.notify(f'Error: {err}', type='negative')
        if ai is not None and had_answer and self.chat.parse_edit_markdown(raw): self.set_pending_edits(raw, ai)

    def settle_orphaned_stream(self):
        if self.state.phase == Phase.STREAMING: self.stream_commit(interrupted=True)

    def finalize_council_member(self, m: CouncilMember):
        if m.finalized: return
        if m.has_answer and m.renderer and hasattr(m.renderer, 'finish'):
            delta = m.renderer.finish() or ''
            if delta: m.display += delta
        raw = (m.raw.rstrip() if m.has_answer else (m.reasoning or m.raw).rstrip()) or 'Response stopped.'
        display = (m.display.rstrip() if m.has_answer else raw) or (self.chat.render_for_display(raw, m.ctx_files) if m.has_answer else raw)
        m.raw, m.display, m.elapsed, m.task, m.renderer, m.finalized = raw, display, self.council_elapsed(m), None, None, True

    def persist_council_synthesis(self, c: CouncilRound):
        if not c.synthesis or c.hidden_assistant_index is None: return
        ai = c.hidden_assistant_index
        self.chat.ensure_assistant_nonempty(ai, c.synthesis.raw)
        self.chat.set_assistant_display(ai, c.synthesis.display)
        self.state.answer_timers[ai] = c.synthesis.elapsed

    def settle_orphaned_councils(self):
        had_pending = False
        for c in self.state.councils:
            for m in self.council_members(c):
                if isinstance(m.task, asyncio.Task) and not m.task.done(): m.task.cancel()
                m.task = None
                if not m.finalized: self.finalize_council_member(m)
            if c.synthesis:
                self.persist_council_synthesis(c)
                if c.hidden_assistant_index is not None and c.synthesis.has_answer and self.chat.parse_edit_markdown(c.synthesis.raw):
                    self.set_pending_edits(c.synthesis.raw, c.hidden_assistant_index)
                    had_pending = True
        if self.state.phase in {Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING} and not had_pending: self.state.phase = Phase.IDLE

    def cleanup_token_refs(self, token: str):
        self.refs.content_ids.pop(token, None)
        for k in ([token, int(token[2:])] if token.startswith('m-') and token[2:].isdigit() else [token]):
            self.refs.timer_labels.pop(k, None)
            if isinstance(k, int): self.refs.edit_slots.pop(k, None)

    def pop_last_node(self) -> str | None:
        if not self.refs.order: return None
        token = self.refs.order.pop()
        self.cleanup_token_refs(token)
        if n := self.refs.nodes.pop(token, None): n.delete()
        return token

    def remove_node(self, token: str):
        self.cleanup_token_refs(token)
        if token in self.refs.order: self.refs.order.remove(token)
        if n := self.refs.nodes.pop(token, None): n.delete()

    def render_pending_attachments(self):
        if not self.refs.attachments: return
        self.refs.attachments.clear()
        with self.refs.attachments:
            for p in self.chat.files:
                with ui.element('div').classes('pending-att'):
                    ui.icon('attach_file').classes('text-green-400')
                    ui.label(Path(p).name).classes('text-green-300 text-sm ellipsis')
                    ui.button(icon='close', on_click=lambda x=p: self.remove_file(x)).props('flat dense size=sm').classes('text-green-400')
            for item in self.state.url_attachments:
                with ui.element('div').classes('pending-att'):
                    ui.icon('link').classes('text-purple-300')
                    ui.label(item.url).classes('text-purple-200 text-sm ellipsis')
                    ui.button(icon='close', on_click=lambda x=item.url: self.remove_url(x)).props('flat dense size=sm').classes('text-purple-300')

    def current_attachments(self) -> list[dict[str, str]]:
        return [{'kind': 'file', 'path': p} for p in (self.chat.files or [])] + [{'kind': 'url', 'url': x.url, 'content': x.content} for x in self.state.url_attachments]

    def restore_attachments(self, attachments: list[dict[str, str]]):
        self.chat.files = [((a.get('path') or '').strip().replace('\\', '/')) for a in attachments if (a.get('kind') or '').lower() == 'file' and (a.get('path') or '').strip()]
        self.state.url_attachments = [UrlAttachment(str(a.get('url') or ''), str(a.get('content') or '')) for a in attachments if (a.get('kind') or '').lower() == 'url' and str(a.get('url') or '').strip()]

    def edit_targets(self, text: str) -> list[str]:
        out = []
        for d in self.chat.parse_edit_markdown(text or '') or []:
            raw = (d.filename or '').strip().replace('\\', '/')
            name = Path(raw).name or raw
            if name and name not in out: out.append(name)
        return out

    def edit_round_meta(self, status: str) -> tuple[str, str, str]:
        if status == 'pending': return 'tips_and_updates', 'text-blue-300', 'Edits available'
        if status == 'success': return 'check_circle', 'text-green-400', 'All edits applied'
        if status == 'partial': return 'warning', 'text-amber-400', 'Some edits applied'
        if status == 'rejected': return 'cancel', 'text-red-400', 'Edits rejected'
        return 'cancel', 'text-red-400', 'Edits not applied'

    def render_edit_round_slot(self, slot: Any, assistant_index: int):
        if not slot: return
        slot.clear()
        r = self.state.edit_rounds.get(assistant_index)
        if not r: return
        icon, cls, label = self.edit_round_meta(r.status)
        with slot:
            with ui.element('div').classes('tool-att'):
                ui.icon(icon).classes(cls)
                ui.label(label).classes('tool-att-label text-gray-300')
                if r.status == 'pending' and assistant_index == self.state.pending_edit_assistant:
                    ui.button('Apply', on_click=lambda i=assistant_index: asyncio.create_task(self.apply_pending_edits(i))).props('flat dense size=sm color=positive').classes('ml-1')
            if r.status != 'pending':
                for it in r.items:
                    icon, cls = ('check', 'text-green-400') if it.status == 'success' else ('warning', 'text-amber-400') if it.status == 'partial' else ('close', 'text-red-400')
                    with ui.element('div').classes('tool-att'):
                        ui.icon(icon).classes(cls)
                        ui.label(it.label).classes('tool-att-label text-gray-300')

    async def apply_pending_edits(self, assistant_index: int):
        if assistant_index != self.state.pending_edit_assistant or not (self.state.pending_edits_text or '').strip(): return
        text = (self.state.pending_edits_text or '').rstrip()
        targets = self.state.pending_edit_targets[:] or self.edit_targets(text) or ['edits']
        self.state.pending_edits_text, self.state.pending_edit_assistant, self.state.pending_edit_targets = None, None, []
        self.set_phase(Phase.IDLE)
        try:
            events = self.chat.apply_markdown_edits(text, assistant_index) or []
        except Exception as e:
            self.state.edit_rounds[assistant_index], self.state.last_edit_status = EditRound(status='error', items=[EditItem(t, 'error') for t in targets], text=text), 'failed'
            self.render_edit_round_slot(self.refs.edit_slots.get(assistant_index), assistant_index)
            ui.notify(f'Edit error: {e}', type='negative')
            return
        kind_map, rank = {'complete': 'success', 'partial': 'partial', 'error': 'error'}, {'success': 0, 'partial': 1, 'error': 2}
        items = [EditItem(Path(ev.path or ev.filename).name or (ev.path or ev.filename or 'edit'), kind_map[ev.kind]) for ev in events if ev.kind in kind_map] or [EditItem(t, 'error') for t in targets]
        merged, order = {}, []
        for it in items:
            if it.label not in merged: merged[it.label], order = it.status, order + [it.label]
            elif rank[it.status] > rank[merged[it.label]]: merged[it.label] = it.status
        items = [EditItem(k, merged[k]) for k in order]
        n_ok, n_partial, n_err = sum(it.status == 'success' for it in items), sum(it.status == 'partial' for it in items), sum(it.status == 'error' for it in items)
        status = 'error' if n_err and not (n_ok or n_partial) else 'partial' if n_partial or (n_ok and n_err) else 'success'
        self.state.edit_rounds[assistant_index], self.state.last_edit_status = EditRound(status=status, items=items, text=text), ('applied' if status == 'success' else 'partial' if status == 'partial' else 'failed')
        self.render_edit_round_slot(self.refs.edit_slots.get(assistant_index), assistant_index)
        if p := self.chat.consume_user_input_prefill():
            self.set_draft_text(p)
            self.focus_input()

    def set_pending_edits(self, text: str, assistant_index: int):
        text, targets = (text or '').rstrip(), self.edit_targets(text) or ['edits']
        self.state.pending_edits_text, self.state.pending_edit_assistant, self.state.pending_edit_targets = text, assistant_index, targets
        self.state.last_edit_status = 'pending'
        self.state.edit_rounds[assistant_index] = EditRound(status='pending', items=[EditItem(t, 'pending') for t in targets], text=text)
        self.set_phase(Phase.AWAITING_EDIT)
        self.render_edit_round_slot(self.refs.edit_slots.get(assistant_index), assistant_index)

    def reopen_edit_round(self, assistant_index: int) -> bool:
        r = self.state.edit_rounds.get(assistant_index)
        text = (r.text if r else '').rstrip()
        if not r or r.status != 'rejected' or not text or self.state.pending_edits_text or not (0 <= assistant_index < len(self.chat.messages)): return False
        targets = [it.label.strip() for it in r.items if it.label.strip()] or self.edit_targets(text) or ['edits']
        self.state.pending_edits_text, self.state.pending_edit_assistant, self.state.pending_edit_targets, self.state.last_edit_status = text, assistant_index, targets, None
        self.state.edit_rounds[assistant_index] = EditRound(status='pending', items=[EditItem(t, 'pending') for t in targets], text=text)
        self.set_phase(Phase.AWAITING_EDIT)
        self.render_edit_round_slot(self.refs.edit_slots.get(assistant_index), assistant_index)
        return True

    def clear_pending_edits(self, reject: bool = False):
        text, ai = (self.state.pending_edits_text or '').rstrip(), self.state.pending_edit_assistant
        if reject and text and ai is not None:
            targets = self.state.pending_edit_targets[:] or self.edit_targets(text) or ['edits']
            self.state.edit_rounds[ai], self.state.last_edit_status = EditRound(status='rejected', items=[EditItem(t, 'error') for t in targets], text=text), 'rejected'
            self.render_edit_round_slot(self.refs.edit_slots.get(ai), ai)
        elif text and ai is not None:
            self.state.edit_rounds.pop(ai, None)
            self.render_edit_round_slot(self.refs.edit_slots.get(ai), ai)
        self.state.pending_edits_text, self.state.pending_edit_assistant, self.state.pending_edit_targets = None, None, []
        if self.state.phase == Phase.AWAITING_EDIT: self.set_phase(Phase.IDLE)

    def clear_edit_round_state(self, before_send: bool = False) -> str | None:
        if before_send and self.state.pending_edits_text: self.clear_pending_edits(reject=True)
        elif not before_send: self.clear_pending_edits(reject=False)
        status = self.state.last_edit_status
        note = {
            'applied': 'I have accepted and implemented your latest round of edits above.',
            'partial': 'I have applied the uniquely matchable parts of your latest round of edits above; some commands failed.',
            'failed': 'I could not apply your latest round of edits above.',
            'rejected': 'I rejected your latest round of edits above.',
        }.get(status) if before_send and status else None
        self.state.last_edit_status = None
        return note

    def build_tools(self, content_id: str, atts: list[dict[str, str]] | None = None, assistant_index: int | None = None, with_timer: bool = False, timer_value: int | None = None, timer_key: Any = None):
        with ui.element('div').classes('answer-tools flex items-center gap-2 flex-wrap'):
            ui.button('', on_click=lambda i=content_id, b=f'{content_id}-copy': self.js_call('copyMarkdown', i, b)).props(f'icon=content_copy flat dense size=sm id={content_id}-copy').classes('tool-btn copy-icon')
            for a in atts or []:
                kind = (a.get('kind') or '').lower()
                text = Path((a.get('path') or '')).name if kind == 'file' else (a.get('url') or '') if kind == 'url' else ''
                if not text: continue
                with ui.element('div').classes('tool-att'):
                    ui.icon('attach_file' if kind == 'file' else 'link').classes('text-gray-400')
                    ui.label(text).classes('tool-att-label text-gray-300')
            if assistant_index is not None:
                self.refs.edit_slots[assistant_index] = ui.element('div').classes('inline-flex items-center gap-2 flex-wrap')
                self.render_edit_round_slot(self.refs.edit_slots[assistant_index], assistant_index)
            if with_timer or timer_value is not None:
                key = timer_key if timer_key is not None else assistant_index
                timer = ui.label(self.timer_text(timer_value)).classes('timer')
                if key is not None: self.refs.timer_labels[key] = timer

    def render_message(self, token: str, role: str, content: str, label: str, atts: list[dict[str, str]] | None = None, streaming: bool = False, assistant_index: int | None = None, timer_value: int | None = None, timer_key: Any = None):
        if token in self.refs.nodes: return
        content_id = f'msg8-{len(self.refs.content_ids)}-{time.time_ns()}'
        self.refs.content_ids[token] = content_id
        with self.refs.container:
            wrap = ui.column().classes('w-full gap-1')
            self.refs.nodes[token], self.refs.order = wrap, self.refs.order + [token]
            with wrap:
                ui.label(label).classes('self-end text-[11px] text-gray-500 px-1' if role == 'user' else 'text-[11px] text-gray-500 px-1')
                if role == 'user':
                    with ui.element('div').classes('flex justify-end mb-1'):
                        with ui.element('div').classes('inline-block bg-blue-600 rounded-lg px-3 py-2 max-w-full min-w-0 user-bubble'):
                            ui.element('div').props(f'id={content_id}').classes(f'{MD_CLASSES} text-white')
                    with ui.element('div').classes('flex justify-end mb-3'):
                        self.build_tools(content_id, atts=atts)
                else:
                    with ui.element('div').classes('flex justify-start mb-1'):
                        with ui.element('div').classes('bg-gray-800 rounded-lg px-3 py-2 w-full min-w-0 answer-bubble'):
                            ui.element('div').props(f'id={content_id}').classes(f'{MD_CLASSES} text-white')
                    with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                        self.build_tools(content_id, assistant_index=assistant_index, with_timer=streaming, timer_value=timer_value, timer_key=timer_key)
        self.set_markdown(content_id, content, True)

    def render_council_round(self, c: CouncilRound):
        self.render_message(self.council_user_token(c), 'user', self.council_query_text(c), 'You · council', c.attachments)
        for m in c.members: self.render_message(m.token, 'assistant', self.council_display_text(m), m.label, streaming=self.active_council_matches(c) and self.state.phase == Phase.COUNCIL_STREAMING and not m.finalized, timer_value=m.elapsed, timer_key=m.token)
        if c.synthesis: self.render_message(c.synthesis.token, 'assistant', self.council_display_text(c.synthesis), c.synthesis.label, streaming=self.active_council_matches(c) and self.state.phase == Phase.COUNCIL_SYNTHESIZING and not c.synthesis.finalized, assistant_index=c.hidden_assistant_index, timer_value=c.synthesis.elapsed, timer_key=c.synthesis.token)

    def render_history(self):
        self.clear_rendered_messages()
        hidden, anchored, trailing = self.hidden_council_indices(), {}, []
        for c in self.state.councils:
            if c.hidden_user_index is None: trailing.append(c)
            else: anchored.setdefault(c.hidden_user_index, []).append(c)
        for i, m in enumerate(self.chat.messages[1:], start=1):
            for c in anchored.get(i, []): self.render_council_round(c)
            role = m.get('role')
            if i in hidden or role not in {'user', 'assistant'}: continue
            atts = [dict(a) for a in (self.chat.message_attachments.get(i, []) or [])] if role == 'user' else []
            label = 'You' if role == 'user' else (self.chat.assistant_models.get(i) or 'Assistant')
            self.render_message(self.normal_token(i), role, self.chat.display_text_for(i), label, atts, assistant_index=i if role == 'assistant' else None, timer_value=self.state.answer_timers.get(i), timer_key=i if role == 'assistant' else None)
        for c in trailing: self.render_council_round(c)
        self.update_controls()

    def render_search_results(self):
        self.refs.file_results_container.clear()
        with self.refs.file_results_container:
            if not self.state.search_results:
                ui.label('No files found').classes('text-gray-500 p-2')
                return
            for i, path in enumerate(self.state.search_results):
                active = ' active' if i == self.state.search_idx else ''
                row = ui.row().classes(f'w-full cursor-pointer p-2 rounded text-gray-300 file-option{active}').props(f'data-idx={i} id=file-opt-{i}')
                row.on('click', lambda _=None, p=path: self.select_file(p))
                with row:
                    ui.icon('description').classes('text-gray-500')
                    ui.label(Path(path).name).classes('flex-grow')
                    ui.label(str(Path(path).parent)).classes('text-xs text-gray-500')

    def select_file(self, path: str):
        if path not in self.chat.files: self.chat.files.append(path)
        self.refresh_file_picker()

    def attach_multiple(self, paths: list[str]):
        for p in paths:
            if p not in self.chat.files: self.chat.files.append(p)
        self.refresh_file_picker()

    async def attach_url(self, url: str):
        try:
            u = self.chat.normalize_url(url)
            content = await self.chat.fetch_url_content(u)
            if not any(x.url == u for x in self.state.url_attachments): self.state.url_attachments.append(UrlAttachment(u, content))
            self.refresh_file_picker()
        except Exception as e:
            ui.notify(f'URL error: {e}', type='negative')

    async def on_search(self):
        q = (self.refs.file_search.value or '').strip()
        self.clear_search_results()
        if len(q) < 2: return
        results = await asyncio.to_thread(search_files, q)
        if (self.refs.file_search.value or '').strip() != q: return
        self.state.search_results = results or []
        self.render_search_results()

    async def on_file_search_keydown(self, event):
        key, results, n = event.args.get('key'), self.state.search_results, len(self.state.search_results)
        if key in ('ArrowDown', 'Down'):
            if n == 0: return
            self.state.search_idx = (self.state.search_idx + 1) % n
            self.render_search_results()
            self.scroll_active_into_view()
            return
        if key in ('ArrowUp', 'Up'):
            if n == 0: return
            self.state.search_idx = (self.state.search_idx - 1) % n
            self.render_search_results()
            self.scroll_active_into_view()
            return
        if key == 'Escape':
            self.refs.file_search.value = ''
            self.clear_search_results()
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
        self.select_file(results[self.state.search_idx if 0 <= self.state.search_idx < n else 0])

    async def on_input_keydown(self, event):
        if event.args.get('key') != 'Enter' or event.args.get('shiftKey'): return
        prevent = getattr(event, 'prevent_default', None)
        if callable(prevent):
            result = prevent()
            if asyncio.iscoroutine(result): await result
        self.send()

    def remove_file(self, path: str):
        try: self.chat.files.remove(path)
        except ValueError: return
        self.render_pending_attachments()
        if self.state.phase in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}: ui.notify('File removed; changes will reflect after the current response finishes.', type='info')

    def remove_url(self, url: str):
        self.state.url_attachments = [x for x in self.state.url_attachments if x.url != url]
        self.render_pending_attachments()
        if self.state.phase in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}: ui.notify('URL removed; changes will reflect after the current response finishes.', type='info')

    async def run_stream(self, stream: Any, run_id: int):
        err = None
        try:
            async for chunk in stream:
                if self.stream.run_id != run_id: return
                if isinstance(chunk, ReasoningEvent):
                    if not self.stream.has_answer and chunk.text:
                        self.stream.reasoning += chunk.text
                        self.stream.reasoning_delta += chunk.text
                    continue
                if not isinstance(chunk, str) or not chunk: continue
                if not self.stream.has_answer: self.stream_start_answer()
                self.stream.raw += chunk
                if self.stream.renderer:
                    delta = self.stream.renderer.feed(chunk)
                    if delta:
                        self.stream.display += delta
                        self.stream.display_delta += delta
        except asyncio.CancelledError:
            return
        except Exception as e:
            err = str(e)
        finally:
            if self.stream.run_id == run_id: self.stream.error, self.stream.done = err, True

    def council_start_answer(self, m: CouncilMember):
        m.has_answer, m.raw, m.display, m.display_delta = True, '', '', ''
        m.reasoning, m.reasoning_delta, m.renderer, m.reset_display = '', '', self.chat.new_display_renderer(m.ctx_files), True

    async def run_council_stream(self, c: CouncilRound, m: CouncilMember, stream: Any):
        err = None
        try:
            async for chunk in stream:
                if not self.active_council_matches(c) or m.finalized: return
                if isinstance(chunk, ReasoningEvent):
                    if not m.has_answer and chunk.text:
                        m.reasoning += chunk.text
                        m.reasoning_delta += chunk.text
                    continue
                if not isinstance(chunk, str) or not chunk: continue
                if not m.has_answer: self.council_start_answer(m)
                m.raw += chunk
                if m.renderer:
                    delta = m.renderer.feed(chunk)
                    if delta:
                        m.display += delta
                        m.display_delta += delta
        except asyncio.CancelledError:
            return
        except Exception as e:
            err = str(e)
        finally:
            m.error, m.done = err, True

    def commit_council_member(self, c: CouncilRound, m: CouncilMember):
        if m.finalized or not m.done: return
        if m.has_answer and m.renderer:
            delta = m.renderer.finish() or ''
            if delta:
                m.display += delta
                if m.token in self.refs.content_ids: self.append_markdown(self.refs.content_ids[m.token], delta)
        raw = (m.raw.rstrip() if m.has_answer else (m.reasoning or m.raw).rstrip()) or 'Response stopped.'
        display = (m.display.rstrip() if m.has_answer else raw) or (self.chat.render_for_display(raw, m.ctx_files) if m.has_answer else raw)
        had_answer = m.has_answer
        m.raw, m.display, m.elapsed, m.task, m.renderer, m.finalized = raw, display, self.council_elapsed(m), None, None, True
        if m.token in self.refs.content_ids and not had_answer: self.set_markdown(self.refs.content_ids[m.token], display, True)
        if m.token in self.refs.timer_labels: self.refs.timer_labels[m.token].text = self.timer_text(m.elapsed)
        if m.error: ui.notify(f'{m.label}: {m.error}', type='negative')
        if c.synthesis and m.token == c.synthesis.token:
            self.persist_council_synthesis(c)
            if self.state.phase == Phase.COUNCIL_SYNTHESIZING and self.active_council_matches(c): self.set_phase(Phase.IDLE)
            if c.hidden_assistant_index is not None and had_answer and self.chat.parse_edit_markdown(raw): self.set_pending_edits(raw, c.hidden_assistant_index)
            return
        if self.state.phase == Phase.COUNCIL_STREAMING and self.active_council_matches(c) and c.synthesis is None and all(x.finalized for x in c.members): self.start_council_synthesis(c)

    def start_single_stream(self, to_send: str, user_display: str, attachments: list[dict[str, str]], model: str, reasoning: str, force_edit: bool = False, restore_text: str | None = None):
        stream = self.chat.stream_message(to_send, model, reasoning, force_edit=force_edit, attachments=attachments, display_user=user_display, restore_user=restore_text if restore_text is not None else user_display)
        self.prune_edit_rounds(), self.prune_answer_timers()
        self.state.url_attachments = []
        self.set_draft_text('')
        user_idx, assistant_idx = len(self.chat.messages) - 2, len(self.chat.messages) - 1
        self.stream_begin(assistant_idx)
        self.render_message(self.normal_token(user_idx), 'user', user_display, 'You', attachments)
        self.render_message(self.normal_token(assistant_idx), 'assistant', '', model, streaming=True, assistant_index=assistant_idx, timer_value=self.state.answer_timers.get(assistant_idx), timer_key=assistant_idx)
        self.render_pending_attachments()
        self.stream.task = asyncio.create_task(self.run_stream(stream, self.stream.run_id))

    def start_council(self, query: str, display_query: str, attachments: list[dict[str, str]], mode: str):
        if self.active_council() or self.council_total() <= 0: return
        counts, round_id = self.state.council_counts.copy(), self.state.next_council_id
        self.state.next_council_id += 1
        self.state.council_counts = {}
        self.refresh_model_picker()
        files = list(dict.fromkeys([((a.get('path') or '').strip().replace('\\', '/')) for a in attachments if (a.get('kind') or '').lower() == 'file' and (a.get('path') or '').strip()]))
        members = []
        for model in MODELS:
            n = counts.get(model, 0)
            for i in range(1, n + 1):
                label = model if n == 1 else f'{model} #{i}'
                members.append(CouncilMember(token=f'c-{round_id}-{len(members)}', model=model, label=label, ordinal=i, ctx_files=files.copy()))
        c = CouncilRound(round_id=round_id, query=query, display_query=display_query, attachments=[dict(a) for a in attachments], members=members)
        self.state.councils.append(c)
        self.prune_edit_rounds(), self.prune_answer_timers()
        self.state.url_attachments = []
        self.set_draft_text('')
        self.chat.files = []
        self.render_council_round(c)
        self.render_pending_attachments()
        self.set_phase(Phase.COUNCIL_STREAMING)
        to_send = f'{display_query}\n\n{EXTRACT_ADD_ON}' if mode == 'extract' else display_query
        reasoning, force_edit = self.refs.reasoning_select.value or self.state.reasoning, mode == 'chat+edit'
        for m in c.members:
            m.started_at = time.monotonic()
            m.task = asyncio.create_task(self.run_council_stream(c, m, self.chat.stream_transient(to_send, m.model, reasoning, force_edit=force_edit, attachments=attachments)))

    def build_council_prompt(self, c: CouncilRound) -> str: return self.build_council_display(c)

    def start_council_synthesis(self, c: CouncilRound):
        if c.synthesis is not None: return
        mode, reasoning, attachments = self.refs.mode_select.value or self.state.mode, self.refs.reasoning_select.value or self.state.reasoning, [dict(a) for a in c.attachments]
        prompt = self.build_council_prompt(c)
        stream = self.chat.stream_message_with_history(prompt, self.council_query_text(c), self.state.model, reasoning, force_edit=mode == 'chat+edit', attachments=attachments, display_user=self.council_query_text(c), restore_user=c.query)
        self.prune_edit_rounds(), self.prune_answer_timers()
        c.hidden_user_index, c.hidden_assistant_index = len(self.chat.messages) - 2, len(self.chat.messages) - 1
        files = list(dict.fromkeys([((a.get('path') or '').strip().replace('\\', '/')) for a in attachments if (a.get('kind') or '').lower() == 'file' and (a.get('path') or '').strip()]))
        c.synthesis = CouncilMember(token=f'c-{c.round_id}-s', model=self.state.model, label=f'Synthesis · {self.state.model}', ctx_files=files, started_at=time.monotonic())
        self.render_message(c.synthesis.token, 'assistant', '', c.synthesis.label, streaming=True, assistant_index=c.hidden_assistant_index, timer_value=self.state.answer_timers.get(c.hidden_assistant_index), timer_key=c.synthesis.token)
        self.set_phase(Phase.COUNCIL_SYNTHESIZING)
        c.synthesis.task = asyncio.create_task(self.run_council_stream(c, c.synthesis, stream))

    def remove_council(self, c: CouncilRound, restore_input: bool = False) -> bool:
        for m in self.council_members(c):
            if isinstance(m.task, asyncio.Task) and not m.task.done(): m.task.cancel()
            m.task = None
        if c.hidden_assistant_index is not None:
            if c.hidden_assistant_index != len(self.chat.messages) - 1 or c.hidden_user_index != len(self.chat.messages) - 2: raise RuntimeError('Cannot remove non-tail council history')
            try: self.chat.undo_last()
            except Exception as e:
                ui.notify(f'Undo failed: {e}', type='negative')
                return False
            self.prune_edit_rounds(), self.prune_answer_timers()
        self.remove_node(self.council_user_token(c))
        for m in c.members: self.remove_node(m.token)
        if c.synthesis:
            self.remove_node(c.synthesis.token)
            if c.hidden_assistant_index is not None: self.refs.edit_slots.pop(c.hidden_assistant_index, None)
        with contextlib.suppress(ValueError): self.state.councils.remove(c)
        if restore_input:
            self.set_draft_text(c.query)
            self.restore_attachments(c.attachments)
            self.render_pending_attachments()
            self.focus_input()
        if self.state.phase in {Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}: self.set_phase(Phase.IDLE)
        return True

    def send(self):
        msg = (self.refs.input_field.value or '').strip()
        if self.state.phase in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING} or not msg: return
        note, mode = self.clear_edit_round_state(before_send=True), (self.refs.mode_select.value or self.state.mode)
        user_display = f'{note}\n\n{msg}' if note else msg
        attachments = self.current_attachments()
        if self.council_total() > 0:
            self.start_council(msg, user_display, attachments, mode)
            return
        to_send = f'{user_display}\n\n{EXTRACT_ADD_ON}' if mode == 'extract' else user_display
        self.start_single_stream(to_send, user_display, attachments, self.state.model, self.refs.reasoning_select.value or self.state.reasoning, force_edit=mode == 'chat+edit', restore_text=msg)

    def stop_streaming(self):
        if self.state.phase == Phase.STREAMING:
            self.stream_commit(interrupted=True)
            ui.notify('Response stopped', type='info')
            return
        if self.state.phase == Phase.COUNCIL_STREAMING:
            c = self.active_council()
            if not c:
                ui.notify('No active response to stop', type='warning')
                return
            self.set_phase(Phase.IDLE)
            for m in c.members:
                if isinstance(m.task, asyncio.Task) and not m.task.done(): m.task.cancel()
                m.done = True
                if not m.finalized: self.commit_council_member(c, m)
            ui.notify('Council stopped', type='info')
            return
        if self.state.phase == Phase.COUNCIL_SYNTHESIZING:
            c = self.active_council()
            if not c or not c.synthesis:
                ui.notify('No active response to stop', type='warning')
                return
            self.set_phase(Phase.IDLE)
            if isinstance(c.synthesis.task, asyncio.Task) and not c.synthesis.task.done(): c.synthesis.task.cancel()
            c.synthesis.done = True
            if not c.synthesis.finalized: self.commit_council_member(c, c.synthesis)
            ui.notify('Synthesis stopped', type='info')
            return
        ui.notify('No active response to stop', type='warning')

    def undo(self):
        if c := self.active_council():
            self.remove_council(c, restore_input=True)
            return
        if self.state.phase == Phase.STREAMING: self.stop_streaming()
        last_council = self.latest_council()
        if last_council and last_council.hidden_assistant_index is not None and last_council.hidden_assistant_index == len(self.chat.messages) - 1:
            if self.remove_council(last_council, restore_input=True): self.clear_edit_round_state()
            return
        try: msg, _, atts = self.chat.undo_last()
        except Exception as e:
            ui.notify(f'Undo failed: {e}', type='negative')
            return
        if msg is None:
            ui.notify('No messages to undo', type='warning')
            return
        self.clear_edit_round_state()
        self.pop_last_node(), self.pop_last_node()
        self.prune_edit_rounds(), self.prune_answer_timers()
        ai = len(self.chat.messages) - 1 if self.chat.messages and self.chat.messages[-1].get('role') == 'assistant' else None
        if ai is not None: self.reopen_edit_round(ai)
        self.restore_attachments(atts or [])
        self.set_draft_text(msg)
        self.render_pending_attachments()
        self.focus_input()

    def clear_chat(self):
        if self.state.phase in {Phase.STREAMING, Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING}: self.stop_streaming()
        self.clear_edit_round_state()
        self.chat = ChatClient()
        self.storage['chat8'] = self.chat
        self.state.url_attachments, self.state.edit_rounds, self.state.answer_timers, self.state.council_counts, self.state.councils = [], {}, {}, {}, []
        self.reset_stream(invalidate=True)
        self.clear_rendered_messages()
        self.set_draft_text('')
        self.clear_search_results()
        self.render_pending_attachments()
        self.refresh_model_picker()
        self.update_controls()
        ui.notify('Chat cleared', type='positive')

    def flush_stream_updates(self):
        ai = self.stream.assistant
        if self.state.phase == Phase.STREAMING and ai is not None and (token := self.normal_token(ai)) in self.refs.content_ids:
            content_id = self.refs.content_ids[token]
            if self.stream.reset_display:
                self.set_markdown(content_id, '', True)
                self.stream.reset_display = False
            if not self.stream.has_answer and self.stream.reasoning_delta:
                delta, self.stream.reasoning_delta = self.stream.reasoning_delta, ''
                self.append_markdown(content_id, delta)
            if self.stream.has_answer and self.stream.display_delta:
                delta, self.stream.display_delta = self.stream.display_delta, ''
                self.append_markdown(content_id, delta)
        if self.stream.done:
            err, self.stream.done, self.stream.error = self.stream.error, False, None
            if self.state.phase == Phase.STREAMING: self.stream_commit(err)

    def flush_council_updates(self):
        for c in self.state.councils:
            for m in self.council_members(c):
                if m.token in self.refs.content_ids:
                    content_id = self.refs.content_ids[m.token]
                    if m.reset_display:
                        self.set_markdown(content_id, '', True)
                        m.reset_display = False
                    if not m.has_answer and m.reasoning_delta:
                        delta, m.reasoning_delta = m.reasoning_delta, ''
                        self.append_markdown(content_id, delta)
                    if m.has_answer and m.display_delta:
                        delta, m.display_delta = m.display_delta, ''
                        self.append_markdown(content_id, delta)
                if m.done and not m.finalized: self.commit_council_member(c, m)

    def flush_updates(self):
        self.flush_stream_updates()
        self.flush_council_updates()

    def tick_timer(self):
        if self.state.phase == Phase.STREAMING and self.stream.assistant in self.refs.timer_labels: self.refs.timer_labels[self.stream.assistant].text = self.timer_text()
        if self.state.phase in {Phase.COUNCIL_STREAMING, Phase.COUNCIL_SYNTHESIZING} and (c := self.active_council()):
            for m in self.council_members(c):
                if not m.finalized and m.token in self.refs.timer_labels: self.refs.timer_labels[m.token].text = self.timer_text(self.council_elapsed(m))

    def build_header(self):
        with ui.element('div').classes('fixed-header'):
            with ui.row().classes('gap-2 p-2 w-full items-start'):
                with ui.element('div').classes('relative'):
                    self.refs.model_button = ui.button(self.model_button_text(), icon='smart_toy').props('dark outline dense color=white').classes('text-white w-72 header-control header-model-btn')
                    with ui.menu() as self.refs.model_menu:
                        self.refs.model_menu_body = ui.column().classes('gap-0 p-1')
                self.refs.reasoning_select = ui.select(REASONING_LEVELS, label='Reasoning').props(P_PROPS).classes('text-white w-32 header-control').bind_value(self.state, 'reasoning')
                with ui.element('div').classes('flex-grow relative'):
                    self.refs.file_search = ui.input(placeholder='Search files or paste URL...').props(f'{P_PROPS} debounce=250 id=file-search').classes('w-full header-control')
                    self.refs.file_results_container = ui.column().classes('file-results')
                    self.refs.file_search.on_value_change(self.on_search)
                    self.refs.file_search.on('keydown', self.on_file_search_keydown)
        self.refresh_model_picker()

    def build_chat(self):
        with ui.element('div').classes('chat-stack'):
            self.refs.container = ui.column().classes('chat-container').props(f'id=chat8-{time.time_ns()}')

    def build_footer(self):
        with ui.element('div').classes('chat-footer'):
            with ui.column().classes('w-full gap-0'):
                self.refs.attachments = ui.row().classes('pending-atts w-full px-3 gap-1 flex-wrap')
                with ui.row().classes('w-full p-2 pt-2 gap-2 items-start'):
                    self.refs.input_field = ui.textarea(placeholder='Type your message...').props(f'{P_PROPS} autogrow input-class="min-h-22 max-h-100" id=input-field').classes('flex-grow text-white').bind_value(self.state, 'draft')
                    self.refs.input_field.on('keydown', self.on_input_keydown)
                    with ui.element('div').classes('ctrl-grid'):
                        self.refs.mode_select = ui.select(['chat+edit', 'chat', 'extract'], label='Mode').props(P_PROPS).classes('ctrl-tile text-white').bind_value(self.state, 'mode')
                        with ui.element('div').classes('ctrl-stack'):
                            self.refs.send_btn = ui.button('Send', on_click=lambda: self.send(), icon='send').props('color=primary').classes('ctrl-tile')
                            self.refs.stop_btn = ui.button('Stop', on_click=self.stop_streaming, icon='stop').props('color=red').classes('ctrl-tile absolute inset-0')
                        self.refs.back_btn = ui.button('Back', on_click=self.undo, icon='undo').props('color=orange').classes('ctrl-tile')
                        ui.button('Clear', on_click=self.clear_chat, icon='delete').props('color=grey').classes('ctrl-tile')

    def mount(self):
        self.prune_edit_rounds(), self.prune_answer_timers(), self.settle_orphaned_stream(), self.settle_orphaned_councils()
        self.build_header()
        self.build_chat()
        self.build_footer()
        self.render_history()
        self.render_pending_attachments()
        if self.state.search_results: self.render_search_results()
        self.update_controls()
        ui.timer(0.05, self.flush_updates)
        ui.timer(1.0, self.tick_timer)
        if (p := self.chat.consume_user_input_prefill()) and not (self.refs.input_field.value or '').strip(): self.set_draft_text(p)


@ui.page('/')
async def main_page():
    ui.add_head_html(STYLE_CSS)
    ui.add_head_html(HEAD_ASSETS)
    await ui.context.client.connected()
    ChatPageController.load(app.storage.tab).mount()


if __name__ in {'__main__', '__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8888)
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False, reconnect_timeout=300, ssl_certfile='cert.pem', ssl_keyfile='key.pem')
