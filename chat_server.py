#!/usr/bin/python3
"""
fleetdeck chat — the tmux fleet as a Messages-style thread list.

Left rail is one row per tmux session (hue-matched to its wall tile, newest
activity on top, unread dot when it has produced output since you last looked).
Tapping a row opens that session in the pane on the right — and it is NOT a
transcript: the pane is a live, writable tmux client. The "conversation" is a
real shell.

WHY A PROXY AND NOT A SECOND ttyd PORT
--------------------------------------
The terminal is ttyd (already trusted here, already carrying the mobile viewport
patch). One instance runs on LOOPBACK with -a/--url-arg, so the browser picks
the session per-iframe:

    /t/?arg=new-session&arg=-A&arg=-s&arg=media   ->   tmux new-session -A -s media

Everything under /t is byte-proxied to it from this server, which means:
  * one origin, so one credential prompt — an iframe pointed at another port
    raises its own basic-auth wall, and Chrome will not fetch() from a URL with
    credentials embedded (the same wall that forced /?key= on the messages app);
  * ttyd itself never listens anywhere but 127.0.0.1, and the proxy injects the
    Authorization header upstream, so it is still credentialed at both hops.
ttyd runs with -b /t because its client derives /token and /ws from
window.location.pathname — the iframe sits at /t/, so those land at /t/token
and /t/ws and ttyd has to know to strip the base.

WINDOW SIZE, i.e. WHAT THIS DOES TO THE WALL
--------------------------------------------
tmux renders one window at one size, so a phone attaching to a session that is
also tiled on the wall is a genuine conflict, not a bug to fix. Two modes:
  live (default) — plain attach. The browser becomes the newest client and
      `window-size latest` hands it the size, so the wall tile reshapes to the
      phone WHILE the tab is open and snaps back when it closes (closing the
      thread tears the iframe down, ttyd SIGHUPs `tmux attach`, the client
      detaches, and the wall client becomes latest again).
  peek — attach -f ignore-size,read-only. The session keeps the wall's
      geometry and refuses keystrokes: look at what an agent is doing without
      touching it or disturbing the wall. Costs you the ability to type, and
      the pane may be wider than the phone.

WHERE THIS LIVES
----------------
Keep this repo OUT of ~/Documents and ~/Desktop. Those are TCC-protected, and a
launchd job cannot read them without a Full Disk Access grant that you will
forget you needed. ~/.config or ~/srv is safe.

Shebang is /usr/bin/python3 — Apple's path never moves, unlike
/opt/homebrew/bin/python3, which every `brew upgrade python` relocates and
which took this project's launchd jobs down once already.

  PORT=8783 BIND=tailscale TTYD_PORT=8784   # env-configured; see the CLI
"""
import os, re, io, json, time, base64, socket, colorsys, hashlib, signal
import shutil
import threading, subprocess, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME      = os.path.expanduser("~")
HERE      = os.path.dirname(os.path.abspath(__file__))


