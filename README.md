# AI Chat Application

A modern, web-based AI chat interface built with [NiceGUI](https://nicegui.io/) and integrated with advanced language models via OpenRouter (or OpenAI). This application supports interactive conversations, file attachments (local or via URL scraping), reasoning-enabled responses, and automatic file editing based on AI-generated markdown directives. It's designed for developers, enabling seamless code review, generation, and modification workflows.

## Features

- **Interactive Chat**: Stream real-time responses from top AI models with optional reasoning traces (e.g., chain-of-thought).
- **File Handling**: Search and attach local files (e.g., code in Python, C++, JS) or scrape web pages into markdown files.
- **Edit Automation**: Parse AI responses for `EDIT` or `REWRITE` directives in markdown format and apply changes atomically to files, with rollback support.
- **UI Enhancements**: Dark theme, copy buttons for code blocks, timers for response duration, undo/clear actions, and keyboard shortcuts.
- **Modes**: Switch between standard chat and "extract" mode (appends a prompt for structured output).
- **Persistence**: Chat state stored in browser tabs; supports SSL for secure deployment.
- **Error Handling**: Robust parsing, notifications for failures (e.g., file not found, edit errors), and safe rollbacks.

### Supported Models
The app integrates with OpenRouter for model access. Here's a table of default supported models:

| Model ID                  | Provider    | Description                          | Strengths                     |
|---------------------------|-------------|--------------------------------------|-------------------------------|
| openai/gpt-5             | OpenAI     | Flagship model (hypothetical/next-gen) | General reasoning, creativity |
| openai/gpt-5-mini        | OpenAI     | Lightweight version                  | Speed, cost-efficiency        |
| anthropic/claude-4.5-sonnet | Anthropic | Advanced conversational AI           | Safety, nuanced responses     |
| x-ai/grok-4-fast         | xAI        | Fast, witty model                    | Humor, quick tasks            |
| openai/gpt-5-pro         | OpenAI     | Pro variant (enhanced capabilities)  | Complex problem-solving       |
| openai/gpt-oss-120b      | OpenAI     | Open-source large model              | Customizable, high capacity   |

*Note*: Set `OPENROUTER_API_KEY` env var for access. OpenAI direct support via `OPENAI_API_KEY` (fallback).

### Reasoning Levels
Control the depth of AI "thinking" before final responses. Higher levels allocate more tokens for reasoning.

| Level    | Token Limit | Use Case                          |
|----------|-------------|-----------------------------------|
| none     | 0          | Direct responses (fastest)        |
| minimal  | 1024       | Basic step-by-step for simple queries |
| low      | 2048       | Light reasoning for code reviews  |
| medium   | 4096       | Balanced for most tasks           |
| high     | 16384      | Deep analysis (e.g., complex edits)|

### File Search and Attachment
- **Local Files**: Searches recursively in project dirs (e.g., code files with whitelisted extensions like `.py`, `.js`, `.md`).
- **URLs**: Paste URLs to scrape HTML content (using BeautifulSoup), clean it, and save as markdown (cached in `~/.cache/ai-chat/web/`).
- **Limits**: Up to 10 search results; attached files are included in prompts.

## Installation

1. **Prerequisites**:
   - Python 3.10+.
   - Install dependencies: `pip install nicegui httpx openai beautifulsoup4`.
   - Set environment variables:
     - `OPENROUTER_API_KEY`: For OpenRouter models (required).
     - `OPENAI_API_KEY`: Optional for direct OpenAI access.
   - For SSL (optional): Generate `cert.pem` and `key.pem` (self-signed or CA-issued).

2. **Project Structure**:
   