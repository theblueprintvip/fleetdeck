#!/usr/bin/env python3
"""fleetdeck portal — the front door to every server and agent on this machine.

A single-screen launcher: one tile per local service, each with its own glyph,
a live status lamp, and a link that actually resolves from the phone. Plus one
tile per launchd agent, which is the half a port scan can never see.

The honesty rule: a tile NEVER shows a link the portal has not resolved from
live system state. Three sources, all read fresh on every scan:

  1. `lsof -nP -iTCP -sTCP:LISTEN` — is the port listening, and what did it bind?
     A service on 127.0.0.1 is unreachable from the phone no matter how much we
     would like it to be, and it gets a "host only" badge, not a dead link.
  2. `tailscale serve status --json` — the escape hatch for (1). Loopback-bound
     services that Tailscale already proxies get their real public URL, so they
     are tappable after all.
  3. `launchctl list` + `~/Library/LaunchAgents/*.plist` — the scheduled jobs.
     These mostly do not listen on anything, so (1) and (2) are blind to them.

Nothing about a URL is hardcoded. Move a service to a new port, add a
`tailscale serve` mapping, kill a container — the next scan tells the truth
about it without an edit here.

Configuration: config.json next to this file (brand, machine, ports, filters).
Registry:      services.json (hot-reloaded per scan — adding an app is one line).

Access: no password. The socket binds 127.0.0.1 and `tailscale serve` fronts it
with real HTTPS on the tailnet:

    tailscale serve --bg --https=8790 http://127.0.0.1:8790

That front matters more than it looks. Served raw on 0.0.0.0 this failed three
ways an iPhone reports identically as "it won't load":

  1. https://…:8790 was a hard TLS failure (`tlsv1 alert protocol version`).
     Safari upgrades typed hostnames to HTTPS first, so every open paid a failed
     handshake and a fallback — and any saved https bookmark simply never loaded.
  2. A plain-HTTP origin is not a secure context: no service worker, and
     Add to Home Screen degrades.
  3. Reached over the local Wi-Fi instead of the tailnet, the request arrived
     from 192.168.x.x and got a bare 403.

Behind `tailscale serve` all three go away: one https:// URL with a real
Let's Encrypt cert, a secure origin, and requests that arrive from 127.0.0.1.
The tailnet boundary is unchanged — Tailscale enforces it at the proxy now
instead of this process enforcing it per-request, so the local Wi-Fi still
cannot enumerate the box. The per-request check below stays as defence in
depth for the loopback socket. FLEETDECK_OPEN=1 lifts it; FLEETDECK_BIND
overrides the bind if the front is ever removed.

Stdlib only. No build step, no pip, no node.
"""

import ipaddress
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(HERE, "config.json")
REGISTRY = os.path.join(HERE, "services.json")
AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
TSBIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

DEFAULTS = {
    "brand": "fleetdeck",
    "machine": "",           # "" → resolved from Tailscale at boot
    "label_prefix": "",      # e.g. "com.acme" — stripped from agent tile names
    "ports": {"portal": 8790, "chat": 8783, "ttyd": 8784},
    "agents": {"show": True, "actions": False, "include": [], "exclude": []},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(CFG_FILE) as fh:
            user = json.load(fh)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        sys.stderr.write(f"fleetdeck: bad config.json ({e}) — using defaults\n")
    return cfg


CONF = load_config()


def tailnet_name():
    """This machine's MagicDNS name. Resolved once, at boot, from Tailscale
    itself — never hardcoded, because a hardcoded tailnet name is the single
    thing most likely to be wrong on a machine that is not the author's."""
    for exe in (TSBIN, "tailscale"):
        try:
            raw = subprocess.run([exe, "status", "--json"], capture_output=True,
                                 text=True, timeout=8).stdout
            return json.loads(raw)["Self"]["DNSName"].rstrip(".")
        except Exception:
            continue
    return os.uname().nodename


# Who may read the portal. 100.64/10 is Tailscale's CGNAT range and
# fd7a:115c:a1e0::/48 its IPv6 ULA — between them, every tailnet peer.
# Loopback covers `fleetdeck status`.
ALLOWED_NETS = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]
OPEN_TO_ALL = os.environ.get("FLEETDECK_OPEN") == "1"

BRAND = os.environ.get("FLEETDECK_BRAND", CONF["brand"])
HOST = os.environ.get("FLEETDECK_HOST") or CONF["machine"] or tailnet_name()
PORT = int(os.environ.get("FLEETDECK_PORT", CONF["ports"]["portal"]))
BIND = os.environ.get("FLEETDECK_BIND", "127.0.0.1")
MACHINE = HOST.split(".")[0]
SCAN_TTL = 4.0  # seconds; a phone poll every 10s should not fork lsof each time

SHOW_AGENTS = CONF["agents"].get("show", True)
# Actions mutate the machine from a surface that has NO password — the tailnet
# bind is the only boundary. That is a defensible read-only posture and a much
# bigger claim for start/stop, so it is off unless switched on deliberately.
AGENT_ACTIONS = (os.environ.get("FLEETDECK_AGENT_ACTIONS") == "1"
                 or CONF["agents"].get("actions", False))
# Vendor agents are not the operator's fleet — they are the OS and its tenants.
AGENT_NOISE = re.compile(
    r"^(com\.apple\.|com\.google\.|com\.microsoft\.|com\.adobe\.|com\.valve"
    r"|homebrew\.mxcl\.|com\.docker\.|org\.mozilla\.|com\.electron\."
    r"|com\.tailscale\.|com\.zoom\.|us\.zoom\.|com\.spotify\.|com\.dropbox)",
    re.I,
)

