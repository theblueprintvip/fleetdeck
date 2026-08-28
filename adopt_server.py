#!/usr/bin/env python3
"""fleetdeck adopt — the board's install surface.

One field. Paste a repo URL, read what it found, press Adopt.

The page is deliberately two buttons apart from a one-click installer. GET the
form, POST /api/inspect to clone-and-read, and only a second POST — /api/adopt,
carrying the plan you were shown — executes anything. See adopt.py for why that
split is the security model rather than a nicety.

Same posture as the rest of fleetdeck: binds loopback, fronted by
`tailscale serve`, tailnet peers only, no password. That last part is exactly
why the gate exists — anything that can reach this page can install software on
this machine, so it must not be able to do it without a human reading the plan
first.
"""

import ipaddress
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adopt as engine  # noqa: E402

CFG = engine.load(os.path.join(HERE, "config.json"), {})
PORT = int(os.environ.get("FLEETDECK_ADOPT_PORT",
                          (CFG.get("ports") or {}).get("adopt", 8793)))
BIND = os.environ.get("FLEETDECK_ADOPT_BIND", "127.0.0.1")
OPEN_TO_ALL = os.environ.get("FLEETDECK_OPEN") == "1"

ALLOWED_NETS = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

# Plans live here between inspect and adopt, keyed by the scratch dir that
# holds the clone. Adopt will not act on a plan the client made up: it looks up
# the one this process produced.
PLANS = {}
LOCK = threading.Lock()

# An inspected-but-never-adopted plan is holding a full clone on disk, and some
# repos are hundreds of megabytes. Nobody is obliged to press Adopt, so the
# clone has to be reaped rather than waited on — two idle inspections cost
# 336 MB the first time this was measured.
PLAN_TTL = 30 * 60
MAX_PENDING = 8


def reap():
    while True:
        time.sleep(120)
        now = time.monotonic()
        with LOCK:
            stale = [k for k, p in PLANS.items() if now - p["_at"] > PLAN_TTL]
            # Also drop the oldest if someone leans on inspect repeatedly.
            if len(PLANS) - len(stale) > MAX_PENDING:
                extra = sorted((p["_at"], k) for k, p in PLANS.items()
                               if k not in stale)
                stale += [k for _, k in extra[:len(PLANS) - len(stale) - MAX_PENDING]]
            for k in stale:
                PLANS.pop(k, None)
        for k in stale:
            shutil.rmtree(k, ignore_errors=True)
        if stale:
            print(f"fleetdeck-adopt: reaped {len(stale)} unadopted clone(s)",
                  flush=True)

PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Adopt">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#05070a">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>adopt</title>
<style>
  :root{--bg:#05070a;--panel:#0a0e13;--line:#18222b;--ink:#8fa3b0;
        --bright:#d6e4ec;--dim:#4a5b68;--on:#4fe3c1;--warn:#d9a441;--bad:#e0594b}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:13px/1.6 ui-monospace,"SF Mono",Menlo,monospace;
       padding:0 16px calc(40px + env(safe-area-inset-bottom))}
  header{display:flex;align-items:center;gap:10px;
         padding:calc(18px + env(safe-area-inset-top)) 0 16px}
  .brand{color:var(--bright);font-weight:600;letter-spacing:.2em;text-transform:uppercase}
  main{max-width:620px;margin:0 auto}
  form{display:flex;gap:8px;margin-bottom:8px}
  input{flex:1;min-width:0;background:var(--panel);border:1px solid var(--line);
        border-radius:3px;color:var(--bright);font:inherit;padding:10px 12px}
  input:focus{outline:none;border-color:var(--on)}
  button{background:transparent;border:1px solid var(--line);border-radius:3px;
         color:var(--ink);font:inherit;padding:10px 14px;cursor:pointer}
  button:hover:not(:disabled){border-color:var(--on);color:var(--on)}
  button:disabled{opacity:.4;cursor:default}
  button.go{border-color:var(--on);color:var(--on)}
  .hint{color:var(--dim);font-size:11px;margin:0 0 22px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:4px;
        padding:14px 16px;margin-bottom:14px}
  .row{display:flex;gap:12px;padding:3px 0}
  .row .k{color:var(--dim);width:88px;flex:none}
  .row .v{color:var(--bright);flex:1;min-width:0;word-break:break-word}
  .warn{color:var(--warn)} .bad{color:var(--bad)} .good{color:var(--on)}
  h3{margin:0 0 10px;font-size:11px;letter-spacing:.2em;text-transform:uppercase;
     color:var(--bright);font-weight:600}
  label{display:block;color:var(--dim);font-size:11px;margin:10px 0 3px}
  .steps div{padding:2px 0}
  .foot{display:flex;gap:8px;align-items:center;margin-top:14px}
  .spin{color:var(--dim)}
</style></head><body>
<header><span class="brand">adopt</span>
  <span style="color:var(--dim)">// paste a repo</span></header>
