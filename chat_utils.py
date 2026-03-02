import os, json, asyncio, contextlib, re, tempfile
from bisect import bisect_left
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set, AsyncGenerator, Union
from dataclasses import dataclass, field

from stuff import *
from openai import AsyncOpenAI

API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api/v1"
BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "openai/gpt-5.3-codex"
DEFAULT_REASONING = "medium"
MODELS = ["google/gemini-3.1-pro-preview", "openai/gpt-5.3-codex", "openai/gpt-5.2-pro", "anthropic/claude-4.6-opus", "openai/gpt-oss-120b"]
REASONING_LEVELS = {"none": 0, "minimal": 1024, "low": 2048, "medium": 4096, "high": 16384}

FILE_LIKE_EXTS = {".py", ".pyw", ".ipynb", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx", ".go", ".rs", ".cs", ".java", ".html", ".htm", ".css", ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".sql", ".sh", ".bash", ".zsh", ".bat", ".ps1"}
REPLACE_DISAMBIG_MIN_UNIQUE_LINE_HITS = 2
ATTACHMENTS_MARKER = "\n\nAttached attachments:\n"

def search_files(query: str, base_path: Optional[str] = None, max_results: int = 20) -> List[str]:
    if not query or len(query) < 2: return []
    base = (Path(base_path).resolve() if base_path else BASE_DIR)

    def rel_of(p: Path) -> Optional[str]:
        try: return p.relative_to(base).as_posix()
        except Exception: return None

    q = (query or "").strip().replace("\\", "/")
    if not q: return []
    q0 = (os.path.expanduser(q) if q.startswith("~") else q)

    with contextlib.suppress(Exception):
        cand = (Path(q0).resolve() if q0.startswith("/") else (base / Path(q0)).resolve())
        if cand.is_file() and cand.is_relative_to(base): return [cand.relative_to(base).as_posix()]

    toks = [t for t in re.split(r"\s+", q0) if t]
    if not toks: return []

    def to_regex(pat: str) -> re.Pattern:
        buf = []
        for ch in pat:
            if ch == "*": buf.append(".*")
            elif ch == "?": buf.append(".")
            else: buf.append(re.escape(ch))
        return re.compile("^" + "".join(buf) + "$", re.IGNORECASE)

    patterns, terms = [], []
    for t in toks: (patterns if any(ch in t for ch in ("*", "?")) else terms).append(t)

    def ok_hidden(rel: str) -> bool: return not any(p.startswith(".") for p in Path(rel).parts)
    def ok_file(item: Path, rel: str) -> bool: return ok_hidden(rel) and item.suffix.lower() in FILE_LIKE_EXTS
    def ok_dir(item: Path, rel: str) -> bool: return ok_hidden(rel)

    def scan(want_dir: bool, strict_name: bool, pats: Optional[List[Tuple[re.Pattern, bool]]] = None, tset: Optional[List[str]] = None) -> List[str]:
        results: List[str] = []
        with contextlib.suppress(Exception):
            for item in base.rglob("*"):
                if len(results) >= max_results: break
                if want_dir and not item.is_dir(): continue
                if not want_dir and not item.is_file(): continue
                rel = rel_of(item)
                if not rel: continue
                if want_dir and not ok_dir(item, rel): continue
                if not want_dir and not ok_file(item, rel): continue
                name, rell = item.name.lower(), rel.lower()

                if pats:
                    if any(not rx.match(item.name if on_name else rel) for rx, on_name in pats): continue
                    if tset and any(t not in rell for t in tset): continue
                else:
                    if not all(t in rell for t in terms_l): continue
                    if strict_name and not any(t in name for t in terms_l): continue

                results.append(rel + ("/" if want_dir else ""))
        return results

    if patterns:
        pats: List[Tuple[re.Pattern, bool]] = []
        for pat in patterns:
            pat = os.path.expanduser(pat) if pat.startswith("~") else pat
            if pat.startswith("/"):
                try: pat = Path(pat).resolve().relative_to(base).as_posix()
                except Exception: return []
            pats.append((to_regex(pat), "/" not in pat))
        tset = [t.lower() for t in terms]
        files = scan(False, False, pats=pats, tset=tset)
        if files: return sorted(set(files))[:max_results]
        dirs = scan(True, False, pats=pats, tset=tset)
        return sorted(set(dirs))[:max_results]

    terms_l = [t.lower() for t in terms]
    files = scan(False, True) or (scan(False, False) if len(terms_l) > 1 else [])
    if files: return sorted(set(files))[:max_results]
    dirs = scan(True, True) or (scan(True, False) if len(terms_l) > 1 else [])
    return sorted(set(dirs))[:max_results]


def read_files(file_paths: List[str]) -> str:
    if not file_paths: return ""
    out: List[str] = []
    for rel in file_paths:
        r = Path((rel or "").strip().replace("\\", "/"))
        name = r.as_posix()
        if not name or r.is_absolute(): out.append(f"### {name or rel}\nError: invalid relative path\n"); continue
        p = (BASE_DIR / r).resolve()
        if not p.is_relative_to(BASE_DIR): out.append(f"### {name}\nError: path escapes base dir\n"); continue
        try:
            if p.name.endswith(".ipynb"):
                nb = json.loads(p.read_text(encoding="utf-8"))
                cells = []
                for cell in nb.get("cells", []):
                    if cell.get("cell_type") != "code": continue
                    src = cell.get("source", [])
                    if isinstance(src, str): src = [src]
                    if not isinstance(src, list): continue
                    cells.append({"source": [s if isinstance(s, str) else str(s) for s in src]})
                payload = json.dumps({"cells": cells}, indent=2) + "\n"
                out.append(f"### {name}\nExtracted only source from notebook; edit cell-by-cell if needed.\n{payload}\n")
            else:
                out.append(f"### {name}\n{p.read_text(encoding='utf-8')}\n")
        except Exception as e:
            out.append(f"### {name}\nError: {e}\n")
    return "\n".join(out)


@dataclass(slots=True)
class EditEvent:
    kind: str
    filename: str
    details: str = ""
    path: Optional[str] = None


@dataclass(slots=True)
class ReasoningEvent:
    kind: str = "reasoning"
    text: str = ""


@dataclass(slots=True)
class ReplaceBlock:
    x: str
    y: str
    single: bool = False
    occ: Optional[int] = None
    new: str = ""
    lang: str = ""
    op: str = "replace"  # "replace" | "insert_after" | "insert_before"

@dataclass(slots=True)
class EditDirective:
    kind: str  # "EDIT"
    filename: str
    explanation: str = ""
    replaces: List[ReplaceBlock] = field(default_factory=list)


class ChatClient:
    _EDIT_TRIGGER_RE = re.compile(r"\b(?:edit|rewrite)\b", re.IGNORECASE)
    _EDIT_HDR_RE = re.compile(r"(?mi)^\s*###\s*edit\s+(.+?)\s*$")
    _REPLACE_HDR_RE = re.compile(r"(?mi)^\s*####\s*replace\s+`+\s*([^\n`]*?)\s*`+\s*(?:(\d+)\s*)?(?:-\s*`+\s*([^\n`]*?)\s*`+\s*(?:(\d+)\s*)?)?\s*$")
    _INSERT_AFTER_HDR_RE = re.compile(r"(?mi)^\s*####\s*insert\s+after\s+`+\s*([^\n`]*?)\s*`+\s*(?:(\d+)\s*)?(?:-\s*`+\s*([^\n`]*?)\s*`+\s*(?:(\d+)\s*)?)?\s*$")
    _INSERT_BEFORE_HDR_RE = re.compile(r"(?mi)^\s*####\s*insert\s+before\s+`+\s*([^\n`]*?)\s*`+\s*(?:(\d+)\s*)?(?:-\s*`+\s*([^\n`]*?)\s*`+\s*(?:(\d+)\s*)?)?\s*$")
    _FENCE_OPEN_RE = re.compile(r"(?m)^\s*```[ \t]*([^\n`]*)\s*$")
    _FENCE_CLOSE_RE = re.compile(r"(?m)^\s*```\s*$")
    _FENCE_ANY_LINE_RE = re.compile(r"(?m)^\s*```")

    def __init__(self):
        self.messages = [{"role": "system", "content": ""}]
        self._chat_prompt_injected = False
        self._edit_prompt_injected = False
        self.files: List[str] = []
        self.message_files: Dict[int, List[str]] = {}
        self.message_attachments: Dict[int, List[Dict[str, str]]] = {}
        self.edited_files: Dict[str, bool] = {}
        self.edit_transactions: List[Dict] = []
        self._last_assistant_index: Optional[int] = None
        self._display_overrides: Dict[int, str] = {}
        self._file_cache: Dict[str, Tuple[int, int, List[str]]] = {}
        self._user_input_prefill = ""
        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=7200, max_retries=20)

    def get_completion(self, data): return self.client.chat.completions.create(**data)

    @staticmethod
    def _norm_newlines(s: str) -> str: return (s or "").replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _canon_line(s: str) -> str: return (s or "").strip()
    @staticmethod
    def _strip_hidden_attachments(s: str) -> str:
        t = s or ""
        for m in (ATTACHMENTS_MARKER, "\n\nAttached files:", "\n\nAttached files:\n"):
            if m in t: t = t.split(m, 1)[0]
        return t

    def _strip_injected_prompts(self, s: str) -> str:
        s = s or ""
        if s.startswith(CHAT_PROMPT): s = s[len(CHAT_PROMPT):]
        if s.startswith(EDIT_PROMPT): s = s[len(EDIT_PROMPT):]
        return s.lstrip("\n")

    def _recompute_prompt_flags(self) -> None:
        chat, edit = False, False
        for m in self.messages:
            if m.get("role") != "user": continue
            c = m.get("content") or ""
            if c.startswith(CHAT_PROMPT): chat = True
            if c.startswith(EDIT_PROMPT) or c.startswith(CHAT_PROMPT + EDIT_PROMPT): edit = True
            if chat and edit: break
        self._chat_prompt_injected, self._edit_prompt_injected = chat, edit

    async def stream_message(self, user_msg: str, model: str = DEFAULT_MODEL, reasoning: str = DEFAULT_REASONING, force_edit: bool = False, attachments: Optional[List[Dict[str, str]]] = None) -> AsyncGenerator[Union[str, ReasoningEvent], None]:
        msg_index, atts = len(self.messages), [a for a in (attachments or []) if isinstance(a, dict)]
        files = [((a.get("path") or "").strip().replace("\\", "/")) for a in atts if (a.get("kind") or "").lower() == "file" and (a.get("path") or "").strip()]
        files = list(dict.fromkeys(files))
        if files: self.message_files[msg_index] = files.copy()
        if atts: self.message_attachments[msg_index] = [dict(a) for a in atts]

        prefix = ""
        if not self._chat_prompt_injected: prefix, self._chat_prompt_injected = prefix + CHAT_PROMPT, True
        want_edit = force_edit or bool(self._EDIT_TRIGGER_RE.search((user_msg or "").lower()))
        if want_edit and not self._edit_prompt_injected: prefix, self._edit_prompt_injected = prefix + EDIT_PROMPT, True
        if prefix: user_msg = f"{prefix}\n\n{user_msg}"

        blocks = [read_files(files).strip()] if files else []
        for a in atts:
            if (a.get("kind") or "").lower() == "file": continue
            c = (a.get("content") or "").strip()
            if c: blocks.append(c)
        payload = "\n\n".join(x for x in blocks if x)
        content = user_msg + (f"{ATTACHMENTS_MARKER}{payload}\n" if payload else "")

        self.messages.append({"role": "user", "content": content})
        self.files = []

        assistant_index = len(self.messages)
        self._last_assistant_index = assistant_index
        self.messages.append({"role": "assistant", "content": ""})

        data = {"model": model, "messages": self.messages[:-1], "max_tokens": 50000, "temperature": 0.2, "stream": True, "reasoning_effort": reasoning}
        full = ""
        try:
            stream = await self.get_completion(data)
            async for chunk in stream:
                try:
                    choice = (getattr(chunk, "choices", None) or [None])[0]
                    if not choice: continue
                    delta = getattr(choice, "delta", None) or {}
                    text = getattr(delta, "content", None)
                    if text:
                        full += text
                        self.messages[assistant_index]["content"] = full
                        yield text
                    r = getattr(delta, "reasoning", None)
                    reason = r.get("content") if isinstance(r, dict) else (r if isinstance(r, str) else None)
                    if reason: yield ReasoningEvent(text=reason)
                except Exception:
                    continue
            self.messages[assistant_index]["content"] = full
        except (asyncio.CancelledError, GeneratorExit):
            raise

    def _get_file_lines_cached(self, rel: str) -> Optional[List[str]]:
        try:
            rp = Path((rel or "").strip().replace("\\", "/"))
            if not rel or rp.is_absolute(): return None
            p = (BASE_DIR / rp).resolve()
            if not p.is_relative_to(BASE_DIR) or not p.exists(): return None
            st = p.stat()
            mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
            cached = self._file_cache.get(rel)
            if cached and cached[0] == mtime_ns and cached[1] == st.st_size: return cached[2]
            norm = self._norm_newlines(p.read_text(encoding="utf-8"))
            lines = norm.split("\n")
            if norm.endswith("\n"): lines = lines[:-1]
            self._file_cache[rel] = (mtime_ns, st.st_size, lines)
            return lines
        except Exception:
            return None

    @staticmethod
    def _parse_fence_from(text: str, pos: int) -> Optional[Tuple[str, str, int]]:
        m = ChatClient._FENCE_OPEN_RE.search(text, pos)
        if not m: return None
        lang, body_start = (m.group(1) or "").strip(), m.end()
        m2 = ChatClient._FENCE_CLOSE_RE.search(text, body_start)
        if not m2: return None
        body = text[body_start:m2.start()]
        if body.startswith("\n"): body = body[1:]
        return lang, body, m2.end()

    @classmethod
    def _find_unique_anchor_span(cls, lines: List[str], x: str, y: str, hint_lines: Optional[List[str]] = None, single: bool = False, occ: Optional[int] = None) -> Optional[Tuple[int, int]]:
        if not lines: return None
        def ind(s: str) -> int: return len(s) - len(s.lstrip(" \t"))
        def blk(s: str) -> bool: return cls._canon_line(s) == ""

        hx_raw, hy_raw = [i for i, l in enumerate(lines) if l == x], [i for i, l in enumerate(lines) if l == y]
        nx, ny = ((lambda s: s) if hx_raw else cls._canon_line), ((lambda s: s) if hy_raw else cls._canon_line)
        a, b = nx(x), ny(y)
        hx = hx_raw or [i for i, l in enumerate(lines) if cls._canon_line(l) == a]

        if occ is not None: hx = [hx[occ - 1]] if 0 < occ <= len(hx) else []
        if single: return (hx[0] + 1, hx[0] + 1) if len(hx) == 1 else None

        hy = [j for j, l in enumerate(lines) if ny(l) == b]
        if not hx or not hy: return None

        cands = [(i, j) for i in hx for j in hy[bisect_left(hy, i):]]
        def ret(c): return (c[0][0] + 1, c[0][1] + 1) if len(c) == 1 else None
        if not cands: return None
        if r := ret(cands): return r

        cands = [(i, j) for i, j in cands if not (j + 1 < len(lines) and ny(lines[j + 1]) == b and ind(lines[j + 1]) == ind(lines[i]))]
        if r := ret(cands): return r

        nxt_ind, nxt = [None] * len(lines), None
        for k in range(len(lines) - 1, -1, -1):
            nxt_ind[k] = nxt
            if not blk(lines[k]): nxt = ind(lines[k])

        def valid_block(i, j):
            t0, tj, tn, seen = ind(lines[i]), ind(lines[j]), nxt_ind[j], False
            if tj != t0 and tn not in (t0, None): return False
            for k in range(i + 1, j):
                if blk(lines[k]): continue
                tk = ind(lines[k])
                if tk < t0 or (seen and tk <= t0): return False
                if tk > t0: seen = True
            return seen

        if r := ret(b_cands := [c for c in cands if valid_block(*c)]): return r
        cands = b_cands or cands

        if r := ret(f_cands := [(i, j) for i, j in cands if not any(ind(lines[k]) < ind(lines[i]) for k in range(i + 1, j + 1) if not blk(lines[k]))]): return r
        cands = f_cands or cands

        def next_nonblank_ind(j: int) -> Optional[int]:
            for k in range(j + 1, len(lines)):
                if not blk(lines[k]): return ind(lines[k])
            return None

        if len(cands) > 1:
            same = [(i, j) for i, j in cands if ind(lines[j]) == ind(lines[i])]
            if r := ret(same): return r
            if same: cands = same
            nxt = [(i, j) for i, j in cands if next_nonblank_ind(j) in (ind(lines[i]), None)]
            if r := ret(nxt): return r
            if nxt: cands = nxt
            if len(cands) > 1 and len({i for i, _ in cands}) == 1: return (cands[0][0] + 1, min(j for _, j in cands) + 1)

        hint = {cls._canon_line(z) for z in (hint_lines or []) if cls._canon_line(z)}
        if len(hint) < REPLACE_DISAMBIG_MIN_UNIQUE_LINE_HITS: return None

        sets = [{cls._canon_line(z) for z in lines[i:j + 1] if cls._canon_line(z)} & hint for i, j in cands]
        freq: Dict[str, int] = {}
        for s in sets:
            for z in s: freq[z] = freq.get(z, 0) + 1
        scores = [sum(1 for z in s if freq.get(z) == 1) for s in sets]

        best = max(scores, default=0)
        return (cands[scores.index(best)][0] + 1, cands[scores.index(best)][1] + 1) if best >= REPLACE_DISAMBIG_MIN_UNIQUE_LINE_HITS and scores.count(best) == 1 else None

    def render_for_display(self, md: str) -> str:
        md = md or ""
        if "####" not in md: return md
        lines, out = md.split("\n"), []
        cur_file, pending = None, None  # {"op": str, "x": str, "y": str, "hdr": str, "between": List[str]}

        def flush() -> None:
            nonlocal pending
            if not pending: return
            out.append(pending["hdr"]); out += pending["between"]; pending = None

        def parse_hdr(line: str) -> Optional[Tuple[str, str, str, bool, Optional[int]]]:
            if m := self._REPLACE_HDR_RE.match(line): op = "replace"
            elif m := self._INSERT_AFTER_HDR_RE.match(line): op = "insert_after"
            elif m := self._INSERT_BEFORE_HDR_RE.match(line): op = "insert_before"
            else: return None
            x, occ, y_raw = (m.group(1) or ""), (m.group(2) or "").strip(), m.group(3)
            _ = (m.group(4) or "").strip()
            n = int(occ) if occ else None
            single = (y_raw is None)
            y = x if (y_raw is None or y_raw == "") else y_raw
            return op, x, y, single, n

        for idx, line in enumerate(lines):
            if medit := self._EDIT_HDR_RE.match(line):
                flush()
                cur_file = (medit.group(1) or "").strip().replace("`", "")
                out.append(line)
                continue

            if not cur_file: out.append(line); continue

            if pending:
                if mfence := self._FENCE_OPEN_RE.match(line):
                    op, between, x, y, occ = pending["op"], pending["between"], pending["x"], pending["y"], pending.get("occ")
                    lang = (mfence.group(1) or "").strip()
                    path = self._resolve_path(cur_file, create_if_missing=False)
                    file_lines = self._get_file_lines_cached(path) if path else None
                    span = self._find_unique_anchor_span(file_lines or [], x, y, single=pending["single"], occ=occ) if file_lines else None
                    if not span:
                        close = next((k for k in range(idx + 1, len(lines)) if self._FENCE_CLOSE_RE.match(lines[k])), None)
                        hint = (lines[idx + 1:close] if close is not None else None)
                        span = self._find_unique_anchor_span(file_lines or [], x, y, hint_lines=hint, single=pending["single"], occ=occ) if (file_lines and hint is not None) else None
                    a, b = span if span else (None, None)
                    inject_ok = bool(lang and file_lines and a and b and not any(self._FENCE_ANY_LINE_RE.match(z) for z in between))
                    hdr = ("Replace" if op == "replace" else ("Insert After" if op == "insert_after" else "Insert Before"))
                    out.append(f"#### {hdr} {a}-{b}" if a and b else pending["hdr"])
                    out += between
                    if inject_ok: out += [f"```{lang}".rstrip(), "\n".join(file_lines[a - 1:b]), "```", ("#### WITH" if op == "replace" else "#### ADD")]
                    pending = None
                    out.append(line)
                    continue

                if h2 := parse_hdr(line):
                    flush()
                    pending = {"op": h2[0], "x": h2[1], "y": h2[2], "single": h2[3], "occ": h2[4], "hdr": line, "between": []}
                    continue

                pending["between"].append(line)
                continue

            if h := parse_hdr(line):
                pending = {"op": h[0], "x": h[1], "y": h[2], "single": h[3], "occ": h[4], "hdr": line, "between": []}
                continue

            out.append(line)

        flush()
        return "\n".join(out)


    def set_last_assistant_display(self, display_md: str) -> None:
        if self._last_assistant_index is not None: self._display_overrides[self._last_assistant_index] = display_md or ""

    def consume_user_input_prefill(self) -> str:
        s, self._user_input_prefill = self._user_input_prefill, ""
        return s

    def parse_edit_markdown(self, md: str) -> List[EditDirective]:
        if not md: return []
        text = self._norm_newlines(md)
        edits = list(self._EDIT_HDR_RE.finditer(text))
        out: List[EditDirective] = []

        for i, m in enumerate(edits):
            filename = (m.group(1) or "").strip().replace("`", "")
            start, end = m.end(), (edits[i + 1].start() if i + 1 < len(edits) else len(text))
            section = text[start:end].strip()

            replaces: List[ReplaceBlock] = []
            pos = 0

            def next_hdr(at: int) -> Tuple[Optional[str], Optional[re.Match]]:
                r = [( "replace", self._REPLACE_HDR_RE.search(section, at) ), ("insert_after", self._INSERT_AFTER_HDR_RE.search(section, at)), ("insert_before", self._INSERT_BEFORE_HDR_RE.search(section, at))]
                r = [(op, m) for op, m in r if m]
                if not r: return None, None
                op, m = min(r, key=lambda t: t[1].start())
                return op, m

            while True:
                op, r = next_hdr(pos)
                if not r: break
                f_new = self._parse_fence_from(section, r.end())
                if not f_new: break
                x, occ, y_raw = (r.group(1) or ""), (r.group(2) or "").strip(), r.group(3)
                _ = (r.group(4) or "").strip()
                n = int(occ) if occ else None
                single = (y_raw is None)
                y = x if (y_raw is None or y_raw == "") else y_raw
                replaces.append(ReplaceBlock(x=x, y=y, single=single, occ=n, new=f_new[1], lang=(f_new[0] or "").strip(), op=op or "replace"))
                pos = f_new[2]

            expl_cut = min([x.start() for x in (self._REPLACE_HDR_RE.search(section), self._INSERT_AFTER_HDR_RE.search(section), self._INSERT_BEFORE_HDR_RE.search(section), self._FENCE_OPEN_RE.search(section)) if x], default=None)
            expl = section[:expl_cut].strip() if expl_cut is not None else section.strip()
            if replaces: out.append(EditDirective(kind="EDIT", filename=filename, explanation=expl, replaces=replaces))

        return out

    def _resolve_path(self, filename: str, create_if_missing: bool = False) -> Optional[str]:
        raw = (filename or "").strip().replace("`", "").replace("\\", "/")
        if not raw: return None
        cand = Path(raw)
        if cand.is_absolute(): return None

        def safe_rel(p: Path) -> Optional[Path]:
            try: return Path(p.relative_to(BASE_DIR).as_posix())
            except Exception: return None

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
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
            tmp.write(content); tmp.flush()
            with contextlib.suppress(Exception): os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, str(path))

    @staticmethod
    def _count_lines_norm(s: str) -> int:
        t = (s or "").replace("\r\n", "\n").replace("\r", "\n")
        if t.endswith("\n"): t = t[:-1]
        return 0 if t == "" else t.count("\n") + 1

    def apply_markdown_edits(self, md: str) -> List[EditEvent]:
        directives = self.parse_edit_markdown(md)
        if not directives: return []
        results: List[EditEvent] = []
        ai = (len(self.messages) - 1) if (self.messages and self.messages[-1]["role"] == "assistant") else None
        tx = {"assistant_index": ai, "files": {}, "changed": set()}
        failed_cmds: List[str] = []

        def _abs(rel: str) -> Path: return (BASE_DIR / Path(rel)).resolve()

        def _remember_prev(rel: str):
            if rel in tx["files"]: return
            p = _abs(rel)
            tx["files"][rel] = p.read_text(encoding="utf-8") if p.exists() else None

        def _fmt_cmd(rel: str, blk: ReplaceBlock) -> str:
            op = {"replace": "Replace", "insert_after": "Insert After", "insert_before": "Insert Before"}.get(blk.op, blk.op)
            core = f"{op} `{blk.x}`" if blk.single else f"{op} `{blk.x}`-`{blk.y}`"
            if blk.occ: core += f" {blk.occ}"
            return f"{rel}: {core}"

        for d in directives:
            try:
                target = self._resolve_path(d.filename, create_if_missing=False)
                if not target: results.append(EditEvent("error", Path(d.filename).name, "Invalid path (must exist, relative to base dir)", d.filename)); continue
                rel, p = target, _abs(target)
                if not p.is_relative_to(BASE_DIR): results.append(EditEvent("error", Path(rel).name, "Path escapes base dir", rel)); continue
                if not p.exists(): results.append(EditEvent("error", Path(rel).name, "File does not exist (no rewrite mode)", rel)); continue
                if not d.replaces: results.append(EditEvent("error", Path(rel).name, "No replace blocks found", rel)); continue

                original = p.read_text(encoding="utf-8")
                eol = "\r\n" if "\r\n" in original else "\n"
                norm = self._norm_newlines(original)
                had_final_nl = norm.endswith("\n")
                lines = norm.split("\n")
                if had_final_nl: lines = lines[:-1]

                spans: List[Tuple[int, int, List[str], str, ReplaceBlock]] = []
                failed_here: List[ReplaceBlock] = []
                for blk in d.replaces:
                    new_norm = self._norm_newlines(blk.new).rstrip("\n")
                    new_lines = ([] if new_norm == "" else new_norm.split("\n"))
                    span = self._find_unique_anchor_span(lines, blk.x, blk.y, hint_lines=new_lines, single=blk.single, occ=blk.occ)
                    if not span: failed_here.append(blk); continue
                    a, b = span
                    i0, i1 = ((a - 1, b) if blk.op == "replace" else ((b, b) if blk.op == "insert_after" else (a - 1, a - 1)))
                    spans.append((i0, i1, new_lines, blk.op, blk))

                if not spans:
                    for blk in failed_here: failed_cmds.append(_fmt_cmd(rel, blk))
                    results.append(EditEvent("error", Path(rel).name, "No edit blocks uniquely matched anchors (X/Y) in file", rel))
                    continue

                spans.sort(key=lambda t: (t[0], t[1]))
                for (a1, b1, _, _, _), (a2, b2, _, _, _) in zip(spans, spans[1:]):
                    if a2 < b1: raise RuntimeError(f"Overlapping edit ranges: {a1 + 1}-{b1} and {a2 + 1}-{b2}")

                updated_lines = lines[:]
                for i0, i1, new_lines, _, _ in sorted(spans, key=lambda t: (t[0], t[1]), reverse=True): updated_lines[i0:i1] = new_lines

                updated_norm = ("\n".join(updated_lines) + ("\n" if had_final_nl else ""))
                updated = updated_norm if eol == "\n" else updated_norm.replace("\n", "\r\n")
                if updated == original:
                    for blk in failed_here: failed_cmds.append(_fmt_cmd(rel, blk))
                    results.append(EditEvent("error", Path(rel).name, "No changes applied", rel))
                    continue

                _remember_prev(rel)
                self._atomic_write(p, updated)
                tx["changed"].add(rel)
                for blk in failed_here: failed_cmds.append(_fmt_cmd(rel, blk))
                extra = f", {len(failed_here)} failed" if failed_here else ""
                results.append(EditEvent("complete", Path(rel).name, f"applied {len(spans)} edit(s){extra}: {self._count_lines_norm(original)} → {self._count_lines_norm(updated)} lines", rel))
            except Exception as e:
                results.append(EditEvent("error", Path(d.filename).name, f"Error: {e}", d.filename))

        if tx["changed"]:
            tx["files"] = {p: tx["files"][p] for p in tx["changed"]}
            self.edit_transactions.append(tx)
            for p in tx["changed"]: self.edited_files[p] = True

        if failed_cmds:
            uniq = []
            for c in failed_cmds:
                if c not in uniq: uniq.append(c)
            lead = "Some edits were applied, but the following commands failed:" if tx["changed"] else "No edits were applied; the following commands failed:"
            self._user_input_prefill = lead + "\n" + "\n".join(f"- {c}" for c in uniq) + "\n\nPlease generate corrected versions."

        return results

    def rollback_file(self, file_path: str) -> bool:
        rel = (file_path or "").strip().replace("\\", "/")
        if not rel or Path(rel).is_absolute(): return False
        p = (BASE_DIR / Path(rel)).resolve()
        if not p.is_relative_to(BASE_DIR): return False

        for i in range(len(self.edit_transactions) - 1, -1, -1):
            tx = self.edit_transactions[i]
            changed: Set[str] = tx.get("changed", set())
            if rel not in changed: continue
            prev = tx["files"].get(rel, None)
            try:
                if prev is None:
                    with contextlib.suppress(FileNotFoundError): p.unlink()
                else:
                    self._atomic_write(p, prev)
            except Exception:
                return False

            changed.remove(rel)
            tx["files"].pop(rel, None)
            if not changed: self.edit_transactions.pop(i)

            current = {}
            for t in self.edit_transactions:
                for path in t.get("changed", set()): current[path] = True
            self.edited_files = current
            return True

        return False

    def rollback_edits_for_assistant(self, assistant_index: int):
        while self.edit_transactions and self.edit_transactions[-1].get("assistant_index") == assistant_index:
            tx = self.edit_transactions.pop()
            for rel in list(tx.get("changed", set())):
                prev = tx["files"].get(rel, None)
                p = (BASE_DIR / Path(rel)).resolve()
                if not p.is_relative_to(BASE_DIR): continue
                with contextlib.suppress(Exception):
                    if prev is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else:
                        self._atomic_write(p, prev)

        current = {}
        for t in self.edit_transactions:
            for path in t.get("changed", set()): current[path] = True
        self.edited_files = current

    def undo_last(self) -> Tuple[Optional[str], List[str], List[Dict[str, str]]]:
        if len(self.messages) < 3 or self.messages[-1]["role"] != "assistant": return None, [], []
        ai = len(self.messages) - 1
        self.rollback_edits_for_assistant(ai)
        self._display_overrides.pop(ai, None)
        self.messages.pop()

        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                content = self.messages.pop(i)["content"]
                atts = self.message_attachments.pop(i, [])
                files = self.message_files.pop(i, [((a.get("path") or "").strip().replace("\\", "/")) for a in atts if (a.get("kind") or "").lower() == "file" and (a.get("path") or "").strip()])
                self.files = files.copy()
                user_msg = self._strip_injected_prompts(self._strip_hidden_attachments(content))
                self._recompute_prompt_flags()
                return user_msg, files, atts

        self._recompute_prompt_flags()
        return None, [], []

    def get_display_messages(self) -> List[Tuple[str, str]]:
        out = []
        for idx, m in enumerate(self.messages[1:], start=1):
            role, content = m.get("role"), m.get("content") or ""
            if role == "user":
                content = self._strip_injected_prompts(self._strip_hidden_attachments(content))
            elif role == "assistant":
                content = self._display_overrides.get(idx, content)
            out.append((role, content))
        return out

    def ensure_last_assistant_nonempty(self, fallback: str = "Response stopped."):
        with contextlib.suppress(Exception):
            if self.messages and self.messages[-1]["role"] == "assistant" and not (self.messages[-1].get("content") or "").strip(): self.messages[-1]["content"] = fallback