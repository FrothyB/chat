STYLE_CSS = '''
<style>
body, .q-page { background: #0b0f14 !important; }
.chat-container::-webkit-scrollbar { width: 8px; }
.chat-container::-webkit-scrollbar-thumb { background: #555; border-radius: 6px; }
.chat-container::-webkit-scrollbar-thumb:hover { background: #666; }
.prose { color: #e2e8f0 !important; }
.prose pre { background-color: #111827; color: #e2e8f0; padding: 0.875rem; border-radius: 0.5rem; overflow-x: auto; }
.prose code { background-color: #1f2937; color: #a8b3cf; padding: 0.875rem; border-radius: 0.5rem; }
.prose pre code { background-color: transparent; color: inherit; padding: 0; }
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
.answer-bubble .q-markdown,.answer-bubble .prose,.user-bubble .q-markdown,.user-bubble .prose{max-width:100%;overflow-wrap:anywhere;word-break:break-word}
.answer-bubble pre,.user-bubble pre,.answer-bubble pre code,.user-bubble pre code,.answer-bubble code,.user-bubble code{white-space:pre-wrap!important;overflow-wrap:anywhere;word-break:break-word}
.file-option.active{background:rgb(55,65,81)} .file-option:hover{background:rgb(45,55,72)}
.no-gap { gap: 0 !important; }
</style>
'''
# /* Remove vertical padding from markdown containers and elements */
# .prose, .q-markdown { padding-top: 0 !important; padding-bottom: 0 !important; }
# .prose > * { padding-top: 0 !important; padding-bottom: 0 !important; }

# /* Collapse extra vertical space between streamed markdown chunks without changing intra-chunk styling */
# .answer-content.no-gap > .q-markdown { margin: 0 !important; padding: 0 !important; }
# .answer-content.no-gap > .q-markdown + .q-markdown { margin-top: 0 !important; }
# .answer-content.no-gap > .q-markdown .prose { margin: 0 !important; }
# .answer-content.no-gap > .q-markdown .prose > :first-child { margin-top: 0 !important; }
# .answer-content.no-gap > .q-markdown .prose > :last-child { margin-bottom: 0 !important; }
# </style>'''
