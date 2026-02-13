import os, json, asyncio, contextlib, re, tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set, AsyncGenerator, Union
from dataclasses import dataclass, field

from stuff import *
from openai import AsyncOpenAI

API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api/v1"
BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "openai/gpt-5.2"
DEFAULT_REASONING = "medium"
MODELS = ["google/gemini-3-flash-preview", "openai/gpt-5.2", "openai/gpt-5.2-pro", "anthropic/claude-4.6-opus", "openai/gpt-oss-120b"]
REASONING_LEVELS = {"none": 0, "minimal": 1024, "low": 2048, "medium": 4096, "high": 16384}

FILE_LIKE_EXTS = {".py",".pyw",".ipynb",".js",".mjs",".cjs",".ts",".tsx",".c",".cc",".cpp",".cxx",".h",".hpp",".hh",".hxx",".go",".rs",".cs",".java",".html",".htm",".css",".md",".markdown",".txt",".rst",".json",".yaml",".yml",".toml",".sql",".sh",".bash",".zsh",".bat",".ps1"}

# Every N lines, prefix that line with "<line_no>" when attaching file contents.
LINE_NUMBER_EVERY = 1

def search_files(query: str, base_path: Optional[str] = None, max_results: int = 20) -> List[str]:
    if not query or len(query) < 2: return []
    base = (Path(base_path).resolve() if base_path else BASE_DIR)

    def rel_of(p: Path) -> Optional[str]:
        try: return p.relative_to(base).as_posix()
        except Exception: return None

    q = (query or '').strip().replace('\\', '/')
    if not q: return []

    toks = [t for t in re.split(r'\s+', q) if t]
    if not toks: return []

    def to_regex(pat: str) -> re.Pattern:
        buf = []
        for ch in pat:
            if ch == '*': buf.append('.*')
            elif ch == '?': buf.append('.')
            else: buf.append(re.escape(ch))
        return re.compile('^' + ''.join(buf) + '$', re.IGNORECASE)

    patterns, terms = [], []
    for t in toks:
        (patterns if any(ch in t for ch in ('*', '?')) else terms).append(t)

    def ok_common(item: Path, rel: str) -> bool:
        return not any(p.startswith('.') for p in Path(rel).parts) and item.suffix.lower() in FILE_LIKE_EXTS

    if patterns:
        pats = []
        for pat in patterns:
            pat = os.path.expanduser(pat) if pat.startswith('~') else pat
            if pat.startswith('/'):
                try: pat = Path(pat).resolve().relative_to(base).as_posix()
                except Exception: return []
            pats.append((to_regex(pat), '/' not in pat))  # (rx, match_basename)

        tset = [t.lower() for t in terms]
        results: List[str] = []
        with contextlib.suppress(Exception):
            for item in base.rglob('*'):
                if len(results) >= max_results: break
                if not item.is_file(): continue
                rel = rel_of(item)
                if not rel or not ok_common(item, rel): continue
                if any(not rx.match(item.name if on_name else rel) for rx, on_name in pats): continue
                rell = rel.lower()
                if any(t not in rell for t in tset): continue
                results.append(rel)
        return sorted(set(results))[:max_results]

    terms_l = [t.lower() for t in terms]
    def scan(strict_name: bool) -> List[str]:
        results: List[str] = []
        with contextlib.suppress(Exception):
            for item in base.rglob('*'):
                if len(results) >= max_results: break
                if not item.is_file(): continue
                rel = rel_of(item)
                if not rel or not ok_common(item, rel): continue
                rell, name = rel.lower(), item.name.lower()
                if len(terms_l) == 1:
                    t = terms_l[0]
                    if t not in name and t not in rell: continue
                else:
                    if not all(t in rell for t in terms_l): continue
                    if strict_name and not any(t in name for t in terms_l): continue
                results.append(rel)
        return results
    results = scan(strict_name=True) or (scan(strict_name=False) if len(terms_l) > 1 else [])
    return sorted(set(results))[:max_results]

def _with_line_tokens(text: str, every: int = LINE_NUMBER_EVERY) -> str:
    if not text or every <= 0: return text or ''
    ls = (text or '').splitlines(True)
    for i in range(every - 1, len(ls), every):
        ls[i] = f"{i + 1}{ls[i]}"
    return ''.join(ls)

