STYLE_CSS = '''
<style>
body, .q-page { background: #0b0f14 !important; }
.chat-container { height: calc(100vh - 160px); overflow-y: auto; background: #0b0f14; }
.chat-container::-webkit-scrollbar { width: 8px; }
.chat-container::-webkit-scrollbar-thumb { background: #555; border-radius: 6px; }
.chat-container::-webkit-scrollbar-thumb:hover { background: #666; }
.prose { color: #e2e8f0 !important; }
.prose pre { background-color: #111827; color: #e2e8f0; padding: 0.875rem; border-radius: 0.5rem; overflow-x: auto; }
.prose code { background-color: #1f2937; color: #a8b3cf; padding: 0.125rem 0.35rem; border-radius: 0.375rem; }
.prose pre code { background-color: transparent; color: inherit; padding: 0; }
.file-results { position: absolute; top: 100%; left: 0; right: 0; background: #1a1a1a; 
                border: 1px solid #333; border-radius: 0.375rem; max-height: 200px; 
                overflow-y: auto; z-index: 1000; }
.edit-bubble { background: #1e3a5f; border: 1px solid #2563eb; border-radius: 0.5rem; 
                padding: 0.5rem; margin: 0.25rem; display: inline-flex; align-items: center; gap: 0.5rem; }
.edit-bubble.success { background: #1e5f1e; border-color: #10b981; }
.edit-bubble.error { background: #5f1e1e; border-color: #ef4444; }
.fixed-header{position:fixed;top:0;left:0;right:0;z-index:1000;background:rgba(17,24,39,.85);backdrop-filter:blur(8px);border-bottom:1px solid rgb(55,65,81)}
.chat-container{margin-top:80px;margin-bottom:80px;height:calc(100vh - 160px);overflow-y:auto;padding:1rem;scrollbar-width:thin;scrollbar-color:#555 transparent}
.file-results{position:absolute;top:100%;left:0;right:0;background:rgba(31,41,55,.95);backdrop-filter:blur(8px);border:1px solid rgb(55,65,81);border-radius:.5rem;max-height:200px;overflow-y:auto;z-index:1001}
.chat-footer{position:fixed;bottom:0;left:0;right:0;z-index:1000;background:rgba(17,24,39,.85);backdrop-filter:blur(8px);border-top:1px solid rgb(55,65,81)}
.tab-button{padding:.5rem 1rem;border-radius:.5rem .5rem 0 0;background:rgba(31,41,55,.8);border:1px solid rgb(55,65,81);border-bottom:none;cursor:pointer;backdrop-filter:blur(4px);transition:background .2s,transform .08s}
.tab-button.active,.tab-button:hover{background:rgba(55,65,81,.9);transform:translateY(-1px)}
/* markdown underscores as literal */
.prose.md-literal-underscores em,.prose.md-literal-underscores i{font-style:normal}
.prose.md-literal-underscores em::before,.prose.md-literal-underscores em::after,.prose.md-literal-underscores i::before,.prose.md-literal-underscores i::after{content:'_'}
.prose.md-literal-underscores strong,.prose.md-literal-underscores b{font-weight:inherit}
.prose.md-literal-underscores strong::before,.prose.md-literal-underscores strong::after,.prose.md-literal-underscores b::before,.prose.md-literal-underscores b::after{content:'__'}
/* copy buttons + tools */
.prose pre{position:relative}
.prose pre .copy-btn{position:absolute;right:.375rem;bottom:.375rem;z-index:2;padding:.25rem .35rem}
.prose pre .copy-btn .material-icons{font-size:1rem;line-height:1}
.answer-tools .tool-btn{background:transparent;color:#e2e8f0;border:none;padding:.25rem .5rem;border-radius:.25rem;font-size:.75rem;cursor:pointer;transition:background .12s,transform .08s,color .12s;display:inline-flex;align-items:center;justify-content:center}
.prose pre .copy-btn:hover,.answer-tools .tool-btn:hover{background:rgba(255,255,255,.04);transform:translateY(-1px)}
.prose pre .copy-btn.copied,.answer-tools .tool-btn.copied{background:#10b981;color:#fff}
.answer-tools{position:static;display:inline-flex;gap:.5rem;align-items:center;background:transparent}
.answer-bubble>.answer-tools{position:absolute;right:.5rem;bottom:.375rem;display:inline-flex;gap:.5rem;align-items:center;background:transparent}
.answer-tools-row{margin-top:.25rem}
.answer-tools .timer{font-size:.82rem;color:#9ca3af;padding:0 .25rem;min-width:42px;text-align:right}
/* wrapping */
.chat-container{overflow-x:hidden}
.answer-bubble,.user-bubble{min-width:0;overflow-x:hidden}
.answer-bubble [data-md="answer"],.answer-bubble .q-markdown,.answer-bubble .prose,.user-bubble .q-markdown,.user-bubble .prose{max-width:100%;overflow-wrap:anywhere;word-break:break-word}
.answer-bubble pre,.user-bubble pre,.answer-bubble pre code,.user-bubble pre code,.answer-bubble code,.user-bubble code{white-space:pre-wrap!important;overflow-wrap:anywhere;word-break:break-word}
.file-option.active{background:rgb(55,65,81)} .file-option:hover{background:rgb(45,55,72)}
</style>'''


