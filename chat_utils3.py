import asyncio, contextlib, json, os, re, tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI
from stuff import CHAT_PROMPT, EDIT_PROMPT, EXTRACT_ADD_ON, STYLE_CSS
from url_utils import fetch_url_content as _fetch_url_content, looks_like_url as _looks_like_url, normalize_url as _normalize_url

API_KEY = os.getenv('OPENROUTER_API_KEY')
BASE_URL = 'https://openrouter.ai/api/v1'
BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = 'openai/gpt-5.4'
DEFAULT_REASONING = 'medium'
MODELS = ['google/gemini-3.1-pro-preview', 'openai/gpt-5.4', 'openai/gpt-5.4-pro', 'x-ai/grok-4.20-beta', 'x-ai/grok-4.20-multi-agent-beta']
REASONING_LEVELS = ['none', 'minimal', 'low', 'medium', 'high']

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
    def normalize_url(u: str) -> str:
        return _normalize_url(u)

    @staticmethod
    def looks_like_url(v: str) -> bool:
        return _looks_like_url(v)

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
            buf = []
            for ch in pat:
                if ch == '*': buf.append('.*')
                elif ch == '?': buf.append('.')
                else: buf.append(re.escape(ch))
            return re.compile('^' + ''.join(buf) + '$', re.IGNORECASE)

        patterns, terms = [], []
        for t in toks: (patterns if any(ch in t for ch in ('*', '?')) else terms).append(t)
        terms_l = [t.lower() for t in terms]

        def visible(rel: str) -> bool:
            return not any(part.startswith('.') for part in Path(rel).parts)

        def scan(want_dir: bool, strict_name: bool, pats: list[tuple[re.Pattern, bool]] | None = None, term_set: list[str] | None = None) -> list[str]:
            out = []
            with contextlib.suppress(Exception):
                for item in base.rglob('*'):
                    if len(out) >= max_results: break
                    if want_dir and not item.is_dir(): continue
                    if not want_dir and not item.is_file(): continue
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
            files = scan(False, False, pats=pats, term_set=terms_l)
            if files: return sorted(set(files))[:max_results]
            dirs = scan(True, False, pats=pats, term_set=terms_l)
            return sorted(set(dirs))[:max_results]

        files = scan(False, True) or (scan(False, False) if len(terms_l) > 1 else [])
        if files: return sorted(set(files))[:max_results]
        dirs = scan(True, True) or (scan(True, False) if len(terms_l) > 1 else [])
        return sorted(set(dirs))[:max_results]

    @staticmethod
    def read_files(file_paths: list[str]) -> str:
        if not file_paths: return ''
        out = []
        for rel in file_paths:
            r = Path((rel or '').strip().replace('\\', '/'))
            name = r.as_posix()
            if not name or r.is_absolute():
                out.append(f'### {name or rel}\nError: invalid relative path\n')
                continue
            p = (BASE_DIR / r).resolve()
            if not p.is_relative_to(BASE_DIR):
                out.append(f'### {name}\nError: path escapes base dir\n')
                continue
            try:
                if p.name.endswith('.ipynb'):
                    nb = json.loads(p.read_text(encoding='utf-8'))
                    cells = []
                    for cell in nb.get('cells', []):
                        if cell.get('cell_type') != 'code': continue
                        src = cell.get('source', [])
                        if isinstance(src, str): src = [src]
                        if not isinstance(src, list): continue
                        cells.append({'source': [s if isinstance(s, str) else str(s) for s in src]})
                    payload = json.dumps({'cells': cells}, indent=2) + '\n'
                    out.append(f'### {name}\nExtracted only source from notebook; edit cell-by-cell if needed.\n{payload}\n')
                else:
                    out.append(f'### {name}\n{p.read_text(encoding="utf-8")}\n')
            except Exception as e:
                out.append(f'### {name}\nError: {e}\n')
        return '\n'.join(out)

    @staticmethod
    async def fetch_url_content(url: str) -> str:
        return await _fetch_url_content(url)


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
        self.transactions: list[dict] = []

    @staticmethod
    def _norm_newlines(s: str) -> str:
        return (s or '').replace('\r\n', '\n').replace('\r', '\n')

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
        lang, body_start = (m.group(1) or '').strip(), m.end()
        if not (m2 := cls._FENCE_CLOSE_RE.search(text, body_start)): return None
        body = text[body_start:m2.start()]
        if body.startswith('\n'): body = body[1:]
        return lang, body, m2.end()

    @classmethod
    def _parse_header_match(cls, op: str, m: re.Match) -> tuple[str, str, bool, int | None, str]:
        x, occ, y_raw = (m.group(1) or ''), (m.group(2) or '').strip(), m.group(3)
        return x, (x if y_raw in {None, ''} else y_raw), (y_raw is None), (int(occ) if occ else None), cls._HEADER_LABELS[op]

    @classmethod
    def _parse_header_line(cls, line: str) -> tuple[str, str, str, bool, int | None, str] | None:
        for op, rx, _ in cls._HEADER_SPECS:
            if m := rx.match(line):
                x, y, single, occ, label = cls._parse_header_match(op, m)
                return op, x, y, single, occ, label
        return None

    @classmethod
    def _next_hdr(cls, section: str, at: int) -> tuple[str | None, re.Match | None]:
        cand = [(op, rx.search(section, at)) for op, rx, _ in cls._HEADER_SPECS]
        cand = [(op, m) for op, m in cand if m]
        if not cand: return None, None
        return min(cand, key=lambda t: t[1].start())

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
        if not raw: return None
        cand = Path(raw)
        if cand.is_absolute(): return None

        def safe_rel(p: Path) -> Path | None:
            try: return Path(p.relative_to(self.base_dir).as_posix())
            except Exception: return None

        abs0 = (self.base_dir / cand).resolve()
        rel0 = safe_rel(abs0)
        if not rel0: return None
        if abs0.exists() or create_if_missing: return rel0.as_posix()

        ctx_paths = [Path(p) for p in (ctx_files or []) if p]

        def suffix_matches(p: Path, suffix: Path) -> bool:
            sp, pp = suffix.parts, p.parts
            return len(pp) >= len(sp) and tuple(pp[-len(sp):]) == sp

        if ctx_paths:
            if len(cand.parts) > 1:
                hits = [p for p in ctx_paths if suffix_matches(p, cand)]
                if len(hits) == 1: return hits[0].as_posix()
                if len(hits) > 1: return None
            name_hits = [p for p in ctx_paths if p.name == cand.name]
            if len(name_hits) == 1: return name_hits[0].as_posix()
            if len(name_hits) > 1: return None

        hits = []
        with contextlib.suppress(Exception):
            for q in self.base_dir.rglob(cand.name):
                if not q.is_file(): continue
                rel = q.relative_to(self.base_dir)
                if len(cand.parts) > 1 and not suffix_matches(rel, cand): continue
                hits.append(Path(rel.as_posix()))
                if len(hits) > 1: break
        if len(hits) == 1: return hits[0].as_posix()
        if len(hits) > 1: return None
        return rel0.as_posix() if create_if_missing else None

    def _read_file_lines(self, rel: str) -> list[str] | None:
        p = (self.base_dir / Path(rel)).resolve()
        if not p.is_relative_to(self.base_dir) or not p.exists(): return None
        norm = self._norm_newlines(p.read_text(encoding='utf-8'))
        xs = norm.split('\n')
        if norm.endswith('\n'): xs = xs[:-1]
        return xs

    def _code_lang(self, rel: str) -> str:
        suffix = Path(rel).suffix.lower()
        return LANG_BY_EXT.get(suffix, suffix.lstrip('.'))

    def render_edit_header(self, filename: str, op: str, x: str, y: str, single: bool, occ: int | None, label: str, ctx_files: list[str]) -> str | None:
        rel = self._resolve_path(filename, ctx_files, create_if_missing=False)
        if not rel: return None
        lines = self._read_file_lines(rel)
        if not lines: return None
        if not (span := self._find_anchor_span(lines, x, y, single=single, occ=occ)): return None
        a, b = span
        body = '\n'.join(lines[a - 1:b])
        lang = self._code_lang(rel)
        fence = f'```{lang}\n{body}\n```' if body else f'```{lang}\n```'
        tail = '#### WITH' if op == 'replace' else '#### ADD'
        return f'#### {label} {a}-{b}\n{fence}\n{tail}'

    def new_display_renderer(self, ctx_files: list[str] | None = None) -> 'DisplayRenderer':
        return DisplayRenderer(self, ctx_files or [])

    def parse_edit_markdown(self, md: str) -> list[EditDirective]:
        if not md: return []
        text = self._norm_newlines(md)
        edits = list(self._EDIT_HDR_RE.finditer(text))
        out = []

        for i, m in enumerate(edits):
            filename = (m.group(1) or '').strip().replace('`', '')
            start, end = m.end(), (edits[i + 1].start() if i + 1 < len(edits) else len(text))
            section = text[start:end].strip()

            replaces, pos, full_new = [], 0, None
            while True:
                op, hdr = self._next_hdr(section, pos)
                if not hdr: break
                if not (f := self._parse_fence_from(section, hdr.end())):
                    pos = hdr.end()
                    continue
                x, y, single, n, _ = self._parse_header_match(op or 'replace', hdr)
                replaces.append(ReplaceBlock(x=x, y=y, single=single, occ=n, new=f[1], lang=(f[0] or '').strip(), op=op or 'replace'))
                pos = f[2]

            has_cmd = any(rx.search(section) for _, rx, _ in self._HEADER_SPECS)
            if not replaces and not has_cmd and (f := self._parse_fence_from(section, 0)): full_new = f[1]

            cuts = [x.start() for x in [self._REPLACE_HDR_RE.search(section), self._INSERT_AFTER_HDR_RE.search(section), self._INSERT_BEFORE_HDR_RE.search(section), self._FENCE_OPEN_RE.search(section)] if x]
            expl = section[:min(cuts)].strip() if cuts else section.strip()
            if replaces or full_new is not None: out.append(EditDirective(kind='EDIT', filename=filename, explanation=expl, replaces=replaces, full_new=full_new))

        return out

    def render_for_display(self, md: str, ctx_files: list[str] | None = None) -> str:
        r = self.new_display_renderer(ctx_files or [])
        return r.feed(md or '') + r.finish()

    def apply_markdown_edits(self, md: str, assistant_index: int | None, ctx_files: list[str]) -> tuple[list[EditEvent], str]:
        directives = self.parse_edit_markdown(md)
        if not directives: return [], ''
        results, failed_cmds = [], []
        tx = {'assistant_index': assistant_index, 'files': {}, 'changed': set()}

        def abs_of(rel: str) -> Path:
            return (self.base_dir / Path(rel)).resolve()

        def remember_prev(rel: str):
            if rel in tx['files']: return
            p = abs_of(rel)
            tx['files'][rel] = p.read_text(encoding='utf-8') if p.exists() else None

        def fmt_cmd(rel: str, blk: ReplaceBlock) -> str:
            op = {'replace': 'Replace', 'insert_after': 'Insert After', 'insert_before': 'Insert Before'}.get(blk.op, blk.op)
            core = f'{op} `{blk.x}`' if blk.single else f'{op} `{blk.x}`-`{blk.y}`'
            if blk.occ: core += f' {blk.occ}'
            return f'{rel}: {core}'

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
                    original = p.read_text(encoding='utf-8') if p.exists() else None
                    updated_norm = self._norm_newlines(d.full_new or '')
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

                original = p.read_text(encoding='utf-8')
                eol = '\r\n' if '\r\n' in original else '\n'
                norm = self._norm_newlines(original)
                had_final_nl = norm.endswith('\n')
                lines = norm.split('\n')
                if had_final_nl: lines = lines[:-1]

                spans, failed_here = [], []
                for blk in d.replaces:
                    new_norm = self._norm_newlines(blk.new).rstrip('\n')
                    new_lines = [] if new_norm == '' else new_norm.split('\n')
                    if not (span := self._find_anchor_span(lines, blk.x, blk.y, single=blk.single, occ=blk.occ)):
                        failed_here.append(blk)
                        continue
                    a, b = span
                    if blk.op == 'replace': i0, i1 = a - 1, b
                    elif blk.op == 'insert_after': i0, i1 = b, b
                    elif blk.op == 'insert_before': i0, i1 = a - 1, a - 1
                    else: raise RuntimeError(f'Unknown op: {blk.op}')
                    spans.append((i0, i1, new_lines, blk))

                if not spans:
                    for blk in failed_here: failed_cmds.append(fmt_cmd(rel, blk))
                    results.append(EditEvent('error', Path(rel).name, 'No edit blocks uniquely matched anchors (X/Y) in file', rel))
                    continue

                spans.sort(key=lambda t: (t[0], t[1]))
                for (a1, b1, _, _), (a2, b2, _, _) in zip(spans, spans[1:]):
                    if a2 < b1: raise RuntimeError(f'Overlapping edit ranges: {a1 + 1}-{b1} and {a2 + 1}-{b2}')

                updated_lines = lines[:]
                for i0, i1, new_lines, _ in sorted(spans, key=lambda t: (t[0], t[1]), reverse=True): updated_lines[i0:i1] = new_lines

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
                kind, extra = ('partial' if failed_here else 'complete'), (f', {len(failed_here)} failed' if failed_here else '')
                results.append(EditEvent(kind, Path(rel).name, f'applied {len(spans)} edit(s){extra}: {self._count_lines_norm(original)} → {self._count_lines_norm(updated)} lines', rel))
            except Exception as e:
                results.append(EditEvent('error', Path(d.filename).name, f'Error: {e}', d.filename))

        if tx['changed']:
            tx['files'] = {p: tx['files'][p] for p in tx['changed']}
            if not isinstance(self.transactions, list): self.transactions = []
            self.transactions.append(tx)
            for p in tx['changed']: self.edited_files[p] = True

        prefill = ''
        if failed_cmds:
            uniq = []
            for c in failed_cmds:
                if c not in uniq: uniq.append(c)
            lead = 'Some edits were applied, but the following commands failed:' if tx['changed'] else 'No edits were applied; the following commands failed:'
            prefill = lead + '\n' + '\n'.join(f'- {c}' for c in uniq) + '\n\nPlease generate corrected versions.'
        return results, prefill

    def _rebuild_edited_files(self):
        current = {}
        for t in self.transactions if isinstance(self.transactions, list) else []:
            for p in t.get('changed', set()): current[p] = True
        self.edited_files = current

    def rollback_file(self, file_path: str) -> bool:
        rel = (file_path or '').strip().replace('\\', '/')
        if not rel or Path(rel).is_absolute(): return False
        p = (self.base_dir / Path(rel)).resolve()
        if not p.is_relative_to(self.base_dir): return False

        txs = self.transactions if isinstance(self.transactions, list) else []
        for i in range(len(txs) - 1, -1, -1):
            tx = txs[i]
            changed = tx.get('changed', set())
            if rel not in changed: continue
            prev = tx.get('files', {}).get(rel, None)
            try:
                if prev is None:
                    with contextlib.suppress(FileNotFoundError): p.unlink()
                else:
                    self._atomic_write(p, prev)
            except Exception:
                return False
            changed.remove(rel)
            tx.get('files', {}).pop(rel, None)
            if not changed: txs.pop(i)
            self._rebuild_edited_files()
            return True
        return False

    def rollback_for_assistant(self, assistant_index: int) -> bool:
        txs = self.transactions if isinstance(self.transactions, list) else []
        j = len(txs)
        while j and txs[j - 1].get('assistant_index') == assistant_index: j -= 1
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
                    else:
                        self._atomic_write(p, prev)
                    restored.append(rel)
        except Exception as e:
            try:
                for rel in dict.fromkeys(reversed(restored)):
                    p = (self.base_dir / Path(rel)).resolve()
                    if not p.is_relative_to(self.base_dir): raise RuntimeError(f'Path escapes base dir during recovery: {rel}')
                    cur = snapshot[rel]
                    if cur is None:
                        with contextlib.suppress(FileNotFoundError): p.unlink()
                    else:
                        self._atomic_write(p, cur)
            except Exception as e2:
                raise RuntimeError(f'Rollback failed for assistant {assistant_index}: {e}; recovery failed: {e2}') from e2
            raise RuntimeError(f'Rollback failed for assistant {assistant_index}: {e}') from e

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
        if t == '': return 'unknown'
        if t.startswith('#'): return 'header'
        if t.startswith('`'): return 'fence'
        return 'ordinary'

    def _append_partial(self, frag: str) -> str:
        if not frag: return ''
        self.tail += frag
        if self.in_fence:
            out = self.tail[self.tail_emitted:]
            self.tail_emitted = len(self.tail)
            return out
        if self._candidate_mode(self.tail) == 'ordinary':
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
            head, data = data[:i], data[i + 1:]
            self.tail += head
            out.append(self._finish_complete_line())
        return ''.join(out)

    def finish(self) -> str:
        return self._finish_tail()


