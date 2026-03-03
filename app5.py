import argparse
import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nicegui import app, ui

from chat_utils import (
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
MD_EXTRAS = ['break-on-newline', 'fenced-code-blocks', 'tables', 'cuddled-lists', 'mermaid', 'latex', 'code-friendly']
MD_CLASSES = 'prose prose-sm max-w-none break-words'
_CODE_FENCE_RE = re.compile(r'(?m)^\s*```')

COPY_BTN_JS = '''
(() => {{
  const root = document.getElementById('{root_id}'); if (!root) return;
  const mainCopy = document.getElementById('{root_id}-copy');
  const bind = (pre, code) => {{
    if (!pre || pre.dataset.copyBound) return;
    pre.dataset.copyBound = '1';
    const btn = document.createElement('button');
    btn.className = 'code-copy-btn tool-btn copy-icon'; btn.type='button';
    btn.title='Copy code'; btn.setAttribute('aria-label','Copy code');
    btn.innerHTML = '<span class="material-icons">content_copy</span>';
    btn.addEventListener('click', async (e) => {{
      e.stopPropagation();
      try {{
        await navigator.clipboard.writeText(code.innerText || '');
        btn.classList.add('copied'); if (mainCopy) mainCopy.classList.add('copied');
        setTimeout(() => {{ btn.classList.remove('copied'); if (mainCopy) mainCopy.classList.remove('copied'); }}, 1000);
      }} catch (e) {{ console.error(e); }}
    }});
    pre.appendChild(btn);
  }};
  const scan = (n) => {{
    if (!n || n.nodeType !== 1) return;
    if (n.matches && n.matches('pre > code')) bind(n.parentElement, n);
    if (n.querySelectorAll) n.querySelectorAll('pre > code').forEach(code => bind(code.parentElement, code));
  }};
  scan(root);
  if (root._copyObserver) root._copyObserver.disconnect();
  const obs = new MutationObserver(ms => {{ for (const m of ms) for (const n of m.addedNodes) scan(n); }});
  obs.observe(root, {{ childList: true, subtree: true }});
  root._copyObserver = obs;
}})();
'''

HEAD_CSS = '''
<style>
  .code-copy-btn{position:absolute;top:.35rem;right:.35rem;padding:.12rem;min-width:1.35rem;min-height:1.35rem}
  .code-copy-btn .material-icons{font-size:.85rem;line-height:1}
  .tool-btn.copy-icon{min-width:1.9rem!important;width:1.9rem!important;height:1.9rem!important;padding:0!important}
  pre{position:relative}
  .ctrl-grid{display:grid;grid-template-columns:repeat(2,8.5rem);gap:.5rem}
  .ctrl-tile{width:8.5rem;height:2.5rem}
  .ctrl-tile .q-field__control,.ctrl-tile .q-btn{height:2.5rem;min-height:2.5rem}
  .ctrl-stack{position:relative}
  .ctrl-stack>.ctrl-tile{width:100%}
  .tool-att{display:inline-flex;align-items:center;gap:.3rem;padding:.1rem .45rem;border-radius:9999px;border:1px solid #4b5563;background:#111827;max-width:20rem}
  .tool-att .q-icon{font-size:.85rem}
  .tool-att-label{font-size:.72rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:16rem}
</style>
'''

@dataclass(slots=True)
class UiState:
    phase: Literal['idle', 'streaming', 'awaiting_edit_decision'] = 'idle'
    draft: str = ''
    model: str = DEFAULT_MODEL
    reasoning: str = DEFAULT_REASONING
    mode: Literal['chat+edit', 'chat', 'extract'] = 'chat+edit'
    url_attachments: list[dict[str, str]] = field(default_factory=list)
    pending_edits_text: str | None = None
    last_edit_status: str | None = None
    pending_edit_assistant: int | None = None
    pending_edit_targets: list[str] = field(default_factory=list)
    edit_rounds: dict[int, dict[str, Any]] = field(default_factory=dict)
    search_results: list[str] = field(default_factory=list)
    search_idx: int = -1
    msg_counter: int = 0
    answer_counter: int = 0
    stream_text: str = ''
    stream_task: asyncio.Task | None = None
    stream_started_at: float = 0.0
    stream_done: bool = False
    stream_error: str | None = None
    stream_has_answer: bool = False
    ui_dirty: bool = True
    render_in_progress: bool = False
    ui_dirty_during_render: bool = False
    next_render_allowed_at: float = 0.0
    last_render_duration: float = 0.0


@dataclass(slots=True)
class UiRefs:
    container: Any = None
    input_field: Any = None
    file_search: Any = None
    file_results_container: Any = None
    model_select: Any = None
    reasoning_select: Any = None
    mode_select: Any = None
    timer_label: Any = None
    stop_btn: Any = None
    back_btn: Any = None
    send_btn: Any = None


def with_temp_code_fence(text: str) -> str:
    if not text or len(_CODE_FENCE_RE.findall(text)) % 2 == 0: return text
    return text + ('```' if text.endswith('\n') else '\n```')


@ui.page('/')
async def main_page():
    await ui.context.client.connected()
    storage = app.storage.tab
    state = storage.get('ui_state') if isinstance(storage.get('ui_state'), UiState) else UiState()
    chat = storage.get('chat') if isinstance(storage.get('chat'), ChatClient) else ChatClient()
    storage['ui_state'], storage['chat'] = state, chat

    # Compatibility for persisted UiState created before newer fields existed.
    if not hasattr(state, 'ui_dirty'): state.ui_dirty = True
    if not hasattr(state, 'render_in_progress'): state.render_in_progress = False
    if not hasattr(state, 'ui_dirty_during_render'): state.ui_dirty_during_render = False
    if not hasattr(state, 'next_render_allowed_at'): state.next_render_allowed_at = 0.0
    if not hasattr(state, 'last_render_duration'): state.last_render_duration = 0.0

    if state.phase != 'streaming':
        state.stream_task, state.stream_text, state.stream_started_at, state.stream_has_answer = None, '', 0.0, False
    elif state.stream_started_at <= 0:
        state.stream_started_at = time.monotonic()
    state.stream_done, state.stream_error = False, None
    state.ui_dirty = True

    refs = UiRefs()
    ui.add_head_html(STYLE_CSS)
    ui.add_head_html(HEAD_CSS)

    def scan_code_copy_buttons(root_id: str):
        if root_id: ui.run_javascript(COPY_BTN_JS.format(root_id=root_id))

    def focus_input():
        ui.run_javascript('document.getElementById("input-field")?.querySelector("textarea")?.focus()')

    def focus_file_search():
        ui.run_javascript('document.querySelector("#file-search")?.focus()')

    def scroll_active_into_view():
        i = state.search_idx
        if i >= 0: ui.run_javascript(f'document.getElementById("file-opt-{i}")?.scrollIntoView({{block:"nearest"}});')

    def update_controls():
        with contextlib.suppress(Exception):
            if refs.stop_btn: refs.stop_btn.set_visibility(state.phase == 'streaming')
            if refs.back_btn: refs.back_btn.set_visibility(state.phase != 'streaming')
            if refs.send_btn: refs.send_btn.set_visibility(state.phase != 'streaming')

    def mark_ui_dirty():
        state.ui_dirty = True
        if state.render_in_progress:
            state.ui_dirty_during_render = True

    def set_draft_text(value: str):
        state.draft = value
        refs.input_field.value = value

    def clear_search_results():
        state.search_results, state.search_idx = [], -1
        if refs.file_results_container: refs.file_results_container.clear()

    def prune_edit_rounds():
        state.edit_rounds = {i: v for i, v in state.edit_rounds.items() if 0 <= i < len(chat.messages)}
        
    def edit_targets(text: str) -> list[str]:
        out = []
        for d in chat.parse_edit_markdown(text or '') or []:
            raw = (d.filename or '').strip().replace('\\', '/')
            name = Path(raw).name or raw
            if name and name not in out: out.append(name)
        return out

    def set_pending_edits(text: str, assistant_index: int):
        targets = edit_targets(text) or ['edits']
        state.pending_edits_text, state.pending_edit_assistant, state.pending_edit_targets = text, assistant_index, targets
        state.last_edit_status, state.phase = 'pending', 'awaiting_edit_decision'
        state.edit_rounds[assistant_index] = {'status': 'pending', 'items': [{'label': t, 'status': 'pending'} for t in targets]}

    def clear_pending_edits(reject: bool = False):
        text, ai = (state.pending_edits_text or '').rstrip(), state.pending_edit_assistant
        if reject and text and ai is not None:
            targets = state.pending_edit_targets[:] or edit_targets(text) or ['edits']
            state.edit_rounds[ai] = {'status': 'error', 'items': [{'label': t, 'status': 'error'} for t in targets]}
            state.last_edit_status = 'rejected'
        elif text and ai is not None:
            state.edit_rounds.pop(ai, None)
        state.pending_edits_text, state.pending_edit_assistant, state.pending_edit_targets = None, None, []
        if state.phase == 'awaiting_edit_decision': state.phase = 'idle'

    def edit_round_meta(status: str) -> tuple[str, str, str]:
        if status == 'pending': return 'tips_and_updates', 'text-blue-300', 'Edits available'
        if status == 'success': return 'check_circle', 'text-green-400', 'All edits applied'
        if status == 'partial': return 'warning', 'text-amber-400', 'Some edits applied'
        return 'cancel', 'text-red-400', 'Edits not applied'

    def render_edit_round_slot(slot, assistant_index: int):
        slot.clear()
        r = state.edit_rounds.get(assistant_index)
        if not r: return
        s, items = (r.get('status') or 'error'), (r.get('items') or [])
        i0, c0, lbl = edit_round_meta(s)
        with slot:
            with ui.element('div').classes('tool-att'):
                ui.icon(i0).classes(c0)
                ui.label(lbl).classes('tool-att-label text-gray-300')
                if s == 'pending' and assistant_index == state.pending_edit_assistant:
                    ui.button('Apply', on_click=lambda i=assistant_index: asyncio.create_task(apply_pending_edits(i))).props('flat dense size=sm color=positive').classes('ml-1')
            if s != 'pending':
                for it in items:
                    s, name = (it.get('status') or 'error'), (it.get('label') or 'edit')
                    icon, cls = ('check', 'text-green-400') if s == 'success' else ('warning', 'text-amber-400') if s == 'partial' else ('close', 'text-red-400')
                    with ui.element('div').classes('tool-att'):
                        ui.icon(icon).classes(cls)
                        ui.label(name).classes('tool-att-label text-gray-300')

    def build_tools(target_id: str, get_text, with_timer: bool = False, atts: list[dict[str, str]] | None = None, assistant_index: int | None = None):
        tools, timer = ui.element('div').classes('answer-tools flex items-center gap-2 flex-wrap').props(f'id={target_id}-tools'), None
        with tools:
            copy_btn = None

            async def on_copy():
                ui.clipboard.write((get_text() or '').rstrip())
                if copy_btn:
                    copy_btn.classes('copied')
                    await asyncio.sleep(1)
                    copy_btn.classes(remove='copied')

            copy_btn = ui.button('', on_click=on_copy).props('icon=content_copy flat dense size=sm').classes('tool-btn copy-icon').props(f'id={target_id}-copy')
            for a in atts or []:
                k = (a.get('kind') or '').lower()
                t = Path((a.get('path') or '')).name if k == 'file' else (a.get('url') or '') if k == 'url' else ''
                if not t: continue
                with ui.element('div').classes('tool-att'):
                    ui.icon('attach_file' if k == 'file' else 'link').classes('text-gray-400')
                    ui.label(t).classes('tool-att-label text-gray-300')
            if assistant_index is not None:
                slot = ui.element('div').classes('inline-flex items-center gap-2 flex-wrap')
                render_edit_round_slot(slot, assistant_index)
            if with_timer: timer = ui.label('0:00').classes('timer')
        return timer

    def render_user_message(content: str, atts: list[dict[str, str]] | None = None):
        state.msg_counter += 1
        uid = f'user-{state.msg_counter}'
        with refs.container:
            with ui.element('div').classes('flex justify-end mb-3'):
                with ui.element('div').classes('inline-block bg-blue-600 rounded-lg px-3 py-2 max-w-full min-w-0 user-bubble').props(f'id={uid}'):
                    ui.markdown(content, extras=MD_EXTRAS).classes(MD_CLASSES)
            with ui.element('div').classes('flex justify-end mt-1'):
                build_tools(uid, get_text=lambda c=content: c, atts=atts)
        scan_code_copy_buttons(uid)

    def render_assistant_message(content: str, streaming: bool = False, message_index: int | None = None):
        state.answer_counter += 1
        aid = f'answer-{state.answer_counter}'
        rendered = with_temp_code_fence(chat.render_for_display(content)) if streaming else content
        with refs.container:
            with ui.element('div').classes('flex justify-start mb-3'):
                with ui.element('div').classes('bg-gray-800 rounded-lg px-3 py-2 w-full min-w-0 answer-bubble').props(f'id={aid}'):
                    md = ui.markdown(rendered, extras=MD_EXTRAS).classes(MD_CLASSES)
            with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                if streaming: refs.timer_label = build_tools(aid, get_text=lambda m=md: m.content, with_timer=True, assistant_index=message_index)
                else: build_tools(aid, get_text=lambda m=md: m.content, assistant_index=message_index)
        scan_code_copy_buttons(aid)

    def render_file_chip(path: str):
        name = Path(path).name
        with refs.container:
            with ui.element('div').classes('flex justify-center mb-3'):
                with ui.element('div').classes('bg-green-900 border border-green-700 rounded-lg px-3 py-2 flex items-center gap-2'):
                    ui.icon('attach_file').classes('text-green-400')
                    ui.label(name).classes('text-green-300 text-sm')
                    ui.button(icon='close', on_click=lambda p=path: remove_file(p)).props('flat dense size=sm').classes('text-green-400')

    def render_url_chip(item: dict[str, str]):
        url = (item or {}).get('url') or ''
        with refs.container:
            with ui.element('div').classes('flex justify-center mb-3'):
                with ui.element('div').classes('bg-purple-900 border border-purple-700 rounded-lg px-3 py-2 flex items-center gap-2'):
                    ui.icon('link').classes('text-purple-300')
                    ui.label(url).classes('text-purple-200 text-sm')
                    ui.button(icon='close', on_click=lambda u=url: remove_url(u)).props('flat dense size=sm').classes('text-purple-300')


    async def apply_pending_edits(assistant_index: int):
        if assistant_index != state.pending_edit_assistant or not (state.pending_edits_text or '').strip(): return
        text = raw = (state.pending_edits_text or '').rstrip()
        targets = state.pending_edit_targets[:] or edit_targets(text) or ['edits']
        state.pending_edits_text, state.pending_edit_assistant, state.pending_edit_targets, state.phase = None, None, [], 'idle'

        with contextlib.suppress(Exception):
            if chat.messages and chat.messages[-1].get('role') == 'assistant': raw = (chat.messages[-1].get('content') or '').rstrip() or raw
            if raw: chat.ensure_last_assistant_nonempty(raw)

        try:
            events = chat.apply_markdown_edits(text) or []
        except Exception as e:
            state.edit_rounds[assistant_index], state.last_edit_status = {'status': 'error', 'items': [{'label': t, 'status': 'error'} for t in targets]}, 'failed'
            mark_ui_dirty()
            ui.notify(f'Edit error: {e}', type='negative')
            return

        kind_map, rank = {'complete': 'success', 'partial': 'partial', 'error': 'error'}, {'success': 0, 'partial': 1, 'error': 2}
        items = [{'label': Path(ev.path or ev.filename).name or (ev.path or ev.filename or 'edit'), 'status': kind_map[ev.kind]} for ev in events if ev.kind in kind_map]
        if not items: items = [{'label': t, 'status': 'error'} for t in targets]
        merged, order = {}, []
        for it in items:
            k, v = it['label'], it['status']
            if k not in merged: merged[k], order = v, order + [k]
            elif rank[v] > rank[merged[k]]: merged[k] = v
        items = [{'label': k, 'status': merged[k]} for k in order]
        n_ok, n_partial, n_err = sum(it['status'] == 'success' for it in items), sum(it['status'] == 'partial' for it in items), sum(it['status'] == 'error' for it in items)
        status = 'error' if n_err and not (n_ok or n_partial) else 'partial' if n_partial or (n_ok and n_err) else 'success'
        state.edit_rounds[assistant_index], state.last_edit_status = {'status': status, 'items': items}, ('applied' if status == 'success' else 'partial' if status == 'partial' else 'failed')
        mark_ui_dirty()
        if p := chat.consume_user_input_prefill():
            with contextlib.suppress(Exception):
                set_draft_text(p)
                focus_input()

    def render_all():
        refs.container.clear()
        refs.timer_label = None
        state.msg_counter, state.answer_counter = 0, 0

        last_assistant_index = len(chat.messages) - 1 if chat.messages and chat.messages[-1].get('role') == 'assistant' else None
        for i, (role, content, atts) in enumerate(chat.get_display_messages(), start=1):
            if role == 'user':
                render_user_message(content, atts)
            elif role == 'assistant':
                render_assistant_message(content, streaming=(state.phase == 'streaming' and i == last_assistant_index), message_index=i)

        for p in chat.files: render_file_chip(p)
        for item in state.url_attachments: render_url_chip(item)
        update_controls()

    def clear_edit_round_state(before_send: bool = False) -> str | None:
        before = (state.pending_edits_text, state.pending_edit_assistant, tuple(state.pending_edit_targets), state.phase, state.last_edit_status, len(state.edit_rounds))
        if before_send and state.pending_edits_text: clear_pending_edits(reject=True)
        elif not before_send: clear_pending_edits(reject=False)
        status, note = state.last_edit_status, None
        if before_send and status:
            note = {
                'applied': 'I have accepted and implemented your latest round of edits above.',
                'partial': 'I have applied the uniquely matchable parts of your latest round of edits above; some commands failed.',
                'failed': 'I could not apply your latest round of edits above.',
                'rejected': 'I rejected your latest round of edits above.',
            }.get(status)
        state.last_edit_status = None
        after = (state.pending_edits_text, state.pending_edit_assistant, tuple(state.pending_edit_targets), state.phase, state.last_edit_status, len(state.edit_rounds))
        if before != after: mark_ui_dirty()
        return note

    async def run_stream(stream):
        err = None
        try:
            async for chunk in stream:
                if isinstance(chunk, ReasoningEvent):
                    if not state.stream_has_answer and chunk.text: state.stream_text += str(chunk.text)
                    mark_ui_dirty()
                    continue
                if not isinstance(chunk, str) or not chunk: continue
                if not state.stream_has_answer:
                    state.stream_text, state.stream_has_answer = '', True
                state.stream_text += chunk
                mark_ui_dirty()
        except asyncio.CancelledError:
            return
        except Exception as e:
            err = str(e)
        finally:
            state.stream_error, state.stream_done = err, True
            mark_ui_dirty()

    def persist_stream_output() -> str:
        full = (state.stream_text or '').rstrip() or 'Response stopped.'
        with contextlib.suppress(Exception):
            chat.ensure_last_assistant_nonempty(full)
            chat.set_last_assistant_display(chat.render_for_display(full))
        return full

    def reset_stream_state():
        state.phase = 'idle'
        state.stream_task = None
        state.stream_started_at = 0.0
        state.stream_text = ''
        state.stream_done = False
        state.stream_error = None
        state.stream_has_answer = False
    def finalize_stream_state(err: str | None = None):
        if state.phase != 'streaming': return
        full = persist_stream_output()
        reset_stream_state()
        if err:
            ui.notify(f'Error: {err}', type='negative')
            return

        if chat.parse_edit_markdown(full):
            ai = len(chat.messages) - 1 if chat.messages and chat.messages[-1].get('role') == 'assistant' else None
            if ai is not None: set_pending_edits(full, ai)
        mark_ui_dirty()

    async def send():
        msg = (refs.input_field.value or '').strip()
        if state.phase == 'streaming' or not msg: return

        note = clear_edit_round_state(before_send=True)
        mode = refs.mode_select.value or state.mode
        user_display = f'{note}\n\n{msg}' if note else msg
        to_send = f'{user_display}\n\n{EXTRACT_ADD_ON}' if mode == 'extract' else user_display

        urls = state.url_attachments[:]
        attachments = ([{'kind': 'file', 'path': p} for p in (chat.files or [])] + [{'kind': 'url', 'url': (x.get('url') or ''), 'content': (x.get('content') or '')} for x in urls])
        state.url_attachments = []
        state.stream_text = ''
        state.phase = 'streaming'
        state.stream_started_at = time.monotonic()
        state.stream_done = False
        state.stream_error = None
        state.stream_has_answer = False
        set_draft_text('')

        stream = chat.stream_message(to_send, refs.model_select.value, refs.reasoning_select.value, force_edit=(mode == 'chat+edit'), attachments=attachments)
        prune_edit_rounds()
        mark_ui_dirty()
        state.stream_task = asyncio.create_task(run_stream(stream))

    def stop_streaming():
        if state.phase != 'streaming':
            ui.notify('No active response to stop', type='warning')
            return

        t = state.stream_task
        if t and not t.done(): t.cancel()
        persist_stream_output()
        reset_stream_state()
        mark_ui_dirty()
        ui.notify('Response stopped', type='info')


    def remove_file(path: str):
        with contextlib.suppress(ValueError):
            chat.files.remove(path)
        if state.phase == 'streaming': ui.notify('File removed; changes will reflect after the response finishes.', type='info')
        else: mark_ui_dirty()

    def remove_url(url: str):
        state.url_attachments = [x for x in state.url_attachments if x.get('url') != url]
        if state.phase == 'streaming': ui.notify('URL removed; changes will reflect after the response finishes.', type='info')
        else: mark_ui_dirty()

    def undo():
        if state.phase == 'streaming': stop_streaming()
        clear_edit_round_state()
        msg, _, atts = chat.undo_last()
        if msg is None:
            ui.notify('No messages to undo', type='warning')
            return
        prune_edit_rounds()
        state.url_attachments = [{'url': (a.get('url') or ''), 'content': (a.get('content') or '')} for a in (atts or []) if (a.get('kind') or '').lower() == 'url' and (a.get('url') or '').strip()]
        set_draft_text(msg)
        mark_ui_dirty()
        focus_input()

    def clear_chat():
        nonlocal chat
        if state.phase == 'streaming': stop_streaming()
        clear_edit_round_state()
        chat = ChatClient()
        storage['chat'] = chat
        state.url_attachments, state.edit_rounds = [], {}
        state.stream_text, state.stream_task, state.stream_started_at = '', None, 0.0
        state.stream_done, state.stream_error, state.stream_has_answer = False, None, False
        set_draft_text('')
        clear_search_results()
        prune_edit_rounds()
        mark_ui_dirty()
        ui.notify('Chat cleared', type='positive')

    def render_search_results():
        refs.file_results_container.clear()
        with refs.file_results_container:
            if not state.search_results:
                ui.label('No files found').classes('text-gray-500 p-2')
                return
            for i, path in enumerate(state.search_results):
                active = ' active' if i == state.search_idx else ''
                row = ui.row().classes(f'w-full cursor-pointer p-2 rounded text-gray-300 file-option{active}').props(f'data-idx={i} id=file-opt-{i}')
                row.on('click', lambda _=None, p=path: select_file(p))
                with row:
                    ui.icon('description').classes('text-gray-500')
                    ui.label(Path(path).name).classes('flex-grow')
                    ui.label(str(Path(path).parent)).classes('text-xs text-gray-500')

    def select_file(path: str):
        if path not in chat.files: chat.files.append(path)
        refs.file_search.value = ''
        clear_search_results()
        mark_ui_dirty()
        focus_file_search()

    def attach_multiple(paths: list[str]):
        for p in paths:
            if p not in chat.files: chat.files.append(p)
        refs.file_search.value = ''
        clear_search_results()
        mark_ui_dirty()
        focus_file_search()

    async def attach_url(url: str):
        try:
            u = chat.normalize_url(url)
            content = await chat.fetch_url_content(u)
            if not any(x.get('url') == u for x in state.url_attachments): state.url_attachments.append({'url': u, 'content': content})
            refs.file_search.value = ''
            clear_search_results()
            mark_ui_dirty()
            focus_file_search()
        except Exception as e:
            ui.notify(f'URL error: {e}', type='negative')

    async def on_search():
        q = (refs.file_search.value or '').strip()
        clear_search_results()
        if len(q) < 2: return
        state.search_results = search_files(q) or []
        render_search_results()

    async def on_file_search_keydown(event):
        key, results, n = event.args.get('key'), state.search_results, len(state.search_results)
        if key in ('ArrowDown', 'Down'):
            if n == 0: return
            state.search_idx = (state.search_idx + 1) % n
            render_search_results()
            scroll_active_into_view()
            return
        if key in ('ArrowUp', 'Up'):
            if n == 0: return
            state.search_idx = (state.search_idx - 1) % n
            render_search_results()
            scroll_active_into_view()
            return
        if key == 'Escape':
            refs.file_search.value = ''
            clear_search_results()
            return
        if key != 'Enter': return

        q = (refs.file_search.value or '').strip()
        if q and chat.looks_like_url(q):
            await attach_url(q)
            return
        if '*' in q:
            attach_multiple(search_files(q) or [])
            return
        if n == 0: return
        i = state.search_idx if 0 <= state.search_idx < n else 0
        select_file(results[i])

    async def on_input_keydown(event):
        if event.args.get('key') == 'Enter' and not event.args.get('shiftKey'):
            with contextlib.suppress(Exception): await event.prevent_default()
            await send()

    def tick_timer():
        if refs.timer_label and state.phase == 'streaming':
            elapsed = int(time.monotonic() - state.stream_started_at)
            refs.timer_label.text = f'{elapsed // 60}:{(elapsed % 60):02d}'

    def render_tick():
        if state.render_in_progress or not state.ui_dirty:
            return
        now = time.monotonic()
        if now < state.next_render_allowed_at:
            return

        state.render_in_progress = True
        state.ui_dirty_during_render = False
        started = now
        rendered_ok = False
        try:
            if state.stream_done:
                err = state.stream_error
                state.stream_done, state.stream_error = False, None
                finalize_stream_state(err)
            prune_edit_rounds()
            render_all()
            rendered_ok = True
        finally:
            elapsed = max(0.0, time.monotonic() - started)
            state.last_render_duration = elapsed
            state.next_render_allowed_at = time.monotonic() + 0.05 + (0.5 * elapsed)
            state.render_in_progress = False
            state.ui_dirty = (not rendered_ok) or state.ui_dirty_during_render

    with ui.element('div').classes('fixed-header'):
        with ui.row().classes('gap-4 p-3 w-full'):
            refs.model_select = ui.select(MODELS, label='Model').props(P_PROPS).classes('text-white w-56').bind_value(state, 'model')
            refs.reasoning_select = ui.select(REASONING_LEVELS, label='Reasoning').props(P_PROPS).classes('text-white w-32').bind_value(state, 'reasoning')
            with ui.element('div').classes('flex-grow relative'):
                refs.file_search = ui.input(placeholder='Search files or paste URL...').props(f'{P_PROPS} debounce=250 id=file-search').classes('w-full')
                refs.file_results_container = ui.column().classes('file-results')
                refs.file_search.on_value_change(on_search)
                refs.file_search.on('keydown', on_file_search_keydown)

    with ui.element('div').classes('chat-stack'):
        refs.container = ui.column().classes('chat-container').props(f'id=chat-{time.time_ns()}')

    with ui.element('div').classes('chat-footer'):
        with ui.row().classes('w-full p-3 gap-2 items-start'):
            refs.input_field = ui.textarea(placeholder='Type your message...').props(f'{P_PROPS} autogrow input-class="min-h-22 max-h-100" id=input-field').classes('flex-grow text-white').bind_value(state, 'draft')
            refs.input_field.on('keydown', on_input_keydown)
            with ui.element('div').classes('ctrl-grid'):
                refs.mode_select = ui.select(['chat+edit', 'chat', 'extract'], label='Mode').props(P_PROPS).classes('ctrl-tile text-white').bind_value(state, 'mode')
                with ui.element('div').classes('ctrl-stack'):
                    refs.send_btn = ui.button('Send', on_click=lambda: asyncio.create_task(send()), icon='send').props('color=primary').classes('ctrl-tile')
                    refs.stop_btn = ui.button('Stop', on_click=stop_streaming, icon='stop').props('color=red').classes('ctrl-tile absolute inset-0')
                refs.back_btn = ui.button('Back', on_click=undo, icon='undo').props('color=orange').classes('ctrl-tile')
                ui.button('Clear', on_click=clear_chat, icon='delete').props('color=grey').classes('ctrl-tile')

    ui.timer(1.0, tick_timer)
    ui.timer(0.01, render_tick)
    prune_edit_rounds()
    mark_ui_dirty()
    render_tick()
    if (p := chat.consume_user_input_prefill()) and not (refs.input_field.value or '').strip():
        set_draft_text(p)
        mark_ui_dirty()


if __name__ in {'__main__', '__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False, reconnect_timeout=300, ssl_certfile='cert.pem', ssl_keyfile='key.pem')