def read_files(file_paths: List[str]) -> str:
    if not file_paths: return ""
    out: List[str] = []

    for rel in file_paths:
        r = Path((rel or '').strip().replace('\\', '/'))
        name = r.as_posix()
        if not name or r.is_absolute():
            out.append(f"### {name or rel}\nError: invalid relative path\n"); continue

        p = (BASE_DIR / r).resolve()
        if not p.is_relative_to(BASE_DIR):
            out.append(f"### {name}\nError: path escapes base dir\n"); continue

        try:
            if p.name.endswith(".ipynb"):
                raw = p.read_text(encoding='utf-8')
                nb = json.loads(raw)
                cells = []
                for cell in nb.get("cells", []):
                    if cell.get("cell_type") != "code": continue
                    src = cell.get("source", [])
                    if isinstance(src, str): src = [src]
                    if not isinstance(src, list): continue
                    src = [s if isinstance(s, str) else str(s) for s in src]
                    cells.append({"source": src})
                payload = json.dumps({"cells": cells}, indent=2) + "\n"
                out.append(f"### {name}\nExtracted only source from notebook; edit cell-by-cell if needed.\n{_with_line_tokens(payload)}\n")
            else:
                out.append(f"### {name}\n{_with_line_tokens(p.read_text(encoding='utf-8'))}\n")
        except Exception as e:
            out.append(f"### {name}\nError: {e}\n")

    return '\n'.join(out)

@dataclass
class EditEvent:
    kind: str
    filename: str
    details: str = ''
    path: Optional[str] = None

@dataclass
class ReasoningEvent:
    kind: str = 'reasoning'
    text: str = ''

@dataclass
class ReplaceBlock:
    start: int
    end: int
    new: str
    old: str = ''
    lang: str = ''

@dataclass
class EditDirective:
    kind: str
    filename: str
    explanation: str = ''
    replaces: List[ReplaceBlock] = field(default_factory=list)

