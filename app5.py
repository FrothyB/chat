import asyncio
import contextlib
import os
import time
import argparse
import re
import tempfile
import uuid
from urllib.parse import urlparse
from pathlib import Path
import httpx
from nicegui import app, ui

from chat_utils import (
    DEFAULT_REASONING, ChatClient, search_files, STYLE_CSS, MODELS, REASONING_LEVELS,
    DEFAULT_MODEL, EXTRACT_ADD_ON, ReasoningEvent, FILE_LIKE_EXTS
)

# --- Constants & Config ---

P_PROPS = 'dark outlined dense color=white'
MD_EXTRAS = ['break-on-newline', 'fenced-code-blocks', 'tables', 'cuddled-lists', 'mermaid', 'latex', 'code-friendly']
MD_CLASSES = 'prose prose-sm max-w-none break-words'

COPY_BTN_JS = '''
(() => {{
  const root = document.getElementById('{root_id}'); if (!root) return;
  const mainCopy = document.getElementById('{root_id}-copy');
  const addButtons = () => {{
    let added = 0;
    root.querySelectorAll('pre > code').forEach(code => {{
      const pre = code.parentElement; if (!pre || pre.dataset.copyBound) return;
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
      pre.appendChild(btn); added++;
    }});
    return added;
  }};
  const countMissing = () => Array.from(root.querySelectorAll('pre')).filter(pre => !pre.dataset.copyBound).length;
  const tryScan = (attempts) => {{
    requestAnimationFrame(() => {{
      addButtons();
      if (countMissing() > 0 && attempts > 0) setTimeout(() => tryScan(attempts - 1), 80);
    }});
  }};
  tryScan(25);
}})();
'''

HEAD_CSS = '''
<style>
  .code-copy-btn{position:absolute;top:.35rem;right:.35rem}
  pre{position:relative}
</style>
'''

# --- Helpers ---

def looks_like_url(sv: str) -> bool:
    if not sv: return False
    u = sv.strip()
    if not u or ' ' in u: return False
    ul = u.lower()
    return ul.startswith('http://') or ul.startswith('https://') or ul.startswith('www.')

def normalize_url(u: str) -> str:
    u = (u or '').strip()
    if not u: return ''
    if u.lower().startswith('www.'): return 'http://' + u
    return u

async def fetch_url_content(url: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError('Missing dependency: beautifulsoup4')

    u = normalize_url(url)

    def make_headers(target: str):
        try:
            p = urlparse(target)
            ref = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else None
        except Exception:
            ref = None
        h = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache', 'Pragma': 'no-cache', 'Upgrade-Insecure-Requests': '1',
        }
        if ref: h['Referer'] = ref
        return h

    async def httpx_get_html(target: str) -> tuple[int, str, str]:
        async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=30) as client:
            headers = make_headers(target)
            resp = await client.get(target, headers=headers)
            if resp.status_code == 403:
                headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15'
                resp = await client.get(target, headers=headers)
            return resp.status_code, (resp.headers.get('content-type') or ''), (resp.text or '')

    async def playwright_get_html(target: str) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("403 from target; install Playwright: <!--CODE_BLOCK_71337--> then <!--CODE_BLOCK_71338-->")

        headless = os.getenv('AI_CHAT_PLAYWRIGHT_HEADLESS', '').strip() in {'1', 'true', 'yes'}
        with tempfile.TemporaryDirectory(prefix='ai-chat-pw-') as d:
            profile_dir = Path(d) / 'profile'
            profile_dir.mkdir(parents=True, exist_ok=True)
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=headless,
                    args=['--disable-blink-features=AutomationControlled'],
                )
                try:
                    page = await ctx.new_page()
                    await page.goto(target, wait_until='networkidle', timeout=60_000)
                    return await page.content()
                finally:
                    with contextlib.suppress(Exception):
                        await ctx.close()

    status, ctype, html = await httpx_get_html(u)
    if status == 403:
        html = await playwright_get_html(u)
        ctype, status = 'text/html', 200

    if status >= 400:
        raise httpx.HTTPStatusError(f'{status} {u}', request=None, response=None)

    ctype = (ctype or '').lower()
    if not any(x in ctype for x in ('text/html', 'xml', 'text/plain')):
        raise ValueError('URL is not HTML/text')

    soup = BeautifulSoup(html, 'html.parser')
    for t in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'picture', 'source', 'canvas', 'meta', 'link']):
        t.decompose()

    title = (soup.title.string or '').strip() if soup.title and soup.title.string else ''
    blocks = []
    for el in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'pre', 'blockquote']):
        txt = el.get_text(' ', strip=True)
        if not txt: continue
        if el.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            txt = ('#' * int(el.name[1])) + ' ' + txt
        elif el.name == 'li':
            txt = '- ' + txt
        blocks.append(txt)

    text = '\n\n'.join(blocks) or soup.get_text('\n', strip=True)
    hdr = f"URL: {u}\n" + (f"Title: {title}\n" if title else '')
    return hdr + "\n" + text + "\n"

