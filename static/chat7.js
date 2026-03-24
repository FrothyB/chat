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


  const B = {tps: 10, lag: 1000, tick: 33, start: 1000, span: 10, render: 33, drain: 250};
  const ema = (y, z, dt, s=B.span) => y ? y + (1 - (1 - 2 / (s + 1)) ** dt) * (z - y) : z;
  const state = node => node._chat7Stream || (node._chat7Stream = {buf: [], startAt: 0, lastAt: 0, lastTick: 0, rate: 0, started: false, done: false, drainAt: 0, drainLen: 0, drainPos: 0, drainRate: 0, credit: 0, timer: 0});
  const resetStream = node => { const x = state(node); if (x.timer) clearTimeout(x.timer); if (node._chat7Frame) cancelAnimationFrame(node._chat7Frame); if (node._chat7RenderTimer) clearTimeout(node._chat7RenderTimer); node._chat7Frame = 0; node._chat7RenderTimer = 0; node._chat7NextRenderAt = 0; node._chat7Stream = {buf: [], startAt: 0, lastAt: 0, lastTick: 0, rate: 0, started: false, done: false, drainAt: 0, drainLen: 0, drainPos: 0, drainRate: 0, credit: 0, timer: 0}; };
  const live = node => { const x = state(node); return !!((x.startAt && !x.done) || x.buf.length || x.timer); };
  const arm = (node, delay=B.tick) => { const x = state(node); if (!x.timer) x.timer = setTimeout(() => pump(node), delay); };
  const pump = node => {
    const x = state(node), now = performance.now(), dt = x.lastTick ? Math.min(250, now - x.lastTick) : B.tick;
    x.timer = 0;
    x.lastTick = now;
    if (!x.started) {
      if (!x.done) {
        const wait = x.startAt + B.start - now;
        if (wait > 0) return arm(node, wait);
        x.rate = Math.max(B.tps, x.buf.length / (B.start / 1000));
      }
      x.started = true;
      x.lastAt = now;
    }
    if (x.done && x.buf.length) {
      if (!x.drainAt) x.drainAt = now, x.drainLen = x.buf.length, x.drainPos = 0, x.drainRate = x.drainLen / (B.drain / 1000);
      const want = now - x.drainAt >= B.drain ? x.drainLen : Math.floor((now - x.drainAt) * x.drainRate / 1000), n = Math.min(x.buf.length, want - x.drainPos);
      if (!n) return arm(node);
      node._chat7Markdown = (node._chat7Markdown || '') + x.buf.splice(0, n).join('');
      x.drainPos += n;
      schedule(node);
      return x.buf.length ? arm(node) : void 0;
    }
    const rate = Math.max(B.tps, x.rate || 0), target = rate * B.lag / 1000;
    if (x.buf.length) {
      x.credit += rate * dt / 1000 * Math.max(0.25, Math.min(2, x.buf.length / Math.max(1, target)));
      const n = Math.min(x.buf.length, Math.floor(x.credit));
      if (!n) return arm(node);
      x.credit -= n;
      node._chat7Markdown = (node._chat7Markdown || '') + x.buf.splice(0, n).join('');
      schedule(node);
    }
    if (x.buf.length) arm(node);
  };
  const queue = (node, chunk) => {
    const x = state(node), now = performance.now();
    if (!x.startAt) x.startAt = now;
    x.buf.push(chunk);
    x.done = false; x.drainAt = 0; x.drainLen = 0; x.drainPos = 0; x.drainRate = 0;
    if (x.started && x.lastAt) { const dt = Math.max(1e-3, (now - x.lastAt) / 1000); x.rate = ema(x.rate, 1 / dt, dt); }
    if (x.started) x.lastAt = now;
    if (!x.timer) arm(node, x.started ? B.tick : Math.max(0, x.startAt + B.start - now));
  };
  const finish = node => { const x = state(node), now = performance.now(); x.done = true; x.drainAt = 0; x.drainLen = 0; x.drainPos = 0; x.drainRate = 0; if (x.timer) clearTimeout(x.timer), x.timer = 0; if (!x.startAt) x.startAt = now; x.buf.length ? arm(node, 0) : schedule(node); };
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
    if (!node || node._chat7Rendering) return;
    if (node._chat7RenderTimer) clearTimeout(node._chat7RenderTimer), node._chat7RenderTimer = 0;
    const atBottom = sticky(node), active = live(node);
    node._chat7Frame = 0;
    node._chat7Rendering = true;
    node._chat7Dirty = false;
    try {
      node.innerHTML = window.DOMPurify.sanitize(md.render(safe(node._chat7Markdown || '')), {USE_PROFILES: {html: true, svg: true, mathMl: true}});
      await mermaidize(node);
      wrapTables(node);
      decorateLinks(node);
      decorateMedia(node);
      if (!active) bindCodeCopy(node);
      if (atBottom) scrollBottom(node);
    } finally {
      node._chat7NextRenderAt = performance.now() + B.render;
      node._chat7Rendering = false;
      if (node._chat7Dirty) schedule(node);
    }
  };

  const schedule = node => { if (!node) return; node._chat7Dirty = true; if (node._chat7Frame || node._chat7Rendering || node._chat7RenderTimer) return; const run = () => { node._chat7RenderTimer = 0; node._chat7Frame = requestAnimationFrame(() => render(node)); }, dt = Math.max(0, (node._chat7NextRenderAt || 0) - performance.now()); dt ? node._chat7RenderTimer = setTimeout(run, dt) : run(); };

  window.chat7 = {
    setMarkdown: (id, text, now=false) => withNode(id, node => { resetStream(node); node._chat7Markdown = text || ''; now ? render(node) : schedule(node); }),
    appendMarkdown: (id, chunk) => { if (!chunk) return; withNode(id, node => { node._chat7Markdown = (node._chat7Markdown || '') + chunk; schedule(node); }); },
    appendMarkdownBuffered: (id, chunk) => { if (!chunk) return; withNode(id, node => queue(node, chunk)); },
    finishMarkdownBuffered: id => withNode(id, node => finish(node)),
    clearMarkdown: id => withNode(id, node => { resetStream(node); node._chat7Markdown = ''; schedule(node); }),
    renderNow: id => withNode(id, node => render(node)),
    getMarkdown: id => { const node = el(id); return node ? (node._chat7Markdown || '') : ''; },
    copyMarkdown: (id, btnId=null) => copy(window.chat7.getMarkdown(id), btnId),
    scrollBottom: () => scrollBottom(),
  };
})();