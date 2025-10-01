import asyncio, contextlib, time, json, argparse
from pathlib import Path
from nicegui import ui
from utils import ChatClient, search_files, STYLE_CSS, MODELS, REASONING_LEVELS, DEFAULT_MODEL, EXTRACT_ADD_ON, ReasoningEvent

@ui.page('/')
async def main_page():
    # State
    tabs = [{'name': 'Chat 1', 'chat': ChatClient(), 'edit_history': [], 'streaming': False}]
    state = {'active_tab': 0, 'answer_counter': 0, 'msg_counter': 0, 'file_results': [], 'file_idx': -1}

    # CSS/JS
    ui.add_head_html(STYLE_CSS + r"""
<script>
window._writeText=async t=>{
t=t.replace(/\s+$/g,'');
if(navigator.clipboard?.writeText)try{await navigator.clipboard.writeText(t);return!0}catch(e){}
try{const ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.left='-9999px';document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);return!0}catch(e){return!1}
};
window.addCopyButtons=()=>{document.querySelectorAll('.prose pre').forEach(pre=>{
if(pre.querySelector('.copy-btn'))return; pre.style.position=pre.style.position||'relative';
const b=document.createElement('button'); b.className='copy-btn tool-btn copy-icon'; b.type='button'; b.title='Copy code'; b.innerHTML='<i class="material-icons">content_copy</i>';
b.addEventListener('click',async ev=>{ev.stopPropagation(); const code=pre.querySelector('code'); const text=(code?code.textContent:pre.textContent).replace(/\s+$/g,''); const ok=await window._writeText(text); if(ok){b.classList.add('copied'); setTimeout(()=>b.classList.remove('copied'),1200)} else {const prev=b.innerHTML; b.textContent='Error'; setTimeout(()=>b.innerHTML=prev,1500)}});
pre.appendChild(b);
})};
const runningTimers=new Map();
window.startAnswerTimer=id=>{
const tools=document.getElementById(id+'-tools'); if(!tools) return;
const t=tools.querySelector('.timer'); if(!t) return;
if(runningTimers.has(id)) clearInterval(runningTimers.get(id));
const start=Date.now();
const h=setInterval(()=>{const s=Math.floor((Date.now()-start)/1000),mm=Math.floor(s/60),ss=(s%60).toString().padStart(2,'0'); t.textContent=`${mm}:${ss}`},200);
runningTimers.set(id,h);
};
window.stopAnswerTimer=id=>{if(!runningTimers.has(id))return; clearInterval(runningTimers.get(id)); runningTimers.delete(id)};
</script>
""")

    # Small helpers and shared strings
    P = 'dark outlined dense color=white'
    MD_USER = 'prose prose-sm md-literal-underscores max-w-none text-white break-words prose-pre:bg-transparent prose-pre:text-white prose-pre:whitespace-pre-wrap prose-code:whitespace-pre-wrap'
    MD_ANS  = 'prose prose-sm md-literal-underscores max-w-none text-gray-200 break-words prose-p:m-0 prose-pre:bg-transparent prose-pre:whitespace-pre-wrap prose-code:whitespace-pre-wrap'

    back_button = None
    stop_button = None

    def tab():
        return tabs[state['active_tab']]

    def update_footer_visibility():
        t = tab()
        if back_button and stop_button:
            back_button.visible = not t.get('streaming', False)
            stop_button.visible = t.get('streaming', False)

    def scroll_bottom(tab_obj=None):
        t = tab_obj or tab()
        cid = t.get('container_id', '')
        if cid:
            ui.run_javascript(f'document.getElementById("{cid}")?.scrollTo(0, document.getElementById("{cid}").scrollHeight)')

    def show_active_container():
        for i, t in enumerate(tabs):
            c = t.get('container')
            if not c: continue
            c.style('display:none' if i != state['active_tab'] else 'display:flex')

    def clear_runtime(tab_obj=None):
        t = tab_obj or tab()
        if t.get('answer_id'):
            ui.run_javascript(f'stopAnswerTimer("{t["answer_id"]}")')
        for k in ('markdown','answer_id','reasoning_buffer','reasoning_last_update','reasoning_mode'):
            t.pop(k, None)

    def balance_fences(s: str) -> str:
        return s + ('\n```' if s.count('```') % 2 == 1 else '')

    def update_reasoning(text: str | None, tab_obj=None):
        if not text: return
        t = tab_obj or tab()
        if not t.get('reasoning_mode'): return
        t['reasoning_buffer'] = (t.get('reasoning_buffer') or '') + text
        md = t.get('markdown')
        if not md: return
        now = time.monotonic()
        last_ts = t.get('reasoning_last_update') or 0.0
        if (now - last_ts) < 0.08: return
        md.content = balance_fences(t['reasoning_buffer'])
        t['reasoning_last_update'] = now

    def build_tools(target_id: str, with_timer: bool = False, get_text=None):
        tools = ui.element('div').classes('answer-tools').props(f'id={target_id}-tools')
        with tools:
            copy_btn_id = f'{target_id}-copy'
            async def on_copy(getter=get_text or (lambda: '')):
                text = (getter() or '').rstrip()
                js = json.dumps(text)
                ui.run_javascript(f"""(async()=>{{const ok=await window._writeText({js});const b=document.getElementById("{copy_btn_id}");if(b){{b.classList.add('copied');setTimeout(()=>b.classList.remove('copied'),1200)}}}})()""")
            ui.button('', on_click=on_copy).props('icon=content_copy flat dense').classes('tool-btn copy-icon').props(f'id={copy_btn_id}')
            if with_timer: ui.label('').classes('timer')
        return tools

    def show_message(role, content):
        t = tab()
        with t['container']:
            if role == 'user':
                state['msg_counter'] += 1
                uid = f'user-{state["msg_counter"]}'
                with ui.element('div').classes('flex justify-end mb-3'):
                    with ui.element('div').classes('inline-block bg-blue-600 rounded-lg px-3 py-2 max-w-full min-w-0 user-bubble').props(f'id={uid}'):
                        ui.markdown(content).classes(MD_USER)
                with ui.element('div').classes('flex justify-end mt-1'):
                    build_tools(uid, get_text=lambda c=content: c)
            else:
                state['answer_counter'] += 1
                aid = f'answer-{state["answer_counter"]}'
                is_stream = (content == '')
                with ui.element('div').classes('flex justify-start mb-3'):
                    with ui.element('div').classes('bg-gray-800 rounded-lg px-3 py-2 w-full min-w-0 answer-bubble').props(f'id={aid}'):
                        md = ui.markdown(content).props('data-md=answer').classes(MD_ANS)
                        if is_stream:
                            t['answer_id'] = aid; t['markdown'] = md
                        if not is_stream: ui.run_javascript("setTimeout(addCopyButtons, 50)")
                with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                    build_tools(aid, with_timer=True, get_text=lambda m=md: m.content)

    def show_file(path):
        name = Path(path).name
        t = tab()
        with t['container']:
            with ui.element('div').classes('flex justify-center mb-3'):
                with ui.element('div').classes('bg-green-900 border border-green-700 rounded-lg px-3 py-2 flex items-center gap-2'):
                    ui.icon('attach_file').classes('text-green-400')
                    ui.label(f'Attached: {name}').classes('text-green-300 text-sm')
                    ui.button(icon='close', on_click=lambda p=path: remove_file(p)).props('flat dense size=sm').classes('text-green-400')

    def show_edit_bubble(key, status='pending', lines_info='', record=True):
        t = tab(); name = Path(key).name
        with t['container']:
            with ui.element('div').classes('flex justify-center mb-2'):
                klass = 'edit-bubble' + (' success' if status == 'success' else ' error' if status == 'error' else '')
                bubble = ui.element('div').classes(klass)
                with bubble:
                    if status in ('editing', 'progress'):
                        ui.spinner('dots', size='sm').classes('text-blue-400')
                        ui.label(f'Editing {name}' + (f' — {lines_info}' if lines_info else '')).classes('text-blue-300 text-sm')
                    elif status == 'success':
                        ui.icon('check_circle').classes('text-green-400')
                        ui.label(f'{name}: {lines_info}').classes('text-green-300 text-sm')
                        ui.button('Reject', on_click=lambda f=key: reject_edit(f)).props('flat dense size=sm').classes('text-red-400 ml-2')
                    elif status == 'error':
                        ui.icon('error').classes('text-red-400')
                        ui.label(f'{name}: Failed' + (f' — {lines_info}' if lines_info else '')).classes('text-red-300 text-sm')
        if record and status in ('success', 'error'):
            (t.setdefault('edit_history', [])).append({'path': key, 'status': status, 'info': lines_info})

    def reject_edit(key):
        t = tab()
        path = next((p for p in t['chat'].edited_files if p == key or Path(p).name == key), None)
        if path and t['chat'].rollback_file(path):
            ui.notify(f'Reverted {Path(path).name}', type='positive')
        else:
            ui.notify('Nothing to revert', type='warning')

    def remove_file(path):
        t = tab()
        with contextlib.suppress(ValueError):
            t['chat'].files.remove(path)
        refresh_ui()

    def refresh_ui():
        t = tab()
        if t.get('streaming'): return
        t['container'].clear()
        for role, content in t['chat'].get_display_messages(): show_message(role, content)
        for p in t['chat'].files: show_file(p)
        for item in (t.get('edit_history') or []):
            show_edit_bubble(item['path'], item.get('status', 'success'), item.get('info', ''), record=False)

    def switch_tab(index):
        state['active_tab'] = index
        show_active_container()
        refresh_ui(); refresh_tabs(); update_footer_visibility()

    def add_tab():
        new = {'name': f'Chat {len(tabs) + 1}', 'chat': ChatClient(), 'edit_history': [], 'streaming': False}
        tabs.append(new)
        with chat_stack:
            new['container_id'] = f'chat-{time.time_ns()}'
            new['container'] = ui.column().classes('chat-container').props(f'id={new["container_id"]}')
            new['container'].style('display:none')
        state['active_tab'] = len(tabs) - 1
        show_active_container()
        refresh_ui(); refresh_tabs(); update_footer_visibility()

    def close_tab(index):
        if len(tabs) == 1: ui.notify('Cannot close last tab', type='warning'); return
        tclose = tabs[index]
        if tclose.get('streaming'):
            tclose['streaming'] = False
            if 'stream' in tclose and hasattr(tclose['stream'], 'aclose'):
                asyncio.create_task(tclose['stream'].aclose())
            for k in ('producer_task', 'consumer_task'):
                task = tclose.pop(k, None)
                if task: task.cancel()
            tclose.pop('queue', None)
            clear_runtime(tclose)
        c = tclose.get('container')
        if c: c.delete()
        tabs.pop(index)
        state['active_tab'] = min(state['active_tab'], len(tabs) - 1)
        show_active_container()
        refresh_ui(); refresh_tabs(); update_footer_visibility()

    def refresh_tabs():
        tab_container.clear()
        with tab_container:
            for i, t in enumerate(tabs):
                def on_click(idx=i):
                    switch_tab(idx)
                with ui.row().classes('gap-1'):
                    ui.button(t['name'], on_click=on_click).classes('tab-button' + (' active' if i == state['active_tab'] else '')).props('flat dense')
                    if len(tabs) > 1:
                        def on_close(idx=i):
                            close_tab(idx)
                        ui.button(icon='close', on_click=on_close).props('flat dense size=xs').classes('text-gray-400 hover:text-white')
            ui.button(icon='add', on_click=add_tab).props('flat dense').classes('text-gray-400 hover:text-white')

    def finish_stream(full_text: str, tab_obj=None):
        t = tab_obj or tab()
        t['streaming'] = False
        t.pop('stream', None)
        # cleanup any background tasks/queues
        for k in ('producer_task', 'consumer_task', 'queue'):
            v = t.pop(k, None)
            with contextlib.suppress(Exception):
                v.cancel() if hasattr(v, 'cancel') else None
        if t.get('answer_id'):
            ui.run_javascript(f'stopAnswerTimer("{t["answer_id"]}")')
        try:
            if t.get('markdown') is not None and full_text is not None and not t.get('reasoning_mode'):
                t['markdown'].content = balance_fences(full_text)
                ui.run_javascript("setTimeout(addCopyButtons, 50)")
        except Exception:
            pass
        for k in ('markdown','answer_id','reasoning_buffer','reasoning_mode'):
            t.pop(k, None)
        update_footer_visibility()

    async def apply_edits_from_response(full_text: str, tab_obj=None):
        t = tab_obj or tab()
        try:
            events = t['chat'].apply_markdown_edits(full_text)
            for ev in events or []:
                key = ev.path or ev.filename
                if ev.kind == 'complete':
                    show_edit_bubble(key, 'success', ev.details)
                elif ev.kind == 'error':
                    show_edit_bubble(key, 'error', ev.details or '')
        except Exception as e:
            ui.notify(f'Edit error: {e}', type='negative')

    async def send():
        msg = input_field.value.strip().replace('\n', '\n\n')
        t = tab()
        if t.get('streaming') or not msg: return
        mode = mode_select.value
        show_message('user', msg)
        message_to_send = f"{msg}\n\n{EXTRACT_ADD_ON}" if mode == 'extract' else msg

        input_field.value = ''
        show_message('assistant', '')
        if t.get('answer_id'):
            ui.run_javascript(f'setTimeout(()=>startAnswerTimer("{t["answer_id"]}"),0)')
        t['streaming'] = True; t['reasoning_mode'] = True; t['reasoning_buffer'] = ''; update_footer_visibility()

        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        t['queue'] = q

        stream = t['chat'].stream_message(message_to_send, model_select.value, reasoning_select.value)
        t['stream'] = stream

        completed, full = False, ""

        async def _safe_put(item):
            q.put_nowait(item)  # may raise asyncio.QueueFull

        async def producer():
            nonlocal completed
            try:
                async for chunk in stream:
                    if not t.get('streaming'): break
                    if isinstance(chunk, ReasoningEvent):
                        await _safe_put(('reasoning', chunk.text)); continue
                    await _safe_put(('text', chunk))
                else:
                    completed = True
            except Exception as e:
                raise
            finally:
                with contextlib.suppress(Exception):
                    if hasattr(stream, 'aclose'): await stream.aclose()
                with contextlib.suppress(Exception):
                    await _safe_put(('done', None))

        async def consumer():
            nonlocal full
            md = t.get('markdown')
            last_update, tick = 0.0, 0.08
            tail2, parity, done = '', 0, False
            reasoning_buf = ''
            in_reasoning = t.get('reasoning_mode', False)
            while not done or not q.empty():
                drained = False
                while True:
                    try:
                        kind, payload = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if kind == 'text':
                        if in_reasoning:
                            in_reasoning = False; t['reasoning_mode'] = False; t['reasoning_buffer'] = ''
                            if md: md.content = ''
                            last_update, tail2, parity = 0.0, '', 0
                        full += payload
                        scan = tail2 + payload
                        c = scan.count('```')
                        if c & 1: parity ^= 1
                        tail2 = scan[-2:] if len(scan) >= 2 else scan
                        drained = True
                    elif kind == 'reasoning':
                        reasoning_buf += payload
                    elif kind == 'error':
                        ui.notify(f"Error: {payload}", type='negative')
                    elif kind == 'done':
                        done = True
                if reasoning_buf:
                    update_reasoning(reasoning_buf, tab_obj=t)
                    reasoning_buf = ''
                now = time.monotonic()
                if md and drained and (now - last_update) >= tick:
                    md.content = full + ('\n```' if (parity & 1) else '')
                    last_update = now
                await asyncio.sleep(0.03)
            if md:
                md.content = full + ('\n```' if (parity & 1) else '')

        t['producer_task'] = asyncio.create_task(producer())
        t['consumer_task'] = asyncio.create_task(consumer())

        try:
            await t['producer_task']
            await t['consumer_task']
        except asyncio.CancelledError:
            pass
        finally:
            finish_stream(full, tab_obj=t)
        if completed:
            scroll_bottom()
            await apply_edits_from_response(full, tab_obj=t)
            
    def stop_streaming():
        t = tab()
        if not t.get('streaming'):
            ui.notify('No active response to stop', type='warning'); return
        t['streaming'] = False
        if 'stream' in t and hasattr(t['stream'], 'aclose'):
            asyncio.create_task(t['stream'].aclose())
        for k in ('producer_task', 'consumer_task'):
            task = t.pop(k, None)
            if task: task.cancel()
        t.pop('queue', None)
        t.pop('stream', None)
        clear_runtime(t)
        update_footer_visibility()
        ui.notify('Response stopped', type='info')

    def undo():
        t = tab()
        msg, files = t['chat'].undo_last()
        if msg:
            input_field.value = msg
            refresh_ui()
        else:
            ui.notify('No messages to undo', type='warning')

    async def handle_keydown(event):
        if event.args.get('key') == 'Enter' and not event.args.get('shiftKey'): await send()

    def render_file_results():
        file_results_container.clear()
        results = state.get('file_results') or []
        idx = state.get('file_idx', -1)
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
            else:
                ui.label('No files found').classes('text-gray-500 p-2')

    def focus_file_search():
        ui.run_javascript('document.querySelector("#file-search")?.focus()')

    def scroll_active_into_view():
        i = state.get('file_idx', -1)
        if i >= 0:
            ui.run_javascript(f'document.getElementById("file-opt-{i}")?.scrollIntoView({{block:"nearest"}});')

    async def search():
        q = file_search.value.strip()
        state['file_idx'] = -1
        state['file_results'] = []
        file_results_container.clear()
        if len(q) < 2: return
        results = search_files(q)
        state['file_results'] = results or []
        render_file_results()

    async def file_search_keydown(event):
        key = event.args.get('key')
        results = state.get('file_results') or []
        n = len(results)
        if key in ('ArrowDown', 'Down'):
            if n == 0: return
            state['file_idx'] = (state.get('file_idx', -1) + 1) % n
            render_file_results(); scroll_active_into_view()
        elif key in ('ArrowUp', 'Up'):
            if n == 0: return
            state['file_idx'] = (state.get('file_idx', -1) - 1) % n
            render_file_results(); scroll_active_into_view()
        elif key == 'Enter':
            i = state.get('file_idx', -1)
            if n == 0: return
            if not (0 <= i < n): i = 0
            select_file(results[i])
        elif key == 'Escape':
            state['file_idx'] = -1
            state['file_results'] = []
            file_results_container.clear()

    def select_file(path):
        t = tab()
        if path not in t['chat'].files:
            t['chat'].files.append(path)
            show_file(path)
        file_search.value = ''
        state['file_idx'] = -1
        state['file_results'] = []
        file_results_container.clear()
        focus_file_search()

    # Header
    with ui.element('div').classes('fixed-header'):
        with ui.row().classes('gap-4 p-3 w-full'):
            model_select = ui.select(MODELS, value=DEFAULT_MODEL, label='Model').props(P).classes('text-white')
            reasoning_select = ui.select(list(REASONING_LEVELS.keys()), value='low', label='Reasoning').props(P).classes('w-32 text-white')
            with ui.element('div').classes('flex-grow relative'):
                file_search = ui.input(placeholder='Search files to attach...').props(f'{P} debounce=250 id=file-search').classes('w-full')
                file_results_container = ui.column().classes('file-results')
                file_search.on_value_change(search)
                file_search.on('keydown', file_search_keydown)
            tab_container = ui.row().classes('gap-2')

    # Chat areas
    chat_stack = ui.element('div').classes('chat-stack')
    with chat_stack:
        tabs[0]['container_id'] = f'chat-{time.time_ns()}'
        tabs[0]['container'] = ui.column().classes('chat-container').props(f'id={tabs[0]["container_id"]}')

    # Footer
    with ui.element('div').classes('chat-footer'):
        with ui.row().classes('w-full p-3 gap-2'):
            input_field = ui.textarea(placeholder='Type your message...').props(f'{P} rows=4 id=input-field').classes('flex-grow text-white')
            input_field.on('keydown', handle_keydown)
            mode_select = ui.select(['chat', 'extract'], value='chat', label='Mode').props(P).classes('w-32 text-white')
            back_button = ui.button('Back', on_click=undo, icon='undo').props('color=orange')
            stop_button = ui.button('Stop', on_click=stop_streaming, icon='stop').props('color=red')
    update_footer_visibility()

    refresh_tabs()

if __name__ in {'__main__','__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080, help='Port to run the server on')
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False)