# Commands that listen but are never "apps" — OS services, editors, VM plumbing.
NOISE = re.compile(
    r"(rapportd|ControlCe|ARDAgent|sharingd|Adobe|Creative|dynamicli|TeamProje"
    r"|identityservices|cloudflar|lmlink|Google|chrome|Slack|Spotify|Docker"
    r"|limactl|Code|Electron|ttyd|LM.{0,4}Stu|node.?_?modules)",
    re.I,
)
# Ports that back a registered app rather than standing on their own (the chat
# server spawns its own ttyd and proxies it under /t, so that port is plumbing,
# not an app, and listing it would invite a tap that lands nowhere).
INTERNAL = {int(CONF["ports"].get("ttyd", 8784))}


def esc_html(s):
    """Escape before interpolating into a page. HOST is env-supplied and the
    client IP comes off the socket — both are constrained, neither is trusted."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def sh(cmd, timeout=15):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


# ── system truth ──────────────────────────────────────────────────────────────

def listeners():
    """port -> {cmd, cls} where cls is the most permissive bind seen.

    open   — bound to * / 0.0.0.0 / [::]  → reachable across the tailnet
    tailnet— bound to the 100.x tailscale address → reachable across the tailnet
    host   — loopback only → NOT reachable from the phone
    """
    rank = {"host": 0, "tailnet": 1, "open": 2}
    found = {}
    for line in sh("lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null").splitlines()[1:]:
        f = line.split()
        if len(f) < 3 or not f[-1].startswith("("):
            continue
        cmd, addr = f[0], f[-2]
        if ":" not in addr:
            continue
        bind, _, port = addr.rpartition(":")
        if not port.isdigit():
            continue
        if bind in ("*", "0.0.0.0", "[::]", "::"):
            cls = "open"
        elif bind.startswith("100."):
            cls = "tailnet"
        elif bind in ("127.0.0.1", "[::1]", "::1"):
            cls = "host"
        else:
            cls = "open"  # a LAN address still answers over the tailnet
        p = int(port)
        prev = found.get(p)
        if prev is None or rank[cls] > rank[prev["cls"]]:
            found[p] = {"cmd": cmd, "cls": cls}
    return found


def serve_map():
    """local port -> public tailscale URL, for anything `tailscale serve` proxies."""
    raw = sh(f"{TSBIN} serve status --json 2>/dev/null")
    if not raw.strip():
        return {}
    try:
        j = json.loads(raw)
    except Exception:
        return {}
    tcp = j.get("TCP") or {}
    out = {}
    for hostport, cfg in (j.get("Web") or {}).items():
        host, _, hp = hostport.rpartition(":")
        scheme = "https" if (tcp.get(hp) or {}).get("HTTPS") else "http"
        base = f"{scheme}://{host}" if hp in ("443", "80") else f"{scheme}://{host}:{hp}"
        for path, h in (cfg.get("Handlers") or {}).items():
            proxy = h.get("Proxy") or ""
            m = re.search(r":(\d+)/?$", proxy)
            if not m:
                continue
            url = base + (path if path != "/" else "")
            out.setdefault(int(m.group(1)), url)
    return out


# ── launchd ───────────────────────────────────────────────────────────────────
#
# The half a port scan cannot see. Most of an operator's fleet is scheduled work
# — nudges, watchdogs, backups, digests — and none of it listens on a socket, so
# `lsof` reports it as simply absent. Two sources, joined on the label:
#
#   ~/Library/LaunchAgents/*.plist  — what is INSTALLED (and its schedule)
#   launchctl list                  — what is LOADED (and how it last exited)
#
# Reading both is what makes "installed but never bootstrapped" visible, which is
# the most common way a launchd job is quietly doing nothing at all.

INTERPRETERS = re.compile(
    r"^(env|bash|sh|zsh|python3?(\.\d+)?|node|ruby|perl|osascript|caffeinate)$")


def launchd_state():
    """label -> {pid, exit}. Exit status may be negative — that is a signal
    number (-15 = SIGTERM), not a failure code, and gets read as such below."""
    out = {}
    for line in sh("launchctl list", timeout=10).splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 3:
            continue
        pid, status, label = f[0].strip(), f[1].strip(), f[2].strip()
        def num(v):
            try:
                return int(v)
            except ValueError:
                return None
        out[label] = {"pid": num(pid), "exit": num(status)}
    return out


def schedule_of(p):
    """Human schedule, in the words the operator would use."""
    if p.get("StartInterval"):
        n = int(p["StartInterval"])
        if n % 3600 == 0:
            return f"every {n // 3600}h"
        if n % 60 == 0:
            return f"every {n // 60}m"
        return f"every {n}s"
    cal = p.get("StartCalendarInterval")
    if cal:
        times = cal if isinstance(cal, list) else [cal]
        stamps = []
        for c in times:
            if not isinstance(c, dict):
                continue
            h, m = c.get("Hour"), c.get("Minute", 0)
            stamps.append(f"{h:02d}:{m:02d}" if h is not None else f":{m:02d}")
        if len(stamps) == 1:
            return f"daily {stamps[0]}"
        if stamps:
            return f"{len(stamps)}x daily · {stamps[0]}…"
        return "scheduled"
    if p.get("WatchPaths") or p.get("QueueDirectories"):
        return "on file change"
    if p.get("KeepAlive"):
        return "always on"
    if p.get("RunAtLoad"):
        return "at login"
    return "manual"


def program_of(p):
    """Best label for what the job actually runs. `/bin/bash -c foo.sh` should
    read as foo.sh, not bash — the interpreter is never the interesting part."""
    args = p.get("ProgramArguments") or ([p["Program"]] if p.get("Program") else [])
    args = [str(a) for a in args if isinstance(a, (str, bytes))]
    if not args:
        return "—"
    first = os.path.basename(args[0])
    if INTERPRETERS.match(first):
        for a in args[1:]:
            if a.startswith("-"):
                continue
            base = os.path.basename(a.split()[0]) if a.split() else ""
            if base and not INTERPRETERS.match(base):
                return base
    return first


def launch_agents():
    if not SHOW_AGENTS:
        return []
    state = launchd_state()
    inc = [s.lower() for s in CONF["agents"].get("include", [])]
    exc = [s.lower() for s in CONF["agents"].get("exclude", [])]
    prefix = (CONF.get("label_prefix") or "").rstrip(".")
    out = []
    try:
        files = sorted(os.listdir(AGENT_DIR))
    except OSError:
        return []
    for fn in files:
        if not fn.endswith(".plist"):
            continue
        label = fn[:-6]
        try:
            with open(os.path.join(AGENT_DIR, fn), "rb") as fh:
                p = plistlib.load(fh)
            label = p.get("Label", label)
        except Exception:
            p = {}
        low = label.lower()
        if AGENT_NOISE.search(label) and not any(s in low for s in inc):
            continue
        if inc and not any(s in low for s in inc):
            continue
        if any(s in low for s in exc):
            continue

        st = state.get(label)
        running = bool(st and st.get("pid"))
        code = st.get("exit") if st else None
        # A periodic job that is not running right now is NORMAL — it already
        # ran and exited 0. Treating "no PID" as "down" would paint a healthy
        # board red, which is the fastest way to make an operator stop reading
        # it. Only a non-zero, non-signal exit is a failure.
        if st is None:
            health = "off"      # installed on disk, never bootstrapped
        elif running:
            health = "run"
        elif code in (0, None):
            health = "ok"
        elif code < 0:
            health = "ok"       # negative == killed by signal (SIGTERM on reload)
        else:
            health = "fail"

        name = label
        if prefix and name.startswith(prefix + "."):
            name = name[len(prefix) + 1:]
        out.append({
            "kind": "agent",
            "label": label,
            "name": name,
            "program": program_of(p),
            "schedule": schedule_of(p),
            "health": health,
            "pid": st.get("pid") if st else None,
            "exit": code,
        })
    out.sort(key=lambda a: ({"fail": 0, "off": 1, "run": 2, "ok": 3}[a["health"]],
                            a["name"]))
    return out


def load_registry():
    try:
        with open(REGISTRY) as fh:
            return json.load(fh)
    except Exception as e:
        return {"groups": [], "services": [], "error": str(e)}


def scan():
    live, served, reg = listeners(), serve_map(), load_registry()
    known = set()
    services = []

    for s in reg.get("services", []):
        port = s.get("port")
        known.add(port)
        hit = live.get(port)
        url = None
        if hit:
            if port in served:
                url = served[port]
            elif hit["cls"] in ("open", "tailnet"):
                url = f"http://{HOST}:{port}"
            if url and s.get("path"):
                url += s["path"]
        # An "api" service has no browser UI: tapping its root lands on a 404 or
        # `{"error":"Unexpected endpoint or method. (GET /)"}`. Badging that was
        # not enough — the tile stayed tappable and led straight to the error.
        # API tiles are therefore never links. The blurb says where to USE them.
        linkable = bool(url) and s.get("kind") != "api"
        services.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "blurb": s.get("blurb", ""),
            "icon": s.get("icon", "server"),
            "group": s.get("group", "core"),
            # "api" == answers HTTP but has no browser UI; tapping it lands on a
            # 404 or raw JSON. Badged, not hidden — sometimes you want /docs.
            "kind": s.get("kind", "app"),
            "port": port,
            "up": bool(hit),
            "proc": hit["cmd"] if hit else None,
            # up + no url == listening but loopback-bound and unproxied
            "reach": ("link" if url else ("host" if hit else "down")),
            "url": url,
            "linkable": linkable,
        })

    # Anything listening that the registry does not know about. Kept separate and
    # unstyled — discovery, not curation. High ephemeral ports are noise.
    extra = []
    for port, hit in sorted(live.items()):
        if (port in known or port in INTERNAL or port == PORT or port >= 49000
                or port < 1024 or NOISE.search(hit["cmd"])):
            continue
        url = served.get(port) or (
            f"http://{HOST}:{port}" if hit["cls"] in ("open", "tailnet") else None
        )
        extra.append({
            "port": port, "proc": hit["cmd"], "url": url,
            "reach": "link" if url else "host",
        })

    agents = launch_agents()
    return {
        "host": HOST,
        "brand": BRAND,
        "machine": MACHINE,
        "groups": reg.get("groups", []),
        "services": services,
        "extra": extra,
        "agents": agents,
        "agentActions": AGENT_ACTIONS,
        "scanned": time.time(),
        "error": reg.get("error"),
    }


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def cached_scan():
    with _lock:
        now = time.time()
        if _cache["data"] is None or now - _cache["at"] > SCAN_TTL:
            _cache["data"] = scan()
            _cache["at"] = now
        return _cache["data"]


# ── access ────────────────────────────────────────────────────────────────────

def allowed(addr):
    """True for tailnet peers and loopback. No password anywhere in the path."""
    if OPEN_TO_ALL:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):  # ::ffff:127.0.0.1 style clients
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETS)


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="__MACHINE__">
<meta name="theme-color" content="#05070a">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<title>__MACHINE__ // portal</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%2305070a'/><path d='M6 12h6l4 10 4-16 4 12h2' stroke='%234fe3c1' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
  :root{
    --bg:#05070a; --panel:#0a0e13; --line:#18222b;
    --ink:#8fa3b0; --bright:#d6e4ec; --dim:#4a5b68;
    --on:#4fe3c1; --off:#2b3a45; --warn:#d9a441;
    /* idle == a scheduled job resting between runs. Deliberately its own
       colour: not the green of "serving", not the grey of "dead". */
    --idle:#3f6d7d; --bad:#e26a6a;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;background:var(--bg);color:var(--ink)}
  body{
    font:13px/1.5 ui-monospace,"SF Mono",Menlo,monospace;
    padding:0 16px calc(40px + env(safe-area-inset-bottom));
    background-image:
      linear-gradient(var(--line) 1px,transparent 1px),
      linear-gradient(90deg,var(--line) 1px,transparent 1px);
    background-size:64px 64px;
    background-position:-1px -1px;
    background-attachment:fixed;
  }
  body::before{ /* scanline veil — texture, not a light show */
    content:"";position:fixed;inset:0;pointer-events:none;z-index:2;
    background:repeating-linear-gradient(180deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px);
    opacity:.5;
  }
  header{
    position:sticky;top:0;z-index:3;margin:0 -16px 22px;padding:14px 16px;
    padding-top:calc(14px + env(safe-area-inset-top));
    background:rgba(5,7,10,.93);backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line);
    display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  }
  .brand{color:var(--on);letter-spacing:.16em;font-weight:600;text-transform:uppercase}
  .cursor{display:inline-block;width:7px;height:13px;background:var(--on);
    vertical-align:-2px;animation:blink 1.2s steps(1) infinite}
  @keyframes blink{50%{opacity:0}}
  .meta{color:var(--dim);margin-left:auto;font-size:11px;letter-spacing:.06em}
  .meta b{color:var(--on);font-weight:600}
  #fs{
    display:none;margin-left:10px;flex:none;
    width:30px;height:30px;padding:0;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--ink);cursor:pointer;line-height:0;
  }
  #fs.on{display:inline-flex;align-items:center;justify-content:center}
  #fs:active,#fs:hover{border-color:var(--on);color:var(--on)}
  /* Launched from the Home Screen: no browser chrome, so the status bar sits on
     top of the page. env(safe-area-inset-top) is already applied to the header;
     standalone just needs a little more breathing room and no fullscreen button
     (it is already fullscreen). */
  body.standalone header{padding-top:calc(20px + env(safe-area-inset-top))}
  body.standalone #fs{display:none !important}
  #hint{
    display:none;margin:0 0 18px;padding:10px 12px;
    border:1px solid var(--line);border-left:2px solid var(--on);border-radius:3px;
    color:var(--dim);font-size:11px;line-height:1.6;
  }
  #hint.on{display:block}
  #hint b{color:var(--ink);font-weight:600}
  #hint button{
    background:none;border:0;color:var(--on);font:inherit;
    text-decoration:underline;cursor:pointer;padding:0;margin-left:6px;
  }
  h2{
    display:flex;align-items:center;gap:12px;margin:26px 0 12px;
    font-size:11px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;
    color:var(--dim);
  }
  h2::after{content:"";flex:1;height:1px;background:var(--line)}
  .grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  .tile{
    position:relative;display:flex;flex-direction:column;gap:9px;
    padding:14px;min-height:104px;
    background:var(--panel);border:1px solid var(--line);border-radius:3px;
    color:inherit;text-decoration:none;overflow:hidden;
    transition:border-color .14s,transform .14s,background .14s;
  }
  a.tile:active{transform:scale(.975);border-color:var(--on);background:#0d1319}
  @media(hover:hover){a.tile:hover{border-color:var(--on);background:#0d1319}}
  a.tile:hover .name,a.tile:active .name{color:var(--on)}
  .tile.down,.tile.host{opacity:.55}
  .tile.down .name,.tile.host .name{color:var(--ink)}
  .top{display:flex;align-items:flex-start;justify-content:space-between}
  .ico{width:24px;height:24px;color:var(--on);flex:none}
  .tile.down .ico,.tile.host .ico{color:var(--off)}
  .lamp{width:6px;height:6px;border-radius:50%;flex:none;margin-top:3px;
    background:var(--off);box-shadow:none}
  .tile.up .lamp{background:var(--on);box-shadow:0 0 0 3px rgba(79,227,193,.14)}
  .tile.host .lamp{background:var(--warn);box-shadow:none}
  .name{color:var(--bright);font-weight:600;letter-spacing:.02em;line-height:1.25}
  .blurb{color:var(--dim);font-size:11px;line-height:1.35;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .foot{display:flex;justify-content:space-between;align-items:center;
    margin-top:auto;padding-top:8px;font-size:10.5px;color:var(--dim);letter-spacing:.06em}
  .badge{color:var(--warn);border:1px solid rgba(217,164,65,.3);
    border-radius:2px;padding:0 4px;font-size:9.5px;letter-spacing:.08em}
  .badge.api{color:var(--dim);border-color:var(--line)}
  .badge.bad{color:var(--bad);border-color:rgba(226,106,106,.35)}
  /* ── agents ──────────────────────────────────────────────────────────────
     A scheduled job that is not running right now is HEALTHY, so its lamp is
     calm, not dark. Only a non-zero exit earns red, and only an unloaded plist
     earns amber. Anything louder and the board stops being readable. */
  .tile.agent{min-height:92px}
  .tile.agent .ico{color:var(--dim)}
  .tile.agent.run .lamp{background:var(--on);box-shadow:0 0 0 3px rgba(79,227,193,.14)}
  .tile.agent.run .ico{color:var(--on)}
  .tile.agent.ok .lamp{background:var(--idle)}
  .tile.agent.off{opacity:.55}
  .tile.agent.off .lamp{background:var(--warn)}
  .tile.agent.fail{border-color:rgba(226,106,106,.4)}
  .tile.agent.fail .lamp{background:var(--bad);box-shadow:0 0 0 3px rgba(226,106,106,.14)}
  .tile.agent.fail .ico,.tile.agent.fail .name{color:var(--bad)}
  .sched{color:var(--ink);font-size:10.5px;letter-spacing:.04em}
  .acts{display:flex;gap:6px;margin-top:8px}
  .acts button{flex:1;background:transparent;border:1px solid var(--line);
    border-radius:2px;color:var(--dim);font:10px/1.8 inherit;letter-spacing:.1em;
    text-transform:uppercase;cursor:pointer;padding:0}
  .acts button:hover,.acts button:active{border-color:var(--on);color:var(--on)}
  .rows{display:flex;flex-wrap:wrap;gap:7px}
  .row{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;
    background:var(--panel);border:1px solid var(--line);border-radius:3px;
    color:var(--dim);text-decoration:none;font-size:11px}
  a.row:active,a.row:hover{border-color:var(--on);color:var(--bright)}
  .row .lamp{margin:0}
  .err{border:1px solid var(--warn);color:var(--warn);padding:10px;border-radius:3px}
  footer{margin-top:32px;padding-top:14px;border-top:1px solid var(--line);
    color:var(--dim);font-size:10.5px;letter-spacing:.06em;line-height:1.7}
</style></head><body>
<header>
  <span class="brand" id="brand">__BRAND__</span>
  <span style="color:var(--dim)">// __MACHINE__</span><span class="cursor"></span>
  <span class="meta"><b id="n">—</b> online<span id="clock"></span></span>
  <button id="fs" title="Fullscreen" aria-label="Toggle fullscreen">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path id="fsi" d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>
    </svg>
  </button>
</header>
<div id="hint"></div>
<main id="app"></main>
<footer>
  every link resolved live from <span style="color:var(--ink)">lsof</span> binds +
  <span style="color:var(--ink)">tailscale serve</span>; every agent from
  <span style="color:var(--ink)">launchctl</span> + its plist — nothing hardcoded.<br>
  <span class="badge">host only</span> = listening on loopback, not reachable from this device.
  add a mapping with <span style="color:var(--ink)">tailscale serve</span> to make it tappable.
  <span class="badge api">api</span> = answers, but has no browser UI — shown, not linked,
  so a tap can never land on an error page.<br>
  <span class="badge">unloaded</span> = plist installed but never bootstrapped — it is doing nothing.
  <span class="badge bad">exit N</span> = last run failed.<br>
  registry: <span id="regpath">services.json</span> · scan <span id="ago">—</span>
</footer>
<script>
const I={
 terminal:'<path d="M4 5h16v14H4z"/><path d="m8 10 2.5 2L8 14"/><path d="M13 15h3"/>',
 chat:'<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
 messages:'<path d="M20 4H4v12h4v4l5-4h7z"/><path d="M8 10h8"/>',
 gauge:'<path d="M3 17a9 9 0 1 1 18 0"/><path d="m12 17 4.5-6"/><circle cx="12" cy="17" r="1.4"/>',
 board:'<path d="M3 4h18v16H3z"/><path d="M9 4v16M15 4v16"/><path d="M5.5 7.5h2M11.5 7.5h2M17.5 7.5h2"/>',
 grid:'<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
 brain:'<path d="M12 6a3.2 3.2 0 0 0-6-1.2A2.9 2.9 0 0 0 4 7.6a3 3 0 0 0 .9 2.1A3 3 0 0 0 4 12a3 3 0 0 0 2 2.8A3 3 0 0 0 9 19a3 3 0 0 0 3-3z"/><path d="M12 6a3.2 3.2 0 0 1 6-1.2 2.9 2.9 0 0 1 2 2.8 3 3 0 0 1-.9 2.1A3 3 0 0 1 20 12a3 3 0 0 1-2 2.8A3 3 0 0 1 15 19a3 3 0 0 1-3-3z"/>',
 sparkle:'<path d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="m18.4 15.6.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7z"/>',
 gallery:'<path d="M7 5h10v14H7z"/><path d="M4 8v8M20 8v8"/><path d="m9.5 15 2-2.5 2 2 1-1.2 2 3.7h-7z"/>',
 chip:'<path d="M7 7h10v10H7z"/><path d="M10 2v3M14 2v3M10 19v3M14 19v3M2 10h3M2 14h3M19 10h3M19 14h3"/>',
 compass:'<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5.5-5.5 2 2-5.5z"/>',
 book:'<path d="M12 6.5C10.5 5 8.3 4.5 5 4.7V18c3.3-.2 5.5.3 7 1.8 1.5-1.5 3.7-2 7-1.8V4.7c-3.3-.2-5.5.3-7 1.8z"/><path d="M12 6.5v13"/>',
 scroll:'<path d="M7 4h10v14a2 2 0 0 0 2 2H8a2 2 0 0 1-2-2V6"/><path d="M10 8h5M10 12h5"/>',
 flask:'<path d="M9 3v6L4 19a1.6 1.6 0 0 0 1.4 2h13.2A1.6 1.6 0 0 0 20 19l-5-10V3"/><path d="M8 3h8M7.5 14h9"/>',
 cpu:'<path d="M6 6h12v12H6z"/><path d="M10 10h4v4h-4z"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
 waveform:'<path d="M3 12h3l3-8 4 16 3-8h5"/>',
 rocket:'<path d="M12 3c3.5 2.5 5 6 5 10l-2.5 3h-5L7 13c0-4 1.5-7.5 5-10z"/><circle cx="12" cy="10" r="1.6"/><path d="M9.5 17 8 21l3-1.5M14.5 17l1.5 4-3-1.5"/>',
 glasses:'<circle cx="6.5" cy="14" r="3.5"/><circle cx="17.5" cy="14" r="3.5"/><path d="M10 14a2.5 2.5 0 0 1 4 0M3 11l2-4M21 11l-2-4"/>',
 film:'<path d="M3 5h18v14H3z"/><path d="M7 5v14M17 5v14"/><path d="M5 8h.01M5 12h.01M5 16h.01M19 8h.01M19 12h.01M19 16h.01"/>',
 orb:'<circle cx="12" cy="12" r="8"/><ellipse cx="12" cy="12" rx="8" ry="3.4"/><path d="M12 4v16"/>',
 play:'<circle cx="12" cy="12" r="9"/><path d="m10 8.2 6.2 3.8-6.2 3.8z"/>',
 speaker:'<path d="M4 9.5h3.5L12 6v12L7.5 14.5H4z"/><path d="M16 9.8a3.4 3.4 0 0 1 0 4.4M18.6 7.4a7 7 0 0 1 0 9.2"/>',
 vr:'<path d="M3 8h18v6.5a1.5 1.5 0 0 1-1.5 1.5H16l-2-2h-4l-2 2H4.5A1.5 1.5 0 0 1 3 14.5z"/><path d="M7.5 5.5h9"/>',
 layers:'<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
 mail:'<path d="M3 6h18v12H3z"/><path d="m3 7 9 6 9-6"/>',
 search:'<circle cx="11" cy="11" r="6.5"/><path d="m16 16 5 5"/>',
 database:'<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
 archive:'<path d="M3 4h18v4H3z"/><path d="M5 8v12h14V8"/><path d="M10 12h4"/>',
 server:'<path d="M3 4h18v6H3zM3 14h18v6H3z"/><path d="M7 7h.01M7 17h.01"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.4 2"/>',
 bolt:'<path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12z"/>',
 loop:'<path d="M4 12a8 8 0 0 1 13.7-5.6L20 8"/><path d="M20 4v4h-4"/><path d="M20 12a8 8 0 0 1-13.7 5.6L4 16"/><path d="M4 20v-4h4"/>',
 eye:'<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/>',
 hand:'<path d="M8 11V5.5a1.5 1.5 0 0 1 3 0V11m0 0V4.5a1.5 1.5 0 0 1 3 0V11m0 0V6.5a1.5 1.5 0 0 1 3 0V14a6 6 0 0 1-6 6h-1a5 5 0 0 1-4.3-2.5L5 15"/>'};

const svg=k=>`<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${I[k]||I.server}</svg>`;
// registry text is operator-authored, but `proc` comes off lsof — escape both
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let last=0;
function render(d){
  last=d.scanned*1000;
  const up=d.services.filter(s=>s.up).length;
  document.getElementById('n').textContent=up+'/'+d.services.length;
  let h=d.error?`<div class="err">registry: ${d.error}</div>`:'';
  for(const g of d.groups){
    const list=d.services.filter(s=>s.group===g.id);
    if(!list.length) continue;
    // reachable first, then still-listening, then dark — taps before diagnostics
    const w={link:0,host:1,down:2};
    list.sort((a,b)=>w[a.reach]-w[b.reach]||a.name.localeCompare(b.name));
    h+=`<h2>${g.label}</h2><div class="grid">`;
    for(const s of list){
      const cls=s.reach==='link'?'up':(s.reach==='host'?'host up':'down');
      const tag=s.reach==='host'?'<span class="badge">host only</span>':
                (!s.up?'<span style="letter-spacing:.08em">offline</span>':
                (s.kind==='api'?'<span class="badge api">api</span>':''));
      const body=`<div class="top">${svg(s.icon)}<span class="lamp"></span></div>
        <div><div class="name">${esc(s.name)}</div><div class="blurb">${esc(s.blurb)}</div></div>
        <div class="foot"><span>:${s.port}</span>${tag}</div>`;
      h+=(s.url&&s.linkable)?`<a class="tile ${cls}" href="${esc(s.url)}">${body}</a>`
              :`<div class="tile ${cls}">${body}</div>`;
    }
    h+='</div>';
  }
  // ── agents ────────────────────────────────────────────────────────────────
  // Sorted server-side by health, so failures are the first thing on screen and
  // the count in the heading answers "is anything wrong?" without scrolling.
  if(d.agents&&d.agents.length){
    const bad=d.agents.filter(a=>a.health==='fail').length;
    const off=d.agents.filter(a=>a.health==='off').length;
    let note='';
    if(bad) note+=` <span class="badge bad">${bad} failing</span>`;
    if(off) note+=` <span class="badge">${off} unloaded</span>`;
    h+=`<h2>agents · ${d.agents.length}${note}</h2><div class="grid">`;
    for(const a of d.agents){
      const s=a.schedule||'';
      const ico = s==='always on' ? 'bolt'
                : s==='manual'    ? 'hand'
                : s==='on file change' ? 'eye'
                : s.startsWith('every') ? 'loop' : 'clock';
      // A signal exit (negative) is a normal reload, not a fault — server-side
      // health already folded that in; only surface a code we called a failure.
      const tag = a.health==='fail' ? `<span class="badge bad">exit ${a.exit}</span>`
                : a.health==='off'  ? '<span class="badge">unloaded</span>'
                : a.health==='run'  ? `<span style="letter-spacing:.08em">pid ${a.pid}</span>`
                : '';
      const acts = d.agentActions ? `<div class="acts">
          <button data-act="start" data-label="${esc(a.label)}">run</button>
          <button data-act="stop"  data-label="${esc(a.label)}">stop</button>
        </div>` : '';
      h+=`<div class="tile agent ${a.health}">
        <div class="top">${svg(ico)}<span class="lamp"></span></div>
        <div><div class="name">${esc(a.name)}</div>
             <div class="blurb">${esc(a.program)}</div></div>
        <div class="foot"><span class="sched">${esc(s)}</span>${tag}</div>
        ${acts}</div>`;
    }
    h+='</div>';
  }
  if(d.extra.length){
    h+=`<h2>unregistered</h2><div class="rows">`;
    for(const e of d.extra){
      const b=`<span class="lamp" style="background:var(--on)"></span>${esc(e.proc)} :${e.port}`;
      h+=e.url?`<a class="row" href="${esc(e.url)}">${b}</a>`:`<span class="row">${b}</span>`;
    }
    h+='</div>';
  }
  document.getElementById('app').innerHTML=h;
}
// Delegated, because render() replaces the whole subtree on every poll and
// per-tile listeners would be rebound (and leak) ten times a minute.
document.getElementById('app').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-act]'); if(!b) return;
  b.disabled=true; b.textContent='…';
  try{
    await fetch('api/agent',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:b.dataset.act,label:b.dataset.label})});
  }catch(_){}
  poll();
});
function tick(){
  const t=new Date();
  document.getElementById('clock').textContent=' · '+
    String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0');
  const s=last?Math.round((Date.now()-last)/1000):0;
  document.getElementById('ago').textContent=last?(s<60?s+'s ago':Math.round(s/60)+'m ago'):'—';
}
async function poll(){
  try{ render(await (await fetch('api/status',{cache:'no-store'})).json()); }catch(e){}
}
// ── fullscreen ───────────────────────────────────────────────────────────────
// Three different platforms, three different answers, so feature-detect rather
// than assume:
//   desktop / Android  — the Fullscreen API works; show the toggle.
//   iOS Safari in a tab — requestFullscreen does not exist for elements. No
//                         button is shown, because a button that silently does
//                         nothing is worse than none. Point at Add to Home
//                         Screen instead, which is the real fullscreen on iOS.
//   launched from Home Screen — already fullscreen; hide both.
const standalone = window.matchMedia('(display-mode: standalone)').matches
                || window.navigator.standalone === true;
const el = document.documentElement;
const canFS = !!(el.requestFullscreen || el.webkitRequestFullscreen);
const fsBtn = document.getElementById('fs');
const hint  = document.getElementById('hint');

if (standalone) document.body.classList.add('standalone');

if (canFS && !standalone) {
  fsBtn.classList.add('on');
  const paint = () => {
    const on = !!(document.fullscreenElement || document.webkitFullscreenElement);
    // arrows point out to enter, in to exit
    document.getElementById('fsi').setAttribute('d', on
      ? 'M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5'
      : 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5');
    fsBtn.title = on ? 'Exit fullscreen' : 'Fullscreen';
  };
  fsBtn.addEventListener('click', () => {
    const on = document.fullscreenElement || document.webkitFullscreenElement;
    if (on) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    else    (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
  });
  document.addEventListener('fullscreenchange', paint);
  document.addEventListener('webkitfullscreenchange', paint);
  paint();
} else if (!standalone && /iPhone|iPad|iPod/.test(navigator.userAgent)) {
  // Dismissible, and the dismissal sticks — a permanent banner on a launcher
  // you open twenty times a day is its own kind of broken.
  if (localStorage.getItem('wb-hint') !== 'off') {
    hint.innerHTML = 'Fullscreen on iOS is <b>Add to Home Screen</b> — ' +
      'Share, then Add to Home Screen. It opens with no browser chrome.' +
      '<button id="hx">dismiss</button>';
    hint.classList.add('on');
    document.getElementById('hx').addEventListener('click', () => {
      localStorage.setItem('wb-hint', 'off');
      hint.classList.remove('on');
    });
  }
}

render(window.__DATA__); tick();
setInterval(tick,1000);
setInterval(poll,10000);
// a launcher is usually reopened, not left open — rescan the moment it returns
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
</script></body></html>"""


