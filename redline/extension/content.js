// Redline content script: spots contract-like pages, adds Gmail attachment buttons,
// and shows the review drawer. Kept small; the work happens in panel.html.
(() => {
  if (window.__redline) return;
  window.__redline = true;

  const LEGAL = /\b(agreement|hereby|herein|hereunder|indemnif\w*|liabilit\w*|governing law|arbitration|terminat\w*|warrant\w*|confidential\w*|terms of (service|use)|licen[cs]e[ds]?|shall|party|parties)\b/gi;
  const DISMISS = 'redline-dismissed:' + location.hostname;

  // Enough legal vocabulary, densely enough, to be worth a look.
  function looksLikeContract(text) {
    if (!text || text.length < 1500) return false;
    const hits = (text.match(LEGAL) || []).length;
    return hits >= 15 && hits / (text.length / 1000) >= 2.5;
  }

  function host(id) {
    const el = document.createElement('div');
    el.id = id;
    const root = el.attachShadow({ mode: 'closed' });
    document.documentElement.appendChild(el);
    return { el, root };
  }

  const STYLE = `
    :host{all:initial}
    .pill{position:fixed;left:20px;bottom:20px;z-index:2147483646;display:flex;align-items:center;gap:10px;background:#14171B;color:#F3F1EC;border-radius:999px;padding:10px 12px 10px 16px;font:600 14px/1 Inter,system-ui,sans-serif;box-shadow:0 10px 30px rgba(20,23,27,.3);cursor:pointer}
    .pill b{color:#D6402A;font-weight:700}
    .pill button{all:unset;cursor:pointer;color:#B9BCC0;font-size:13px;padding:2px 6px}
    .gm{all:unset;cursor:pointer;margin-left:6px;background:#14171B;color:#F3F1EC;border-radius:999px;padding:3px 9px;font:600 11px/1.4 Inter,system-ui,sans-serif}
    .drawer{position:fixed;top:0;right:0;height:100vh;width:min(420px,100vw);z-index:2147483647;box-shadow:-12px 0 40px rgba(20,23,27,.25);background:#F3F1EC}
    .drawer iframe{border:0;width:100%;height:100%;display:block}
  `;

  let drawer = null;
  function showPanel() {
    if (drawer) { drawer.el.remove(); drawer = null; }
    drawer = host('redline-drawer');
    drawer.root.innerHTML = `<style>${STYLE}</style><div class="drawer"><iframe title="Redline review" allow="clipboard-write"></iframe></div>`;
    drawer.root.querySelector('iframe').src = chrome.runtime.getURL('panel.html') + '#embedded';
  }

  window.addEventListener('message', e => {
    if (!drawer || e.data !== 'redline:close') return;
    const frame = drawer.root.querySelector('iframe');
    if (frame && e.source === frame.contentWindow) { drawer.el.remove(); drawer = null; }
  });

  chrome.runtime.onMessage.addListener(msg => {
    if (msg && msg.type === 'redline:show-panel') showPanel();
  });

  const send = job => chrome.runtime.sendMessage({ type: 'redline:job', job });

  // Floating "Redline this" pill on pages that read like a contract.
  let pill = null;
  let looks = 0;
  function offerPill() {
    if (pill || sessionStorage.getItem(DISMISS) || location.hostname === 'mail.google.com') return;
    if (++looks > 5) return; // Reading the whole page is costly; stop after a few tries.
    if (!looksLikeContract(document.body ? document.body.innerText : '')) return;
    pill = host('redline-pill');
    pill.root.innerHTML = `<style>${STYLE}</style><div class="pill" role="button" tabindex="0" aria-label="Review this page with Redline">Redline this <b>&#9873;</b><button aria-label="Dismiss">&#x2715;</button></div>`;
    const p = pill.root.querySelector('.pill');
    const run = () => send({ kind: 'text', text: document.body.innerText, name: document.title || location.hostname, source: location.href });
    p.addEventListener('click', e => {
      if (e.target.closest('button')) {
        sessionStorage.setItem(DISMISS, '1');
        pill.el.remove();
        return;
      }
      run();
    });
    p.addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
  }

  // Gmail: a Redline button beside each PDF attachment.
  function gmailButtons() {
    if (location.hostname !== 'mail.google.com') return;
    document.querySelectorAll('span[download_url]:not([data-redline])').forEach(span => {
      span.setAttribute('data-redline', '1');
      const parts = String(span.getAttribute('download_url')).split(':');
      const mime = parts[0];
      const name = parts[1] || 'Attachment.pdf';
      const url = parts.slice(2).join(':');
      if (!/pdf/i.test(mime) || !url.startsWith('https://mail.google.com/')) return;
      const { el, root } = host('redline-gm');
      el.style.display = 'inline-block';
      span.after(el);
      root.innerHTML = `<style>${STYLE}</style><button class="gm" title="Review ${name.replace(/"/g, '')} with Redline">Redline</button>`;
      root.querySelector('button').addEventListener('click', async e => {
        e.preventDefault();
        e.stopPropagation();
        const btn = e.currentTarget;
        btn.textContent = 'Reading…';
        try {
          const res = await fetch(url, { credentials: 'include' });
          const buf = new Uint8Array(await res.arrayBuffer());
          if (buf.length > 7 * 1024 * 1024) throw new Error('too big');
          let bin = '';
          for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
          await send({ kind: 'pdf-base64', data: btoa(bin), name });
          btn.textContent = 'Redline';
        } catch {
          btn.textContent = 'Open it, then try again';
        }
      });
    });
  }

  let timer = null;
  const check = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { offerPill(); gmailButtons(); }, 1200);
  };
  check();
  new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true });
})();
