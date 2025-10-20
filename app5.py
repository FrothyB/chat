import asyncio, contextlib, time, argparse, re, hashlib
from urllib.parse import urlparse
import httpx
from pathlib import Path
from nicegui import app, ui
from utils import ChatClient, search_files, STYLE_CSS, MODELS, REASONING_LEVELS, DEFAULT_MODEL, EXTRACT_ADD_ON, ReasoningEvent

@ui.page('/')
async def main_page():
    await ui.context.client.connected()
    s = app.storage.tab
    s.setdefault('chat', ChatClient()); s.setdefault('draft', ''); s.setdefault('model', DEFAULT_MODEL)
    s.setdefault('reasoning', 'medium'); s.setdefault('mode', 'chat'); s.setdefault('streaming', False)
    s.setdefault('reasoning_mode', False); s.setdefault('reasoning_buffer', ''); s.setdefault('answer_counter', 0)
    s.setdefault('msg_counter', 0); s.setdefault('file_results', []); s.setdefault('file_idx', -1)
    s.setdefault('edit_history', []); s.setdefault('pending_edits_text', None); s.setdefault('apply_all_bubble', None)
    s.setdefault('last_edit_round_status', None)
    for k in ('container','container_id','answer_md','answer_container','reasoning_md','answer_id','stream','renderer'):
        s.pop(k, None)

    ui.add_head_html(STYLE_CSS)
    ui.add_head_html('''
    <style>
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
          const root = document.getElementById('{cid}'); if (!root) return;
          const addButtons = r => {{
            r.querySelectorAll('pre > code').forEach(code => {{
              const pre = code.parentElement; if (!pre || pre.dataset.copyBound) return;
              pre.dataset.copyBound = '1';
              const btn = document.createElement('button');
              btn.className = 'code-copy-btn tool-btn copy-icon'; btn.type='button';
              btn.title='Copy code'; btn.setAttribute('aria-label','Copy code');
              btn.innerHTML='<span class="material-icons">content_copy</span>';
              btn.addEventListener('click', async (e) => {{
                e.stopPropagation();
                try {{ await navigator.clipboard.writeText(code.innerText || ''); btn.classList.add('copied'); setTimeout(()=>btn.classList.remove('copied'), 1000); }}
                catch(e) {{ console.error(e); }}
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

    def update_reasoning(text: str | None):
        if not text or not s.get('reasoning_mode'): return
        s['reasoning_buffer'] = (s.get('reasoning_buffer') or '') + text
        md = s.get('reasoning_md'); now = time.monotonic(); last = s.get('reasoning_last_update') or 0.0
        if md and (now - last) >= 0.08:
            md.content = s['reasoning_buffer']; s['reasoning_last_update'] = now

    def build_tools(target_id: str, with_timer: bool = False, get_text=None):
        tools = ui.element('div').classes('answer-tools').props(f'id={target_id}-tools'); timer = None
        with tools:
            copy_btn_id = f'{target_id}-copy'; btn = None
            async def on_copy(getter=get_text or (lambda: '')):
                text = (getter() or '').rstrip(); ui.clipboard.write(text)
                if btn: btn.classes('copied'); await asyncio.sleep(1); btn.classes(remove='copied')
            btn = ui.button('', on_click=on_copy).props('icon=content_copy flat dense').classes('tool-btn copy-icon').props(f'id={copy_btn_id}')
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
                    build_tools(uid, get_text=lambda c=content: c)
            else:
                s['answer_counter'] += 1; aid = f'answer-{s["answer_counter"]}'
                is_stream = (content == '')
                with ui.element('div').classes('flex justify-start mb-3'):
                    with ui.element('div').classes('bg-gray-800 rounded-lg px-3 py-2 w-full min-w-0 answer-bubble').props(f'id={aid}'):
                        if is_stream:
                            ans_container = ui.column().classes('answer-content')
                            with ans_container:
                                reasoning_md = ui.markdown('', extras=MD_EXTRAS).classes(MD_ANS)
                            s['answer_id'] = aid; s['answer_container'] = ans_container
                            s['reasoning_md'] = reasoning_md; s['answer_md'] = None; s['renderer'] = None
                            def getter():
                                md = s.get('answer_md'); 
                                if md and md.content: return md.content
                                r = s.get('reasoning_md'); return (r.content if r else '') or ''
                        else:
                            md = ui.markdown(content, extras=MD_EXTRAS).classes(MD_ANS); getter = lambda m=md: m.content
                with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                    tools, timer = build_tools(aid, with_timer=True, get_text=getter); return timer

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
        for role, content in s['chat'].get_display_messages(): show_message(role, content)
        for p in s['chat'].files: show_file(p)
        for item in (s.get('edit_history') or []): show_edit_bubble(item['path'], item.get('status','success'), item.get('info',''), record=False)

    def finish_stream(full_text: str):
        full_text = (full_text or '').rstrip() or 'Response stopped.'
        with contextlib.suppress(Exception): s['chat'].ensure_last_assistant_nonempty(full_text)
        md = s.get('answer_md') or s.get('reasoning_md')
        if md: md.content = full_text
        s['streaming'] = False
        with contextlib.suppress(Exception):
            stream = s.pop('stream', None)
            if hasattr(stream, 'aclose'): asyncio.create_task(stream.aclose())
        for k in ('answer_id','reasoning_buffer','reasoning_last_update','reasoning_mode'): s.pop(k, None)

    async def apply_edits_from_response(full_text: str):
        try:
            events = s['chat'].apply_markdown_edits(full_text)
            for ev in events or []:
                key = ev.path or ev.filename
                if ev.kind == 'complete': show_edit_bubble(key, 'success', ev.details)
                elif ev.kind == 'error': show_edit_bubble(key, 'error', ev.details or '')
        except Exception as e:
            ui.notify(f'Edit error: {e}', type='negative')

    def show_apply_all_bubble(full_text: str):
        with contextlib.suppress(Exception):
            old = s.pop('apply_all_bubble', None)
            if old: old.delete()
        s['pending_edits_text'] = full_text; s['last_edit_round_status'] = 'pending'
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-2'):
                bubble = ui.element('div').classes('edit-bubble'); s['apply_all_bubble'] = bubble
                with bubble:
                    ui.icon('tips_and_updates').classes('text-blue-300')
                    ui.label('Edits available. Apply to all files?').classes('text-blue-200 text-sm')
                    async def on_apply():
                        with contextlib.suppress(Exception):
                            b = s.pop('apply_all_bubble', None)
                            if b: b.delete()
                        text = s.get('pending_edits_text') or ''; s['pending_edits_text'] = None
                        await apply_edits_from_response(text); s['last_edit_round_status'] = 'applied'
                    ui.button('Apply edits', on_click=on_apply).props('flat dense size=sm color=positive').classes('ml-2')

    class StreamAssembler:
        def __init__(self, md):
            self.md = md
            self.text = ''     # committed full lines
            self.tail = ''     # current partial line buffer
            self.in_code = False
            self.indent = ''
            self.fchar = ''    # '`' or '~'
            self.flen = 0      # fence length (>=3)
            self.last_update = 0.0

        def _open_match(self, line: str):
            # up to 3 spaces, then >=3 backticks or tildes, then optional info, CRLF-safe
            return re.match(r'^( {0,3})(`{3,}|~{3,})([^\r\n]*)\r?\n$', line)

        def _close_match(self, line: str):
            if not self.in_code: return False
            # same indent, same fence char, at least same length, optional spaces/tabs, CRLF-safe
            pat = rf'^{re.escape(self.indent)}{re.escape(self.fchar)}{{{self.flen},}}[ \t]*\r?\n$'
            return re.match(pat, line)

        def feed(self, chunk: str):
            self.tail += chunk or ''
            while True:
                i = self.tail.find('\n')
                if i == -1: break
                line = self.tail[:i+1]; self.tail = self.tail[i+1:]
                if not self.in_code:
                    m = self._open_match(line)
                    if m:
                        self.indent, fence, _ = m.groups()
                        self.fchar, self.flen = fence[0], len(fence)
                        self.in_code = True
                        self.text += line
                        self.render(force=True)
                        continue
                    self.text += line
                else:
                    if self._close_match(line):
                        self.text += line
                        self.in_code = False
                        self.render(force=True)
                        continue
                    self.text += line
                self.render()
            self.render()

        def render(self, force: bool = False):
            now = time.monotonic()
            if not force and (now - self.last_update) < 0.033: return
            content = self.text + self.tail
            if self.in_code:
                content += f'\n{self.indent}{self.fchar * self.flen}'
            self.md.content = content
            self.last_update = now

        def finish(self) -> str:
            self.render(force=True)
            return self.text + self.tail


    async def send():
        msg = (input_field.value or '').strip()
        if s.get('streaming') or not msg: return

        status = s.get('last_edit_round_status'); note = None
        if status == 'pending': status = 'skipped'
        if status == 'applied': note = 'I have accepted and implemented your latest round of edits above.'
        elif status == 'skipped': note = 'I have not accepted or implemented your latest round of edits above.'
        s['last_edit_round_status'] = None
        with contextlib.suppress(Exception):
            b = s.pop('apply_all_bubble', None)
            if b: b.delete()
        s['pending_edits_text'] = None

        mode = mode_select.value
        user_display = f"{note}\n\n{msg}" if note else msg
        show_message('user', user_display)
        to_send = f"{user_display}\n\n{EXTRACT_ADD_ON}" if mode == 'extract' else user_display
        s['draft'] = ''; input_field.value = ''
        timer = show_message('assistant', '')
        start_time = time.time() if timer else None
        s['streaming'] = True; s['reasoning_mode'] = True; s['reasoning_buffer'] = ''

        stream = s['chat'].stream_message(to_send, model_select.value, reasoning_select.value)
        s['stream'] = stream; error_msg = None

        try:
            async for chunk in stream:
                if timer and start_time is not None:
                    elapsed = int(time.time() - start_time); timer.text = f"{elapsed // 60}:{(elapsed % 60):02d}"

                if not s.get('streaming'): break
                if isinstance(chunk, ReasoningEvent):
                    if s.get('reasoning_mode'): update_reasoning(chunk.text); continue

                if s.get('reasoning_mode'):
                    s['reasoning_mode'] = False; s['reasoning_buffer'] = ''
                    r = s.get('reasoning_md'); 
                    if r: 
                        with contextlib.suppress(Exception): r.delete()
                    s['reasoning_md'] = None
                    with s['answer_container']:
                        s['answer_md'] = ui.markdown('', extras=MD_EXTRAS).classes(MD_ANS)
                    s['renderer'] = StreamAssembler(s['answer_md'])

                s['renderer'].feed(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_msg = str(e)
        finally:
            renderer = s.get('renderer')
            full = renderer.finish() if renderer else (s.get('reasoning_md').content if s.get('reasoning_md') else '')
            full = full or ''
            finish_stream(full)

        if error_msg: ui.notify(f"Error: {error_msg}", type='negative')
        else:
            if s['chat'].parse_edit_markdown(full): show_apply_all_bubble(full)

    def stop_streaming():
        if not s.get('streaming'):
            ui.notify('No active response to stop', type='warning'); return
        if s.get('reasoning_mode'):
            current = (s.get('reasoning_md').content if s.get('reasoning_md') else '') if s.get('reasoning_md') else ''
        else:
            md = s.get('answer_md'); current = (md.content if md else '') or ''
        s['streaming'] = False
        with contextlib.suppress(Exception):
            stream = s.get('stream')
            if hasattr(stream, 'aclose'): asyncio.create_task(stream.aclose())
        finish_stream((current or 'Response stopped.').rstrip())
        ui.notify('Response stopped', type='info')

    def clear_chat():
        if s.get('streaming'):
            with contextlib.suppress(Exception):
                stream = s.get('stream')
                if hasattr(stream, 'aclose'): asyncio.create_task(stream.aclose())
            s['streaming'] = False
        with contextlib.suppress(Exception):
            b = s.pop('apply_all_bubble', None)
            if b: b.delete()
        s['pending_edits_text'] = None; s['last_edit_round_status'] = None
        s['chat'] = ChatClient(); s['draft'] = ''
        for k in ('answer_md','answer_container','reasoning_md','answer_id','reasoning_buffer','reasoning_last_update','reasoning_mode','stream','renderer'): s.pop(k, None)
        s['answer_counter'] = 0; s['msg_counter'] = 0; s['file_results'] = []; s['file_idx'] = -1; s['edit_history'] = []
        with contextlib.suppress(Exception): input_field.value = ''
        c = s.get('container'); 
        if c: c.clear()
        refresh_ui(); ui.notify('Chat cleared', type='positive')

    def undo():
        msg, _ = s['chat'].undo_last()
        if msg: s['draft'] = msg; input_field.value = msg; refresh_ui()
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
            else:
                ui.label('No files found').classes('text-gray-500 p-2')

    def looks_like_url(sv: str) -> bool:
        if not sv or ' ' in sv: return False
        u = sv.strip()
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', u):
            if re.match(r'^[\w.-]+\.[a-zA-Z]{2,}(/|$)', u): u = 'http://' + u
            else: return False
        try:
            p = urlparse(u); return p.scheme in ('http','https') and bool(p.netloc)
        except Exception: return False

    def normalize_url(u: str) -> str:
        u = u.strip()
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', u): u = 'http://' + u
        return u

    async def attach_url(url: str):
        try:
            from bs4 import BeautifulSoup
        except Exception:
            ui.notify('Missing dependency: beautifulsoup4', type='negative'); return
        u = normalize_url(url)

        def make_headers(target: str):
            try:
                p = urlparse(target); ref = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else None
            except Exception:
                ref = None
            h = {
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                               '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'),
                'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'),
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Cache-Control': 'no-cache', 'Pragma': 'no-cache', 'Upgrade-Insecure-Requests': '1',
            }
            if ref: h['Referer'] = ref
            return h

        try:
            async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=30) as client:
                headers = make_headers(u); resp = await client.get(u, headers=headers)
                if resp.status_code == 403:
                    h2 = make_headers(u)
                    h2['User-Agent'] = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15')
                    resp = await client.get(u, headers=h2)
                resp.raise_for_status()
                ctype = (resp.headers.get('content-type') or '').lower()
                if 'text/html' not in ctype and 'xml' not in ctype:
                    ui.notify('URL is not HTML', type='warning'); return
                html = resp.text or ''
        except Exception as e:
            ui.notify(f'Fetch failed: {e}', type='negative'); return

        try:
            soup = BeautifulSoup(html, 'html.parser')
            for t in soup(['script','style','noscript','iframe','svg','picture','source','canvas','meta','link']): t.decompose()
            title = (soup.title.string or '').strip() if soup.title and soup.title.string else ''
            blocks = []
            for el in soup.find_all(['h1','h2','h3','h4','h5','h6','p','li','pre','blockquote']):
                txt = el.get_text(' ', strip=True)
                if not txt: continue
                if el.name in ('h1','h2','h3','h4','h5','h6'): txt = ('#' * int(el.name[1])) + ' ' + txt
                elif el.name == 'li': txt = '- ' + txt
                blocks.append(txt)
            text = '\n\n'.join(blocks) or soup.get_text('\n', strip=True)
            host = urlparse(u).netloc.replace(':','_')
            slug = re.sub(r'[^a-zA-Z0-9]+', '-', (title or urlparse(u).path.strip('/') or 'page')).strip('-').lower()
            h = hashlib.sha1(u.encode('utf-8')).hexdigest()[:8]
            name = f'{host}__{slug or "page"}__{h}.md'
            cache_dir = Path.home() / '.cache' / 'ai-chat' / 'web'; cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / name
            content = f'# {title or u}\n\nSource: {u}\n\n{text}\n'
            path.write_text(content, encoding='utf-8')
        except Exception as e:
            ui.notify(f'Parse failed: {e}', type='negative'); return

        if str(path) not in s['chat'].files:
            s['chat'].files.append(str(path)); show_file(str(path))
        file_search.value = ''; s['file_idx'] = -1; s['file_results'] = []; file_results_container.clear(); focus_file_search()

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
            q = (file_search.value or '').strip()
            if q and looks_like_url(q): await attach_url(q); return
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
                file_search = ui.input(placeholder='Search files or paste URL...').props(f'{P} debounce=250 id=file-search').classes('w-full')
                file_results_container = ui.column().classes('file-results')
                file_search.on_value_change(search); file_search.on('keydown', file_search_keydown)

    chat_stack = ui.element('div').classes('chat-stack')
    with chat_stack:
        s['container_id'] = f'chat-{time.time_ns()}'
        s['container'] = ui.column().classes('chat-container').props(f'id={s["container_id"]}')
    refresh_ui(); init_code_copy_buttons()

    with ui.element('div').classes('chat-footer'):
        with ui.row().classes('w-full p-3 gap-2'):
            input_field = ui.textarea(placeholder='Type your message...').props(f'{P} rows=4 id=input-field').classes('flex-grow text-white').bind_value(app.storage.tab, 'draft')
            input_field.on('keydown', handle_keydown)
            mode_select = ui.select(['chat','extract'], label='Mode').props(P).classes('w-32 text-white').bind_value(app.storage.tab, 'mode')
            with ui.column().classes('gap-2'):
                back_button = ui.button('Back', on_click=undo, icon='undo').bind_visibility_from(s, 'streaming', lambda v: not v).props('color=orange')
                stop_button = ui.button('Stop', on_click=stop_streaming, icon='stop').bind_visibility_from(s, 'streaming').props('color=red')
                clear_button = ui.button('Clear', on_click=clear_chat, icon='delete').props('color=grey')

if __name__ in {'__main__','__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False, reconnect_timeout=300, ssl_certfile="cert.pem", ssl_keyfile="key.pem")