// Redline background: context menus, toolbar button, and handing jobs to the review panel.
const SITE = 'https://redline-mpr16m.v2.appdeploy.ai';

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: 'rl-selection', title: 'Redline selected text', contexts: ['selection'] });
  chrome.contextMenus.create({ id: 'rl-page', title: 'Redline this page', contexts: ['page'] });
  chrome.contextMenus.create({ id: 'rl-pdf', title: 'Redline this PDF', contexts: ['link'], targetUrlPatterns: ['*://*/*.pdf', '*://*/*.pdf?*', '*://*/*.PDF'] });
});

// A job is what the panel reviews: text, a PDF URL, or PDF bytes (base64) from Gmail.
async function startJob(job, tabId) {
  await chrome.storage.session.set({ job: { ...job, id: Date.now() } });
  if (tabId != null) {
    try {
      await chrome.tabs.sendMessage(tabId, { type: 'redline:show-panel' });
      return;
    } catch {
      // No content script here (Chrome PDF viewer, store pages): use a tab instead.
    }
  }
  await chrome.tabs.create({ url: chrome.runtime.getURL('panel.html') });
}

function pageText() {
  const sel = String(window.getSelection() || '').trim();
  return { text: sel.length > 200 ? sel : document.body.innerText, title: document.title };
}

// Runs inside the Redline site tab: waits for the paste box, fills it and submits.
function pasteIntoRedline(text) {
  if (window.__redlinePasted) return; // Retries must never submit twice.
  window.__redlinePasted = true;
  const run = () => {
    const box = document.getElementById('pasteText');
    const go = document.getElementById('pasteGo');
    if (!box || !go) return setTimeout(run, 300);
    const details = box.closest('details');
    if (details) details.open = true;
    box.value = text;
    go.click();
  };
  run();
}

async function reviewTab(tab) {
  if (/\.pdf($|[?#])/i.test(tab.url || '')) return startJob({ kind: 'pdf-url', url: tab.url, name: decodeURIComponent((tab.url.split('/').pop() || 'Contract.pdf').split(/[?#]/)[0]) }, null);
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: pageText });
    await startJob({ kind: 'text', text: res.result.text, name: res.result.title || 'This page', source: tab.url }, tab.id);
  } catch {
    await chrome.tabs.create({ url: SITE + '/#scan' });
  }
}

chrome.action.onClicked.addListener(reviewTab);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'rl-selection') startJob({ kind: 'text', text: info.selectionText, name: 'Selected text', source: tab && tab.url }, tab && tab.id);
  if (info.menuItemId === 'rl-page' && tab) reviewTab(tab);
  if (info.menuItemId === 'rl-pdf') startJob({ kind: 'pdf-url', url: info.linkUrl, name: decodeURIComponent((info.linkUrl.split('/').pop() || 'Contract.pdf').split(/[?#]/)[0]) }, tab && tab.id);
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'redline:job') {
    startJob(msg.job, sender.tab && sender.tab.id).then(() => reply({ ok: true }));
    return true;
  }
  // Panel asks to continue on the site: open it and drop the contract into the paste box.
  if (msg && msg.type === 'redline:open-site') {
    chrome.tabs.create({ url: SITE + '/#scan' }, tab => {
      let tries = 0;
      // A new tab reports "complete" for its initial blank page too, so wait for the site itself.
      const onSite = t => t && t.status === 'complete' && String(t.url || '').startsWith(SITE);
      const go = () => {
        chrome.scripting.executeScript({ target: { tabId: tab.id }, args: [msg.text], func: pasteIntoRedline })
          .then(() => chrome.tabs.onUpdated.removeListener(listener))
          // An error page (offline, slow start) can be retried on the next load.
          .catch(() => { if (++tries >= 3) chrome.tabs.onUpdated.removeListener(listener); });
      };
      const listener = (id, _change, t) => { if (id === tab.id && onSite(t)) go(); };
      chrome.tabs.onUpdated.addListener(listener);
      chrome.tabs.get(tab.id, t => { if (onSite(t)) go(); });
    });
    reply({ ok: true });
  }
  return false;
});
