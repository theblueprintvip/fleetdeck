// Redline review panel: reads the job, extracts PDF text locally, calls the Redline API.
const SITE = 'https://redline-mpr16m.v2.appdeploy.ai';
const embedded = location.hash === '#embedded';
const out = document.getElementById('out');
const LVL = { high: 'High', medium: 'Medium', low: 'Low' };
const esc = s => String(s ?? '').replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));

pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdf.worker.min.js';

document.getElementById('close').addEventListener('click', () => {
  if (embedded) parent.postMessage('redline:close', '*');
  else window.close();
});

async function pdfText(data) {
  const doc = await pdfjsLib.getDocument({ data }).promise;
  const pages = [];
  for (let i = 1; i <= Math.min(doc.numPages, 40); i++) {
    const page = await doc.getPage(i);
    pages.push((await page.getTextContent()).items.map(it => it.str).join(' '));
  }
  return pages.join('\n');
}

async function jobText(job) {
  if (job.kind === 'text') return job.text || '';
  if (job.kind === 'pdf-base64') {
    const bin = atob(job.data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return pdfText(bytes);
  }
  if (job.kind === 'pdf-url') {
    const res = await fetch(job.url, { credentials: 'include' });
    if (!res.ok) throw new Error('Could not download that PDF.');
    return pdfText(new Uint8Array(await res.arrayBuffer()));
  }
  return '';
}

function fail(msg, text) {
  out.innerHTML = `<p class="big">${esc(msg)}</p>${text ? '<div class="actions"><button class="btn" type="button" id="site">Open in Redline</button></div>' : ''}`;
  if (text) document.getElementById('site').addEventListener('click', () => openSite(text));
}

function openSite(text) {
  chrome.runtime.sendMessage({ type: 'redline:open-site', text });
}

function render(d, text, name) {
  const safe = d.risk <= 20;
  const n = d.findings.length;
  const desc = n ? `${n} issue${n > 1 ? 's' : ''} found.` : 'No risky clauses found.';
  const dates = d.dates || [];
  out.innerHTML = `
    <p class="meta">${esc(name)} · ${esc(d.contract_label)}</p>
    <div class="row"><div class="num${safe ? ' safe' : ''}">${d.risk}</div><p class="desc"><b>Risk score</b><br>${esc(desc)}</p></div>
    <div class="bar"><i></i></div>
    ${d.findings.map(f => `
      <div class="trap">
        <h3>${esc(f.clause)}<span class="lvl ${esc(f.risk)}">${LVL[f.risk] || esc(f.risk)}</span></h3>
        <p class="why">${esc(f.explanation)}</p>
        <q>…${esc(f.quote)}…</q>
        <p class="src">${f.confidence === 'library' ? 'Source: ' + esc(f.source) : 'General AI review · review recommended'}</p>
        ${f.locked ? '<div class="fix"><b>Safe rewrite</b>Unlock the full review on Redline.</div>' : `<div class="fix"><b>Safe rewrite</b>${esc(f.rewrite)}</div>`}
      </div>`).join('')}
    ${dates.length ? `<h4>Time bomb timeline</h4>${dates.map(t => `<div class="tl"><b>${esc(t.date || t.when)}</b><span>${esc(t.label)}</span></div>`).join('')}` : ''}
    <div class="actions">
      <button class="btn" type="button" id="site">${d.locked ? 'Unlock full review on Redline' : 'Save and draft a counter-offer'}</button>
      ${n ? '<button class="btn ghost" type="button" id="copy">Copy safe rewrites</button>' : ''}
    </div>
    <p class="fine">Redline flags common contract risks. It is not legal advice.</p>`;
  requestAnimationFrame(() => requestAnimationFrame(() => { out.querySelector('.bar>i').style.width = d.risk + '%'; }));
  document.getElementById('site').addEventListener('click', () => openSite(text));
  const copy = document.getElementById('copy');
  if (copy) copy.addEventListener('click', async () => {
    const lines = d.findings.filter(f => f.rewrite).map(f => `${f.clause}\nInstead of: ${f.quote}\nPropose: ${f.rewrite}`);
    try { await navigator.clipboard.writeText(lines.join('\n\n')); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy failed'; }
  });
}

async function run() {
  const { job } = await chrome.storage.session.get('job');
  if (!job) return fail('Nothing to review yet. Select contract text and right-click “Redline selected text”.');
  const name = job.name || 'Contract';
  document.getElementById('name').textContent = name;
  let text = '';
  try {
    text = String(await jobText(job)).replace(/\s+\n/g, '\n').trim();
  } catch (e) {
    return fail(e.message || 'Could not read that file.');
  }
  if (text.length < 80) return fail('There isn’t enough contract text here to review. Try selecting the contract text, or open the PDF.');
  out.querySelector('.big').textContent = 'Checking every clause. About 20 seconds…';
  try {
    const res = await fetch(SITE + '/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text.slice(0, 120000), name, share_stats: true }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return fail(data.error || data.message || 'Redline could not review this right now.', text);
    render(data, text, name);
  } catch {
    fail('Redline is unreachable. Check your connection and try again.', text);
  }
}

run();
