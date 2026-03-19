(() => {
  if (window.chat7) return;
  const esc = s => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const safe = t => { const n = ((t || '').match(/^\s*```/gm) || []).length; return n % 2 ? (t.endsWith('\n') ? t + '```' : t + '\n```') : (t || ''); };
  const el = id => document.getElementById(id);
  const withNode = (id, f, n=40) => { const node = el(id); if (node) return f(node); if (n > 0) setTimeout(() => withNode(id, f, n - 1), 25); };
  const isDoc = x => !x || x === window || x === document || x === document.body || x === document.documentElement || x === document.scrollingElement;
  const scrollable = x => x instanceof Element && x.scrollHeight > x.clientHeight + 1 && /(auto|scroll|overlay)/.test(getComputedStyle(x).overflowY || '');
  const defaultHost = () => [...document.querySelectorAll('.chat-container')].at(-1) || document.scrollingElement || document.documentElement;
  const scrollHost = node => (node instanceof Element && node.closest('.chat-container')) || (() => { for (let x = node; x; x = x.parentElement) if (scrollable(x)) return x; return defaultHost(); })();
  const sticky = node => { const x = scrollHost(node), d = document.scrollingElement || document.documentElement; return isDoc(x) ? window.innerHeight + window.scrollY >= d.scrollHeight - 160 : x.scrollTop + x.clientHeight >= x.scrollHeight - 160; };
  const scrollBottom = (node=null, n=8) => { const x = scrollHost(node), d = document.scrollingElement || document.documentElement; isDoc(x) ? window.scrollTo({top: d.scrollHeight, behavior: 'auto'}) : x.scrollTop = x.scrollHeight; if (n > 0) requestAnimationFrame(() => scrollBottom(x, n - 1)); };

  const md = window.markdownit({
    html: false,
    linkify: true,
    breaks: true,
    highlight: (s, l) => {
      const c = l ? ` class="hljs language-${l}"` : ' class="hljs"';
      if (!window.hljs) return `<pre><code${l ? ` class="language-${l}"` : ''}>${esc(s)}</code></pre>`;
      try {
        const v = l && window.hljs.getLanguage(l) ? window.hljs.highlight(s, {language: l}).value : window.hljs.highlightAuto(s).value;
        return `<pre><code${c}>${v}</code></pre>`;
      } catch {
        return `<pre><code${l ? ` class="language-${l}"` : ''}>${esc(s)}</code></pre>`;
      }
    },
  }).use(window.texmath, {engine: window.katex, delimiters: 'dollars', katexOptions: {throwOnError: false}}).enable(['table']);

  let mermaidReady = false;
  const initMermaid = () => { if (!window.mermaid || mermaidReady) return; window.mermaid.initialize({startOnLoad: false, securityLevel: 'loose', theme: 'dark', flowchart: {htmlLabels: true}}); mermaidReady = true; };

  const copy = async (text, btnId=null) => {
    try {
      await navigator.clipboard.writeText(text || '');
      const btn = btnId ? el(btnId) : null;
      if (btn) { btn.classList.add('copied'); setTimeout(() => btn.classList.remove('copied'), 1000); }
    } catch (e) { console.error(e); }
  };

  const bindCodeCopy = root => {
    root.querySelectorAll('pre').forEach(pre => {
      if (pre.querySelector('.code-copy-btn')) return;
      const code = pre.querySelector('code'); if (!code) return;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.title = 'Copy code';
      btn.setAttribute('aria-label', 'Copy code');
      btn.className = 'code-copy-btn tool-btn copy-icon';
      btn.innerHTML = '<span class="material-icons">content_copy</span>';
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        await copy(code.textContent || '');
        btn.classList.add('copied');
        setTimeout(() => btn.classList.remove('copied'), 1000);
      });
      pre.appendChild(btn);
    });
  };


  const mermaidize = async root => {
    const nodes = [];
    root.querySelectorAll('pre > code').forEach(code => {
      if (!/\blanguage-mermaid\b/.test(code.className || '')) return;
      const box = document.createElement('div');
      box.className = 'mermaid';
      box.textContent = code.textContent || '';
      code.parentElement.replaceWith(box);
      nodes.push(box);
    });
    if (!nodes.length || !window.mermaid) return;
    try { initMermaid(); await window.mermaid.run({nodes}); } catch (e) { console.error(e); }
  };

  const wrapTables = root => {
    root.querySelectorAll('table').forEach(table => {
      if (table.parentElement?.classList.contains('chat7-table-wrap')) return;
      const wrap = document.createElement('div');
      wrap.className = 'chat7-table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    });
  };

  const decorateLinks = root => {
    root.querySelectorAll('a[href]').forEach(a => {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    });
  };

  const decorateMedia = root => {
    root.querySelectorAll('img').forEach(img => { img.loading = 'lazy'; });
  };

  const render = async node => {
    if (!node) return;
    const atBottom = sticky(node);
    node._chat7Timer = 0;
    node._chat7Dirty = false;
    node.innerHTML = window.DOMPurify.sanitize(md.render(safe(node._chat7Markdown || '')), {USE_PROFILES: {html: true, svg: true, mathMl: true}});
    await mermaidize(node);
    wrapTables(node);
    decorateLinks(node);
    decorateMedia(node);
    bindCodeCopy(node);
    if (atBottom) scrollBottom(node);
    if (node._chat7Dirty && !node._chat7Timer) node._chat7Timer = setTimeout(() => render(node), 50);
  };

  const schedule = node => { if (!node) return; node._chat7Dirty = true; if (!node._chat7Timer) node._chat7Timer = setTimeout(() => render(node), 50); };

  window.chat7 = {
    setMarkdown: (id, text, now=false) => withNode(id, node => { node._chat7Markdown = text || ''; now ? render(node) : schedule(node); }),
    appendMarkdown: (id, chunk) => { if (!chunk) return; withNode(id, node => { node._chat7Markdown = (node._chat7Markdown || '') + chunk; schedule(node); }); },
    clearMarkdown: id => withNode(id, node => { node._chat7Markdown = ''; schedule(node); }),
    renderNow: id => withNode(id, node => render(node)),
    getMarkdown: id => { const node = el(id); return node ? (node._chat7Markdown || '') : ''; },
    copyMarkdown: (id, btnId=null) => copy(window.chat7.getMarkdown(id), btnId),
    scrollBottom: () => scrollBottom(),
  };
})();