def _conf():
    try:
        with open(os.path.join(HERE, "config.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


CONF      = _conf()
_ports    = CONF.get("ports") or {}
_chat     = CONF.get("chat") or {}


def _bin(name, *fallbacks):
    """Resolve a tool by PATH first. Hardcoding /opt/homebrew is an
    Apple-silicon assumption — Intel Macs put brew in /usr/local — and a
    launchd job runs with a PATH that may not include either."""
    found = shutil.which(name)
    if found:
        return found
    for f in fallbacks:
        if os.path.exists(f):
            return f
    return name  # let it fail loudly at exec, with the name in the error


PORT      = int(os.environ.get("PORT", _ports.get("chat", 8783)))
TTYD_PORT = int(os.environ.get("TTYD_PORT", _ports.get("ttyd", 8784)))
BIND      = os.environ.get("BIND", "tailscale")
BASE      = "/t"                       # proxied prefix == ttyd's -b base path
TMUX      = _bin("tmux", "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux")
TTYD      = _bin("ttyd", "/opt/homebrew/bin/ttyd", "/usr/local/bin/ttyd")
BRAND     = os.environ.get("FLEETDECK_BRAND", CONF.get("brand", "fleetdeck"))
CFG       = HERE
AUTHFILE  = f"{CFG}/auth"
INDEX     = f"{CFG}/ttyd-index.html"   # ttyd's index + the viewport meta it omits
CACHE     = f"{HOME}/.cache/fleetdeck"
SEENFILE  = f"{CACHE}/seen.json"
TSBIN     = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# Sessions to keep out of the hue rotation — typically the one backing a plain
# ttyd terminal, which would otherwise fight the others for window size. Hues
# are assigned over this same ordered list wherever else you tile these
# sessions, so a row here keeps the colour it has there.
WALL_EXCLUDE = set(_chat.get("exclude_sessions", ["tunnel"]))
ACTIVE_SECS  = 90        # produced output this recently -> "active" pip
SNAP_TTL     = 2.0       # sidebar polls every 3s; this keeps two clients cheap


def version():
    """Single source of truth is the repo's VERSION file — the CLI, the health
    endpoint and the UI badge all read it, so there is no copy to drift."""
    try:
        return open(f"{HERE}/VERSION").read().strip()
    except Exception:
        return "unknown"


VERSION = version()


# ── auth ───────────────────────────────────────────────────────────────
def credentials():
    try:
        u, p = open(AUTHFILE).read().strip().split(":", 1)
        return u, p
    except Exception:
        return None, None


# ── ttyd child ─────────────────────────────────────────────────────────
_stop = threading.Event()
_ttyd = {"proc": None}


def ttyd_argv():
    u, p = credentials()
    argv = [TTYD, "-p", str(TTYD_PORT), "-i", "127.0.0.1",
            "-W",                 # writable; peek mode is enforced by tmux, not ttyd
            "-a",                 # command args from the URL == session picker
            "-b", BASE,
            "-T", "xterm-256color",
            "-P", "10"]
    if u:
        # Credentialed even on loopback: -a means a URL can shape the command
        # line, so this must not be reachable by anything but our own proxy.
        argv += ["-c", f"{u}:{p}"]
    if os.path.exists(INDEX):
        argv += ["-I", INDEX]
    argv += [
        "-t", "fontSize=13",
        "-t", "fontFamily=SFMono-Regular,Menlo,monospace",
        "-t", 'theme={"background":"#07090c","foreground":"#c9d8e4",'
              '"cursor":"#28e0d0","selectionBackground":"#123b40"}',
        "-t", "cursorBlink=true",
        "-t", "scrollback=10000",
        "-t", "macOptionIsMeta=true",
        TMUX,                     # url args land here: new-session -A -s <name>
    ]
    return argv


def supervise_ttyd():
    while not _stop.is_set():
        try:
            proc = subprocess.Popen(ttyd_argv())
        except Exception as e:
            print(f"ttyd spawn failed: {e}", flush=True)
            time.sleep(5)
            continue
        _ttyd["proc"] = proc
        print(f"ttyd up on 127.0.0.1:{TTYD_PORT} (pid {proc.pid})", flush=True)
        proc.wait()
        if _stop.is_set():
            return
        print(f"ttyd exited rc={proc.returncode} — restarting in 3s", flush=True)
        time.sleep(3)


def ttyd_alive():
    p = _ttyd.get("proc")
    return bool(p and p.poll() is None)


def reclaim_ttyd_port():
    """Clear anything already sitting on the loopback ttyd port.

    launchd SIGKILLs a job's whole process tree, so under the LaunchAgent the
    child goes with us. Run by hand (or killed hard) it does not: the ttyd is
    reparented to pid 1 and keeps the port, the fresh supervisor's child then
    dies on bind, and the UI serves a terminal whose every request 502s. The
    port is exclusively ours, so whatever holds it is ours to clear."""
    try:
        out = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{TTYD_PORT}",
                              "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return
    for pid in {int(x) for x in out.split() if x.isdigit()} - {os.getpid()}:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"reclaimed :{TTYD_PORT} from orphaned pid {pid}", flush=True)
        except Exception as e:
            print(f"could not reclaim :{TTYD_PORT} from pid {pid}: {e}", flush=True)
    time.sleep(0.5)


def shutdown(signum=None, frame=None):
    _stop.set()
    p = _ttyd.get("proc")
    if p and p.poll() is None:
        p.terminate()
    raise SystemExit(0)


# ── tmux ───────────────────────────────────────────────────────────────
def tmux(*args, timeout=10):
    r = subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "tmux failed").strip()[:200])
    return r.stdout


