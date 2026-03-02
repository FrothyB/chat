# AI Chat

Fast, developer-first AI chat UI with streaming, file/URL attachments, and safe, atomic code edits.

## Quick start

1. **Requirements**
   - Python `3.10+`
   - `OPENROUTER_API_KEY` set in your environment

2. **Install**
   ```bash
   pip install -U nicegui httpx openai beautifulsoup4 playwright
   ```

3. **Optional (for JS-heavy sites / 403 fallback)**
   ```bash
   playwright install chromium
   ```

4. **Run**
   ```bash
   python chat/app5.py --port 8080
   ```

The app runs with HTTPS if `cert.pem` and `key.pem` are present (clipboard works best on HTTPS/localhost).

---

## What it does

- **Token streaming UI** with adaptive markdown rendering and response timer
- **Attach local files** via fuzzy search / wildcard patterns
- **Attach URLs** by scraping page content into markdown-like text context
- **Model + reasoning selection** per session
- **AI-proposed file edits** with explicit apply step
- **Atomic writes + rollback**
  - Per-file **Reject** button
  - Full-turn **Back** undo (message + edits)
- **Persistent tab state** across refresh/reconnect

---

## Main workflow

### 1) Add context
Use the top search input:

- Type at least 2 chars to search files
- Use arrow keys + Enter to select
- Use wildcards (`*`, `?`) then Enter to attach many
- Paste a URL and press Enter to attach fetched page content

### 2) Chat or edit
Choose mode:

- `chat+edit`: normal chat, but strongly edit-oriented
- `chat`: plain chat
- `extract`: app appends an extraction add-on to your message

### 3) Apply edits
When the assistant returns edit directives, the app can show **Apply edits**.

Supported operations are parsed from assistant markdown sections like:

- `### EDIT <filepath>`
- `#### Replace \`X\`-\`Y\`` (or single-anchor form)
- `#### Insert After ...`
- `#### Insert Before ...`
- fenced replacement payloads

Edits are anchor-matched against the current file content, then applied atomically.

---

## Controls

- **Back**: undo last user+assistant turn and revert edits from that assistant response
- **Stop**: stop active stream and finalize current partial text
- **Clear**: reset session state
- **Reject** (on edit result bubble): rollback that specific edited file
- **Copy buttons**: available on assistant/user bubbles and code blocks

---

## Notes

- Paths are treated as project-relative and constrained to the app base directory.
- File edits are intentionally strict and fail when anchors are ambiguous.
- For Playwright scraping visibility, set:
  - `AI_CHAT_PLAYWRIGHT_HEADLESS=1` for headless mode