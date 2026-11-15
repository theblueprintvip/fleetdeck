import json, sys
root = sys.argv[1]
p = f'{root}/index.html'
s = open(p, encoding='utf-8').read()
cloud_ui = open(f'{root}/appdeploy/patches/cloud-ui.js', encoding='utf-8').read()
onboard = open(f'{root}/appdeploy/patches/onboard.js', encoding='utf-8').read()

CSS = """  .nav-actions{display:flex;align-items:center;gap:10px}
  .btn.sm{min-height:40px;padding:0 16px;font-size:14px}
  @media(max-width:420px){.nav .cta-scan{display:none}}
  .lvl{display:inline-block;margin-left:8px;font:500 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;padding:3px 8px;border-radius:999px;vertical-align:middle}
  .lvl.high{background:#F3D9D3;color:var(--red-text)}
  .lvl.medium{background:#F1E6CF;color:#7A5A16}
  .lvl.low{background:#E6EEE8;color:#2F5B3E}
  .src{margin-top:10px;font:400 12px/1.5 var(--mono);color:var(--muted)}
  .src.review{color:#7A5A16}
  .note{margin-top:16px;padding:12px 14px;border-radius:12px;background:#F1E6CF;font-size:14px}
  .timeline{margin-top:32px;border-top:1px solid var(--line);padding-top:24px}
  .timeline h4,.wallet h3{margin:0 0 12px;font:600 17px var(--sans)}
  .tl{display:flex;gap:14px;padding:10px 0;border-bottom:1px dashed var(--line);font-size:15px}
  .tl b{min-width:110px;font:500 13px var(--mono);color:var(--ink)}
  .tl span{color:var(--muted);overflow-wrap:anywhere;min-width:0}
  .counter textarea,.paste textarea{width:100%;min-height:220px;margin-top:16px;border:1px solid var(--line);border-radius:16px;padding:14px;font:400 14px/1.6 var(--mono);background:#fff;color:var(--ink);resize:vertical}
  .paste textarea{min-height:160px}
  .paste summary{list-style:none}
  .fine{margin-top:20px;font-size:13px;color:var(--muted)}
  .wallet{display:grid;grid-template-columns:1fr;gap:40px}
  @media(min-width:1024px){.wallet{grid-template-columns:1fr 1fr}}
  .row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);font-size:15px}
  .row span:first-child{min-width:0;overflow-wrap:anywhere}
  .row .r{font:600 20px var(--serif);color:var(--red-text)}
  .row button{background:none;border:0;color:var(--muted);cursor:pointer;font-size:13px;text-decoration:underline}
  .chips-pick{display:flex;flex-wrap:wrap;gap:8px}
  .pick{border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 14px;font-size:14px;cursor:pointer}
  .pick:hover{border-color:var(--red);color:var(--red-text)}
  .ob{display:flex;flex-direction:column;gap:10px;white-space:normal}
  .ob .two{display:flex;gap:8px}
  .ob input{min-height:44px;border:1px solid var(--line);border-radius:12px;padding:0 12px;background:#fff;font-size:16px;min-width:0}
  .ob .two input:first-child{flex:1}
  .ob .two input:last-child{width:96px}
  .ob .err{color:var(--red-text);font-size:13px}
  .ob .go{align-self:flex-start;min-height:40px;border:0;border-radius:999px;background:var(--ink);color:var(--paper);padding:0 18px;font-weight:600;cursor:pointer}
"""

pairs = [
  ("  [hidden]{display:none!important}\n", CSS + "  [hidden]{display:none!important}\n"),
  ('    <a class="btn" href="#scan">Scan a contract</a>\n  </div>\n</header>',
   '    <div class="nav-actions">\n      <button class="btn ghost sm" id="authBtn" type="button" hidden>Sign in</button>\n      <a class="btn sm cta-scan" href="#scan">Scan a contract</a>\n    </div>\n  </div>\n</header>'),
  ('          <button class="linkbtn" type="button" data-sample>No contract handy? Scan a sample.</button>\n        </div>',
   '          <button class="linkbtn" type="button" data-sample>No contract handy? Scan a sample.</button>\n'
   '          <details class="paste">\n            <summary class="linkbtn">Paste contract text instead</summary>\n'
   '            <label class="sr" for="pasteText">Contract text</label>\n'
   '            <textarea id="pasteText" placeholder="Paste the full contract here"></textarea>\n'
   '            <button class="btn sm" type="button" id="pasteGo" style="margin-top:12px">Review this text</button>\n'
   '          </details>\n        </div>'),
  ('  <section id="channels" class="dark">',
   '  <section id="wallet" hidden style="padding-top:0">\n    <div class="wrap">\n      <div class="head">\n'
   '        <p class="eyebrow">Your contracts</p>\n        <h2 style="margin-top:12px">Every deadline, in one place.</h2>\n      </div>\n'
   '      <div class="wallet">\n'
   '        <div class="card"><h3>Coming up</h3><div id="walletDates"><p class="fine">Scan a contract and its renewals, notice deadlines and payment dates show up here.</p></div></div>\n'
   '        <div class="card"><h3>Saved contracts</h3><div id="walletScans"></div>\n'
   '          <label class="fine" style="display:flex;gap:8px;align-items:flex-start"><input type="checkbox" id="shareStats" checked> Share anonymous clause stats (never your contract text) to power Redline benchmarks</label>\n'
   '        </div>\n      </div>\n    </div>\n  </section>\n\n  <section id="channels" class="dark">'),
  ("    checkoutUrl: '',\n  };", "    checkoutUrl: '',\n    lawyerUrl: '',\n  };"),
  ('      <p style="font:600 28px var(--serif)">Scanning for 14 traps…</p><div class="bar"><i></i></div></div>`;',
   '      <p style="font:600 28px var(--serif)">Scanning for traps…</p><p class="meta" id="scanNote"></p><div class="bar"><i></i></div></div>`;'),
  ("      if (String(text).trim().length < 40) return renderError(name, 'This PDF has no readable text. It may be a photo or a scan; try the original file.');\n      renderResult(scan(text), { name });",
   "      if (String(text).trim().length < 40) return renderError(name, 'This PDF has no readable text. It may be a photo or a scan; try the original file.');\n      if (await cloudReview(text, name)) return;\n      renderResult(scan(text), { name });"),
  ("  // ---- Upload → 1.8s auto-scan ----\n", cloud_ui + "  // ---- Upload → 1.8s auto-scan ----\n"),
  ("    if (!greeted) { greeted = true; say(t.greet, 'bot'); }",
   "    if (!greeted) { greeted = true; if (profile().who) say(t.greet, 'bot'); else onboard(); }"),
  ("  function say(text, who) {\n", onboard + "  function say(text, who) {\n"),
  ("</script>\n</body>", '</script>\n<script type="module" src="./src/cloud.js"></script>\n</body>'),
]
for old, new in pairs:
    n = s.count(old)
    assert n == 1, (n, old[:70])
    s = s.replace(old, new)
open(p, 'w', encoding='utf-8').write(s)
json.dump([{"from": o, "to": n} for o, n in pairs], open(f'{root}/appdeploy/patches/index-diffs.json', 'w'), ensure_ascii=False, indent=1)
print('applied', len(pairs), 'edits; index.html now', len(s.encode()), 'bytes')