class ChatClient:
    _EDIT_TRIGGER_RE = re.compile(r'\b(?:edit|rewrite)\b')

    # New edit schema (breaking):
    #   "###EDIT path/to/file"
    #   "###PLAN ..." (ignored)
    #   "###REPLACE X-Y" followed by a fenced block containing the new text.
    _EDIT_HDR_RE = re.compile(r'(?m)^\s*###EDIT[ \t]+(.+?)\s*$')
    _PLAN_HDR_RE = re.compile(r'(?m)^\s*####PLANNING\b.*$')
    _REPLACE_HDR_RE = re.compile(r'(?m)^\s*####REPLACE[ \t]+(\d+)(?:[ \t]*-[ \t]*(\d+))?\s*$')
    _CODE_FENCE_RE = re.compile(r'```[ \t]*([^\n]*)\n(.*?)```', re.DOTALL)

    _FENCE_OPEN_LINE_RE = re.compile(r'(?m)^\s*```[ \t]*([^\n]*)\s*$')
    _FENCE_ANY_LINE_RE = re.compile(r'(?m)^\s*```')

    def __init__(self):
        self.messages = [{"role": "system", "content": ""}]
        self._chat_prompt_injected = False
        self._edit_prompt_injected = False
        self.files: List[str] = []
        self.message_files: Dict[int, List[str]] = {}
        self.edited_files: Dict[str, bool] = {}
        self.edit_transactions: List[Dict] = []

        self._last_assistant_index: Optional[int] = None
        self._display_overrides: Dict[int, str] = {}
        self._file_cache: Dict[str, Tuple[int, int, List[str]]] = {}

        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=7200, max_retries=20)

    def get_completion(self, data):
        return self.client.chat.completions.create(**data)

    @staticmethod
    def _norm_newlines(s: str) -> str:
        return (s or '').replace('\r\n', '\n').replace('\r', '\n')

    def _strip_injected_prompts(self, s: str) -> str:
        s = s or ''
        if s.startswith(CHAT_PROMPT): s = s[len(CHAT_PROMPT):]
        if s.startswith(EDIT_PROMPT): s = s[len(EDIT_PROMPT):]
        return s.lstrip('\n')

    def _recompute_prompt_flags(self) -> None:
        chat, edit = False, False
        for m in self.messages:
            if m.get('role') != 'user': continue
            c = m.get('content') or ''
            if c.startswith(CHAT_PROMPT): chat = True
            if c.startswith(EDIT_PROMPT) or c.startswith(CHAT_PROMPT + EDIT_PROMPT): edit = True
            if chat and edit: break
        self._chat_prompt_injected, self._edit_prompt_injected = chat, edit

    async def stream_message(self, user_msg: str, model: str = DEFAULT_MODEL, reasoning: str = "minimal", force_edit: bool = False
                            ) -> AsyncGenerator[Union[str, ReasoningEvent], None]:
        msg_index = len(self.messages)
        if self.files: self.message_files[msg_index] = self.files.copy()

        prefix = ''
        if not self._chat_prompt_injected:
            prefix += CHAT_PROMPT
            self._chat_prompt_injected = True

        want_edit = force_edit or bool(self._EDIT_TRIGGER_RE.search((user_msg or '').lower()))
        if want_edit and not self._edit_prompt_injected:
            prefix += EDIT_PROMPT
            self._edit_prompt_injected = True

        if prefix: user_msg = f"{prefix}\n\n{user_msg}"

        content = user_msg + (f"\n\nAttached files:\n{read_files(self.files)}" if self.files else "")
        self.messages.append({"role": "user", "content": content})
        self.files = []

        assistant_index = len(self.messages)
        self._last_assistant_index = assistant_index
        self.messages.append({"role": "assistant", "content": ""})

        data = {"model": model, "messages": self.messages[:-1], "max_tokens": 50000, "temperature": 0.2, "stream": True, "reasoning_effort": reasoning}
        full_response = ""
        try:
            stream = await self.get_completion(data)
            async for chunk in stream:
                try:
                    choice = (getattr(chunk, "choices", None) or [None])[0]
                    if not choice: continue
                    delta = getattr(choice, "delta", None) or {}
                    text = getattr(delta, "content", None)
                    if text:
                        full_response += text
                        self.messages[assistant_index]["content"] = full_response
                        yield text
                    r = getattr(delta, "reasoning", None)
                    reason = r.get("content") if isinstance(r, dict) else (r if isinstance(r, str) else None)
                    if reason:
                        yield ReasoningEvent(text=reason)
                except Exception:
                    continue
            self.messages[assistant_index]["content"] = full_response
        except (asyncio.CancelledError, GeneratorExit):
            raise

    # --- Display-only expansion: inject file text + "WITH" after "###REPLACE a-b" ---
    def _get_file_lines_cached(self, rel: str) -> Optional[List[str]]:
        try:
            rp = Path((rel or '').strip().replace('\\', '/'))
            if not rel or rp.is_absolute(): return None
            p = (BASE_DIR / rp).resolve()
            if not p.is_relative_to(BASE_DIR) or not p.exists(): return None

            st = p.stat()
            mtime_ns = getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))
            cached = self._file_cache.get(rel)
            if cached and cached[0] == mtime_ns and cached[1] == st.st_size:
                return cached[2]

            norm = self._norm_newlines(p.read_text(encoding='utf-8'))
            lines = norm.split('\n')
            if norm.endswith('\n'): lines = lines[:-1]
            self._file_cache[rel] = (mtime_ns, st.st_size, lines)
            return lines
        except Exception:
            return None

    def render_for_display(self, md: str) -> str:
        md = md or ''
        if '###REPLACE' not in md: return md

        lines = md.split('\n')
        out: List[str] = []
        cur_edit_file: Optional[str] = None
        pending: Optional[Dict[str, object]] = None  # {"hdr": str, "a": int, "b": int, "between": List[str]}

        def flush_pending() -> None:
            nonlocal pending
            if not pending: return
            out.append(pending["hdr"])  # type: ignore[index]
            out += (pending["between"] or [])  # type: ignore[operator]
            pending = None

        for line in lines:
            medit = self._EDIT_HDR_RE.match(line)
            if medit:
                flush_pending()
                cur_edit_file = (medit.group(1) or '').strip().replace('`', '')
                out.append(line)
                continue

            if self._EDIT_HDR_RE.match(line) and not medit:
                flush_pending()
                cur_edit_file = None
                out.append(line)
                continue

            if cur_edit_file and pending:
                mfence = self._FENCE_OPEN_LINE_RE.match(line)
                if mfence:
                    lang = (mfence.group(1) or '').strip()
                    a, b = int(pending["a"]), int(pending["b"])  # type: ignore[arg-type]
                    path = self._resolve_path(cur_edit_file, create_if_missing=False)
                    file_lines = self._get_file_lines_cached(path) if path else None
                    between = pending["between"] or []  # type: ignore[assignment]
                    should_inject = bool(lang and file_lines and (1 <= a <= b <= len(file_lines)) and not any(self._FENCE_ANY_LINE_RE.match(x) for x in between))
                    out.append(pending["hdr"])  # type: ignore[index]
                    out += between
                    if should_inject:
                        out += [f"```{lang}".rstrip(), '\n'.join(file_lines[a - 1:b]), "```", "####WITH"]
                    pending = None
                    out.append(line)
                    continue

                mrep2 = self._REPLACE_HDR_RE.match(line)
                if mrep2:
                    flush_pending()
                    pending = {"hdr": line, "a": int(mrep2.group(1)), "b": int(mrep2.group(2) or mrep2.group(1)), "between": []}
                    continue

                pending["between"].append(line)  # type: ignore[index]
                continue

            if cur_edit_file:
                mrep = self._REPLACE_HDR_RE.match(line)
                if mrep:
                    flush_pending()
                    pending = {"hdr": line, "a": int(mrep.group(1)), "b": int(mrep.group(2) or mrep.group(1)), "between": []}
                    continue

            out.append(line)

        flush_pending()
        return '\n'.join(out)

    def set_last_assistant_display(self, display_md: str) -> None:
        if self._last_assistant_index is not None:
            self._display_overrides[self._last_assistant_index] = display_md or ''

    # --- Edit parsing + applying (line ranges only) ---
    def parse_edit_markdown(self, md: str) -> List[EditDirective]:
        if not md: return []
        text = self._norm_newlines(md)
        matches = list(self._EDIT_HDR_RE.finditer(text))
        out: List[EditDirective] = []

        for i, m in enumerate(matches):
            filename = (m.group(1) or '').strip().replace('`', '')
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[start:end].strip()

            replaces: List[ReplaceBlock] = []
            pos = 0
            while True:
                r = self._REPLACE_HDR_RE.search(section, pos)
                if not r: break
                mnew = self._CODE_FENCE_RE.search(section, r.end())
                if not mnew: break
                a, b = int(r.group(1)), int(r.group(2) or r.group(1))
                replaces.append(ReplaceBlock(start=a, end=b, new=mnew.group(2), lang=(mnew.group(1) or '').strip()))
                pos = mnew.end()

            if not replaces:
                fences = list(self._CODE_FENCE_RE.finditer(section))
                if len(fences) == 1:
                    f = fences[0]
                    replaces = [ReplaceBlock(start=0, end=0, new=f.group(2), lang=(f.group(1) or '').strip())]

            expl_cut = min([x.start() for x in (self._REPLACE_HDR_RE.search(section), self._CODE_FENCE_RE.search(section)) if x], default=None)
            expl = section[:expl_cut].strip() if expl_cut is not None else section.strip()
            out.append(EditDirective(kind='EDIT', filename=filename, explanation=expl, replaces=replaces))

        return out

    def _resolve_path(self, filename: str, create_if_missing: bool = False) -> Optional[str]:
        raw = (filename or '').strip().replace('`', '').replace('\\', '/')
        if not raw: return None
        cand = Path(raw)
        if cand.is_absolute(): return None

        def safe_rel(p: Path) -> Optional[Path]:
            try:
                rp = p.relative_to(BASE_DIR)
                return Path(rp.as_posix())
            except Exception:
                return None

        abs0 = (BASE_DIR / cand).resolve()
        rel0 = safe_rel(abs0)
        if not rel0: return None
        if abs0.exists() or create_if_missing: return rel0.as_posix()

        ctx_files = self.message_files.get(max(self.message_files.keys()), []) if self.message_files else []
        ctx_paths = [Path(p) for p in ctx_files]

        def suffix_matches(p: Path, suffix: Path) -> bool:
            sp, pp = suffix.parts, p.parts
            return len(pp) >= len(sp) and tuple(pp[-len(sp):]) == sp

        if ctx_paths:
            if len(cand.parts) > 1:
                hits = [p for p in ctx_paths if suffix_matches(p, cand)]
                if len(hits) == 1: return hits[0].as_posix()
                if len(hits) > 1: return None
            hits = [p for p in ctx_paths if p.name == cand.name]
            if len(hits) == 1: return hits[0].as_posix()
            if len(hits) > 1: return None

        hits: List[Path] = []
        with contextlib.suppress(Exception):
            for q in BASE_DIR.rglob(cand.name):
                if not q.is_file(): continue
                rel = q.relative_to(BASE_DIR)
                if len(cand.parts) > 1 and not suffix_matches(rel, cand): continue
                hits.append(Path(rel.as_posix()))
                if len(hits) > 1: break
        if len(hits) == 1: return hits[0].as_posix()
        if len(hits) > 1: return None

        return rel0.as_posix() if create_if_missing else None

    def _atomic_write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent), delete=False) as tmp:
            tmp.write(content); tmp.flush()
            with contextlib.suppress(Exception): os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, str(path))

    def apply_markdown_edits(self, md: str) -> List[EditEvent]:
        directives = self.parse_edit_markdown(md)
        if not directives: return []
        results: List[EditEvent] = []
        ai = (len(self.messages) - 1) if (self.messages and self.messages[-1]['role'] == 'assistant') else None
        tx = {'assistant_index': ai, 'files': {}, 'changed': set()}

        def _abs(rel: str) -> Path:
            return (BASE_DIR / Path(rel)).resolve()

        def _remember_prev(rel: str):
            if rel in tx['files']: return
            p = _abs(rel)
            tx['files'][rel] = p.read_text(encoding='utf-8') if p.exists() else None

        def _count_lines(s: str) -> int:
            norm = self._norm_newlines(s or '')
            ls = norm.split('\n')
            if norm.endswith('\n'): ls = ls[:-1]
            return len(ls)

        for d in directives:
            try:
                target = self._resolve_path(d.filename, create_if_missing=True)
                if not target:
                    results.append(EditEvent('error', Path(d.filename).name, 'Invalid path (must be relative to base dir)', d.filename)); continue

                rel, p = target, _abs(target)
                if not p.is_relative_to(BASE_DIR):
                    results.append(EditEvent('error', Path(rel).name, 'Path escapes base dir', rel)); continue

                is_new = not p.exists()
                original = p.read_text(encoding='utf-8') if not is_new else ''

                if not d.replaces:
                    results.append(EditEvent('error', Path(rel).name, 'No edit blocks found', rel)); continue

                full = [b for b in d.replaces if b.start == 0 and b.end == 0]
                if full:
                    if len(full) != 1 or len(d.replaces) != 1:
                        results.append(EditEvent('error', Path(rel).name, 'Full rewrites must contain exactly one fenced block', rel)); continue

                    body = self._norm_newlines(full[0].new)
                    if is_new:
                        norm2 = body.rstrip('\n')
                        updated = norm2 + ('\n' if norm2 else '')
                    else:
                        eol = '\r\n' if '\r\n' in original else '\n'
                        had_final_nl = self._norm_newlines(original).endswith('\n')
                        norm2 = body.rstrip('\n')
                        updated = (norm2 + ('\n' if had_final_nl else '')).replace('\n', eol)

                    _remember_prev(rel)
                    self._atomic_write(p, updated)
                    tx['changed'].add(rel)
                    results.append(EditEvent('complete', Path(rel).name, f"{'created' if is_new else 'rewritten'}: {_count_lines(original) if not is_new else 0} → {_count_lines(updated)} lines", rel))
                    continue

                if is_new:
                    parts = [self._norm_newlines(b.new).rstrip('\n') for b in d.replaces]
                    norm2 = '\n'.join(parts)
                    updated = norm2 + ('\n' if norm2 else '')
                    _remember_prev(rel)
                    self._atomic_write(p, updated)
                    tx['changed'].add(rel)
                    results.append(EditEvent('complete', Path(rel).name, f"created: 0 → {_count_lines(updated)} lines", rel))
                    continue

                eol = '\r\n' if '\r\n' in original else '\n'
                norm = self._norm_newlines(original)
                had_final_nl = norm.endswith('\n')
                lines = norm.split('\n')
                if had_final_nl: lines = lines[:-1]

                n0 = len(lines)
                missing: List[str] = []
                replaced = 0

                for blk in sorted(d.replaces, key=lambda b: (b.start, b.end), reverse=True):
                    a, b = int(blk.start), int(blk.end)
                    if not (1 <= a <= b <= len(lines)):
                        missing.append(f"{a}-{b}"); continue
                    new_norm = self._norm_newlines(blk.new)
                    new_lines = new_norm.split('\n')
                    if new_norm.endswith('\n'): new_lines = new_lines[:-1]
                    lines[a - 1:b] = new_lines
                    replaced += 1

                if missing:
                    results.append(EditEvent('error', Path(rel).name, f"LINES range(s) invalid: {', '.join(missing)}", rel)); continue

                norm2 = '\n'.join(lines) + ('\n' if had_final_nl else '')
                updated = norm2 if eol == '\n' else norm2.replace('\n', '\r\n')
                if updated == original:
                    results.append(EditEvent('error', Path(rel).name, 'No changes applied', rel)); continue

                _remember_prev(rel)
                self._atomic_write(p, updated)
                tx['changed'].add(rel)
                results.append(EditEvent('complete', Path(rel).name, f"replaced {replaced} range(s): {n0} → {_count_lines(updated)} lines", rel))
            except Exception as e:
                results.append(EditEvent('error', Path(d.filename).name, f'Error: {e}', d.filename))

        if tx['changed']:
            tx['files'] = {p: tx['files'][p] for p in tx['changed']}
            self.edit_transactions.append(tx)
            for p in tx['changed']: self.edited_files[p] = True
        return results

    def rollback_file(self, file_path: str) -> bool:
        rel = (file_path or '').strip().replace('\\', '/')
        if not rel or Path(rel).is_absolute(): return False
        p = (BASE_DIR / Path(rel)).resolve()
        if not p.is_relative_to(BASE_DIR): return False

        for i in range(len(self.edit_transactions) - 1, -1, -1):
            tx = self.edit_transactions[i]
            changed: Set[str] = tx.get('changed', set())
            if rel not in changed: continue
            prev = tx['files'].get(rel, None)
            try:
                if prev is None:
                    with contextlib.suppress(FileNotFoundError): p.unlink()
                else:
                    p.write_text(prev, encoding='utf-8')
            except Exception:
                return False

            changed.remove(rel)
            tx['files'].pop(rel, None)
            if not changed:
                self.edit_transactions.pop(i)

            current = {}
            for t in self.edit_transactions:
                for path in t.get('changed', set()): current[path] = True
            self.edited_files = current
            return True

        return False

    def rollback_edits_for_assistant(self, assistant_index: int):
        while self.edit_transactions and self.edit_transactions[-1].get('assistant_index') == assistant_index:
            tx = self.edit_transactions.pop()
            for rel in list(tx.get('changed', set())):
                prev = tx['files'].get(rel, None)
                p = (BASE_DIR / Path(rel)).resolve()
                if not p.is_relative_to(BASE_DIR): continue
                with contextlib.suppress(Exception):
                    if prev is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else:
                        p.write_text(prev, encoding='utf-8')

        current = {}
        for t in self.edit_transactions:
            for path in t.get('changed', set()): current[path] = True
        self.edited_files = current

    def undo_last(self) -> Tuple[Optional[str], List[str]]:
        if len(self.messages) < 3 or self.messages[-1]['role'] != 'assistant':
            return None, []
        ai = len(self.messages) - 1
        self.rollback_edits_for_assistant(ai)
        self._display_overrides.pop(ai, None)
        self.messages.pop()

        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]['role'] == 'user':
                content = self.messages.pop(i)['content']
                files = self.message_files.pop(i, [])
                self.files = files.copy()
                user_msg = content.split('\n\nAttached files:')[0] if '\n\nAttached files:' in content else content
                if EXTRACT_ADD_ON in user_msg:
                    user_msg = user_msg.split(EXTRACT_ADD_ON, 1)[0].rstrip()
                user_msg = self._strip_injected_prompts(user_msg)
                self._recompute_prompt_flags()
                return user_msg, files

        self._recompute_prompt_flags()
        return None, []

    def get_display_messages(self) -> List[Tuple[str, str]]:
        out = []
        for idx, m in enumerate(self.messages[1:], start=1):
            role, content = m.get('role'), m.get('content') or ''
            if role == 'user':
                content = content.split('\n\nAttached files:')[0] if '\n\nAttached files:' in content else content
                content = self._strip_injected_prompts(content)
            elif role == 'assistant':
                content = self._display_overrides.get(idx, content)
            out.append((role, content))
        return out

    def ensure_last_assistant_nonempty(self, fallback: str = "Response stopped."):
        with contextlib.suppress(Exception):
            if self.messages and self.messages[-1]["role"] == "assistant" and not (self.messages[-1].get("content") or "").strip():
                self.messages[-1]["content"] = fallback