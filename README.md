# AI Chat

A fast, developer-focused AI chat app with file context, web page scraping, and one-click AI-driven edits.

## Run it

1) Requirements
- Python 3.10+
- Env var: OPENROUTER_API_KEY (or OPENAI_API_KEY for direct OpenAI)

2) Install
`pip install -U nicegui httpx openai beautifulsoup4`


3) Start
`python app5.py --port 8080`

- Open http://localhost:8080 (or https if <!--CODE_BLOCK_1039-->/<!--CODE_BLOCK_1040--> exist)
- Dark mode on by default

## What it can do

- Chat with top models via OpenRouter (OpenAI optional)
- Streamed responses with a timer
- Attach local files to give the AI context
- Paste URLs to scrape pages into Markdown and attach automatically
- Apply AI-suggested file edits (EDIT/REWRITE) safely with rollback
- Copy code blocks, Stop/Undo/Clear, and an “extract” mode for structured output
- Per-tab chat state persistence (state maintained on refresh/disconnect)

## How to use

- Typing and sending
  - Enter sends
  - Shift+Enter inserts a newline
- Models and reasoning
  - Pick a model and a reasoning level (none → high) in the header
- Add files
  - Use the “Search files or paste URL...” box
  - Type at least 2 chars to search; arrows to navigate; Enter to attach
  - Attached files are shown as green chips; click × to remove
- Add web pages
  - Paste a URL in the same box and press Enter
  - The page is scraped to Markdown and attached automatically
  - Cached at: ~/.cache/ai-chat/web
- Apply edits
  - Ask the AI to modify files; it will reply with EDIT/REWRITE blocks
  - Click “Apply edits” to apply all changes atomically (with backup)
  - Success/error bubbles show line counts; “Reject” reverts a file
- Controls
  - Stop: halt streaming
  - Back: undo last user message and revert its edits
  - Clear: reset chat and attachments
  - Copy: use the copy icon on any code block
- Modes
  - chat: normal conversation
  - extract: app auto-appends a helper prompt for structured output

## Configuration

- Environment
  - OPENROUTER_API_KEY for OpenRouter (recommended)
  - OPENAI_API_KEY for direct OpenAI (optional)
- SSL (optional)
  - Place cert.pem and key.pem in the project root to enable HTTPS

## Notes

- Paths are resolved relative to directory above that which the app is run from