FMT = "\t".join(["#{session_name}", "#{session_attached}", "#{session_activity}",
                 "#{window_width}", "#{window_height}", "#{pane_current_command}",
                 "#{pane_current_path}", "#{pane_title}"])

# A row of ─ or ═ is a Claude/tmux frame, not output. `❯` alone is an empty
# prompt. `⏵⏵ bypass permissions…` is the permanent status footer — it would be
# every session's preview forever.
BOX      = set("─│╭╮╰╯├┤┬┴┼═║╔╗╚╝╡╞╪▔▁▀▄█▌▐░▒▓•·—-_=~ ")
BOX_DRAW = set("─│╭╮╰╯├┤┬┴┼═║╔╗╚╝╡╞╪▔▁▀▄█▌▐░▒▓")


def is_chrome(line):
    t = line.strip()
    if not t:
        return True
    if t.startswith("⏵⏵") or t.startswith("? for shortcuts"):
        return True
    if t in ("❯", ">", "$", "%", "#", "▐", "│"):
        return True
    if all(ch in BOX for ch in t):
        return True
    # A LABELLED rule is still a rule. Claude divides sections with
    # "──── some-branch-name ────", which carries letters and so survives the
    # all-box test — and being the last drawn line, it wins every preview.
    # Ratio, not presence: `├── src/index.ts` is output and must survive.
    draw = sum(ch in BOX_DRAW for ch in t)
    return draw >= 8 and draw / len(t) >= 0.5


def screen(name):
    """(preview, hash) for one session.

    Preview is the last PARAGRAPH of real output, not the last line — a wrapped
    recap or tool result ends on a fragment ("in /config)") that reads as noise
    on its own. Hash covers every non-chrome line so the unread pip tracks real
    output; a working agent whose spinner line keeps changing SHOULD read as
    active."""
    try:
        raw = tmux("capture-pane", "-p", "-t", name, "-S", "-60").splitlines()
    except Exception:
        return "", ""
    body = [l for l in raw if not is_chrome(l)]
    digest = hashlib.sha1("\n".join(body).encode("utf-8", "replace")).hexdigest()[:16]

    i = len(raw) - 1
    while i >= 0 and is_chrome(raw[i]):
        i -= 1
    if i < 0:
        return "", digest
    end = i
    while i >= 0 and not is_chrome(raw[i]):
        i -= 1
    para = " ".join(l.strip() for l in raw[i + 1:end + 1])
    return re.sub(r"\s+", " ", para).strip()[:220], digest


def hexcolor(hue, sat=0.62, val=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return "#%02x%02x%02x" % (int(r * 255 + .5), int(g * 255 + .5), int(b * 255 + .5))


_lock  = threading.Lock()
_snap  = {"at": 0.0, "rows": []}
_state = {}                      # name -> {"hash": str, "changed": epoch}
try:
    _seen = json.load(open(SEENFILE))
except Exception:
    _seen = {}                   # name -> hash the operator has already looked at


def save_seen():
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = SEENFILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_seen, f)
        os.replace(tmp, SEENFILE)
    except Exception as e:
        print(f"seen persist failed: {e}", flush=True)


