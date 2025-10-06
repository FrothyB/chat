import os, json, aiohttp, asyncio, contextlib, re, tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
from aiohttp import ClientResponse
from stuff import * 

API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-5"
MODELS = ["openai/gpt-5", "openai/gpt-5-mini", "anthropic/claude-4.5-sonnet", "x-ai/grok-4-fast"]
REASONING_LEVELS = {"none": 0, "minimal": 1024, "low": 2048, "medium": 4096, "high": 16384}

def search_files(query: str, base_path: Optional[str] = None, max_results: int = 10) -> List[str]:
    if not query or len(query) < 2: return []
    default_base = Path(__file__).resolve().parent.parent
    home = Path(base_path) if base_path else default_base
    results, q = [], query.lower()
    WHITELIST_EXTS = {'.py', '.cpp', '.cc', '.cxx', '.hpp', '.hh', '.hxx', '.h', '.go', '.cs', '.java', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.html', '.rs', '.md', '.sql'}
    try:
        for item in home.rglob('*'):
            if len(results) >= max_results: break
            try:
                if not item.is_file(): continue
                if any(p.startswith('.') for p in item.parts): continue
                if q not in item.name.lower(): continue
                if item.suffix.lower() not in WHITELIST_EXTS: continue
                results.append(str(item))
            except Exception: continue
    except Exception: pass
    return sorted(set(results))[:max_results]

def read_files(file_paths: List[str]) -> str:
    if not file_paths: return ""
    contents = []
    for path in file_paths:
        name = Path(path).name
        try:
            with open(path, 'r', encoding='utf-8') as f:
                contents.append(f"### {name}\n{f.read()}\n")
        except Exception as e:
            contents.append(f"### {name} \nError: {e}\n")
    return '\n'.join(contents)

@dataclass
class EditFile:
    path: str
    original_content: Optional[str]  # None for newly created files

@dataclass
class EditEvent:
    kind: str                 # 'complete' | 'error'
    filename: str
    details: str = ''
    path: Optional[str] = None

@dataclass
class ReasoningEvent:
    kind: str = 'reasoning'
    text: str = ''

@dataclass
class ReplaceBlock:
    old: str
    new: str

@dataclass
class EditDirective:
    kind: str                 # 'REWRITE' | 'EDIT'
    filename: str
    explanation: str = ''
    rewrite: Optional[str] = None
    replaces: List[ReplaceBlock] = field(default_factory=list)

async def _iter_sse_lines(resp: ClientResponse):
    buf = b""
    data_lines, event = [], None
    async def dispatch():
        nonlocal data_lines, event
        if not data_lines: event = None; return None
        payload = "\n".join(data_lines); data_lines, event = [], None; return payload
    try:
        async for chunk in resp.content.iter_any():
            if not chunk: continue
            buf += chunk
            while True:
                i = buf.find(b'\n')
                if i == -1: break
                raw, buf = buf[:i], buf[i+1:]
                line = raw.decode('utf-8', errors='ignore').rstrip('\r')
                if line == '':
                    payload = await dispatch()
                    if payload not in (None, ''): yield payload
                    continue
                if line.startswith(':'): continue
                if ':' in line:
                    field, value = line.split(':', 1); value = value[1:] if value.startswith(' ') else value
                else:
                    field, value = line, ''
                if field == 'data': data_lines.append(value)
                elif field == 'event': event = value
        if buf:
            line = buf.decode('utf-8', errors='ignore').rstrip('\r')
            if line and not line.startswith(':'):
                if ':' in line:
                    field, value = line.split(':', 1); value = value[1:] if value.startswith(' ') else value
                else:
                    field, value = line, ''
                if field == 'data': data_lines.append(value)
        payload = await dispatch()
        if payload not in (None, ''): yield payload
    except (asyncio.CancelledError, GeneratorExit):
        raise

class ChatClient:
    def __init__(self):
        self.messages = [{"role": "system", "content": CHAT_PROMPT}]
        self.files: List[str] = []
        self.message_files: Dict[int, List[str]] = {}
        self.headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", "Accept": "text/event-stream"}
        self.edited_files: Dict[str, bool] = {}
        self.edit_transactions: List[Dict] = []

    async def stream_message(self, user_msg: str, model: str = DEFAULT_MODEL, reasoning: str = "minimal"):
        msg_index = len(self.messages)
        if self.files: self.message_files[msg_index] = self.files.copy()
        content = user_msg + (f"\n\nAttached files:\n{read_files(self.files)}" if self.files else "")
        self.messages.append({"role": "user", "content": content})
        self.files = []
        assistant_index = len(self.messages)
        self.messages.append({"role": "assistant", "content": ""})
        data = {"model": model, "messages": self.messages[:-1], "max_tokens": 50000, "temperature": 0.2, "stream": True}
        if reasoning != "none":
            key = "effort" if ('openai' in model or 'x-ai' in model) else "max_tokens"
            data["reasoning"] = {key: reasoning if key == "effort" else REASONING_LEVELS[reasoning], "enabled": True}
        full_response, full_reasoning = "", ""
        timeout = aiohttp.ClientTimeout(total=1800, sock_read=1800)
        connector = aiohttp.TCPConnector(enable_cleanup_closed=True)
        try:
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.post(f"{BASE_URL}/chat/completions", headers=self.headers, json=data) as resp:
                    if resp.status != 200: raise Exception(f"API error: {await resp.text()}")
                    async for payload in _iter_sse_lines(resp):
                        if not payload: continue
                        if payload.strip() == '[DONE]': break
                        with contextlib.suppress(Exception):
                            obj = json.loads(payload)
                            choice = (obj.get('choices') or [{}])[0]
                            delta = choice.get('delta', {})
                            if (text := delta.get('content')):
                                full_response += text
                                self.messages[assistant_index]["content"] = full_response
                                yield text
                            r = delta.get('reasoning')
                            reason = r.get('content') if isinstance(r, dict) else (r if isinstance(r, str) else None)
                            if reason:
                                full_reasoning += reason
                                yield ReasoningEvent(text=reason)
            self.messages[assistant_index]["content"] = full_response
        except (asyncio.CancelledError, GeneratorExit):
            raise

    # --- Simplified, more robust edit parsing + applying ---

    def parse_edit_markdown(self, md: str) -> List[EditDirective]:
        if not md: return []
        text = md.replace('\r\n', '\n').replace('\r', '\n')

        sec_hdr = re.compile(r'(?im)^###\s*(EDIT|REWRITE)\s+(.+?)\s*$')
        code_fence = re.compile(r'```[ \t]*([^\n]*)\n(.*?)```', re.DOTALL)
        rep_hdr = re.compile(r'(?im)^####\s*REPLACE\s*$')
        with_hdr = re.compile(r'(?im)^####\s*WITH\s*$')

        matches = list(sec_hdr.finditer(text))
        out: List[EditDirective] = []
        for i, m in enumerate(matches):
            kind, filename = m.group(1).upper(), m.group(2).strip()
            start = m.end()
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            section = text[start:end].strip()

            if kind == 'REWRITE':
                mcode = code_fence.search(section)
                explanation = section[:mcode.start()].strip() if mcode else section.strip()
                rewrite = mcode.group(2) if mcode else ''
                out.append(EditDirective(kind='REWRITE', filename=filename, explanation=explanation, rewrite=rewrite))
                continue

            # EDIT
            replaces: List[ReplaceBlock] = []
            pos = 0
            while True:
                r = rep_hdr.search(section, pos)
                if not r: break
                m_old = code_fence.search(section, r.end())
                if not m_old:
                    pos = r.end(); continue
                w = with_hdr.search(section, m_old.end())
                if not w:
                    pos = m_old.end(); continue
                m_new = code_fence.search(section, w.end())
                if not m_new:
                    pos = w.end(); continue
                replaces.append(ReplaceBlock(old=m_old.group(2), new=m_new.group(2)))
                pos = m_new.end()

            first_rep = rep_hdr.search(section)
            explanation = section[:first_rep.start()].strip() if first_rep else section.strip()
            out.append(EditDirective(kind='EDIT', filename=filename, explanation=explanation, replaces=replaces))
        return out

    def _resolve_path(self, filename: str, create_if_missing: bool = False) -> Optional[str]:
        name = filename.strip()
        cand = Path(name)
        if cand.exists(): return str(cand)

        ctx_files: List[str] = []
        if self.message_files:
            ctx_files = self.message_files.get(max(self.message_files.keys()), [])
        ctx_paths = [Path(p) for p in ctx_files]

        def suffix_matches(p: Path, suffix: Path) -> bool:
            sp, pp = suffix.parts, p.parts
            return len(pp) >= len(sp) and tuple(pp[-len(sp):]) == sp

        if ctx_paths:
            if len(cand.parts) > 1:
                matches = [p for p in ctx_paths if suffix_matches(p, cand)]
                if len(matches) == 1: return str(matches[0])
                if len(matches) > 1: return None
            base_matches = [p for p in ctx_paths if p.name == cand.name]
            if len(base_matches) == 1: return str(base_matches[0])
            if len(base_matches) > 1: return None

        for base in ['/home/pygmy/code', '.']:
            p = Path(base) / name
            if p.exists(): return str(p)
            if len(cand.parts) > 1:
                with contextlib.suppress(Exception):
                    hits = [q for q in Path(base).rglob(cand.name) if q.is_file() and suffix_matches(q, cand)]
                    if len(hits) == 1: return str(hits[0])
                    if len(hits) > 1: return None
        return str(cand) if create_if_missing else None

    def _atomic_write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent), delete=False) as tmp:
            tmp.write(content); tmp.flush()
            with contextlib.suppress(Exception): os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, str(path))

    def _remember_original(self, path: str, original: Optional[str]):
        if path not in self.edited_files:
            self.edited_files[path] = EditFile(path=path, original_content=original)

    @staticmethod
    def _norm_newlines(s: str) -> str:
        return s.replace('\r\n', '\n').replace('\r', '\n')

    def apply_markdown_edits(self, md: str) -> List[EditEvent]:
        directives = self.parse_edit_markdown(md)
        if not directives: return []
        results: List[EditEvent] = []
        ai = (len(self.messages) - 1) if (self.messages and self.messages[-1]['role'] == 'assistant') else None
        tx = {'assistant_index': ai, 'files': {}, 'changed': set()}  # files: path->prev content (or None)

        def _remember_prev(path: str):
            if path in tx['files']: return
            p = Path(path)
            prev = p.read_text(encoding='utf-8') if p.exists() else None
            tx['files'][path] = prev

        def _mark_changed(path: str):
            tx['changed'].add(path)

        for d in directives:
            try:
                if d.kind == 'REWRITE':
                    target = self._resolve_path(d.filename, create_if_missing=True)
                    if not target:
                        results.append(EditEvent('error', Path(d.filename).name, 'Cannot resolve path', d.filename)); continue
                    name, p = Path(target).name, Path(target)
                    original = None
                    if p.exists():
                        try: original = p.read_text(encoding='utf-8')
                        except Exception as e: results.append(EditEvent('error', name, f'Read error: {e}', target)); continue
                    try:
                        _remember_prev(target)
                        self._atomic_write(p, d.rewrite or '')
                        _mark_changed(target)
                        o = 0 if original is None else len(self._norm_newlines(original).split('\n'))
                        n = len(self._norm_newlines(d.rewrite or '').split('\n'))
                        results.append(EditEvent('complete', name, (f"{o} → {n} lines" if original is not None else f"created: {n} lines"), target))
                    except Exception as e:
                        results.append(EditEvent('error', name, f'Write error: {e}', target))
                    continue

                # EDIT
                target = self._resolve_path(d.filename, create_if_missing=False)
                if not target:
                    results.append(EditEvent('error', Path(d.filename).name, 'File not found', d.filename)); continue
                name, p = Path(target).name, Path(target)
                try: original = p.read_text(encoding='utf-8')
                except Exception as e: results.append(EditEvent('error', name, f'Read error: {e}', target)); continue

                if not d.replaces:
                    results.append(EditEvent('error', name, 'No REPLACE blocks found', target)); continue

                eol = '\r\n' if '\r\n' in original else '\n'
                norm = self._norm_newlines(original)

                replaced, missing = 0, []
                for i, blk in enumerate(d.replaces, 1):
                    old = self._norm_newlines(blk.old)
                    new = self._norm_newlines(blk.new)
                    candidates = [old]
                    if old.endswith('\n'): candidates.append(old[:-1])
                    if old and not old.endswith('\n'): candidates.append(old + '\n')

                    found_cand, idx = None, -1
                    for cand in candidates:
                        idx = norm.find(cand)
                        if idx != -1:
                            found_cand = cand; break

                    if found_cand is None:
                        missing.append(i); continue

                    norm = norm[:idx] + new + norm[idx+len(found_cand):]
                    replaced += 1

                if missing:
                    results.append(EditEvent('error', name, f"REPLACE block(s) not found: {', '.join(map(str, missing))}", target)); continue

                updated = norm if eol == '\n' else norm.replace('\n', '\r\n')
                if updated == original and replaced == 0:
                    results.append(EditEvent('error', name, 'No changes applied', target)); continue
                try:
                    _remember_prev(target)
                    self._atomic_write(p, updated)
                    _mark_changed(target)
                    o = len(self._norm_newlines(original).split('\n'))
                    n = len(self._norm_newlines(updated).split('\n'))
                    results.append(EditEvent('complete', name, f"replaced {replaced} block(s): {o} → {n} lines", target))
                except Exception as e:
                    results.append(EditEvent('error', name, f'Write error: {e}', target))
            except Exception as e:
                results.append(EditEvent('error', Path(d.filename).name, f'Error: {e}', d.filename))

        if tx['changed']:
            # keep only changed files in the tx
            tx['files'] = {p: tx['files'][p] for p in tx['changed']}
            self.edit_transactions.append(tx)
            for p in tx['changed']: self.edited_files[p] = True
        return results

    def rollback_file(self, file_path: str) -> bool:
        # revert only the most recent transaction that touched this file
        for i in range(len(self.edit_transactions) - 1, -1, -1):
            tx = self.edit_transactions[i]
            changed: Set[str] = tx.get('changed', set())
            if file_path not in changed: continue
            prev = tx['files'].get(file_path, None)
            p = Path(file_path)
            try:
                if prev is None:
                    with contextlib.suppress(FileNotFoundError): p.unlink()
                else:
                    p.write_text(prev, encoding='utf-8')
            except Exception:
                return False
            # update transaction
            changed.remove(file_path)
            tx['files'].pop(file_path, None)
            if not changed:
                self.edit_transactions.pop(i)
            # rebuild edited_files index
            current = {}
            for t in self.edit_transactions:
                for path in t.get('changed', set()): current[path] = True
            self.edited_files = current
            return True
        return False

    def rollback_all_edits(self):
        # revert everything across all transactions (used rarely)
        while self.edit_transactions:
            tx = self.edit_transactions.pop()
            for path in list(tx.get('changed', set())):
                prev = tx['files'].get(path, None)
                p = Path(path)
                with contextlib.suppress(Exception):
                    if prev is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else:
                        p.write_text(prev, encoding='utf-8')
        self.edited_files = {}

    def rollback_edits_for_assistant(self, assistant_index: int):
        # revert only the transactions for this assistant (LIFO)
        while self.edit_transactions and self.edit_transactions[-1].get('assistant_index') == assistant_index:
            tx = self.edit_transactions.pop()
            for path in list(tx.get('changed', set())):
                prev = tx['files'].get(path, None)
                p = Path(path)
                with contextlib.suppress(Exception):
                    if prev is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else:
                        p.write_text(prev, encoding='utf-8')
        # rebuild edited_files index
        current = {}
        for t in self.edit_transactions:
            for path in t.get('changed', set()): current[path] = True
        self.edited_files = current

    def undo_last(self) -> Tuple[Optional[str], List[str]]:
        if len(self.messages) < 3 or self.messages[-1]['role'] != 'assistant':
            return None, []
        ai = len(self.messages) - 1
        self.rollback_edits_for_assistant(ai)
        self.messages.pop()
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]['role'] == 'user':
                content = self.messages.pop(i)['content']
                files = self.message_files.pop(i, [])
                self.files = files.copy()
                user_msg = content.split('\n\nAttached files:')[0] if '\n\nAttached files:' in content else content
                for addon in (EXTRACT_ADD_ON,):
                    if addon in user_msg:
                        user_msg = user_msg.split(addon, 1)[0].rstrip(); break
                return user_msg, files
        return None, []

    def get_display_messages(self) -> List[Tuple[str, str]]:
        return [(m['role'], m['content'].split('\n\nAttached files:')[0] if m['role'] == 'user' else m['content']) for m in self.messages[1:]]

    # --- New helper to finalize placeholders ---

    def ensure_last_assistant_nonempty(self, fallback: str = 'Response stopped.'):
        try:
            if self.messages and self.messages[-1]['role'] == 'assistant':
                content = (self.messages[-1].get('content') or '').strip()
                if content == '':
                    self.messages[-1]['content'] = fallback
        except Exception:
            pass