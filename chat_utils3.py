import asyncio, contextlib, json, os, re, tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

from openai import AsyncOpenAI
from stuff import CHAT_PROMPT, EDIT_PROMPT, EXTRACT_ADD_ON
from url_utils import fetch_url_content as _fetch_url_content, looks_like_url as _looks_like_url, normalize_url as _normalize_url

API_KEY = os.getenv('OPENROUTER_API_KEY')
BASE_URL = 'https://openrouter.ai/api/v1'
BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = 'openai/gpt-5.4'
DEFAULT_REASONING = 'medium'
MODELS = ['google/gemini-3.1-pro-preview', 'openai/gpt-5.4', 'openai/gpt-5.4-pro', 'openai/gpt-5.4-mini', 'anthropic/claude-4.6-opus', 'x-ai/grok-4.20-multi-agent-beta']
REASONING_LEVELS = ['none', 'minimal', 'low', 'medium', 'high']
MAX_ATTACHMENT_BYTES = 500 * 1024

FILE_LIKE_EXTS = {'.py', '.pyw', '.ipynb', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.hh', '.hxx', '.go', '.rs', '.cs', '.java', '.html', '.htm', '.css', '.md', '.markdown', '.txt', '.rst', '.json', '.yaml', '.yml', '.toml', '.sql', '.sh', '.bash', '.zsh', '.bat', '.ps1'}
ATTACHMENTS_MARKER = '\n\nAttached attachments:\n'
LANG_BY_EXT = {
    '.py': 'python', '.pyw': 'python', '.ipynb': 'json', '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'tsx', '.c': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.h': 'c', '.hpp': 'cpp',
    '.hh': 'cpp', '.hxx': 'cpp', '.go': 'go', '.rs': 'rust', '.cs': 'csharp', '.java': 'java', '.html': 'html', '.htm': 'html',
    '.css': 'css', '.md': 'markdown', '.markdown': 'markdown', '.txt': 'text', '.rst': 'text', '.json': 'json', '.yaml': 'yaml',
    '.yml': 'yaml', '.toml': 'toml', '.sql': 'sql', '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash', '.bat': 'bat', '.ps1': 'powershell',
}


@dataclass(slots=True)
class Attachment:
    kind: Literal['file', 'url']
    path: str = ''
    url: str = ''
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
class PendingEdit:
    assistant_id: str
    text: str
    targets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UserTurn:
    id: str
    display_text: str
    restore_text: str
    history_text: str
    attachments: list[Attachment] = field(default_factory=list)
    force_edit: bool = False


@dataclass(slots=True)
class AssistantTurn:
    id: str
    model: str
    label: str
    raw_text: str = ''
    display_text: str = ''
    ctx_files: list[str] = field(default_factory=list)
    elapsed: int = 0
    finalized: bool = False
    interrupted: bool = False
    error: str | None = None
    has_answer: bool = False


@dataclass(slots=True)
class ExchangeEntry:
    id: str
    user: UserTurn
    assistant: AssistantTurn
    kind: Literal['exchange'] = 'exchange'


@dataclass(slots=True)
class CouncilEntry:
    id: str
    query: UserTurn
    member_prompt_text: str
    members: list[AssistantTurn] = field(default_factory=list)
    synthesis: AssistantTurn | None = None
    status: Literal['streaming_members', 'streaming_synthesis', 'completed', 'interrupted'] = 'streaming_members'
    kind: Literal['council'] = 'council'


Entry = ExchangeEntry | CouncilEntry


@dataclass(slots=True)
class ConversationState:
    entries: list[Entry] = field(default_factory=list)
    pending_edit: PendingEdit | None = None
    edit_rounds: dict[str, EditRound] = field(default_factory=dict)


@dataclass(slots=True)
class EditEvent:
    kind: str
    filename: str
    details: str = ''
    path: str | None = None


@dataclass(slots=True)
class ReasoningEvent:
    kind: str = 'reasoning'
    text: str = ''


@dataclass(slots=True)
class ReplaceBlock:
    x: str
    y: str
    single: bool = False
    occ: int | None = None
    new: str = ''
    lang: str = ''
    op: str = 'replace'


@dataclass(slots=True)
class EditDirective:
    kind: str
    filename: str
    explanation: str = ''
    replaces: list[ReplaceBlock] = field(default_factory=list)
    full_new: str | None = None


class AttachmentService:
    @staticmethod
    def normalize_url(u: str) -> str: return _normalize_url(u)

    @staticmethod
    def looks_like_url(v: str) -> bool: return _looks_like_url(v)

    @staticmethod
    def validate_file_attachment(path: str, base_path: str | None = None) -> str | None:
        base, rel = Path(base_path).resolve() if base_path else BASE_DIR, (path or '').strip().replace('\\', '/')
        if not rel: return 'Attachment path is empty'
        p = Path(rel)
        if p.is_absolute(): return f'Attachment must be relative: {rel}'
        q = (base / p).resolve()
        if not q.is_relative_to(base) or not q.is_file(): return f'Not a file: {rel}'
        return f'Attachment exceeds 500KB: {rel}' if q.stat().st_size > MAX_ATTACHMENT_BYTES else None

    @staticmethod
    def search_files(query: str, base_path: str | None = None, max_results: int = 20) -> list[str]:
        if not query or len(query) < 2: return []
        base = Path(base_path).resolve() if base_path else BASE_DIR

        def rel_of(p: Path) -> str | None:
            try: return p.relative_to(base).as_posix()
            except Exception: return None

        q = (query or '').strip().replace('\\', '/')
        if not q: return []
        q0 = os.path.expanduser(q) if q.startswith('~') else q

        with contextlib.suppress(Exception):
            cand = Path(q0).resolve() if q0.startswith('/') else (base / Path(q0)).resolve()
            if cand.is_file() and cand.is_relative_to(base): return [cand.relative_to(base).as_posix()]

        toks = [t for t in re.split(r'\s+', q0) if t]
        if not toks: return []

        def to_regex(pat: str) -> re.Pattern:
            return re.compile('^' + ''.join('.*' if c == '*' else '.' if c == '?' else re.escape(c) for c in pat) + '$', re.IGNORECASE)

        patterns, terms = [], []
        for t in toks: (patterns if any(c in t for c in '*?') else terms).append(t)
        terms_l = [t.lower() for t in terms]

        def visible(rel: str) -> bool: return not any(part.startswith('.') for part in Path(rel).parts)

        def scan(want_dir: bool, strict_name: bool, pats: list[tuple[re.Pattern, bool]] | None = None, term_set: list[str] | None = None) -> list[str]:
            out = []
            with contextlib.suppress(Exception):
                for item in base.rglob('*'):
                    if len(out) >= max_results: break
                    if want_dir and not item.is_dir() or not want_dir and not item.is_file(): continue
                    rel = rel_of(item)
                    if not rel or not visible(rel): continue
                    if not want_dir and item.suffix.lower() not in FILE_LIKE_EXTS: continue
                    name_l, rel_l = item.name.lower(), rel.lower()
                    if pats:
                        if any(not rx.match(item.name if on_name else rel) for rx, on_name in pats): continue
                        if term_set and any(t not in rel_l for t in term_set): continue
                    else:
                        if not all(t in rel_l for t in terms_l): continue
                        if strict_name and not any(t in name_l for t in terms_l): continue
                    out.append(rel + ('/' if want_dir else ''))
            return out

        if patterns:
            pats = []
            for pat in patterns:
                pat = os.path.expanduser(pat) if pat.startswith('~') else pat
                if pat.startswith('/'):
                    try: pat = Path(pat).resolve().relative_to(base).as_posix()
                    except Exception: return []
                pats.append((to_regex(pat), '/' not in pat))
            return sorted(set(scan(False, False, pats=pats, term_set=terms_l)))[:max_results]

        files = scan(False, True) or (scan(False, False) if len(terms_l) > 1 else [])
        return sorted(set(files))[:max_results]

    @staticmethod
    def read_files(file_paths: list[str]) -> str:
        if not file_paths: return ''
        out = []
        for rel in file_paths:
            r, name = Path((rel or '').strip().replace('\\', '/')), Path((rel or '').strip().replace('\\', '/')).as_posix()
            if not name or r.is_absolute():
                out.append(f'### {name or rel}\nError: invalid relative path\n')
                continue
            p = (BASE_DIR / r).resolve()
            if not p.is_relative_to(BASE_DIR):
                out.append(f'### {name}\nError: path escapes base dir\n')
                continue
            try:
                if not p.exists():
                    out.append(f'### {name}\nError: file does not exist\n')
                    continue
                if not p.is_file():
                    out.append(f'### {name}\nError: not a file\n')
                    continue
                if p.stat().st_size > MAX_ATTACHMENT_BYTES:
                    out.append(f'### {name}\nError: attachment exceeds 500KB\n')
                    continue
                if p.name.endswith('.ipynb'):
                    nb = json.loads(p.read_text(encoding='utf-8'))
                    cells = []
                    for cell in nb.get('cells', []):
                        if cell.get('cell_type') != 'code': continue
                        src = cell.get('source', [])
                        if isinstance(src, str): src = [src]
                        if not isinstance(src, list): continue
                        cells.append({'source': [s if isinstance(s, str) else str(s) for s in src]})
                    out.append(f'### {name}\nExtracted only source from notebook; edit cell-by-cell if needed.\n{json.dumps({"cells": cells}, indent=2)}\n')
                else:
                    out.append(f'### {name}\n{p.read_text(encoding="utf-8")}\n')
            except Exception as e:
                out.append(f'### {name}\nError: {e}\n')
        return '\n'.join(out)

    @staticmethod
    async def fetch_url_content(url: str) -> str:
        content = await _fetch_url_content(url)
        if len(content.encode('utf-8')) > MAX_ATTACHMENT_BYTES: raise ValueError('Attachment exceeds 500KB')
        return content


def search_files(query: str, base_path: str | None = None, max_results: int = 20) -> list[str]:
    return AttachmentService.search_files(query, base_path=base_path, max_results=max_results)


def read_files(file_paths: list[str]) -> str:
    return AttachmentService.read_files(file_paths)


class EditService:
    _EDIT_HDR_RE = re.compile(r'(?mi)^\s*#+\s*edit\s+(.+?)\s*$')
    _REPLACE_HDR_RE = re.compile(r'(?mi)^\s*#+\s*replace\s+`+([^\n`]*)`+\s*(?:(\d+)\s*)?(?:-\s*`+([^\n`]*)`+\s*(?:(\d+)\s*)?)?\s*$')
    _INSERT_AFTER_HDR_RE = re.compile(r'(?mi)^\s*#+\s*insert\s+after\s+`+([^\n`]*)`+\s*(?:(\d+)\s*)?(?:-\s*`+([^\n`]*)`+\s*(?:(\d+)\s*)?)?\s*$')
    _INSERT_BEFORE_HDR_RE = re.compile(r'(?mi)^\s*#+\s*insert\s+before\s+`+([^\n`]*)`+\s*(?:(\d+)\s*)?(?:-\s*`+([^\n`]*)`+\s*(?:(\d+)\s*)?)?\s*$')
    _FENCE_OPEN_RE = re.compile(r'(?m)^\s*```[ \t]*([^\n`]*)\s*$')
    _FENCE_CLOSE_RE = re.compile(r'(?m)^\s*```\s*$')
    _HEADER_SPECS = (('replace', _REPLACE_HDR_RE, 'Replace'), ('insert_after', _INSERT_AFTER_HDR_RE, 'Insert After'), ('insert_before', _INSERT_BEFORE_HDR_RE, 'Insert Before'))
    _HEADER_LABELS = {op: label for op, _, label in _HEADER_SPECS}

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.edited_files: dict[str, bool] = {}
        self.transactions: list[dict[str, Any]] = []

    @staticmethod
    def _norm_newlines(s: str) -> str: return (s or '').replace('\r\n', '\n').replace('\r', '\n')

    @staticmethod
    def _atomic_write(path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent), delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            with contextlib.suppress(Exception): os.fsync(tmp.fileno())
            name = tmp.name
        os.replace(name, str(path))

    @staticmethod
    def _count_lines_norm(s: str) -> int:
        t = (s or '').replace('\r\n', '\n').replace('\r', '\n')
        if t.endswith('\n'): t = t[:-1]
        return 0 if t == '' else t.count('\n') + 1

    @classmethod
    def _parse_fence_from(cls, text: str, pos: int) -> tuple[str, str, int] | None:
        if not (m := cls._FENCE_OPEN_RE.search(text, pos)): return None
        if not (m2 := cls._FENCE_CLOSE_RE.search(text, m.end())): return None
        body = text[m.end():m2.start()]
        return (m.group(1) or '').strip(), body[1:] if body.startswith('\n') else body, m2.end()
    @classmethod
    def _parse_full_edit_fence(cls, text: str) -> tuple[str, str] | None:
        text = cls._norm_newlines(text).strip()
        if not text or any(rx.search(text) for _, rx, _ in cls._HEADER_SPECS): return None
        if not (m := cls._FENCE_OPEN_RE.search(text)) or not (f := cls._parse_fence_from(text, m.start())) or text[f[2]:].strip(): return None
        return text[:m.start()].strip(), f[1]

    @classmethod
    def _parse_header_match(cls, op: str, m: re.Match) -> tuple[str, str, bool, int | None, str]:
        x, occ, y_raw = (m.group(1) or ''), (m.group(2) or '').strip(), m.group(3)
        return x, (x if y_raw in {None, ''} else y_raw), (y_raw is None), (int(occ) if occ else None), cls._HEADER_LABELS[op]

    @classmethod
    def _parse_header_line(cls, line: str) -> tuple[str, str, str, bool, int | None, str] | None:
        for op, rx, _ in cls._HEADER_SPECS:
            if m := rx.match(line): return (op, *cls._parse_header_match(op, m))
        return None

    @classmethod
    def _next_hdr(cls, section: str, at: int) -> tuple[str | None, re.Match | None]:
        xs = [(op, rx.search(section, at)) for op, rx, _ in cls._HEADER_SPECS]
        xs = [(op, m) for op, m in xs if m]
        return min(xs, key=lambda t: t[1].start()) if xs else (None, None)

    @classmethod
    def _find_anchor_span(cls, lines: list[str], x: str, y: str, single: bool = False, occ: int | None = None) -> tuple[int, int] | None:
        if not lines: return None

        def run(norm) -> tuple[int, int] | None:
            vals, xv, yv = [norm(v) for v in lines], norm(x), norm(y)
            xs = [i for i, v in enumerate(vals) if v == xv]
            if occ is not None: xs = [xs[occ - 1]] if 0 < occ <= len(xs) else []
            if single: return (xs[0] + 1, xs[0] + 1) if len(xs) == 1 else None
            ys = [i for i, v in enumerate(vals) if v == yv]
            cands = [(i, next((j for j in ys if j >= i), -1)) for i in xs]
            cands = [(i, j) for i, j in cands if j >= 0]
            return (cands[0][0] + 1, cands[0][1] + 1) if len(cands) == 1 else None

        return run(lambda s: (s or '').rstrip()) or run(lambda s: (s or '').strip())

    def _resolve_path(self, filename: str, ctx_files: list[str], create_if_missing: bool = False) -> str | None:
        raw = (filename or '').strip().replace('`', '').replace('\\', '/')
        if not raw or Path(raw).is_absolute(): return None
        cand, abs0 = Path(raw), (self.base_dir / Path(raw)).resolve()

        def safe_rel(p: Path) -> Path | None:
            try: return Path(p.relative_to(self.base_dir).as_posix())
            except Exception: return None

        if not (rel0 := safe_rel(abs0)): return None
        if abs0.exists() or create_if_missing: return rel0.as_posix()

        ctx_paths = [Path(p) for p in (ctx_files or []) if p]

        def suffix_matches(p: Path, suffix: Path) -> bool:
            return len(p.parts) >= len(suffix.parts) and tuple(p.parts[-len(suffix.parts):]) == suffix.parts

        if ctx_paths:
            if len(cand.parts) > 1:
                hits = [p for p in ctx_paths if suffix_matches(p, cand)]
                if len(hits) == 1: return hits[0].as_posix()
                if len(hits) > 1: return None
            hits = [p for p in ctx_paths if p.name == cand.name]
            if len(hits) == 1: return hits[0].as_posix()
            if len(hits) > 1: return None

        hits = []
        with contextlib.suppress(Exception):
            for q in self.base_dir.rglob(cand.name):
                if not q.is_file(): continue
                rel = q.relative_to(self.base_dir)
                if len(cand.parts) > 1 and not suffix_matches(rel, cand): continue
                hits.append(Path(rel.as_posix()))
                if len(hits) > 1: break
        if len(hits) == 1: return hits[0].as_posix()
        return rel0.as_posix() if not hits and create_if_missing else None

    def _read_file_lines(self, rel: str) -> list[str] | None:
        p = (self.base_dir / Path(rel)).resolve()
        if not p.is_relative_to(self.base_dir) or not p.exists(): return None
        norm, xs = self._norm_newlines(p.read_text(encoding='utf-8')), self._norm_newlines(p.read_text(encoding='utf-8')).split('\n')
        return xs[:-1] if norm.endswith('\n') else xs

    def _code_lang(self, rel: str) -> str: return LANG_BY_EXT.get(Path(rel).suffix.lower(), Path(rel).suffix.lower().lstrip('.'))

    def render_edit_header(self, filename: str, op: str, x: str, y: str, single: bool, occ: int | None, label: str, ctx_files: list[str]) -> str | None:
        rel = self._resolve_path(filename, ctx_files, create_if_missing=False)
        if not rel or not (lines := self._read_file_lines(rel)) or not (span := self._find_anchor_span(lines, x, y, single=single, occ=occ)): return None
        a, b = span
        body, lang, tail = '\n'.join(lines[a - 1:b]), self._code_lang(rel), '#### WITH' if op == 'replace' else '#### ADD'
        fence = f'```{lang}\n{body}\n```' if body else f'```{lang}\n```'
        return f'#### {label} {a}-{b}\n{fence}\n{tail}'

    def new_display_renderer(self, ctx_files: list[str] | None = None) -> 'DisplayRenderer':
        return DisplayRenderer(self, ctx_files or [])

    def parse_edit_markdown(self, md: str) -> list[EditDirective]:
        if not md: return []
        text, edits, out = self._norm_newlines(md), list(self._EDIT_HDR_RE.finditer(self._norm_newlines(md))), []
        for i, m in enumerate(edits):
            filename, section = (m.group(1) or '').strip().replace('`', ''), text[m.end():(edits[i + 1].start() if i + 1 < len(edits) else len(text))].strip()
            replaces, pos = [], 0
            while True:
                op, hdr = self._next_hdr(section, pos)
                if not hdr: break
                if not (f := self._parse_fence_from(section, hdr.end())):
                    pos = hdr.end()
                    continue
                x, y, single, n, _ = self._parse_header_match(op or 'replace', hdr)
                replaces.append(ReplaceBlock(x=x, y=y, single=single, occ=n, new=f[1], lang=(f[0] or '').strip(), op=op or 'replace'))
                pos = f[2]
            full = None if replaces else self._parse_full_edit_fence(section)
            cuts = [x.start() for x in [self._REPLACE_HDR_RE.search(section), self._INSERT_AFTER_HDR_RE.search(section), self._INSERT_BEFORE_HDR_RE.search(section), self._FENCE_OPEN_RE.search(section)] if x]
            expl = full[0] if full else section[:min(cuts)].strip() if cuts else section.strip()
            if replaces or full: out.append(EditDirective(kind='EDIT', filename=filename, explanation=expl, replaces=replaces, full_new=(full[1] if full else None)))
        return out

    def render_for_display(self, md: str, ctx_files: list[str] | None = None) -> str:
        r = self.new_display_renderer(ctx_files or [])
        return r.feed(md or '') + r.finish()

    def apply_markdown_edits(self, md: str, assistant_id: str | None, ctx_files: list[str]) -> tuple[list[EditEvent], str]:
        directives = self.parse_edit_markdown(md)
        if not directives: return [], ''
        results, failed_cmds, tx = [], [], {'assistant_id': assistant_id, 'files': {}, 'changed': set()}

        def abs_of(rel: str) -> Path: return (self.base_dir / Path(rel)).resolve()

        def remember_prev(rel: str):
            if rel in tx['files']: return
            p = abs_of(rel)
            tx['files'][rel] = p.read_text(encoding='utf-8') if p.exists() else None

        def fmt_cmd(rel: str, blk: ReplaceBlock) -> str:
            op, core = {'replace': 'Replace', 'insert_after': 'Insert After', 'insert_before': 'Insert Before'}.get(blk.op, blk.op), f'`{blk.x}`' if blk.single else f'`{blk.x}`-`{blk.y}`'
            return f'{rel}: {op} {core}' + (f' {blk.occ}' if blk.occ else '')

        def cur_loc(span: tuple[int, int], op: str) -> tuple[int, int]:
            a, b = span
            return (a - 1, b) if op == 'replace' else (b, b) if op == 'insert_after' else (a - 1, a - 1)

        def orig_loc(m: list[int | None], span: tuple[int, int], op: str) -> tuple[int, int, int, int] | None:
            a, b = span
            s, t = (a - 1, b) if op == 'replace' else (b, b) if op == 'insert_after' else (a - 1, a - 1)
            i0, i1 = m[s], m[t]
            return None if i0 is None or i1 is None else (i0, i1, s, t)

        def rebase(m: list[int | None], i0: int, i1: int, k: int, op: str, s: int | None = None, t: int | None = None):
            d = k - (i1 - i0)
            for j, v in enumerate(m):
                if v is None: continue
                if s is not None:
                    if op == 'replace':
                        if j < s: continue
                        if j == s: m[j] = i0
                        elif j == t: m[j] = i0 + k
                        elif s < j < t: m[j] = None
                        elif j > t and v >= i1: m[j] = v + d
                    elif op == 'insert_before':
                        if j >= s: m[j] = v + k
                    elif j > t:
                        m[j] = v + k
                    continue
                if op == 'replace':
                    if i0 < v < i1: m[j] = None
                    elif v == i1: m[j] = i0 + k
                    elif v > i1: m[j] = v + d
                elif op == 'insert_before':
                    if v >= i0: m[j] = v + k
                elif v > i0:
                    m[j] = v + k
            op, core = {'replace': 'Replace', 'insert_after': 'Insert After', 'insert_before': 'Insert Before'}.get(blk.op, blk.op), f'`{blk.x}`' if blk.single else f'`{blk.x}`-`{blk.y}`'
            return f'{rel}: {op} {core}' + (f' {blk.occ}' if blk.occ else '')

        for d in directives:
            try:
                full_edit = d.full_new is not None and not d.replaces
                rel = self._resolve_path(d.filename, ctx_files=ctx_files, create_if_missing=full_edit)
                if not rel:
                    results.append(EditEvent('error', Path(d.filename).name, 'Invalid path (must be relative to base dir)', d.filename))
                    continue
                p = abs_of(rel)
                if not p.is_relative_to(self.base_dir):
                    results.append(EditEvent('error', Path(rel).name, 'Path escapes base dir', rel))
                    continue
                if full_edit:
                    original, updated_norm = p.read_text(encoding='utf-8') if p.exists() else None, self._norm_newlines(d.full_new or '')
                    updated = updated_norm if original is None or '\r\n' not in original else updated_norm.replace('\n', '\r\n')
                    if original is not None and updated == original:
                        results.append(EditEvent('error', Path(rel).name, 'No changes applied', rel))
                        continue
                    remember_prev(rel)
                    self._atomic_write(p, updated)
                    tx['changed'].add(rel)
                    results.append(EditEvent('complete', Path(rel).name, f'full rewrite: {self._count_lines_norm(original or "")} → {self._count_lines_norm(updated)} lines', rel))
                    continue
                if not p.exists():
                    results.append(EditEvent('error', Path(rel).name, 'File does not exist', rel))
                    continue
                if not d.replaces:
                    results.append(EditEvent('error', Path(rel).name, 'No replace blocks found', rel))
                    continue

                original, eol = p.read_text(encoding='utf-8'), '\r\n' if '\r\n' in p.read_text(encoding='utf-8') else '\n'
                norm, had_final_nl = self._norm_newlines(original), self._norm_newlines(original).endswith('\n')
                lines = norm.split('\n')
                if had_final_nl: lines = lines[:-1]

                updated_lines, applied, failed_here, orig_pos = lines[:], 0, [], list(range(len(lines) + 1))
                for blk in d.replaces:
                    new_norm = self._norm_newlines(blk.new).rstrip('\n')
                    new_lines = [] if new_norm == '' else new_norm.split('\n')
                    if (span := self._find_anchor_span(lines, blk.x, blk.y, single=blk.single, occ=blk.occ)) and (loc := orig_loc(orig_pos, span, blk.op)):
                        i0, i1, s, t = loc
                        from_orig = True
                    elif span := self._find_anchor_span(updated_lines, blk.x, blk.y, single=blk.single, occ=blk.occ):
                        i0, i1 = cur_loc(span, blk.op)
                        from_orig = False
                    else:
                        failed_here.append(blk)
                        continue
                    next_lines = updated_lines[:]
                    next_lines[i0:i1] = new_lines
                    if next_lines != updated_lines: applied += 1
                    updated_lines = next_lines
                    rebase(orig_pos, i0, i1, len(new_lines), blk.op, s, t) if from_orig else rebase(orig_pos, i0, i1, len(new_lines), blk.op)

                updated_norm = '\n'.join(updated_lines) + ('\n' if had_final_nl else '')
                updated = updated_norm if eol == '\n' else updated_norm.replace('\n', '\r\n')
                if updated == original:
                    for blk in failed_here: failed_cmds.append(fmt_cmd(rel, blk))
                    results.append(EditEvent('error', Path(rel).name, 'No changes applied', rel))
                    continue

                remember_prev(rel)
                self._atomic_write(p, updated)
                tx['changed'].add(rel)
                for blk in failed_here: failed_cmds.append(fmt_cmd(rel, blk))
                results.append(EditEvent('partial' if failed_here else 'complete', Path(rel).name, f'applied {applied} edit(s)' + (f', {len(failed_here)} failed' if failed_here else '') + f': {self._count_lines_norm(original)} → {self._count_lines_norm(updated)} lines', rel))
            except Exception as e:
                results.append(EditEvent('error', Path(d.filename).name, f'Error: {e}', d.filename))

        if tx['changed']:
            tx['files'] = {p: tx['files'][p] for p in tx['changed']}
            self.transactions.append(tx)
            for p in tx['changed']: self.edited_files[p] = True

        uniq = list(dict.fromkeys(failed_cmds))
        prefill = ('Some edits were applied, but the following commands failed:' if tx['changed'] else 'No edits were applied; the following commands failed:') + '\n' + '\n'.join(f'- {c}' for c in uniq) + '\n\nPlease generate corrected versions.' if uniq else ''
        return results, prefill

    def _rebuild_edited_files(self):
        self.edited_files = {p: True for t in self.transactions if isinstance(self.transactions, list) for p in t.get('changed', set())}

    def rollback_file(self, file_path: str) -> bool:
        rel = (file_path or '').strip().replace('\\', '/')
        if not rel or Path(rel).is_absolute(): return False
        p, txs = (self.base_dir / Path(rel)).resolve(), self.transactions if isinstance(self.transactions, list) else []
        if not p.is_relative_to(self.base_dir): return False
        for i in range(len(txs) - 1, -1, -1):
            tx, changed = txs[i], txs[i].get('changed', set())
            if rel not in changed: continue
            prev = tx.get('files', {}).get(rel, None)
            try:
                if prev is None: 
                    with contextlib.suppress(FileNotFoundError): p.unlink()
                else: self._atomic_write(p, prev)
            except Exception:
                return False
            changed.remove(rel)
            tx.get('files', {}).pop(rel, None)
            if not changed: txs.pop(i)
            self._rebuild_edited_files()
            return True
        return False

    def rollback_for_assistant(self, assistant_id: str) -> bool:
        txs, j = self.transactions if isinstance(self.transactions, list) else [], len(self.transactions if isinstance(self.transactions, list) else [])
        while j and txs[j - 1].get('assistant_id') == assistant_id: j -= 1
        if j == len(txs): return True

        snapshot, restored = {}, []
        try:
            for tx in reversed(txs[j:]):
                for rel in list(tx.get('changed', set())):
                    p = (self.base_dir / Path(rel)).resolve()
                    if not p.is_relative_to(self.base_dir): raise RuntimeError(f'Path escapes base dir: {rel}')
                    if rel not in snapshot: snapshot[rel] = p.read_text(encoding='utf-8') if p.exists() else None
                    prev = tx.get('files', {}).get(rel, None)
                    if prev is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else: self._atomic_write(p, prev)
                    restored.append(rel)
        except Exception as e:
            try:
                for rel in dict.fromkeys(reversed(restored)):
                    p, cur = (self.base_dir / Path(rel)).resolve(), snapshot[rel]
                    if not p.is_relative_to(self.base_dir): raise RuntimeError(f'Path escapes base dir during recovery: {rel}')
                    if cur is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else: self._atomic_write(p, cur)
            except Exception as e2:
                raise RuntimeError(f'Rollback failed for assistant {assistant_id}: {e}; recovery failed: {e2}') from e2
            raise RuntimeError(f'Rollback failed for assistant {assistant_id}: {e}') from e

        del txs[j:]
        self._rebuild_edited_files()
        return True


class DisplayRenderer:
    def __init__(self, service: EditService, ctx_files: list[str]):
        self.service = service
        self.ctx_files = [p for p in ctx_files if p]
        self.current_file = ''
        self.in_fence = False
        self.tail = ''
        self.tail_emitted = 0

    @staticmethod
    def _candidate_mode(s: str) -> str:
        t = s.lstrip()
        return 'unknown' if t == '' else 'header' if t.startswith('#') else 'fence' if t.startswith('`') else 'ordinary'

    def _append_partial(self, frag: str) -> str:
        if not frag: return ''
        self.tail += frag
        if self.in_fence or self._candidate_mode(self.tail) == 'ordinary':
            out = self.tail[self.tail_emitted:]
            self.tail_emitted = len(self.tail)
            return out
        return ''

    def _finish_complete_line(self) -> str:
        line, emitted = self.tail, self.tail_emitted
        self.tail, self.tail_emitted = '', 0
        if self.in_fence:
            if self.service._FENCE_CLOSE_RE.match(line): self.in_fence = False
            return line[emitted:] + '\n'
        text = line
        if m := self.service._EDIT_HDR_RE.match(line): self.current_file = (m.group(1) or '').strip().replace('`', '')
        elif self.current_file and (h := self.service._parse_header_line(line)): text = self.service.render_edit_header(self.current_file, *h, self.ctx_files) or line
        if self.service._FENCE_OPEN_RE.match(line): self.in_fence = True
        return (text if text != line or emitted == 0 else line[emitted:]) + '\n'

    def _finish_tail(self) -> str:
        line, emitted = self.tail, self.tail_emitted
        self.tail, self.tail_emitted = '', 0
        if line == '': return ''
        if self.in_fence: return line[emitted:]
        text = line
        if m := self.service._EDIT_HDR_RE.match(line): self.current_file = (m.group(1) or '').strip().replace('`', '')
        elif self.current_file and (h := self.service._parse_header_line(line)): text = self.service.render_edit_header(self.current_file, *h, self.ctx_files) or line
        return text if text != line or emitted == 0 else line[emitted:]

    def feed(self, chunk: str) -> str:
        if not chunk: return ''
        data, out = self.service._norm_newlines(chunk), []
        while True:
            i = data.find('\n')
            if i < 0:
                if tail := self._append_partial(data): out.append(tail)
                break
            self.tail, data = self.tail + data[:i], data[i + 1:]
            out.append(self._finish_complete_line())
        return ''.join(out)

    def finish(self) -> str: return self._finish_tail()


class PromptBuilder:
    _EDIT_TRIGGER_RE = re.compile(r'\b(?:edit|rewrite)\b', re.IGNORECASE)

    @classmethod
    def _file_paths(cls, atts: list[Attachment]) -> list[str]:
        return list(dict.fromkeys(a.path.strip().replace('\\', '/') for a in atts if a.kind == 'file' and a.path.strip()))

    @classmethod
    def _compose_request(cls, text: str, atts: list[Attachment], force_edit: bool, chat_on: bool, edit_on: bool) -> tuple[dict[str, str], bool, bool]:
        prefix = '' if chat_on else CHAT_PROMPT
        chat_on = True
        wants_edit = force_edit or bool(cls._EDIT_TRIGGER_RE.search(text or ''))
        if wants_edit and not edit_on: prefix, edit_on = prefix + EDIT_PROMPT, True
        body = f'{prefix}\n\n{text}' if prefix else text
        blocks = [AttachmentService.read_files(cls._file_paths(atts)).strip()] if cls._file_paths(atts) else []
        blocks += [(a.content or '').strip() for a in atts if a.kind == 'url' and (a.content or '').strip()]
        payload = '\n\n'.join(x for x in blocks if x)
        return {'role': 'user', 'content': body + (f'{ATTACHMENTS_MARKER}{payload}\n' if payload else '')}, chat_on, edit_on

    @classmethod
    def _history_slice(cls, entries: list[Entry], entry_id: str) -> list[Entry]:
        for i, e in enumerate(entries):
            if e.id == entry_id: return entries[:i]
        return entries

    @classmethod
    def _history_state(cls, entries: list[Entry]) -> tuple[list[dict[str, str]], bool, bool]:
        out, chat_on, edit_on = [], False, False
        for e in entries:
            if isinstance(e, ExchangeEntry):
                msg, chat_on, edit_on = cls._compose_request(e.user.history_text, e.user.attachments, e.user.force_edit, chat_on, edit_on)
                out.append(msg)
                if t := (e.assistant.raw_text or '').rstrip() or ('Response stopped.' if e.assistant.finalized else ''): out.append({'role': 'assistant', 'content': t})
                continue
            if e.synthesis and ((t := (e.synthesis.raw_text or '').rstrip()) or e.synthesis.finalized):
                msg, chat_on, edit_on = cls._compose_request(e.query.history_text, e.query.attachments, e.query.force_edit, chat_on, edit_on)
                out.append(msg)
                out.append({'role': 'assistant', 'content': t or 'Response stopped.'})
        return out, chat_on, edit_on

    @classmethod
    def history_messages(cls, s: ConversationState) -> list[dict[str, str]]:
        return cls._history_state(s.entries)[0]

    @classmethod
    def normal_request_messages(cls, s: ConversationState, e: ExchangeEntry) -> list[dict[str, str]]:
        out, chat_on, edit_on = cls._history_state(cls._history_slice(s.entries, e.id))
        msg, *_ = cls._compose_request(e.user.history_text, e.user.attachments, e.user.force_edit, chat_on, edit_on)
        return out + [msg]

    @classmethod
    def member_request_messages(cls, s: ConversationState, c: CouncilEntry) -> list[dict[str, str]]:
        out, chat_on, edit_on = cls._history_state(cls._history_slice(s.entries, c.id))
        msg, *_ = cls._compose_request(c.member_prompt_text, c.query.attachments, c.query.force_edit, chat_on, edit_on)
        return out + [msg]

    @classmethod
    def synthesis_request_messages(cls, s: ConversationState, c: CouncilEntry, prompt: str) -> list[dict[str, str]]:
        out, chat_on, edit_on = cls._history_state(cls._history_slice(s.entries, c.id))
        msg, *_ = cls._compose_request(prompt, c.query.attachments, c.query.force_edit, chat_on, edit_on)
        return out + [msg]


class ChatClient:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=7200, max_retries=20)
        self.edit_service = EditService(BASE_DIR)
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions
        self._user_input_prefill = ''

    def get_completion(self, data: dict[str, Any]):
        return self.client.chat.completions.create(**data)

    @staticmethod
    def normalize_url(u: str) -> str: return AttachmentService.normalize_url(u)

    @staticmethod
    def looks_like_url(v: str) -> bool: return AttachmentService.looks_like_url(v)

    @staticmethod
    def validate_file_attachment(path: str) -> str | None: return AttachmentService.validate_file_attachment(path)

    async def fetch_url_content(self, url: str) -> str:
        return await AttachmentService.fetch_url_content(url)

    def new_display_renderer(self, ctx_files: list[str] | None = None) -> DisplayRenderer:
        return self.edit_service.new_display_renderer(ctx_files or [])

    def render_for_display(self, md: str, ctx_files: list[str] | None = None) -> str:
        return self.edit_service.render_for_display(md, ctx_files or [])

    def parse_edit_markdown(self, md: str) -> list[EditDirective]:
        return self.edit_service.parse_edit_markdown(md)

    def apply_markdown_edits(self, md: str, assistant_id: str, ctx_files: list[str]) -> list[EditEvent]:
        events, prefill = self.edit_service.apply_markdown_edits(md, assistant_id, ctx_files)
        self.edited_files, self.edit_transactions = self.edit_service.edited_files, self.edit_service.transactions
        if prefill: self._user_input_prefill = prefill
        return events

    def consume_user_input_prefill(self) -> str:
        s, self._user_input_prefill = self._user_input_prefill, ''
        return s

    def rollback_file(self, file_path: str) -> bool:
        ok = self.edit_service.rollback_file(file_path)
        self.edited_files, self.edit_transactions = self.edit_service.edited_files, self.edit_service.transactions
        return ok

    def rollback_edits_for_assistant(self, assistant_id: str) -> bool:
        ok = self.edit_service.rollback_for_assistant(assistant_id)
        self.edited_files, self.edit_transactions = self.edit_service.edited_files, self.edit_service.transactions
        return ok

    def stream(self, messages: list[dict[str, str]], model: str = DEFAULT_MODEL, reasoning: str = DEFAULT_REASONING) -> AsyncGenerator[str | ReasoningEvent, None]:
        data = {'model': model, 'messages': messages, 'max_tokens': 50000, 'temperature': 0.4, 'stream': True, 'reasoning_effort': reasoning}

        async def gen():
            full = ''

            def pick(obj, key): return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

            try:
                stream = await self.get_completion(data)
                async for chunk in stream:
                    choice = (getattr(chunk, 'choices', None) or [None])[0]
                    if not choice: continue
                    delta = pick(choice, 'delta') or {}
                    text = pick(delta, 'content')
                    if text:
                        full += text
                        yield text
                    r = pick(delta, 'reasoning')
                    reason = pick(r, 'content') if not isinstance(r, str) else r
                    if reason: yield ReasoningEvent(text=reason)
            except (asyncio.CancelledError, GeneratorExit):
                raise

        return gen()