def snapshot(open_name=None):
    now = time.time()
    with _lock:
        if now - _snap["at"] < SNAP_TTL and _snap["rows"]:
            rows = _snap["rows"]
            if open_name:
                _mark_seen(open_name, rows)
            return rows

    meta = []
    for line in tmux("list-sessions", "-F", FMT).splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        meta.append(parts)

    with ThreadPoolExecutor(max_workers=8) as pool:
        scans = list(pool.map(lambda m: screen(m[0]), meta))

    hued = [m[0] for m in meta if m[0] not in WALL_EXCLUDE] or [m[0] for m in meta]
    rows = []
    for m, (preview, digest) in zip(meta, scans):
        name, attached, activity, w, h, cmd, path, title = m[:8]
        # First sight seeds from tmux's own last-activity stamp, not now(), or a
        # restart would report the whole fleet as having just spoken.
        st = _state.setdefault(name, {"hash": digest,
                                      "changed": float(activity or now)})
        if digest and digest != st["hash"]:
            st["hash"], st["changed"] = digest, now
        hue = (hued.index(name) / len(hued)) if name in hued else None
        rows.append({
            "name": name,
            "color": hexcolor(hue) if hue is not None else "#5d7183",
            "title": title if title and title != name else "",
            "cmd": cmd,
            "path": path.replace(HOME, "~"),
            "preview": preview,
            "changed": int(st["changed"]),
            "active": now - st["changed"] < ACTIVE_SECS,
            "unread": _seen.get(name) != st["hash"],
            "clients": int(attached or 0),
            "size": f"{w}x{h}",
        })
    rows.sort(key=lambda r: -r["changed"])

    with _lock:
        _snap["at"], _snap["rows"] = now, rows
    if open_name:
        _mark_seen(open_name, rows)
    return rows


def _mark_seen(name, rows):
    """The open thread is by definition read — mark it on every poll so a
    session you are watching never accumulates a pip."""
    st = _state.get(name)
    if not st or _seen.get(name) == st["hash"]:
        return
    _seen[name] = st["hash"]
    for r in rows:
        if r["name"] == name:
            r["unread"] = False
    save_seen()


# ── page ───────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html><html><head>
<meta charset="utf-8"><title>▩ fleet</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<style>
:root{--bg:#07090c;--panel:#0c1015;--line:#18222c;--dim:#5d7183;
      --txt:#c9d8e4;--neon:#28e0d0;--mag:#ff4ecd}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
     height:100dvh;display:flex;overflow:hidden}
#list{width:320px;flex:none;border-right:1px solid var(--line);display:flex;flex-direction:column;background:var(--panel)}
#head{padding:11px 13px;border-bottom:1px solid var(--line);color:var(--neon);letter-spacing:.13em;font-size:11px;
      display:flex;justify-content:space-between;align-items:center;gap:8px}
#head b{font-weight:400;color:#33414e}
#beta{color:#ffcf4e;border:1px solid #4a3d12;border-radius:5px;padding:1px 4px;font-size:9px;letter-spacing:.1em}
#head .hb{display:flex;gap:6px;align-items:center}
.btn{cursor:pointer;color:#3d4d5c;border:1px solid #223140;border-radius:7px;padding:3px 7px;
     font-size:10px;letter-spacing:.1em;flex:none;user-select:none}