class ChatClient:
    _EDIT_TRIGGER_RE = re.compile(r'\b(?:edit|rewrite)\b', re.IGNORECASE)

    def __init__(self):
        self.messages = [{'role': 'system', 'content': ''}]
        self._chat_prompt_injected = False
        self._edit_prompt_injected = False
        self.files: list[str] = []
        self.message_files: dict[int, list[str]] = {}
        self.message_attachments: dict[int, list[dict[str, str]]] = {}
        self.message_restore_text: dict[int, str] = {}
        self.assistant_models: dict[int, str] = {}
        self._last_assistant_index: int | None = None
        self._display_overrides: dict[int, str] = {}
        self._user_input_prefill = ''
        self.edit_service = EditService(BASE_DIR)
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions
        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=7200, max_retries=20)

    def get_completion(self, data: dict[str, Any]):
        return self.client.chat.completions.create(**data)

    @staticmethod
    def normalize_url(u: str) -> str:
        return AttachmentService.normalize_url(u)

    @staticmethod
    def looks_like_url(v: str) -> bool:
        return AttachmentService.looks_like_url(v)

    async def fetch_url_content(self, url: str) -> str:
        return await AttachmentService.fetch_url_content(url)

    @staticmethod
    def _strip_hidden_attachments(s: str) -> str:
        t = s or ''
        for m in (ATTACHMENTS_MARKER, '\n\nAttached files:', '\n\nAttached files:\n'):
            if m in t: t = t.split(m, 1)[0]
        return t

    def _strip_injected_prompts(self, s: str) -> str:
        out = s or ''
        if out.startswith(CHAT_PROMPT + EDIT_PROMPT): out = out[len(CHAT_PROMPT + EDIT_PROMPT):]
        elif out.startswith(CHAT_PROMPT): out = out[len(CHAT_PROMPT):]
        elif out.startswith(EDIT_PROMPT): out = out[len(EDIT_PROMPT):]
        return out.lstrip('\n')

    def _recompute_prompt_flags(self):
        chat, edit = False, False
        for m in self.messages:
            if m.get('role') != 'user': continue
            c = m.get('content') or ''
            if c.startswith(CHAT_PROMPT): chat = True
            if c.startswith(EDIT_PROMPT) or c.startswith(CHAT_PROMPT + EDIT_PROMPT): edit = True
            if chat and edit: break
        self._chat_prompt_injected, self._edit_prompt_injected = chat, edit

    def _refresh_last_assistant_index(self):
        self._last_assistant_index = next((i for i in range(len(self.messages) - 1, -1, -1) if self.messages[i].get('role') == 'assistant'), None)

    def _latest_context_files(self) -> list[str]:
        if not self.message_files: return []
        return self.message_files.get(max(self.message_files.keys()), []) or []

    def new_display_renderer(self, ctx_files: list[str] | None = None) -> DisplayRenderer:
        return self.edit_service.new_display_renderer(ctx_files if ctx_files is not None else self._latest_context_files())

    def _compose_user_content_state(self, user_msg: str, force_edit: bool = False, attachments: list[dict[str, str]] | None = None, chat_injected: bool | None = None, edit_injected: bool | None = None) -> tuple[str, list[str], list[dict[str, str]], bool, bool]:
        atts = [dict(a) for a in (attachments or []) if isinstance(a, dict)]
        files = [((a.get('path') or '').strip().replace('\\', '/')) for a in atts if (a.get('kind') or '').lower() == 'file' and (a.get('path') or '').strip()]
        files = list(dict.fromkeys(files))
        prefix, chat_injected, edit_injected = '', self._chat_prompt_injected if chat_injected is None else chat_injected, self._edit_prompt_injected if edit_injected is None else edit_injected
        if not chat_injected: prefix, chat_injected = prefix + CHAT_PROMPT, True
        if (force_edit or bool(self._EDIT_TRIGGER_RE.search(user_msg or ''))) and not edit_injected: prefix, edit_injected = prefix + EDIT_PROMPT, True
        text = f'{prefix}\n\n{user_msg}' if prefix else user_msg
        blocks = [AttachmentService.read_files(files).strip()] if files else []
        for a in atts:
            if (a.get('kind') or '').lower() == 'file': continue
            c = (a.get('content') or '').strip()
            if c: blocks.append(c)
        payload = '\n\n'.join(x for x in blocks if x)
        return text + (f'{ATTACHMENTS_MARKER}{payload}\n' if payload else ''), files, atts, chat_injected, edit_injected

    def _compose_user_content(self, user_msg: str, force_edit: bool = False, attachments: list[dict[str, str]] | None = None, mutate: bool = False) -> tuple[str, list[str], list[dict[str, str]]]:
        content, files, atts, chat_injected, edit_injected = self._compose_user_content_state(user_msg, force_edit=force_edit, attachments=attachments)
        if mutate: self._chat_prompt_injected, self._edit_prompt_injected = chat_injected, edit_injected
        return content, files, atts

    def _stream(self, messages: list[dict[str, str]], assistant_sink: dict[str, str], model: str, reasoning: str) -> AsyncGenerator[str | ReasoningEvent, None]:
        data = {'model': model, 'messages': messages, 'max_tokens': 50000, 'temperature': 0.4, 'stream': True, 'reasoning_effort': reasoning}

        async def gen():
            full = ''

            def pick(obj, key):
                if isinstance(obj, dict): return obj.get(key)
                return getattr(obj, key, None)

            try:
                stream = await self.get_completion(data)
                async for chunk in stream:
                    choice = (getattr(chunk, 'choices', None) or [None])[0]
                    if not choice: continue
                    delta = pick(choice, 'delta') or {}
                    text = pick(delta, 'content')
                    if text:
                        full += text
                        assistant_sink['content'] = full
                        yield text
                    r = pick(delta, 'reasoning')
                    reason = pick(r, 'content') if not isinstance(r, str) else r
                    if reason: yield ReasoningEvent(text=reason)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                assistant_sink['content'] = full
                raise
            assistant_sink['content'] = full

        return gen()

    def stream_message(self, user_msg: str, model: str = DEFAULT_MODEL, reasoning: str = DEFAULT_REASONING, force_edit: bool = False, attachments: list[dict[str, str]] | None = None, display_user: str | None = None, restore_user: str | None = None) -> AsyncGenerator[str | ReasoningEvent, None]:
        content, files, atts = self._compose_user_content(user_msg, force_edit=force_edit, attachments=attachments, mutate=True)
        msg_index = len(self.messages)
        if files: self.message_files[msg_index] = files.copy()
        if atts: self.message_attachments[msg_index] = [dict(a) for a in atts]
        if restore_user is not None: self.message_restore_text[msg_index] = restore_user
        self.messages.append({'role': 'user', 'content': content})
        if display_user is not None: self._display_overrides[msg_index] = display_user
        self.files = []
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions

        assistant_index = len(self.messages)
        self._last_assistant_index = assistant_index
        self.messages.append({'role': 'assistant', 'content': ''})
        self.assistant_models[assistant_index] = model
        return self._stream(self.messages[:-1], self.messages[assistant_index], model, reasoning)
    
    def stream_message_with_history(self, request_user_msg: str, history_user_msg: str, model: str = DEFAULT_MODEL, reasoning: str = DEFAULT_REASONING, force_edit: bool = False, attachments: list[dict[str, str]] | None = None, display_user: str | None = None, restore_user: str | None = None) -> AsyncGenerator[str | ReasoningEvent, None]:
        base_messages, chat_injected, edit_injected = [dict(m) for m in self.messages], self._chat_prompt_injected, self._edit_prompt_injected
        request_content, _, _, next_chat_injected, next_edit_injected = self._compose_user_content_state(request_user_msg, force_edit=force_edit, attachments=attachments, chat_injected=chat_injected, edit_injected=edit_injected)
        history_content, files, atts, _, _ = self._compose_user_content_state(history_user_msg, force_edit=force_edit, attachments=attachments, chat_injected=chat_injected, edit_injected=edit_injected)
        self._chat_prompt_injected, self._edit_prompt_injected = next_chat_injected, next_edit_injected

        msg_index = len(self.messages)
        if files: self.message_files[msg_index] = files.copy()
        if atts: self.message_attachments[msg_index] = [dict(a) for a in atts]
        if restore_user is not None: self.message_restore_text[msg_index] = restore_user
        self.messages.append({'role': 'user', 'content': history_content})
        if display_user is not None: self._display_overrides[msg_index] = display_user
        self.files = []
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions

        assistant_index = len(self.messages)
        self._last_assistant_index = assistant_index
        self.messages.append({'role': 'assistant', 'content': ''})
        self.assistant_models[assistant_index] = model
        return self._stream(base_messages + [{'role': 'user', 'content': request_content}], self.messages[assistant_index], model, reasoning)

    def stream_transient(self, user_msg: str, model: str = DEFAULT_MODEL, reasoning: str = DEFAULT_REASONING, force_edit: bool = False, attachments: list[dict[str, str]] | None = None, base_messages: list[dict[str, str]] | None = None) -> AsyncGenerator[str | ReasoningEvent, None]:
        content, _, _ = self._compose_user_content(user_msg, force_edit=force_edit, attachments=attachments, mutate=False)
        messages = [dict(m) for m in (base_messages or self.messages)] + [{'role': 'user', 'content': content}]
        sink = {'role': 'assistant', 'content': ''}
        return self._stream(messages, sink, model, reasoning)

    def render_for_display(self, md: str, ctx_files: list[str] | None = None) -> str:
        return self.edit_service.render_for_display(md, ctx_files if ctx_files is not None else self._latest_context_files())

    def display_text_for(self, idx: int) -> str:   
        if not (0 <= idx < len(self.messages)): return ''
        if idx in self._display_overrides: return self._display_overrides[idx]
        m, role, content = self.messages[idx], self.messages[idx].get('role'), self.messages[idx].get('content') or ''
        if role == 'user': return self._strip_injected_prompts(self._strip_hidden_attachments(content))
        return content
    
    def restore_text_for(self, idx: int) -> str:
        if not (0 <= idx < len(self.messages)) or self.messages[idx].get('role') != 'user': return ''
        if idx in self.message_restore_text: return self.message_restore_text[idx]
        return self._strip_injected_prompts(self._strip_hidden_attachments(self.messages[idx].get('content') or ''))

    def set_message_display(self, idx: int, display_md: str):
        if 0 <= idx < len(self.messages): self._display_overrides[idx] = display_md or ''

    def set_assistant_display(self, assistant_index: int, display_md: str):
        if 0 <= assistant_index < len(self.messages) and self.messages[assistant_index].get('role') == 'assistant': self.set_message_display(assistant_index, display_md)

    def set_last_assistant_display(self, display_md: str):
        if self._last_assistant_index is not None: self.set_assistant_display(self._last_assistant_index, display_md)

    def parse_edit_markdown(self, md: str) -> list[EditDirective]:
        return self.edit_service.parse_edit_markdown(md)

    def apply_markdown_edits(self, md: str, assistant_index: int | None = None) -> list[EditEvent]:
        ai = assistant_index if assistant_index is not None else self._last_assistant_index
        events, prefill = self.edit_service.apply_markdown_edits(md, ai, self._latest_context_files())
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions
        if prefill: self._user_input_prefill = prefill
        return events

    def consume_user_input_prefill(self) -> str:
        s, self._user_input_prefill = self._user_input_prefill, ''
        return s

    def rollback_file(self, file_path: str) -> bool:
        ok = self.edit_service.rollback_file(file_path)
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions
        return ok

    def rollback_edits_for_assistant(self, assistant_index: int) -> bool:
        ok = self.edit_service.rollback_for_assistant(assistant_index)
        self.edited_files = self.edit_service.edited_files
        self.edit_transactions = self.edit_service.transactions
        return ok

    def undo_last(self) -> tuple[str | None, list[str], list[dict[str, str]]]:
        if len(self.messages) < 3 or self.messages[-1]['role'] != 'assistant': return None, [], []
        ai = len(self.messages) - 1
        if not self.rollback_edits_for_assistant(ai): raise RuntimeError(f'Failed to rollback edits for assistant {ai}')
        self._display_overrides.pop(ai, None)
        self.assistant_models.pop(ai, None)
        self.messages.pop()

        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]['role'] != 'user': continue
            restore = self.restore_text_for(i)
            self.messages.pop(i)
            self._display_overrides.pop(i, None)
            self.message_restore_text.pop(i, None)
            atts = self.message_attachments.pop(i, [])
            files = self.message_files.pop(i, [((a.get('path') or '').strip().replace('\\', '/')) for a in atts if (a.get('kind') or '').lower() == 'file' and (a.get('path') or '').strip()])
            self.files = files.copy()
            self._recompute_prompt_flags()
            self._refresh_last_assistant_index()
            return restore, files, atts

        self._recompute_prompt_flags()
        self._refresh_last_assistant_index()
        return None, [], []

    def get_display_messages(self) -> list[tuple[str, str, list[dict[str, str]]]]:
        out = []
        for idx, m in enumerate(self.messages[1:], start=1):
            role = m.get('role')
            if role not in {'user', 'assistant'}: continue
            atts = [dict(a) for a in (self.message_attachments.get(idx, []) or [])] if role == 'user' else []
            out.append((role, self.display_text_for(idx), atts))
        return out

    def ensure_assistant_nonempty(self, assistant_index: int, fallback: str = 'Response stopped.'):
        with contextlib.suppress(Exception):
            if 0 <= assistant_index < len(self.messages) and self.messages[assistant_index]['role'] == 'assistant' and not (self.messages[assistant_index].get('content') or '').strip(): self.messages[assistant_index]['content'] = fallback

    def ensure_last_assistant_nonempty(self, fallback: str = 'Response stopped.'):
        if self._last_assistant_index is not None: self.ensure_assistant_nonempty(self._last_assistant_index, fallback)
