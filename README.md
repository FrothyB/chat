# AI Chat

An exceptionally fast, developer-centric AI interface featuring deep file integration, web scraping, and surgical AI-driven code modifications.

## Quick Start

1.  **Requirements**: Python 3.10+, `OPENROUTER_API_KEY` environment variable.
2.  **Install**: `pip install -U nicegui httpx openai beautifulsoup4 playwright`
3.  **Setup Playwright** (for JS-heavy sites): `playwright install chromium`
4.  **Launch**: `python app5.py --port 8080`

## Core Capabilities

-   **Advanced Streaming**: High-performance UI with adaptive rendering and real-time response timers.
-   **Context Injection**: Attach local files or scrape web URLs directly into the conversation.
-   **Surgical Edits**: Apply AI-suggested changes using a robust `REPLACE X-Y` line-range system.
-   **Safety First**: Atomic file writes with one-click "Reject" (rollback) and "Undo" (revert edits + message).
-   **Reasoning Control**: Toggle model reasoning effort (none to high) for complex problem solving.
-   **Persistence**: Per-tab state management ensures your context survives refreshes or disconnects.

## Workflow

### Context Management
-   **Search**: Type 2+ chars in the search bar to find local files. Use arrows to navigate, Enter to attach.
-   **Globs**: Use `*` or `?` (e.g., `src/**/*.py`) and press Enter to batch-attach matching files.
-   **Web**: Paste a URL to scrape its content into Markdown. Uses Playwright for 403-bypass if needed.
-   **Line Numbers**: Files are automatically attached with line numbers to facilitate precise editing.

### AI-Driven Editing
The system uses a specific protocol for modifications:
1.  Ask the AI to "edit" or "rewrite" a file.
2.  The AI responds with `### EDIT <path>` and `#### REPLACE X-Y` blocks.
3.  **Preview**: The app dynamically renders the "original" code inside the AI's response for immediate verification.
4.  **Apply**: Click "Apply edits" to execute changes.
5.  **Revert**: Use "Reject" on a specific file bubble or "Back" to undo the entire transaction.

### Interface Controls
-   **Mode (chat/extract)**: `extract` appends a prompt for structured data output.
-   **Stop**: Immediately halts an active stream and finalizes the buffer.
-   **Back**: Reverts the last exchange and any associated file modifications.
-   **Clear**: Resets the entire session state.
-   **Copy**: Integrated copy buttons on all code blocks (requires HTTPS/localhost for clipboard API).

## Configuration

-   **SSL**: Place `cert.pem` and `key.pem` in the root to enable HTTPS (recommended for clipboard support).
-   **Headless**: Set `AI_CHAT_PLAYWRIGHT_HEADLESS=1` to hide the browser during scraping.
-   **Cache**: Web content is cached at `~/.cache/ai-chat/web`.