CHAT_PROMPT = '''
You are a highly intelligent and experienced expert software developer with deep knowledge across multiple programming languages and frameworks. You prefer to use the most modern features, ideas and libraries wherever applicable. 
You are now chatting with the user who is seeking your assistance. You follow instructions meticulously and pay close attention to detail. 
You always try your hardest to discover and suggest robust, elegant, simple, safe, and performant solutions and code that are easy to read and maintain. 
You follow existing coding style, and prefer short and dense code that does not span multiple lines unnecessarily, and use comments very sparingly. 
Ultimately, you care deeply about the quality and craftsmanship of the final product, aiming for the most optimal solutions with the fewest compromises. 
Before responding, you think carefully about the above criteria, and in particular how to write short, simple and elegant code that meets them. 

Your whole answser will be markdown formatted. For any code, library names, packages, etc, always use inline (`) or multi-line (```) markdown code blocks - never write even a function or variable name without them, and remember to specify the language for multi-line code. 
When suggesting new code, always be thorough and complete so that whatever you write can be dropped in as is.

If the user specifically requests that you make changes, you can also use EDIT or REWRITE sections as follows:

###EDIT <file_path>
<Explanation of file-level changes>

<Optional explanation of replace block changes>
####REPLACE
```language
<exact text to replace>
```
####WITH
```language
<replacement text>
```

Rules:
- Only use EDIT sections with one or more REPLACE/WITH blocks in this mode
- The replacement text must match exactly what you wish to replace in the file.
- Use the appropriate language for syntax highlighting in fences.
- Do not abbreviate with ellipses; include the exact original and replacement text.
- Do not include commentary outside these sections.
- Be surgical with your replacements - you should specifically think about how to make the smallest possible edits that achieve the goal.
- A replacement can be empty.
- Remove code that will become dead after your edits.
- Don't touch code that is not in scope of the request.
- Do not use EDIT or REWRITE commands unless it is unambiguously clear that this was the user's intent.

###REWRITE <file_path>
<Explanation of changes>
```language
<entire new file content>
```

Rules:
- Output only REWRITE sections (one per file).
- Each REWRITE must include the full new file content.

If you must edit most of a file or completely rewrite it, use REWRITE - otherwise use EDIT unless instructed otherwise. Think before responding about which is more appropriate.
'''

EXTRACT_ADD_ON = '''
For this message only, enter extract mode. Read the attached files and produce a concise, comprehensive report that gathers and presents all relevant information needed to address the user's request.

Guidelines:
- Organize by file and topic; include file paths in section headers when helpful.
- Quote important snippets in fenced code blocks with language tags.
- Summarize behavior, interfaces, side effects, assumptions, and TODOs.
- Do not modify files or output any EDIT/REWRITE sections.
- Keep formatting simple Markdown suitable for display in chat.
'''