.btn:hover{color:var(--neon);border-color:#2b4353}
.btn.peek{color:#ffcf4e;border-color:#4a3d12}
#rows{overflow-y:auto;flex:1}
.r{padding:10px 13px;border-bottom:1px solid #10171e;cursor:pointer;display:flex;gap:10px;align-items:flex-start}
.r:hover{background:#111820}
.r.on{background:#0f1c22}
.r.on .av{box-shadow:0 0 0 2px #07090c,0 0 0 3px currentColor}
.av{width:26px;height:26px;flex:none;border-radius:8px;margin-top:2px;display:grid;place-items:center;
    font-size:11px;font-weight:700;background:currentColor;color:inherit;position:relative}
.av i{color:#04211f;font-style:normal}
.av .pip{position:absolute;right:-3px;top:-3px;width:9px;height:9px;border-radius:50%;
         background:var(--mag);border:2px solid var(--panel);display:none}
.r.unread .av .pip{display:block}
.b{min-width:0;flex:1}
.n{color:#e6f1f8;font-size:12.5px;letter-spacing:.06em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.r.unread .n{color:#fff}
.t{color:#3d4d5c;font-size:10px;float:right;margin-left:8px;font-weight:400;letter-spacing:0}
.sub{font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.p{color:var(--dim);font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.8}
.live{display:inline-block;width:5px;height:5px;border-radius:50%;background:#2ee06a;margin-right:5px;
      vertical-align:middle;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.25}}
#pane{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
#title{padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel);
       font-size:12px;letter-spacing:.09em;display:flex;gap:10px;align-items:center}
#who{color:#e6f1f8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#meta{color:#33414e;font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
#back{display:none;color:var(--mag);cursor:pointer;font-size:17px;line-height:1}
#ro{display:none;color:#ffcf4e;font-size:10px;letter-spacing:.1em;border:1px solid #4a3d12;
    border-radius:7px;padding:3px 7px;flex:none}
body.peek #ro{display:block}
#frame{flex:1;position:relative;min-height:0}
#frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0;display:block}
.empty{position:absolute;inset:0;display:grid;place-items:center;color:#2b3742;font-size:12px;text-align:center;padding:20px}
#hue{height:2px;background:#18222c;flex:none}
@media(max-width:720px){
  #list{width:100%}
  body.thread #list{display:none} body.thread #pane{display:flex} body:not(.thread) #pane{display:none}
  #back{display:block}
  #title .btn{padding:4px 9px}
}
</style></head><body>
<div id="list">
  <div id="head">
    <span>▩ FLEET <b id="cnt"></b> <span id="beta" title="fleetdeck __VERSION__ — beta: working and in daily use, not yet behind a production security gate">BETA</span></span>
    <span class="hb">
      <span class="btn" id="mode" title="live = writable, and this browser takes the window size · peek = read-only, leaves the wall's geometry alone">LIVE</span>
      <span class="btn" id="sortbtn" title="sort order">⇅</span>
      <span class="btn" id="new" title="new session">+</span>
    </span>
  </div>
  <div id="rows"></div>
</div>
<div id="pane">
  <div id="hue"></div>
  <div id="title">
    <span id="back">‹</span>
    <span id="who">select a session</span>
    <span id="meta"></span>
    <span id="ro">READ-ONLY</span>
    <span class="btn" id="pop" title="open full screen">↗</span>
  </div>
  <div id="frame"><div class="empty">pick an agent on the left</div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let cur=null, rows=[], sig='';
let mode=localStorage.getItem('wbmode')||'live';
let sortBy=localStorage.getItem('wbsort')||'recent';

const ago=ts=>{const s=Math.max(0,Math.floor(Date.now()/1000)-ts);
  if(s<45)return 'now'; if(s<3600)return Math.floor(s/60)+'m';
  if(s<86400)return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'d';};

// ttyd runs bare `tmux`; the whole command line comes from these args.
// live -> plain attach, so `window-size latest` gives this browser the size.
// peek -> ignore-size,read-only: the wall keeps the geometry, keys are dropped.
function termUrl(n){
  const a = mode==='peek'
    ? ['attach-session','-f','ignore-size,read-only','-t',n]
    : ['new-session','-A','-s',n];
  return '/t/?'+a.map(x=>'arg='+encodeURIComponent(x)).join('&');
}

function paint(){
  const list=sortBy==='name'?[...rows].sort((a,b)=>a.name.localeCompare(b.name)):rows;
  const s=JSON.stringify(list.map(r=>[r.name,r.preview,r.title,r.changed,r.unread,r.active,r.clients]))+cur+mode;
  if(s===sig)return; sig=s;
  $('cnt').textContent=rows.length+(rows.some(r=>r.unread)?' · '+rows.filter(r=>r.unread).length+' new':'');
  $('rows').innerHTML=list.map(r=>`
    <div class="r${r.name===cur?' on':''}${r.unread?' unread':''}" data-n="${esc(r.name)}" style="color:${r.color}">
      <span class="av"><i>${esc(r.name[0].toUpperCase())}</i><span class="pip"></span></span>
      <div class="b">
        <div class="n"><span class="t">${ago(r.changed)}</span>${r.active?'<span class="live"></span>':''}${esc(r.name.toUpperCase())}</div>
        <div class="sub" style="color:${r.color};opacity:.85">${esc(r.title||r.path||'')}</div>
        <div class="p">${esc(r.preview||'—')}</div>
      </div></div>`).join('');
  [...document.querySelectorAll('.r')].forEach(el=>el.onclick=()=>open_(el.dataset.n));
  if(cur){const r=rows.find(x=>x.name===cur);
    if(r){$('meta').textContent=`${r.size} · ${r.clients} client${r.clients===1?'':'s'} · ${r.cmd}`;
          $('hue').style.background=r.color;}}
}

function mount(){
  if(!cur){$('frame').innerHTML='<div class="empty">pick an agent on the left</div>';return;}
  const f=document.createElement('iframe');
  f.src=termUrl(cur); f.setAttribute('allow','clipboard-read; clipboard-write');
  $('frame').replaceChildren(f);
}

function open_(n){
  if(cur===n)return;
  cur=n; document.body.classList.add('thread');
  $('who').textContent=n.toUpperCase();
  const r=rows.find(x=>x.name===n); if(r)$('hue').style.background=r.color;
  mount(); sig=''; paint(); load();
}

// Dropping the iframe closes the websocket; ttyd SIGHUPs `tmux attach`, the
// client detaches, and a wall tile that had reshaped to this browser snaps back.
function close_(){
  cur=null; document.body.classList.remove('thread');
  $('who').textContent='select a session'; $('meta').textContent='';
  $('hue').style.background='#18222c'; mount(); sig=''; paint();
}

async function load(){
  try{
    const r=await fetch('/api/sessions'+(cur?'?open='+encodeURIComponent(cur):''));
    if(!r.ok)throw new Error(r.status); rows=await r.json(); paint();
  }catch(e){$('cnt').textContent='offline';}
}

function showMode(){
  $('mode').textContent=mode.toUpperCase();
  $('mode').classList.toggle('peek',mode==='peek');
  document.body.classList.toggle('peek',mode==='peek');
}
$('back').onclick=close_;
// The toggle lives in the LIST header, not the thread header: on a phone the
// thread header only exists once a session is already open, so a peek-first
// workflow (look without reshaping the wall tile) would be unreachable.
$('mode').onclick=()=>{
  mode=mode==='live'?'peek':'live'; localStorage.setItem('wbmode',mode);
  showMode(); if(cur)mount(); sig=''; paint();
};
$('pop').onclick=()=>{if(cur)window.open(termUrl(cur),'_blank');};
$('sortbtn').onclick=()=>{
  sortBy=sortBy==='recent'?'name':'recent'; localStorage.setItem('wbsort',sortBy);
  $('sortbtn').title='sort: '+sortBy; sig=''; paint();
};
$('new').onclick=()=>{
  const n=(prompt('new session name')||'').trim();
  if(!n)return;
  if(!/^[A-Za-z0-9_.-]{1,32}$/.test(n)){alert('letters, digits, . _ - only');return;}
  rows.unshift({name:n,color:'#28e0d0',title:'',cmd:'',path:'',preview:'new',
                changed:Math.floor(Date.now()/1000),active:true,unread:false,clients:0,size:''});
  open_(n);
};
showMode(); $('sortbtn').title='sort: '+sortBy;
load(); setInterval(load,3000);
</script></body></html>"""


# ── server ─────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    server_version = "fleetdeck"

    def log_message(self, fmt, *a):
        pass

    # -- auth (identical contract to the messages app; the `wbt` cookie is
    #    deliberately the same name, since cookies ignore port — one /?key= tap
    #    on the phone authorises both surfaces) --
    def authed(self):
        """Open — no password. Removed at the operator's request 2026-08-22.

        The boundary is now the socket rather than a header: this server binds
        the tailnet address and degrades CLOSED to loopback (see resolve_bind and
        the guard in __main__), so nothing off the tailnet can reach it at all.

        The loopback ttyd child KEEPS its credential. The proxy injects that
        header upstream, so it costs no prompt here, and it stops any other local
        process from reaching a `-a` ttyd that takes its command line from a URL.
        """
        return True

    def try_key(self):
        _, pw = credentials()
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        for part in q.split("&"):
            k, _, v = part.partition("=")
            if k == "key" and pw and v == pw:
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 f"wbt={pw}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
                self.send_header("Location", "/")
                self.end_headers()
                return True
        return False

    def reply(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else \
            (json.dumps(payload).encode() if ctype == "application/json" else payload.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- ttyd proxy --------------------------------------------------------
    def proxy(self):
        """Proxy to the loopback ttyd, upgrade-transparent.

        Forward the request head verbatim (path included — ttyd's -b matches /t
        itself), then splice raw bytes both ways until one side closes. Handling
        the 101 like any other response is what makes the websocket work without
        special-casing it.

        The one thing this must NOT do is relay the response head verbatim.
        libwebsockets hangs up right after serving the index, but its 200 does
        not say `Connection: close` — so a browser parks the very next request
        (/t/token, which ttyd's client fetches before it will open the socket)
        on a connection this end has already dropped, and the terminal sits
        blank forever with the fetch pending and no error anywhere. Costs an
        hour to find because curl reconnects silently and never reproduces it.
        So: force close semantics in both directions on ordinary responses, and
        leave a 101 strictly alone (its `Connection: Upgrade` is load-bearing)."""
        try:
            up = socket.create_connection(("127.0.0.1", TTYD_PORT), timeout=10)
        except Exception as e:
            return self.reply(502, {"error": f"terminal backend down: {e}"})

        u, p = credentials()
        upgrade = "upgrade" in self.headers.get("Connection", "").lower()
        head = io.StringIO()
        head.write(f"{self.command} {self.path} HTTP/1.1\r\n")
        for k, v in self.headers.items():
            if k.lower() in ("host", "authorization"):
                continue
            if k.lower() in ("connection", "keep-alive") and not upgrade:
                continue
            head.write(f"{k}: {v}\r\n")
        head.write(f"Host: 127.0.0.1:{TTYD_PORT}\r\n")
        if not upgrade:
            head.write("Connection: close\r\n")
        if u:
            tok = base64.b64encode(f"{u}:{p}".encode()).decode()
            head.write(f"Authorization: Basic {tok}\r\n")
        head.write("\r\n")

        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)

        self.close_connection = True
        dbg = os.environ.get("WBT_DEBUG")
        if dbg:
            print(f"[proxy] > {self.command} {self.path} "
                  f"hdrs={dict(self.headers).keys()}", flush=True)
        try:
            up.sendall(head.getvalue().encode("latin-1") + body)
            up.settimeout(None)
            self.connection.settimeout(None)

            def up_stream():
                try:
                    while True:
                        chunk = self.rfile.read1(65536)
                        if not chunk:
                            break
                        up.sendall(chunk)
                except Exception:
                    pass
                finally:
                    try:
                        up.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass

            t = threading.Thread(target=up_stream, daemon=True)
            t.start()

            # Read the response head before relaying anything, so a non-101 can
            # be rewritten to say `Connection: close`.
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = up.recv(65536)
                if not d:
                    break
                buf += d
            if not buf:
                if dbg:
                    print(f"[proxy] = {self.path} upstream closed with no response",
                          flush=True)
                return
            hb, _, rest = buf.partition(b"\r\n\r\n")
            lines = hb.split(b"\r\n")
            if b" 101 " in lines[0]:
                self.wfile.write(buf)
            else:
                kept = [l for l in lines[1:]
                        if not re.match(rb"(?i)(connection|keep-alive)\s*:", l)]
                self.wfile.write(b"\r\n".join([lines[0]] + kept + [b"Connection: close"])
                                 + b"\r\n\r\n" + rest)
            self.wfile.flush()
            if dbg:
                print(f"[proxy] < {lines[0]!r} ({len(rest)}B body so far)", flush=True)

            total = len(rest)
            while True:
                chunk = up.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            if dbg:
                print(f"[proxy] = {self.path} upstream EOF after {total}B", flush=True)
        except Exception:
            pass
        finally:
            for s in (up,):
                try:
                    s.close()
                except Exception:
                    pass

    def do_GET(self):
        if self.path.startswith("/?key=") and self.try_key():
            return
        if not self.authed():
            return
        path = self.path.split("?")[0]
        if path == BASE or path.startswith(BASE + "/"):
            return self.proxy()
        qs = {}
        if "?" in self.path and self.path.split("?", 1)[1]:
            for part in self.path.split("?", 1)[1].split("&"):
                k, _, v = part.partition("=")
                qs[k] = urllib.parse.unquote(v)
        try:
            if path == "/":
                return self.reply(200, PAGE.replace("__VERSION__", VERSION),
                                  "text/html; charset=utf-8")
            if path == "/api/sessions":
                return self.reply(200, snapshot(qs.get("open") or None))
            if path == "/healthz":
                return self.reply(200, {"ok": True, "version": VERSION,
                                        "stage": "beta", "ttyd": ttyd_alive(),
                                        "sessions": len(snapshot())})
            if path == "/favicon.ico":
                return self.reply(200, b"", "image/x-icon")
            self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def do_POST(self):
        if not self.authed():
            return
        if self.path.split("?")[0].startswith(BASE):
            return self.proxy()
        self.reply(404, {"error": "not found"})


def resolve_bind(spec):
    """'tailscale' → the tailnet IPv4. Never 0.0.0.0: this serves writable
    shells. Degrades CLOSED to loopback if Tailscale is down.

    Reads the address off utun rather than asking the Tailscale binary — the
    sandboxed App Store build returns nothing under launchd, so the CLI is only
    a fallback."""
    if spec != "tailscale":
        return spec
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=10).stdout
        for m in re.finditer(r"inet (100\.(\d+)\.\d+\.\d+)", out):
            if 64 <= int(m.group(2)) <= 127:      # 100.64.0.0/10 — the tailnet CGNAT range
                return m.group(1)
    except Exception as e:
        print(f"ifconfig probe failed ({e})", flush=True)
    try:
        st = json.loads(subprocess.run([TSBIN, "status", "--json"],
                                       capture_output=True, text=True, timeout=10).stdout)
        for ip in st["Self"]["TailscaleIPs"]:
            if ":" not in ip:
                return ip
    except Exception as e:
        print(f"tailscale IP unresolved ({e}) — binding loopback only", flush=True)
    return "127.0.0.1"


if __name__ == "__main__":
    # With the password gone the BIND is the whole boundary, so check it before
    # serving writable shells rather than checking for a credential. resolve_bind
    # returns the tailnet IP or degrades to loopback; a LAN address or 0.0.0.0
    # here would mean that logic broke, and a writable shell is not the place to
    # find out by serving it anyway.
    addr = resolve_bind(BIND)
    if addr != "127.0.0.1" and not addr.startswith("100."):
        print(f"refusing to serve writable shells on {addr} — tailnet or loopback only",
              flush=True)
        raise SystemExit(78)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    reclaim_ttyd_port()
    threading.Thread(target=supervise_ttyd, daemon=True).start()
    srv = ThreadingHTTPServer((addr, PORT), H)
    print(f"fleetdeck chat on http://{addr}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        _stop.set()
        p = _ttyd.get("proc")
        if p and p.poll() is None:
            p.terminate()
