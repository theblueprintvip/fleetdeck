  // ---- Full AI review, counter-offers and saved contracts (via src/cloud.js) ----
  const LVL = { high: 'High', medium: 'Medium', low: 'Low' };
  const profile = () => { try { return JSON.parse(store.get('rl-profile') || '{}'); } catch { return {}; } };
  const errMsg = e => (e && e.response && e.response.data && (e.response.data.error || e.response.data.message)) || (e && e.message) || 'Something went wrong.';
  let lastCloud = null;

  async function cloudReview(text, name) {
    const cloud = window.RedlineCloud;
    if (!cloud) return false;
    const p = profile();
    const note = document.getElementById('scanNote');
    if (note) note.textContent = 'Reading every clause against 66 known traps. This takes about 20 seconds.';
    try {
      const data = await cloud.analyze({ text, name, who: p.who, city: p.city, state: p.state, share_stats: store.get('rl-share') !== '0' });
      renderCloud(data, text, name);
    } catch (e) {
      renderResult(scan(text), { name });
      const n = document.createElement('p');
      n.className = 'note';
      n.textContent = e && e.response && e.response.status === 429 ? errMsg(e) : 'The full AI review is unavailable right now, so this is the quick scan.';
      result.prepend(n);
    }
    return true;
  }

  function renderCloud(d, text, name) {
    lastCloud = { d, text: String(text).replace(/\s+/g, ' ') };
    last = { risk: d.risk, traps: d.findings.map(f => ({ label: f.clause })), text: '' };
    const safe = d.risk <= 20;
    const bench = d.benchmarks || {};
    const n = d.findings.length;
    const top = d.findings.slice(0, 3).map(f => f.clause.toLowerCase()).join(', ');
    const desc = n ? `${n} issue${n > 1 ? 's' : ''}: ${top}${n > 3 ? ' and more' : ''}.` : 'No risky clauses found.';
    const dates = d.dates || [];
    result.hidden = false;
    result.innerHTML = `
      <p class="meta">${esc(name)} · ${esc(d.contract_label)}${d.counterparty ? ' · ' + esc(d.counterparty) : ''}</p>
      <div class="risk-row" style="margin-top:16px">
        <div class="risk-num${safe ? ' safe' : ''}">${d.risk}</div>
        <p class="risk-desc"><b>Risk score</b>${esc(desc)}</p>
      </div>
      <div class="bar${safe ? ' safe' : ''}" role="img" aria-label="Risk ${d.risk} out of 100"><i></i></div>
      ${d.truncated ? '<p class="note">This contract is very long, so Redline reviewed the first 60,000 characters.</p>' : ''}
      ${n ? `<div class="traps">${d.findings.map(f => `
        <div class="trap"><h4>${esc(f.clause)}<span class="lvl ${esc(f.risk)}">${LVL[f.risk] || esc(f.risk)}</span></h4>
          <p class="why">${esc(f.explanation)}</p>
          <q>…${esc(f.quote)}…</q>
          <p class="src${f.confidence === 'library' ? '' : ' review'}">${f.confidence === 'library' ? 'Source: ' + esc(f.source) : 'General AI review · review recommended'}${f.library_id && bench[f.library_id] != null ? ` · Seen in ${bench[f.library_id]}% of ${esc(d.contract_label)} reviews` : ''}</p>
          <div class="fix"><b>Safe rewrite</b>${esc(f.rewrite)}</div></div>`).join('')}</div>` : ''}
      ${dates.length ? `<div class="timeline"><h4>Time bomb timeline</h4>${dates.map(t => `<div class="tl"><b>${esc(t.date || t.when)}</b><span>${esc(t.label)}${t.date && t.when ? ' · ' + esc(t.when) : ''}</span></div>`).join('')}</div>` : ''}
      <div class="actions">
        ${n ? '<button class="btn" type="button" data-act="counter">Draft counter-offer</button><button class="btn ghost" type="button" data-act="safe">Download safe version</button>' : ''}
        ${d.signed_in ? '' : '<button class="btn ghost" type="button" data-act="signin">Sign in to save and track deadlines</button>'}
        <button class="btn ghost" type="button" data-act="ask">Ask Tila about this</button>
      </div>
      <div class="counter" id="counterOut"></div>
      <p class="fine">Redline flags common contract risks. It is not legal advice.${d.risk >= 60 && CONFIG.lawyerUrl ? ` <a href="${esc(CONFIG.lawyerUrl)}" target="_blank" rel="noopener">Talk to a lawyer</a>.` : ''}</p>`;
    requestAnimationFrame(() => requestAnimationFrame(() => { result.querySelector('.bar>i').style.width = d.risk + '%'; }));
    const on = (act, fn) => { const b = result.querySelector(`[data-act="${act}"]`); if (b) b.addEventListener('click', fn); };
    on('counter', () => draftCounter(d));
    on('safe', () => download(cloudSafeVersion(), 'redline-safe-version.txt'));
    on('signin', () => window.RedlineCloud.signIn().catch(() => {}));
    on('ask', () => { openTila(); say(answer('my scan'), 'bot'); });
    result.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (d.signed_in) refreshWallet();
  }

  function cloudSafeVersion() {
    let t = lastCloud.text;
    for (const f of lastCloud.d.findings) {
      if (!f.quote_verified) continue;
      const i = t.toLowerCase().indexOf(f.quote.toLowerCase());
      if (i >= 0) t = t.slice(0, i) + f.rewrite + t.slice(i + f.quote.length);
    }
    return t.replace(/\s*(\d+\.\s)/g, '\n\n$1').trim() + '\n';
  }

  async function draftCounter(d) {
    const out = document.getElementById('counterOut');
    out.innerHTML = '<p class="fine">Drafting your reply…</p>';
    try {
      const r = await window.RedlineCloud.counterOffer({
        title: d.title,
        counterparty: d.counterparty,
        tone: 'friendly',
        findings: d.findings.slice(0, 6).map(f => ({ clause: f.clause, quote: f.quote, rewrite: f.rewrite })),
      });
      out.innerHTML = '<label class="sr" for="counterText">Counter-offer email</label><textarea id="counterText"></textarea>'
        + '<div class="actions"><button class="btn" type="button" id="copyCounter">Copy</button><a class="btn ghost" id="mailCounter" href="#">Open in email</a></div>';
      const ta = document.getElementById('counterText');
      ta.value = r.email;
      const m = r.email.match(/^Subject:\s*(.*)\n+([\s\S]*)$/i);
      document.getElementById('mailCounter').href = 'mailto:?subject=' + encodeURIComponent(m ? m[1] : 'Contract changes') + '&body=' + encodeURIComponent(m ? m[2] : r.email);
      const copy = document.getElementById('copyCounter');
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(ta.value); copy.textContent = 'Copied'; } catch { ta.select(); }
      });
    } catch (e) {
      out.innerHTML = `<p class="note">${esc(errMsg(e))}</p>`;
    }
  }

  async function refreshWallet() {
    const cloud = window.RedlineCloud;
    const sec = document.getElementById('wallet');
    if (!cloud || !cloud.user) { sec.hidden = true; return; }
    sec.hidden = false;
    try {
      const [w, s] = await Promise.all([cloud.wallet(), cloud.scans()]);
      const up = w.entries.filter(e => !e.past);
      document.getElementById('walletDates').innerHTML = up.length
        ? up.slice(0, 20).map(e => `<div class="tl"><b>${esc(e.date || 'As written')}</b><span>${esc(e.label)} · ${esc(e.contract)}${e.when ? ' · ' + esc(e.when) : ''}</span></div>`).join('')
        : '<p class="fine">No upcoming deadlines yet. Scan a contract to start tracking.</p>';
      const box = document.getElementById('walletScans');
      box.innerHTML = s.scans.length
        ? s.scans.map(x => `<div class="row"><span>${esc(x.name || x.title)}<br><small class="meta">${esc(x.contract_label || '')} · ${esc(String(x.created_at || '').slice(0, 10))}</small></span><span class="r">${esc(x.risk)}</span><button type="button" data-del="${esc(x.id)}">Remove</button></div>`).join('')
        : '<p class="fine">Nothing saved yet.</p>';
      box.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => {
        if (confirm('Remove this contract and its deadlines?')) cloud.deleteScan(b.dataset.del).then(refreshWallet).catch(() => {});
      }));
    } catch (e) {
      document.getElementById('walletDates').innerHTML = `<p class="note">${esc(errMsg(e))}</p>`;
    }
  }

  const authBtn = document.getElementById('authBtn');
  function syncAuth() {
    const cloud = window.RedlineCloud;
    if (!cloud || !cloud.ready) return;
    authBtn.hidden = false;
    authBtn.textContent = cloud.user ? 'Sign out' : 'Sign in';
    refreshWallet();
  }
  authBtn.addEventListener('click', async () => {
    const cloud = window.RedlineCloud;
    if (!cloud) return;
    try {
      if (cloud.user) await cloud.signOut(); else await cloud.signIn();
    } catch {
      authBtn.textContent = 'Try again';
    }
  });
  addEventListener('redline:cloud', syncAuth);
  syncAuth();

  const shareBox = document.getElementById('shareStats');
  shareBox.checked = store.get('rl-share') !== '0';
  shareBox.addEventListener('change', () => {
    store.set('rl-share', shareBox.checked ? '1' : '0');
    const cloud = window.RedlineCloud;
    const p = profile();
    if (cloud && cloud.user) cloud.saveMe({ who: p.who, city: p.city, state: p.state, share_stats: shareBox.checked }).catch(() => {});
  });

  document.getElementById('pasteGo').addEventListener('click', async () => {
    const text = document.getElementById('pasteText').value;
    const name = 'Pasted contract';
    if (text.trim().length < 80) { renderError(name, 'Paste the full contract text, at least a few sentences.'); return; }
    renderScanning(name);
    await wait(1800);
    if (await cloudReview(text, name)) return;
    renderResult(scan(text), { name });
  });

