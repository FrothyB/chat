import contextlib, os, tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx


def normalize_url(u: str) -> str:
    u = (u or '').strip()
    if not u: return ''
    return 'http://' + u if u.lower().startswith('www.') else u


def looks_like_url(v: str) -> bool:
    if not v: return False
    u = v.strip()
    if not u or ' ' in u: return False
    ul = u.lower()
    return ul.startswith('http://') or ul.startswith('https://') or ul.startswith('www.')


def _make_headers(target: str) -> dict[str, str]:
    ref = None
    with contextlib.suppress(Exception):
        p = urlparse(target)
        ref = f'{p.scheme}://{p.netloc}/' if p.scheme and p.netloc else None
    h = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Upgrade-Insecure-Requests': '1',
    }
    if ref: h['Referer'] = ref
    return h


async def _http_get(target: str) -> tuple[int, str, str]:
    async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=30) as client:
        headers = _make_headers(target)
        resp = await client.get(target, headers=headers)
        if resp.status_code == 403:
            headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15'
            resp = await client.get(target, headers=headers)
        return resp.status_code, (resp.headers.get('content-type') or ''), (resp.text or '')


async def _playwright_get(target: str) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError('403 from target; install Playwright and browser binaries')
    headless = os.getenv('AI_CHAT_PLAYWRIGHT_HEADLESS', '').strip().lower() in {'1', 'true', 'yes'}
    with tempfile.TemporaryDirectory(prefix='ai-chat-pw-') as d:
        profile = Path(d) / 'profile'
        profile.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(str(profile), headless=headless, args=['--disable-blink-features=AutomationControlled'])
            try:
                page = await ctx.new_page()
                await page.goto(target, wait_until='networkidle', timeout=60_000)
                return await page.content()
            finally:
                with contextlib.suppress(Exception): await ctx.close()


async def fetch_url_content(url: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError('Missing dependency: beautifulsoup4')

    u = normalize_url(url)
    if not u: raise ValueError('Empty URL')

    status, ctype, html = await _http_get(u)
    if status == 403: status, ctype, html = 200, 'text/html', await _playwright_get(u)
    if status >= 400: raise httpx.HTTPStatusError(f'{status} {u}', request=None, response=None)

    ctype_l = (ctype or '').lower()
    if not any(x in ctype_l for x in ('text/html', 'xml', 'text/plain')): raise ValueError('URL is not HTML/text')

    soup = BeautifulSoup(html, 'html.parser')
    for t in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'picture', 'source', 'canvas', 'meta', 'link']): t.decompose()

    title = (soup.title.string or '').strip() if soup.title and soup.title.string else ''
    blocks = []
    for el in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'pre', 'blockquote']):
        txt = el.get_text(' ', strip=True)
        if not txt: continue
        if el.name in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}: txt = ('#' * int(el.name[1])) + ' ' + txt
        elif el.name == 'li': txt = '- ' + txt
        blocks.append(txt)

    text = '\n\n'.join(blocks) or soup.get_text('\n', strip=True)
    hdr = f'URL: {u}\n' + (f'Title: {title}\n' if title else '')
    return hdr + '\n' + text + '\n'