# A refusal is the one failure mode a phone reports as nothing at all. Plain text
# gave the operator a blank-looking screen with no next action; this names the
# cause (wrong network) and the fix, in the portal's own visual language.
DENIED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>%(machine)s // off-tailnet</title>
<style>
 html,body{margin:0;background:#05070a;color:#8fa3b0;
   font:13px/1.6 ui-monospace,"SF Mono",Menlo,monospace}
 main{max-width:34rem;margin:0 auto;padding:14vh 22px}
 h1{color:#d9a441;font-size:12px;letter-spacing:.22em;text-transform:uppercase;margin:0 0 18px}
 p{margin:0 0 14px}b{color:#d6e4ec;font-weight:600}
 code{color:#4fe3c1}
 .box{border:1px solid #18222b;border-left:2px solid #d9a441;border-radius:3px;padding:14px}
</style></head><body><main>
<h1>off tailnet</h1>
<div class="box">
<p>This device reached %(machine)s from <b>%(ip)s</b> — the local Wi&#8209;Fi, not the
tailnet. The portal only answers over Tailscale.</p>
<p>Open the <b>Tailscale</b> app and switch it on, then load
<code>https://%(host)s:%(port)s</code> again.</p>
</div>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "wb-portal"
    protocol_version = "HTTP/1.1"
    timeout = 30  # don't let an abandoned keep-alive socket hold a thread forever

    def log_message(self, *a):
        pass  # the LaunchAgent log is for failures, not for every poll

    def handle_one_request(self):
        """Swallow the normal ways a phone ends a connection.

        Mobile Safari opens speculative connections and drops them without a
        close — with HTTP/1.1 keep-alive that surfaces as ConnectionResetError /
        BrokenPipeError, and the stdlib's default is to dump a full traceback and
        kill the thread. The log filled with them. They are routine client
        behaviour, not server faults; close the connection and move on.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
            self.close_connection = True

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/healthz":
            return self._send(200, "ok\n", "text/plain")

        if not allowed(self.client_address[0]):
            # Logged loudly and on purpose: a refusal is the one failure mode a
            # phone reports only as "it won't load". Without this line there is
            # nothing to diagnose it from.
            ip = self.client_address[0]
            sys.stderr.write(f"DENIED {ip} -> {path}\n")
            sys.stderr.flush()
            body = DENIED_PAGE % {"ip": esc_html(ip), "host": esc_html(HOST),
                                  "machine": esc_html(MACHINE), "port": PORT}
            return self._send(403, body, "text/html; charset=utf-8")

        # Fullscreen on iPhone is not the Fullscreen API — Safari on iOS refuses
        # requestFullscreen for anything but <video>. The only real fullscreen
        # there is Add to Home Screen, which needs a manifest and a PNG icon
        # (iOS ignores SVG for apple-touch-icon). Hence these two routes. They
        # sit behind the tailnet check like everything else.
        if path == "/manifest.webmanifest":
            return self._send(200, json.dumps({
                "name": f"{BRAND} // {MACHINE}",
                "short_name": MACHINE,
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": "#05070a",
                "theme_color": "#05070a",
                "orientation": "portrait",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
                     "purpose": "maskable"},
                ],
            }), "application/manifest+json")

        if path.startswith("/icon-") and path.endswith(".png"):
            icon = os.path.join(HERE, "assets", os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        if path in ("/api/status", "/api"):
            return self._send(200, json.dumps(cached_scan()), "application/json")

        if path == "/":
            data = json.dumps(cached_scan()).replace("</", "<\\/")
            page = (PAGE
                    .replace("__BRAND__", esc_html(BRAND))
                    .replace("__MACHINE__", esc_html(MACHINE))
                    .replace("window.__DATA__", f"JSON.parse({json.dumps(data)})"))
            return self._send(200, page, "text/html; charset=utf-8")

        self._send(404, "not here\n", "text/plain")

    def do_POST(self):
        """The one mutating route. Off unless agents.actions is switched on —
        see AGENT_ACTIONS. Even then it can only touch labels the scan already
        returned, so a caller cannot name an arbitrary launchd job."""
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not allowed(self.client_address[0]):
            return self._send(403, "no\n", "text/plain")
        if path != "/api/agent":
            return self._send(404, "not here\n", "text/plain")
        if not AGENT_ACTIONS:
            return self._send(403, json.dumps(
                {"ok": False, "error": "agent actions disabled"}),
                "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "bad body"}),
                              "application/json")

        label, action = body.get("label", ""), body.get("action", "")
        # Allowlist by identity, not by pattern: the label must be one this
        # portal actually discovered. Anything else is refused even if it is a
        # perfectly valid launchd label.
        known = {a["label"] for a in launch_agents()}
        if label not in known:
            return self._send(404, json.dumps({"ok": False, "error": "unknown label"}),
                              "application/json")
        uid = os.getuid()
        if action == "start":
            cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
        elif action == "stop":
            cmd = ["launchctl", "kill", "SIGTERM", f"gui/{uid}/{label}"]
        else:
            return self._send(400, json.dumps({"ok": False, "error": "bad action"}),
                              "application/json")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            ok = r.returncode == 0
            out = (r.stderr or r.stdout).strip()
        except Exception as e:
            ok, out = False, str(e)
        with _lock:                      # force the next poll to read fresh state
            _cache["at"] = 0.0
        sys.stderr.write(f"AGENT {action} {label} -> {'ok' if ok else out}\n")
        sys.stderr.flush()
        self._send(200 if ok else 500,
                   json.dumps({"ok": ok, "detail": out}), "application/json")


def main():
    if not os.path.exists(REGISTRY):
        sys.stderr.write(f"fleetdeck: no registry at {REGISTRY}\n")
        sys.exit(1)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    # Loopback means the reachable URL is the `tailscale serve` front, not this
    # socket. Announcing http:// here sends the operator to a URL that 400s.
    front = "https" if BIND.startswith("127.") else "http"
    sys.stderr.write(
        f"fleetdeck portal {BIND}:{PORT} -> {front}://{HOST}:{PORT}"
        f"  (agents={'on' if SHOW_AGENTS else 'off'}"
        f" actions={'on' if AGENT_ACTIONS else 'off'})\n")
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