async def run_timer(label, storage_tab):
    start = time.time()
    while storage_tab.get('streaming') and label:
        elapsed = int(time.time() - start)
        with contextlib.suppress(Exception):
            label.text = f"{elapsed // 60}:{(elapsed % 60):02d}"
        await asyncio.sleep(1.0)

# --- Main Page ---

@ui.page('/')
async def main_page():
    await ui.context.client.connected()
    s = app.storage.tab

    # Persistent state defaults
    defaults = {
        'chat': ChatClient(), 'draft': '', 'model': DEFAULT_MODEL,
        'reasoning': DEFAULT_REASONING, 'mode': 'chat+edit', 'streaming': False,
        'answer_counter': 0, 'msg_counter': 0, 'file_results': [],
        'file_idx': -1, 'edit_history': [], 'pending_edits_text': None,
        'apply_all_bubble': None, 'last_edit_round_status': None,
        'url_attachments': [],
        # Streaming state
        'session_id': str(uuid.uuid4()), 'stream_buffer': [],
        'stream_done': False, 'stream_error': None, 'render_pos': 0,
        'finalized': False, 'producer_task': None, 'consumer_timer': None,
        'answer_started': False, 'last_render_time': 0.0, 'last_render_duration': 0.0,
    }
    for k, v in defaults.items():
        s.setdefault(k, v)

    # Ephemeral state cleanup
    for k in ('container', 'container_id', 'answer_md', 'answer_container', 'answer_id', 'stream'):
        s.pop(k, None)

    ui.add_head_html(STYLE_CSS)
    ui.add_head_html(HEAD_CSS)

    def scan_code_copy_buttons(root_id: str):
        if root_id: ui.run_javascript(COPY_BTN_JS.format(root_id=root_id))

    def build_tools(target_id: str, with_timer: bool = False, get_text=None):
        tools = ui.element('div').classes('answer-tools').props(f'id={target_id}-tools')
        timer = None
        with tools:
            copy_btn_id = f'{target_id}-copy'
            btn = None
            async def on_copy(getter=get_text or (lambda: '')):
                text = (getter() or '').rstrip()
                ui.clipboard.write(text)
                if btn:
                    btn.classes('copied')
                    await asyncio.sleep(1)
                    btn.classes(remove='copied')
            
            btn = ui.button('', on_click=on_copy).props('icon=content_copy flat dense').classes('tool-btn copy-icon').props(f'id={copy_btn_id}')
            if with_timer:
                timer = ui.label('0:00').classes('timer')
        return tools, timer

    def show_message(role, content):
        with s['container']:
            if role == 'user':
                s['msg_counter'] += 1
                uid = f'user-{s["msg_counter"]}'
                with ui.element('div').classes('flex justify-end mb-3'):
                    with ui.element('div').classes('inline-block bg-blue-600 rounded-lg px-3 py-2 max-w-full min-w-0 user-bubble').props(f'id={uid}'):
                        ui.markdown(content, extras=MD_EXTRAS).classes(MD_CLASSES)
                with ui.element('div').classes('flex justify-end mt-1'):
                    build_tools(uid, get_text=lambda c=content: c)
            else:
                s['answer_counter'] += 1
                aid = f'answer-{s["answer_counter"]}'
                is_stream = (content == '')
                with ui.element('div').classes('flex justify-start mb-3'):
                    with ui.element('div').classes('bg-gray-800 rounded-lg px-3 py-2 w-full min-w-0 answer-bubble').props(f'id={aid}'):
                        if is_stream:
                            # Single markdown block for streaming
                            ans_container = ui.column().classes('answer-content no-gap')
                            s['answer_id'] = aid
                            s['answer_container'] = ans_container
                            with ans_container:
                                md = ui.markdown('', extras=MD_EXTRAS).classes(MD_CLASSES)
                            s['answer_md'] = md
                            def getter():
                                md = s.get('answer_md')
                                return md.content if md else ''
                        else:
                            md = ui.markdown(content, extras=MD_EXTRAS).classes(MD_CLASSES)
                            getter = lambda m=md: m.content
                with ui.element('div').classes('flex justify-start answer-tools-row mb-3'):
                    tools, timer = build_tools(aid, with_timer=True, get_text=getter)
                    return timer

    def show_file(path):
        name = Path(path).name
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-3'):
                with ui.element('div').classes('bg-green-900 border border-green-700 rounded-lg px-3 py-2 flex items-center gap-2'):
                    ui.icon('attach_file').classes('text-green-400')
                    ui.label(f'Attached: {name}').classes('text-green-300 text-sm')
                    ui.button(icon='close', on_click=lambda p=path: remove_file(p)).props('flat dense size=sm').classes('text-green-400')

    def show_url(item):
        url = (item or {}).get('url') or ''
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-3'):
                with ui.element('div').classes('bg-purple-900 border border-purple-700 rounded-lg px-3 py-2 flex items-center gap-2'):
                    ui.icon('link').classes('text-purple-300')
                    ui.label(f'Attached URL: {url}').classes('text-purple-200 text-sm')
                    ui.button(icon='close', on_click=lambda u=url: remove_url(u)).props('flat dense size=sm').classes('text-purple-300')

    def show_edit_bubble(key, status='pending', lines_info='', record=True):
        name = Path(key).name
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-2'):
                klass = 'edit-bubble' + (' success' if status == 'success' else ' error' if status == 'error' else '')
                with ui.element('div').classes(klass):
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
            s['edit_history'].append({'path': key, 'status': status, 'info': lines_info})

    def reject_edit(key):
        path = next((p for p in s['chat'].edited_files if p == key or Path(p).name == key), None)
        if path and s['chat'].rollback_file(path):
            ui.notify(f'Reverted {Path(path).name}', type='positive')
        else:
            ui.notify('Nothing to revert', type='warning')

    def remove_url(url: str):
        s['url_attachments'] = [x for x in (s.get('url_attachments') or []) if x.get('url') != url]
        if s.get('streaming'):
            ui.notify('URL removed; changes will reflect after the response finishes.', type='info')
            return
        refresh_ui()

    def remove_file(path):
        with contextlib.suppress(ValueError):
            s['chat'].files.remove(path)
        if s.get('streaming'):
            ui.notify('File removed; changes will reflect after the response finishes.', type='info')
            return
        refresh_ui()

    def refresh_ui():
        s['container'].clear()
        for role, content in s['chat'].get_display_messages():
            t = show_message(role, content)
            if role == 'assistant' and content == '' and t:
                asyncio.create_task(run_timer(t, s))
        for p in s['chat'].files:
            show_file(p)
        for item in (s.get('url_attachments') or []):
            show_url(item)
        for item in (s.get('edit_history') or []):
            show_edit_bubble(item['path'], item.get('status', 'success'), item.get('info', ''), record=False)

    def finish_stream(full_text: str):
        full_text = (full_text or '').rstrip() or 'Response stopped.'
        with contextlib.suppress(Exception):
            s['chat'].ensure_last_assistant_nonempty(full_text)

        rendered = s['chat'].render_for_display(full_text)
        with contextlib.suppress(Exception):
            s['chat'].set_last_assistant_display(rendered)

        md = s.get('answer_md')
        if md:
            with contextlib.suppress(Exception):
                md.set_content(rendered)
                scan_code_copy_buttons(s.get('answer_id', ''))

        s['streaming'] = False
        with contextlib.suppress(Exception):
            stream = s.pop('stream', None)
            if hasattr(stream, 'aclose'):
                asyncio.create_task(stream.aclose())
        s.pop('answer_id', None)

    async def apply_edits_from_response(full_text: str):
        try:
            events = s['chat'].apply_markdown_edits(full_text)
            for ev in events or []:
                key = ev.path or ev.filename
                if ev.kind == 'complete':
                    show_edit_bubble(key, 'success', ev.details)
                elif ev.kind == 'error':
                    show_edit_bubble(key, 'error', ev.details or '')
        except Exception as e:
            ui.notify(f'Edit error: {e}', type='negative')

    def show_apply_all_bubble(full_text: str):
        with contextlib.suppress(Exception):
            old = s.pop('apply_all_bubble', None)
            if old: old.delete()
        
        s['pending_edits_text'] = full_text
        s['last_edit_round_status'] = 'pending'
        
        with s['container']:
            with ui.element('div').classes('flex justify-center mb-2'):
                bubble = ui.element('div').classes('edit-bubble')
                s['apply_all_bubble'] = bubble
                with bubble:
                    ui.icon('tips_and_updates').classes('text-blue-300')
                    ui.label('Edits available. Apply to all files?').classes('text-blue-200 text-sm')
                    async def on_apply():
                        with contextlib.suppress(Exception):
                            b = s.pop('apply_all_bubble', None)
                            if b: b.delete()
                        text = s.get('pending_edits_text') or ''
                        s['pending_edits_text'] = None
                        await apply_edits_from_response(text)
                        s['last_edit_round_status'] = 'applied'
                    ui.button('Apply edits', on_click=on_apply).props('flat dense size=sm color=positive').classes('ml-2')

    def clear_edit_round_state(before_send: bool = False) -> str | None:
        status = s.get('last_edit_round_status')
        note = None
        if before_send and status:
            if status == 'pending': status = 'skipped'
            if status == 'applied':
                note = 'I have accepted and implemented your latest round of edits above.'
            elif status == 'skipped':
                note = 'I have not accepted or implemented your latest round of edits above.'
        s['last_edit_round_status'] = None
        s['pending_edits_text'] = None
        with contextlib.suppress(Exception):
            b = s.pop('apply_all_bubble', None)
            if b: b.delete()
        return note

    # --- Streaming Logic ---

    _CODE_FENCE_RE = re.compile(r'(?m)^\s*```')

    def with_temp_code_fence(text: str) -> str:
        if not text or len(_CODE_FENCE_RE.findall(text)) % 2 == 0:
            return text
        return text + ('```' if text.endswith('\n') else '\n```')
    
    def cancel_producer():
        t = s.get('producer_task')
        if t and not t.done(): t.cancel()
        s['producer_task'] = None

    def stop_consumer():
        tm = s.get('consumer_timer')
        if tm:
            with contextlib.suppress(Exception): tm.active = False
        s['consumer_timer'] = None

    def reset_stream_state():
        cancel_producer()
        stop_consumer()
        s['stream_buffer'] = []
        s['stream_done'] = False
        s['stream_error'] = None
        s['render_pos'] = 0
        s['finalized'] = False
        s['answer_started'] = False
        s['last_render_time'] = 0.0
        s['last_render_duration'] = 0.0

    def ensure_answer_md():
        if not s.get('answer_md'):
            cont = s.get('answer_container')
            if not cont:
                timer_label = show_message('assistant', '')
                if timer_label: asyncio.create_task(run_timer(timer_label, s))
                cont = s.get('answer_container')
            if cont:
                with cont:
                    md = ui.markdown('', extras=MD_EXTRAS).classes(MD_CLASSES)
                s['answer_md'] = md

    def consume():
        try:
            ensure_answer_md()
            chunks, pos = s.get('stream_buffer', []), s.get('render_pos', 0)
            n = len(chunks)
            if pos < n:
                text = ''.join(chunks)
                s['stream_buffer'] = [text]
                s['render_pos'] = 1
                md = s.get('answer_md')
                if md:
                    now = time.monotonic()
                    last = s.get('last_render_time', 0.0)
                    dur = s.get('last_render_duration', 0.0) or 0.0
                    min_gap = 0.02 + 1.5 * dur
                    if (now - last) >= min_gap or s.get('stream_done'):
                        with contextlib.suppress(Exception):
                            t0 = time.perf_counter()
                            md.set_content(with_temp_code_fence(s['chat'].render_for_display(text)))
                            s['last_render_time'] = now
                            s['last_render_duration'] = time.perf_counter() - t0

            if s.get('stream_done') and not s.get('finalized'):
                full = ''.join(s.get('stream_buffer') or [])
                full = (full or '').rstrip()
                finish_stream(full)
                s['finalized'] = True
                s['streaming'] = False
                stop_consumer()
                
                err = s.get('stream_error')
                if err:
                    ui.notify(f"Error: {err}", type='negative')
                else:
                    if s['chat'].parse_edit_markdown(full):
                        show_apply_all_bubble(full)
        except Exception:
            pass

    def start_consumer():
        stop_consumer()
        s['consumer_timer'] = ui.timer(0.05, consume, active=True)

    async def send():
        msg = (input_field.value or '').strip()
        if s.get('streaming') or not msg: return

        note = clear_edit_round_state(before_send=True)
        mode = mode_select.value

        urls = s.get('url_attachments') or []
        url_blob = '\n\n'.join((x.get('content') or '').rstrip() for x in urls if (x.get('content') or '').strip())

        user_display = f"{note}\n\n{msg}" if note else msg
        if url_blob:
            user_display += f"\n\nAttached URLs:\n{url_blob}"

        show_message('user', user_display)
        to_send = f"{user_display}\n\n{EXTRACT_ADD_ON}" if mode == 'extract' else user_display

        s['draft'] = ''
        input_field.value = ''
        s['streaming'] = True
        s['url_attachments'] = []

        timer = show_message('assistant', '')
        if timer: asyncio.create_task(run_timer(timer, s))

        reset_stream_state()
        stream = s['chat'].stream_message(to_send, model_select.value, reasoning_select.value, force_edit=(mode == 'chat+edit'))
        s['stream'] = stream

        async def producer():
            error_msg = None
            try:
                async for chunk in stream:
                    if isinstance(chunk, ReasoningEvent):
                        if not s.get('answer_started'):
                            s['stream_buffer'].append(chunk.text or '')
                        continue

                    if not s.get('answer_started'):
                        s['answer_started'] = True
                        cont = s.get('answer_container')
                        if cont:
                            with contextlib.suppress(Exception): cont.clear()
                        s['answer_md'] = None
                        s['stream_buffer'] = []
                        s['render_pos'] = 0

                    s['stream_buffer'].append(chunk or '')
            except asyncio.CancelledError:
                pass
            except Exception as e:
                error_msg = str(e)
            finally:
                s['stream_done'] = True
                s['stream_error'] = error_msg

        s['producer_task'] = asyncio.create_task(producer())
        start_consumer()

    def stop_streaming():
        if not s.get('streaming'):
            ui.notify('No active response to stop', type='warning')
            return
        cancel_producer()
        s['stream_done'] = True
        md = s.get('answer_md')
        current = ''.join(s.get('stream_buffer') or []) or ((md.content if md else '') or '')
        finish_stream((current or 'Response stopped.').rstrip())
        stop_consumer()
        s['finalized'] = True
        s['streaming'] = False
        ui.notify('Response stopped', type='info')

    def clear_chat():
        if s.get('streaming'): stop_streaming()
        stop_consumer()
        cancel_producer()

        clear_edit_round_state()
        with contextlib.suppress(Exception):
            stream = s.pop('stream', None)
            if hasattr(stream, 'aclose'):
                asyncio.create_task(stream.aclose())

        reset_stream_state()
        s['chat'] = ChatClient()
        s['draft'] = ''
        s['url_attachments'] = []

        for k in ('answer_md', 'answer_container', 'answer_id'):
            s.pop(k, None)

        s['answer_counter'] = 0
        s['msg_counter'] = 0
        s['file_results'] = []
        s['file_idx'] = -1
        s['edit_history'] = []

        with contextlib.suppress(Exception): input_field.value = ''
        c = s.get('container')
        if c: c.clear()
        refresh_ui()
        ui.notify('Chat cleared', type='positive')

    def undo():
        msg, _ = s['chat'].undo_last()
        if msg:
            clear_edit_round_state()
            s['draft'] = msg
            input_field.value = msg
            refresh_ui()
        else:
            ui.notify('No messages to undo', type='warning')

    async def handle_keydown(event):
        if event.args.get('key') == 'Enter' and not event.args.get('shiftKey'):
            with contextlib.suppress(Exception): await event.prevent_default()
            await send()

    # --- File Search & Attachment ---

    def render_file_results():
        file_results_container.clear()
        results = s.get('file_results') or []
        idx = s.get('file_idx', -1)
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

    async def attach_url(url: str):
        try:
            u = normalize_url(url)
            content = await fetch_url_content(u)
            items = (s.get('url_attachments') or [])
            if not any(x.get('url') == u for x in items):
                items.append({'url': u, 'content': content})
                s['url_attachments'] = items
                show_url(items[-1])

            file_search.value = ''
            s['file_idx'] = -1
            s['file_results'] = []
            file_results_container.clear()
            focus_file_search()
        except Exception as e:
            ui.notify(f'URL error: {e}', type='negative')

    def focus_file_search():
        ui.run_javascript('document.querySelector("#file-search")?.focus()')

    def scroll_active_into_view():
        i = s.get('file_idx', -1)
        if i >= 0:
            ui.run_javascript(f'document.getElementById("file-opt-{i}")?.scrollIntoView({{block:"nearest"}});')

    async def search():
        q = (file_search.value or '').strip()
        s['file_idx'] = -1
        s['file_results'] = []
        file_results_container.clear()
        if len(q) < 2: return
        s['file_results'] = search_files(q) or []
        render_file_results()

    async def file_search_keydown(event):
        key = event.args.get('key')
        results = s.get('file_results') or []
        n = len(results)
        
        if key in ('ArrowDown', 'Down'):
            if n == 0: return
            s['file_idx'] = (s.get('file_idx', -1) + 1) % n
            render_file_results()
            scroll_active_into_view()
        elif key in ('ArrowUp', 'Up'):
            if n == 0: return
            s['file_idx'] = (s.get('file_idx', -1) - 1) % n
            render_file_results()
            scroll_active_into_view()
        elif key == 'Enter':
            q = (file_search.value or '').strip()
            if q and looks_like_url(q):
                await attach_url(q)
                return
            if '*' in q:
                try:
                    s['file_results'] = search_files(q) or []
                    results = s['file_results']
                    n = len(results)
                except Exception:
                    results, n = [], 0
                if n == 0: return
                attach_multiple(results)
                return
            
            i = s.get('file_idx', -1)
            if n == 0: return
            if not (0 <= i < n): i = 0
            select_file(results[i])
        elif key == 'Escape':
            s['file_idx'] = -1
            s['file_results'] = []
            file_results_container.clear()

    def select_file(path):
        if path not in s['chat'].files:
            s['chat'].files.append(path)
            show_file(path)
        file_search.value = ''
        s['file_idx'] = -1
        s['file_results'] = []
        file_results_container.clear()
        focus_file_search()

    def attach_multiple(paths):
        for p in paths:
            if p not in s['chat'].files:
                s['chat'].files.append(p)
                show_file(p)
        file_search.value = ''
        s['file_idx'] = -1
        s['file_results'] = []
        file_results_container.clear()
        focus_file_search()

    # --- Layout ---

    with ui.element('div').classes('fixed-header'):
        with ui.row().classes('gap-4 p-3 w-full'):
            model_select = ui.select(MODELS, label='Model').props(P_PROPS).classes('text-white w-56').bind_value(app.storage.tab, 'model')
            reasoning_select = ui.select(list(REASONING_LEVELS.keys()), label='Reasoning').props(P_PROPS).classes('text-white w-32').bind_value(app.storage.tab, 'reasoning')
            with ui.element('div').classes('flex-grow relative'):
                file_search = ui.input(placeholder='Search files or paste URL...').props(f'{P_PROPS} debounce=250 id=file-search').classes('w-full')
                file_results_container = ui.column().classes('file-results')
                file_search.on_value_change(search)
                file_search.on('keydown', file_search_keydown)

    chat_stack = ui.element('div').classes('chat-stack')
    with chat_stack:
        s['container_id'] = f'chat-{time.time_ns()}'
        s['container'] = ui.column().classes('chat-container').props(f'id={s["container_id"]}')
    
    refresh_ui()

    def reattach_consumer_if_needed():
        if (s.get('stream_buffer') or []) and not s.get('finalized'):
            start_consumer()

    reattach_consumer_if_needed()

    with ui.element('div').classes('chat-footer'):
        with ui.row().classes('w-full p-3 gap-2'):
            input_field = ui.textarea(placeholder='Type your message...').props(f'{P_PROPS} autogrow input-class="min-h-20 max-h-100" id=input-field').classes('flex-grow text-white').bind_value(app.storage.tab, 'draft')
            input_field.on('keydown', handle_keydown)
            mode_select = ui.select(['chat+edit', 'chat', 'extract'], label='Mode').props(P_PROPS).classes('w-32 text-white').bind_value(app.storage.tab, 'mode')
            with ui.column().classes('gap-2'):
                ui.button('Back', on_click=undo, icon='undo').bind_visibility_from(s, 'streaming', lambda v: not v).props('color=orange')
                ui.button('Stop', on_click=stop_streaming, icon='stop').bind_visibility_from(s, 'streaming').props('color=red')
                ui.button('Clear', on_click=clear_chat, icon='delete').props('color=grey')

if __name__ in {'__main__', '__mp_main__'}:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ui.run(title='AI Chat', port=args.port, host='0.0.0.0', dark=True, show=False, reconnect_timeout=300, ssl_certfile="cert.pem", ssl_keyfile="key.pem")