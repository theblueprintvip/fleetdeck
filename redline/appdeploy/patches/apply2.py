import json, sys
root = sys.argv[1]
billing = open(f'{root}/appdeploy/patches/billing-ui.js', encoding='utf-8').read()

PLANS = '''          <p class="note" id="planNote" hidden></p>
          <p class="fine" id="planStatus" hidden></p>
          <button class="linkbtn" id="manageBilling" type="button" hidden>Manage subscription</button>
          <div class="plans">
            <div class="card plan">
              <h3>Free</h3>
              <div class="price">$0</div>
              <ul><li>Risk score and every flag</li><li>Plain-English explanations</li><li>The first safe rewrite</li><li>Deadline timeline</li></ul>
              <a class="btn ghost" href="#scan">Start scanning</a>
            </div>
            <div class="card plan">
              <h3>Single review</h3>
              <div class="price">$9<small> one contract</small></div>
              <ul><li>Every safe rewrite</li><li>Counter-offer email</li><li>Saved with deadline tracking</li></ul>
              <button class="btn ghost" type="button" data-buy="single">Buy one review</button>
            </div>
            <div class="card plan">
              <h3>3-Pack</h3>
              <div class="price">$19<small> $6.33 each</small></div>
              <ul><li>Three full reviews</li><li>A lease, an offer and a deal</li><li>Never expire</li></ul>
              <button class="btn" type="button" data-buy="pack3">Get 3-Pack</button>
            </div>
            <div class="card plan">
              <h3>10-Pack</h3>
              <div class="price">$39<small> $3.90 each</small></div>
              <ul><li>Ten full reviews</li><li>For agents, managers and freelancers</li><li>Never expire</li></ul>
              <button class="btn ghost" type="button" data-buy="pack10">Get 10-Pack</button>
            </div>
            <div class="card plan pro">
              <h3>Pro</h3>
              <div class="price">$19<small> /month</small></div>
              <ul><li>Unlimited full reviews</li><li>Unlimited counter-offers</li><li>Contract wallet for every deadline</li><li>Or $144 a year (save 37%)</li></ul>
              <button class="btn light" type="button" data-buy="pro_year">Go Pro yearly</button>
              <button class="btn ghost" type="button" data-buy="pro_month" style="color:var(--paper);border-color:#3A3F45">Monthly</button>
            </div>
          </div>'''

OLD_PLANS_REMOTE = '''          <div class="plans">
            <div class="card plan">
              <h3>Free</h3>
              <div class="price">$0<small> /month</small></div>
              <ul>
                <li>3 scans a month</li>
                <li>All 14 traps</li>
                <li>Text and email scans</li>
              </ul>
              <a class="btn ghost" href="#scan">Start scanning</a>
            </div>
            <div class="card plan pro">
              <h3>Pro</h3>
              <div class="price">$19<small> /month</small></div>
              <ul>
                <li>Unlimited scans</li>
                <li>Unlimited safe drafts</li>
                <li>Country notes for 195 countries</li>
              </ul>
              <a class="btn light" href="#pay">Go Pro</a>
            </div>
            <div class="card plan">
              <h3>Team</h3>
              <div class="price">$49<small> /month</small></div>
              <ul>
                <li>Everything in Pro</li>
                <li>5 seats, shared history</li>
                <li>Priority support</li>
              </ul>
              <a class="btn ghost" href="#pay">Choose Team</a>
            </div>
          </div>'''

common = [
  ("""          <div class="fix"><b>Safe rewrite</b>${esc(f.rewrite)}</div></div>`).join('')}</div>` : ''}""",
   """          ${f.locked ? '<div class="fix"><b>Safe rewrite</b><button class="linkbtn" type="button" data-act="unlock">Unlock the safe rewrite</button></div>' : `<div class="fix"><b>Safe rewrite</b>${esc(f.rewrite)}</div>`}</div>`).join('')}</div>` : ''}"""),
  ("""        ${n ? '<button class="btn" type="button" data-act="counter">Draft counter-offer</button><button class="btn ghost" type="button" data-act="safe">Download safe version</button>' : ''}""",
   """        ${n ? (d.locked ? '<button class="btn" type="button" data-act="unlock">Unlock full review</button>' : '<button class="btn" type="button" data-act="counter">Draft counter-offer</button><button class="btn ghost" type="button" data-act="safe">Download safe version</button>') : ''}"""),
  ("    on('ask', () => { openTila(); say(answer('my scan'), 'bot'); });\n",
   "    on('ask', () => { openTila(); say(answer('my scan'), 'bot'); });\n    result.querySelectorAll('[data-act=\"unlock\"]').forEach(b => b.addEventListener('click', () => unlockReview(d)));\n"),
  ("        title: d.title,\n        counterparty: d.counterparty,\n",
   "        title: d.title,\n        counterparty: d.counterparty,\n        scan_id: d.scan_id,\n"),
  ("  function cloudSafeVersion() {\n", billing + "  function cloudSafeVersion() {\n"),
]

# Local file: same edits plus its own (unformatted) pricing block.
p = f'{root}/index.html'
s = open(p, encoding='utf-8').read()
local_old_plans = s[s.index('      <div class="plans">'):s.index('</a></div>\n      </div>\n    </div>\n  </section>', s.index('Choose Team')) + len('</a></div>\n      </div>')]
local = common + [(local_old_plans, PLANS)]
for old, new in local:
    assert s.count(old) == 1, ('local', s.count(old), old[:70])
    s = s.replace(old, new)
open(p, 'w', encoding='utf-8').write(s)

remote = common + [(OLD_PLANS_REMOTE, PLANS)]
json.dump([{"from": o, "to": n} for o, n in remote], open(f'{root}/appdeploy/patches/index-diffs-2.json', 'w'), ensure_ascii=False)
print('local edits:', len(local), '| remote diffs:', len(remote))
