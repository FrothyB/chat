## Design Doc: Slimmer 2-File Architecture (`app5.py` frontend, `chat_utils.py` backend)

### 1) Intent

Keep the current UX/features, but make the codebase **predictable, typed, and fail-fast** while preserving the same split:

- `chat/app5.py` → UI + user interaction orchestration
- `chat/chat_utils.py` → chat domain logic, streaming, attachments, edit parsing/applying/rollback, filesystem/search

No behavioral downgrade: keep streaming, stop, undo, file/url attachments, edit suggestions + apply/reject, and markdown rendering.

---

### 2) Current behavior (as implemented)

#### Frontend (`app5.py`)
- Initializes many keys in `app.storage.tab`
- Builds header controls (model/reasoning/search), chat list, footer input/actions
- Manages:
  - sending messages
  - producer/consumer streaming loop
  - stop/clear/undo
  - file/url attach/detach
  - edit proposal bubble + apply flow
- Renders markdown and injects code-copy buttons with JS

#### Backend (`chat_utils.py`)
- Maintains conversation state + prompt injection flags
- Calls OpenRouter via streaming API
- Reads attached files / serializes ipynb code cells
- Parses markdown edit directives
- Resolves target files and applies anchored replacements
- Tracks transactions for rollback and undo

---

### 3) Issues

## A) Structural
1. `main_page()` is a monolith with deeply nested closures.
2. State is untyped string-key dictionary (easy to drift/break).
3. UI concerns and stream/edit workflow concerns are intertwined.

## B) Correctness/Brittleness
1. Implicit state machine (`streaming`, `finalized`, `stream_done`, etc.) can desync.
2. Broad `except Exception` + suppression hides real faults.
3. Producer/consumer split introduces race windows and duplicated finalization logic.

## C) Maintainability
1. Repeated “reset this bundle of fields” code in many places.
2. Backend `ChatClient` handles too many responsibilities in one class.
3. Hard to unit test flow-level behavior because contracts are implicit.

---

### 4) New architecture (still only 2 files)

## 4.1 High-level split

| File | Responsibility |
|---|---|
| `chat/app5.py` | UI composition + explicit action handlers + render helpers |
| `chat/chat_utils.py` | Domain services + typed events/results + filesystem + LLM integration |

---

## 4.2 Frontend design (`app5.py`)

### Core idea
Replace key-soup with one typed session object and a small controller.

```python
@dataclass(slots=True)
class UiState:
    phase: Literal["idle", "streaming", "awaiting_edit_decision"] = "idle"
    draft: str = ""
    model: str = DEFAULT_MODEL
    reasoning: str = DEFAULT_REASONING
    mode: Literal["chat+edit", "chat", "extract"] = "chat+edit"
    stream_text: str = ""
    pending_edit_text: str | None = None
```

Also keep a `UiRefs` dataclass for widget handles (`input_field`, `container`, etc.) so they are explicit, not hidden in storage.

### Frontend sections (in same file)
1. **State/refs models**
2. **Pure render helpers** (`render_message`, `render_attachments`, `render_edit_status`)
3. **Action handlers**
   - `async send()`
   - `stop()`
   - `undo()`
   - `clear()`
   - `attach_file()`
   - `attach_url()`
4. **Wiring** (UI component creation + event binding)

### Streaming simplification
Use **one stream task**, no separate producer/consumer timer pair:
- task appends tokens to `state.stream_text`
- throttled render inline
- one `finally` finalization path

This removes multiple sync flags and race-prone cross-callback logic.

---

## 4.3 Backend design (`chat_utils.py`)

Keep one backend file, but separate by internal classes and typed contracts.

### Typed contracts
```python
@dataclass(slots=True)
class StreamEvent:
    t: Literal["text", "reasoning", "done", "error"]
    c: str = ""

@dataclass(slots=True)
class EditRoundResult:
    status: Literal["none", "applied", "partial", "failed"]
    events: list[EditEvent] = field(default_factory=list)
    prefill: str = ""
```

### Internal backend components (same file)
1. **`ChatSession`**
   - message history, prompt injection, undo hooks
   - `stream(...) -> AsyncGenerator[StreamEvent, None]`
2. **`AttachmentService`**
   - `search_files`, `read_files`, URL normalization/fetch/extract text
3. **`EditService`**
   - parse markdown edit directives
   - apply edits atomically
   - rollback/reject
4. **Facade `ChatClient`**
   - thin API used by frontend, delegates to above

This keeps frontend/backend split while removing “everything in one mega-class”.

---

### 5) Explicit state machine

```mermaid
flowchart TD
  A["\"idle\""] -->| "\"send\"" | B["\"streaming\""]
  B -->| "\"stop\"" | A
  B -->| "\"done (no edits)\"" | A
  B -->| "\"done (edits detected)\"" | C["\"awaiting_edit_decision\""]
  C -->| "\"apply edits\"" | A
  C -->| "\"skip edits + next send\"" | A
```

No hidden intermediate booleans; `phase` is the source of truth.

---

### 6) Frontend↔backend API (clean contract)

| Method | In | Out |
|---|---|---|
| `stream_message(req)` | message + mode/model/reasoning + attachments | `StreamEvent` stream |
| `undo_last()` | none | last user text + attachments |
| `detect_edits(text)` | assistant markdown | bool / directives |
| `apply_edits(text)` | assistant markdown | `EditRoundResult` |
| `reject_edit(path)` | file path | bool |
| `search_files(q)` | query | list[path] |
| `fetch_url(url)` | URL | extracted text payload |

Frontend should not parse edit markdown or resolve file paths itself.

---

### 7) Error handling policy (less brittle)

- **Fail fast** in domain logic (invalid path, ambiguous anchors, impossible state).
- Only suppress errors for non-critical UI cleanup.
- One user-notification path for task failures.
- Keep raw exceptions in logs; keep user messages concise.

---

### 8) Migration plan (small safe steps)

1. Introduce `UiState` + `UiRefs`; keep existing behavior.
2. Replace producer/consumer dual loop with single stream task.
3. Move URL fetching/extraction to backend service API.
4. Split backend internals (`ChatSession`, `EditService`, `AttachmentService`) without changing external `ChatClient` interface.
5. Remove obsolete flags/keys and dead code.

---

### 9) Expected outcomes

- ~40–60% less orchestration complexity in `app5.py`
- Fewer race-condition surfaces in streaming
- Much easier to test edit logic and streaming independently
- Cleaner long-term evolution while preserving your strict 2-file layout

---

If you want, next I can draft the **exact target skeleton** for both files (class/function outlines only), so implementation is mostly fill-in work.