<main>
  <form id="f" autocomplete="off">
    <input id="u" placeholder="https://github.com/owner/name" spellcheck="false"
           autocapitalize="off">
    <button class="go" id="b">inspect</button>
  </form>
  <p class="hint">Inspect clones and <b>reads</b>. Nothing from the repo runs
    until you press Adopt.</p>
  <div id="out"></div>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let PLAN=null;

const row=(k,v,cls)=>`<div class="row"><span class="k">${k}</span>`+
  `<span class="v ${cls||''}">${esc(v)}</span></div>`;

$('#f').addEventListener('submit',async e=>{
  e.preventDefault();
  const url=$('#u').value.trim(); if(!url) return;
  $('#b').disabled=true; $('#out').innerHTML='<p class="spin">cloning and reading…</p>';
  try{
    const r=await (await fetch('/api/inspect',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({url})})).json();
    render(r);
  }catch(err){ $('#out').innerHTML=`<p class="bad">${esc(err)}</p>`; }
  $('#b').disabled=false;
});

function render(p){
  if(!p.ok){ $('#out').innerHTML=`<div class="card"><span class="bad">${esc(p.error)}</span></div>`; return; }
  PLAN=p;
  let h='<div class="card"><h3>plan</h3>';
  h+=row('stack',p.stack)+row('entry',p.entry||'—')
    +row('install',p.install||'nothing to install')
    +row('port',p.port)+row('dest',p.dest)+row('license',p.license);
  for(const w of p.warnings) h+=row('warn',w,'warn');
  for(const b of p.blockers) h+=row('blocked',b,'bad');
  h+='</div>';

  if(p.env.length){
    h+='<div class="card"><h3>environment</h3><p class="hint">'
      +'These have no value in the repo\\'s example file. Blank is fine — the '
      +'app decides whether it can start without them.</p>';
    for(const k of p.env)
      h+=`<label>${esc(k)}</label><input class="envk" data-k="${esc(k)}" `
        +`type="password" placeholder="leave blank to skip">`;
    h+='</div>';
  }

  h+='<div class="foot">';
  h+= p.blockers.length
    ? '<span class="bad">blocked — nothing to do</span>'
    : '<button class="go" id="go">adopt — this runs the repo\\'s install</button>';
  h+='</div>';
  $('#out').innerHTML=h;

  const go=$('#go');
  if(go) go.addEventListener('click',async()=>{
    go.disabled=true; go.textContent='adopting…';
    const env={};
    document.querySelectorAll('.envk').forEach(i=>{ if(i.value) env[i.dataset.k]=i.value; });
    try{
      const r=await (await fetch('/api/adopt',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({scratch:PLAN.scratch,env})})).json();
      let s='<div class="card"><h3>result</h3><div class="steps">';
      for(const st of (r.steps||[]))
        s+=`<div class="${st.ok?'good':'bad'}">${st.ok?'✓':'✗'} ${esc(st.msg)}</div>`;
      s+='</div>';
      if(r.url) s+=`<div class="row" style="margin-top:10px"><span class="k">url</span>`
        +`<span class="v good">${esc(r.url)}</span></div>`;
      s+='<p class="hint" style="margin-top:10px">The tile appears on the board '
        +'on its next scan. Give the port a tailscale serve mapping if adopt '
        +'could not.</p></div>';
      $('#out').insertAdjacentHTML('beforeend',s);
    }catch(err){ $('#out').insertAdjacentHTML('beforeend',
      `<div class="card bad">${esc(err)}</div>`); }
  });
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fleetdeck-adopt"

    def log_message(self, *a):
        pass

    def allowed(self):
        if OPEN_TO_ALL:
            return True
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in n for n in ALLOWED_NETS)

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed():
            return self._send(403, "tailnet only\n", "text/plain")
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path.startswith("/icon-") and path.endswith(".png"):
            f = os.path.join(HERE, "assets", os.path.basename(path))
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
        self._send(404, "not here\n", "text/plain")

    def do_POST(self):
        if not self.allowed():
            return self._send(403, json.dumps({"ok": False, "error": "tailnet only"}))
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "bad json"}))
        path = self.path.split("?")[0]

        if path == "/api/inspect":
            plan = engine.inspect((body.get("url") or "").strip())
            if plan.get("ok"):
                plan["_at"] = time.monotonic()
                with LOCK:
                    PLANS[plan["scratch"]] = plan
            return self._send(200, json.dumps(plan))

        if path == "/api/adopt":
            # Look the plan up rather than trusting one off the wire — the
            # client can only name a scratch dir this process created, so it
            # cannot hand us a destination or an install command of its own.
            with LOCK:
                plan = PLANS.pop(body.get("scratch"), None)
            if not plan:
                return self._send(400, json.dumps(
                    {"ok": False, "steps": [{"ok": False,
                     "msg": "no such plan — inspect again"}]}))
            env = {k: str(v) for k, v in (body.get("env") or {}).items()
                   if isinstance(k, str)}
            res = engine.adopt(plan, env_values=env,
                               label_prefix=CFG.get("label_prefix"))
            return self._send(200, json.dumps(res))

        self._send(404, json.dumps({"ok": False, "error": "not here"}))


def main():
    threading.Thread(target=reap, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"fleetdeck-adopt {BIND}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
