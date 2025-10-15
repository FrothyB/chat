# app_single.py
import asyncio, contextlib, time, json, argparse
from pathlib import Path
from nicegui import app, ui
from utils import ChatClient, search_files, STYLE_CSS, MODELS, REASONING_LEVELS, DEFAULT_MODEL, EXTRACT_ADD_ON, ReasoningEvent

@ui.page('/')
async def main_page():
    await ui.context.client.connected()
    s = app.storage.tab
    s.setdefault('chat', ChatClient())
    s.setdefault('draft', '')
    s.setdefault('model', DEFAULT_MODEL)
    s.setdefault('reasoning', 'medium')
    s.setdefault('mode', 'chat')
    s.setdefault('streaming', False)
    s.setdefault('reasoning_mode', False)
    s.setdefault('reasoning_buffer', '')
    s.setdefault('answer_counter', 0)
    s.setdefault('msg_counter', 0)
    s.setdefault('file_results', [])
    s.setdefault('file_idx', -1)
    s.setdefault('edit_history', [])
    for k in ('container','container_id','markdown','markdown_head','markdown_tail','answer_id','stream'):
        s.pop(k, None)

    ui.add_head_html(STYLE_CSS)
    ui.add_head_html('''
    <style>
      /* Position only; styling inherits from .tool-btn/.copy-icon in global styles */
      .code-copy-btn{position:absolute;top:.35rem;right:.35rem}
      pre{position:relative}
    </style>
    ''')

    P = 'dark outlined dense color=white'
    MD_EXTRAS = ['break-on-newline','fenced-code-blocks','tables','cuddled-lists','mermaid','latex','code-friendly']
    MD_USER = 'prose prose-sm max-w-none break-words'
    MD_ANS = MD_USER

    def init_code_copy_buttons():
        cid = s.get('container_id','')
        if not cid: return
        js = f'''
        (() => {{
          const root = document.getElementById('{cid}');
          if (!root) return;
          const addButtons = r => {{
            r.querySelectorAll('pre > code').forEach(code => {{
              const pre = code.parentElement; if (!pre || pre.dataset.copyBound) return;
              pre.dataset.copyBound = '1';
              const btn = document.createElement('button');
              btn.className = 'code-copy-btn tool-btn copy-icon';
              btn.setAttribute('type', 'button');
              btn.setAttribute('title', 'Copy code');
              btn.setAttribute('aria-label', 'Copy code');
              btn.innerHTML = '<span class="material-icons">content_copy</span>';
              btn.addEventListener('click', async (e) => {{
                e.stopPropagation();
                try {{
                  await navigator.clipboard.writeText(code.innerText || '');
                  btn.classList.add('copied'); setTimeout(() => btn.classList.remove('copied'), 1000);
                }} catch(e) {{ console.error(e); }}
              }});
              pre.appendChild(btn);
            }});
          }};
          const obs = new MutationObserver(() => addButtons(root));
          obs.observe(root, {{childList:true, subtree:true}});
          addButtons(root);
        }})();
        '''
        ui.run_javascript(js)

    def update_footer_visibility():
        back_button.visible = not s.get('streaming', False)
        stop_button.visible = s.get('streaming', False)

    def update_reasoning(text: str | None):
        if not text or not s.get('reasoning_mode'): return
        s['reasoning_buffer'] = (s.get('reasoning_buffer') or '') + text
        md = s.get('markdown_tail') or s.get('markdown')
        if not md: return
        now = time.monotonic(); last_ts = s.get('reasoning_last_update') or 0.0
        if (now - last_ts) < 0.08: return
        md.content = s['reasoning_buffer']
        s['reasoning_last_update'] = now

    def build_tools(target_id: str, with_timer: bool = False):
        tools = ui.element('div').classes('answer-tools').props(f'id={target_id}-tools')
        timer = None
        with tools:
            copy_btn_id = f'{target_id}-copy'
            text_expr = f'(document.getElementById("{target_id}")?.innerText || "").trim()'
            js = f'''
                () => {{
                    const btn = document.getElementById("{copy_btn_id}");
                    const text = {text_expr};
                    if (!text) return;
                    navigator.clipboard?.writeText(text).then(() => {{
                        btn?.classList.add('copied');
                        setTimeout(() => btn?.classList.remove('copied'), 1000);
                    }}).catch(err => console.error('Clipboard error:', err));
                }}
            '''
            btn = ui.button('', icon='content_copy').props('flat dense').classes('tool-btn copy-icon').props(f'id={copy_btn_id}')
            btn.on('click', js_handler=js)
            if with_timer: timer = ui.label('0:00').classes('timer')
        return tools, timer

    def show_message(role, content):
        with s['container']:
            if role == 'user':
                s['msg_counter'] += 1; uid = f'user-{s["msg_counter"]}'
                with ui.element('div').classes('flex justify-end mb-3'):
                    with ui.element('div').classes('inline-block bg-blue-600 rounded-lg px-3 py-2 max-w-full min-w-0 user-bubble').props(f'id={uid}'):
                        ui.markdown(content, extras=MD_EXTRAS).classes(MD_USER)
                with ui.element('div').classes('flex justify-end mt-1'):
                    build_tools(uid)
            else:
                s['answer_counter'] += 1; aid = f'answer-{s["answer_counter"]}'
                is_stream = (content == '')
                with ui.element('div').classes('flex justify-start mb-3'):
                    with ui.element('div').classes('bg-gray-800 rounded-lg px-3 py-2 w-full min-w-0 answer-bubble').props(f'id={aid}'):
                        if is_stream:
                            md_head = ui.markdown('', extras=MD_EXTRAS).classes(MD_ANS)
                            md_tail = ui.markdown('', extras=MD_EXTRAS).classes(MD_ANS)
                            s['answer_id'] = aid; s['markdown_head'] = md_head; s['markdown_tail'] = md_tail
                        else:
                            ui.markdown(content, extras=MD_EXTRAS).classes(MD_ANS)
                with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                    tools, timer = build_tools(aid, with_timer=True)
                    return timer

    def show_file(path):
        name = Path(path).name
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-3'):
                with ui.element('div').classes('bg-green-900 border border-green-700 rounded-lg px-3 py-2 flex items-center gap-2'):
                    ui.icon('attach_file').classes('text-green-400')
                    ui.label(f'Attached: {name}').classes('text-green-300 text-sm')
                    ui.button(icon='close', on_click=lambda p=path: remove_file(p)).props('flat dense size=sm').classes('text-green-400')

    def show_edit_bubble(key, status='pending', lines_info='', record=True):
        name = Path(key).name
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-2'):
                klass = 'edit-bubble' + (' success' if status == 'success' else ' error' if status == 'error' else '')
                with ui.element('div').classes(klass):
                    if status in ('editing','progress'):
                        ui.spinner('dots', size='sm').classes('text-blue-400')
                        ui.label(f'Editing {name}' + (f' — {lines_info}' if lines_info else '')).classes('text-blue-300 text-sm')
                    elif status == 'success':
                        ui.icon('check_circle').classes('text-green-400')
                        ui.label(f'{name}: {lines_info}').classes('text-green-300 text-sm')
                        ui.button('Reject', on_click=lambda f=key: reject_edit(f)).props('flat dense size=sm').classes('text-red-400 ml-2')
                    elif status == 'error':
                        ui.icon('error').classes('text-red-400')
                        ui.label(f'{name}: Failed' + (f' — {lines_info}' if lines_info else '')).classes('text-red-300 text-sm')
        if record and status in ('success','error'): s['edit_history'].append({'path': key, 'status': status, 'info': lines_info})

    def reject_edit(key):
        path = next((p for p in s['chat'].edited_files if p == key or Path(p).name == key), None)
        if path and s['chat'].rollback_file(path): ui.notify(f'Reverted {Path(path).name}', type='positive')
        else: ui.notify('Nothing to revert', type='warning')

    def remove_file(path):
        with contextlib.suppress(ValueError): s['chat'].files.remove(path)
        refresh_ui()

    def refresh_ui():
        s['container'].clear()
        for role, content in s['chat'].get_display_messages(): 
            show_message(role, content)
        for p in s['chat'].files: show_file(p)
        for item in (s.get('edit_history') or []): show_edit_bubble(item['path'], item.get('status','success'), item.get('info',''), record=False)

    def finish_stream(full_text: str):
        md_head, md_tail = s.get('markdown_head'), s.get('markdown_tail')
        fallback = (full_text or '').rstrip()
        current_rendered = f"{(md_head.content if md_head else '') or ''}{(md_tail.content if md_tail else '') or ''}".rstrip() if (md_head or md_tail) else ''
        if not fallback: fallback = current_rendered
        with contextlib.suppress(Exception): s['chat'].ensure_last_assistant_nonempty(fallback or 'Response stopped.')
        if md_head or md_tail:
            head_text = (md_head.content if md_head else '') or ''
            tail_text = (md_tail.content if md_tail else '') or ''
            final = (head_text + tail_text).rstrip() or (fallback or '')
            if md_head: md_head.content = final
            if md_tail: md_tail.content = ''
        s['streaming'] = False
        with contextlib.suppress(Exception):
            stream = s.pop('stream', None)
            if hasattr(stream, 'aclose'): 
                asyncio.create_task(stream.aclose())
        for k in ('markdown','markdown_head','markdown_tail','answer_id','reasoning_buffer','reasoning_last_update','reasoning_mode'):
            s.pop(k, None)
        update_footer_visibility()

    async def apply_edits_from_response(full_text: str):
        try:
            events = s['chat'].apply_markdown_edits(full_text)
            for ev in events or []:
                key = ev.path or ev.filename
                if ev.kind == 'complete': show_edit_bubble(key, 'success', ev.details)
                elif ev.kind == 'error': show_edit_bubble(key, 'error', ev.details or '')
        except Exception as e:
            ui.notify(f'Edit error: {e}', type='negative')

    async def send():
        msg = (input_field.value or '').strip()
        if s.get('streaming') or not msg: return
        mode = mode_select.value
        show_message('user', msg)
        to_send = f"{msg}\n\n{EXTRACT_ADD_ON}" if mode == 'extract' else msg
        s['draft'] = ''; input_field.value = ''
        timer = show_message('assistant', '')
        start_time = time.time() if timer else None
        s['streaming'] = True; s['reasoning_mode'] = True; s['reasoning_buffer'] = ''; update_footer_visibility()

        stream = s['chat'].stream_message(to_send, model_select.value, reasoning_select.value); s['stream'] = stream
        full, error_msg = "", None
        md_head, md_tail = s.get('markdown_head'), s.get('markdown_tail')
        head_text, tail_text = '', ''
        fence_count, trailing_backticks = 0, ''
        last_update, tick = 0.0, 0.05

        def update_fences(chunk: str):
            nonlocal fence_count, trailing_backticks
            scan = (trailing_backticks + chunk) if chunk else trailing_backticks
            if scan:
                fence_count += scan.count('```')
                i, c = len(scan) - 1, 0
                while i >= 0 and scan[i] == '`' and c < 2: c += 1; i -= 1
                trailing_backticks = '`' * c

        def render_tail() -> str: return tail_text + ('\n```' if (fence_count & 1) else '')

        def try_promote():
            nonlocal head_text, tail_text
            if (fence_count & 1): return
            para = tail_text.rfind('\n\n')
            if para != -1: cut = para + 2
            else:
                line = tail_text.rfind('\n')
                if line == -1: return
                cut = line + 1
            if cut <= 0: return
            head_text += tail_text[:cut]; tail_text = tail_text[cut:]

        try:
            async for chunk in stream:
                if timer and start_time is not None:
                    elapsed = int(time.time() - start_time)
                    timer.text = f"{elapsed // 60}:{(elapsed % 60):02d}"

                if not s.get('streaming'): break
                if isinstance(chunk, ReasoningEvent):
                    if s.get('reasoning_mode'): update_reasoning(chunk.text); continue
                else:
                    if s.get('reasoning_mode'):
                        s['reasoning_mode'] = False; s['reasoning_buffer'] = ''
                        if md_tail: md_tail.content = ''
                        if md_head: md_head.content = ''
                        last_update = 0.0; fence_count = 0; trailing_backticks = ''

                    full += chunk; tail_text += chunk; update_fences(chunk)

                    now = time.monotonic()
                    if (md_tail or md_head) and (now - last_update) >= tick:
                        try_promote()
                        if md_head: md_head.content = head_text
                        if md_tail: md_tail.content = render_tail()
                        last_update = now

                await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_msg = str(e)
        finally:
            if md_head or md_tail:
                try_promote()
                if md_head: md_head.content = head_text
                if md_tail: md_tail.content = render_tail()
            with contextlib.suppress(Exception):
                if hasattr(stream, 'aclose'): await stream.aclose()
            finish_stream(full)

        if error_msg: ui.notify(f"Error: {error_msg}", type='negative')
        else:
            await apply_edits_from_response(full)

    def stop_streaming():
        if not s.get('streaming'):
            ui.notify('No active response to stop', type='warning'); return
        if s.get('reasoning_mode'): current = ''
        else:
            mh, mt = s.get('markdown_head'), s.get('markdown_tail')
            current = f"{(mh.content if mh else '') or ''}{(mt.content if mt else '') or ''}".rstrip()
        s['streaming'] = False
        with contextlib.suppress(Exception):
            stream = s.get('stream')
            if hasattr(stream, 'aclose'): asyncio.create_task(stream.aclose())
        finish_stream(current or 'Response stopped.')
        ui.notify('Response stopped', type='info')

    def undo():
        msg, files = s['chat'].undo_last()
        if msg:
            s['draft'] = msg; input_field.value = msg; refresh_ui()
        else: ui.notify('No messages to undo', type='warning')

    async def handle_keydown(event): 
        if event.args.get('key') == 'Enter' and not event.args.get('shiftKey'): await send()

    def render_file_results():
        file_results_container.clear()
        results = s.get('file_results') or []; idx = s.get('file_idx', -1)
        with file_results_container:
            if results:
                for i, path in enumerate(results):
                    active = ' active' if i == idx else ''
                    row = ui.row().classes(f'w-full cursor-pointer p-2 rounded text-gray-300 file-option{active}').props(f'data-idx={i} id=file-opt-{i}')
                    row.on('click', lambda e=None, p=path: select_file(p))
                    with row:
                        ui.icon('description').classes('text-gray-500')
                        ui.label(Path(path).name).classes('flex-grow')
                        ui.label(str(Path(path).parent)).classes('text-xs text-gray-500')
            else: ui.label('No files found').classes('text-gray-500 p-2')

    def focus_file_search(): ui.run_javascript('document.querySelector("#file-search")?.focus()')
    def scroll_active_into_view():
        i = s.get('file_idx', -1)
        if i >= 0: ui.run_javascript(f'document.getElementById("file-opt-{i}")?.scrollIntoView({{block:"nearest"}});')

    async def search():
        q = (file_search.value or '').strip()
        s['file_idx'] = -1; s['file_results'] = []; file_results_container.clear()
        if len(q) < 2: return
        s['file_results'] = search_files(q) or []; render_file_results()

    async def file_search_keydown(event):
        key = event.args.get('key'); results = s.get('file_results') or []; n = len(results)
        if key in ('ArrowDown','Down'):
            if n == 0: return
            s['file_idx'] = (s.get('file_idx', -1) + 1) % n; render_file_results(); scroll_active_into_view()
        elif key in ('ArrowUp','Up'):
            if n == 0: return
            s['file_idx'] = (s.get('file_idx', -1) - 1) % n; render_file_results(); scroll_active_into_view()
        elif key == 'Enter':
            i = s.get('file_idx', -1)
            if n == 0: return
            if not (0 <= i < n): i = 0
            select_file(results[i])
        elif key == 'Escape':
            s['file_idx'] = -1; s['file_results'] = []; file_results_container.clear()

    def select_file(path):
        if path not in s['chat'].files:
            s['chat'].files.append(path); show_file(path)
        file_search.value = ''; s['file_idx'] = -1; s['file_results'] = []; file_results_container.clear(); focus_file_search()

    with ui.element('div').classes('fixed-header'):
        with ui.row().classes('gap-4 p-3 w-full'):
            model_select = ui.select(MODELS, label='Model').props(P).classes('text-white w-56').bind_value(app.storage.tab, 'model')
            reasoning_select = ui.select(list(REASONING_LEVELS.keys()), label='Reasoning').props(P).classes('text-white w-32').bind_value(app.storage.tab, 'reasoning')
            with ui.element('div').classes('flex-grow relative'):
                file_search = ui.input(placeholder='Search files to attach...').props(f'{P} debounce=250 id=file-search').classes('w-full')
                file_results_container = ui.column().classes('file-results')
                file_search.on_value_change(search); file_search.on('keydown', file_search_keydown)

    chat_stack = ui.element('div').classes('chat-stack')
    with chat_stack:
        s['container_id'] = f'chat-{time.time_ns()}'
        s['container'] = ui.column().classes('chat-container').props(f'id={s["container_id"]}')
    refresh_ui()
    init_code_copy_buttons()

    with ui.element('div').classes('chat-footer'):
        with ui.row().classes('w-full p-3 gap-2'):
            input_field = ui.textarea(placeholder='Type your message...').props(f'{P} rows=4 id=input-field').classes('flex-grow text-white').bind_value(app.storage.tab, 'draft')
            input_field.on('keydown', handle_keydown)
            mode_select = ui.select(['chat','extract'], label='Mode').props(P).classes('w-32 text-white').bind_value(app.storage.tab, 'mode')
            back_button = ui.button('Back', on_click=undo, icon='undo').props('color=orange')
            stop_button = ui.button('Stop', on_click=stop_streaming, icon='stop').props('color=red')
    update_footer_visibility()

if __name__ in {'__main__','__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False, reconnect_timeout=300)