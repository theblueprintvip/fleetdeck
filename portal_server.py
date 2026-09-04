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
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(HERE, "config.json")
REGISTRY = os.path.join(HERE, "services.json")
GLYPHS = os.path.join(HERE, "glyphs.json")
ICONS = os.path.join(HERE, "icons")     # per-service PNGs from make-icons.py
AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
TSBIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# The one built-in glyph. Everything else lives in glyphs.json, which
# make-icons.py reads too — the board and the home-screen icons are drawn from
# the same paths, so they cannot drift. If that file is unreadable the board
# still renders, every tile just wearing this.
FALLBACK_GLYPH = '<path d="M3 4h18v6H3zM3 14h18v6H3z"/><path d="M7 7h.01M7 17h.01"/>'

DEFAULTS = {
    "brand": "fleetdeck",
    "machine": "",           # "" → resolved from Tailscale at boot
    "label_prefix": "",      # e.g. "com.acme" — stripped from agent tile names
    "ports": {"portal": 8790, "chat": 8783, "ttyd": 8784, "adopt": 8793},
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
            name = json.loads(raw)["Self"]["DNSName"].rstrip(".")
            # Installed but not logged in answers with a well-formed status whose
            # DNSName is "". That parses, so the except below never fires and the
            # nodename fallback was being skipped — the portal then advertised
            # "https://:8790". An empty name is a miss, not an answer.
            if name:
                return name
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
        # An unreadable plist used to fall through as an empty dict, which
        # rendered a tile with no program, no schedule and "no log" — the exact
        # shape of a badly configured job, for a file that is simply not
        # parseable. Say which it is. This is not hypothetical: XML forbids `--`
        # inside a comment, `plutil -lint` accepts it anyway, and two agents on
        # this machine were silently blank because of a hyphen in a comment.
        unreadable = None
        try:
            with open(os.path.join(AGENT_DIR, fn), "rb") as fh:
                p = plistlib.load(fh)
            label = p.get("Label", label)
        except Exception as err:
            # NB: not `as exc` — that name is the exclude list a few lines down,
            # and Python unbinds an `except ... as` target when the block ends.
            p = {}
            unreadable = str(err)
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

        # launchd keeps no last-run time. Not in `launchctl list`, not in
        # `launchctl print` — there is a cumulative `runs` count and a last exit
        # code, and no clock anywhere. So the honest proxy is when the job last
        # WROTE something: stat the log it declares.
        #
        # This is last OUTPUT, not last run, and the UI says so. A job that runs
        # silently leaves it blank, and conflating the two is exactly how a
        # quiet healthy job comes to look dead.
        #
        # StandardOutPath is already in the plist being parsed here, so this
        # costs one stat and no subprocess. `launchctl print` per agent would be
        # 79 subprocesses on the render path.
        last_output, logged = None, False
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = p.get(key)
            if not isinstance(path, str) or not path:
                continue
            logged = True
            try:
                mtime = os.stat(os.path.expanduser(path)).st_mtime
            except OSError:
                continue  # declared but absent — the job has written nothing
            last_output = mtime if last_output is None else max(last_output, mtime)

        name = label
        if prefix and name.startswith(prefix + "."):
            name = name[len(prefix) + 1:]
        out.append({
            "kind": "agent",
            "label": label,
            "name": name,
            "program": ("plist will not parse" if unreadable else program_of(p)),
            "schedule": schedule_of(p),
            "unreadable": unreadable,
            "health": health,
            "pid": st.get("pid") if st else None,
            "exit": code,
            # None is a real answer: no log path configured, or nothing written
            # yet. The board is rescanned rather than remembered, so an invented
            # timestamp costs more than a blank one. `logged` separates the two
            # absences so the tile can say which it is.
            "last_output": last_output,
            "logged": logged,
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


def glyph_map():
    """Read glyphs.json fresh, like the registry — editing a glyph is a reload,
    not a restart. Keys starting with '_' are comments, not icons."""
    try:
        with open(GLYPHS) as fh:
            return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception as exc:
        sys.stderr.write(f"fleetdeck: glyphs.json unreadable ({exc})\n")
        sys.stderr.flush()
        return {"server": FALLBACK_GLYPH}


def scan():
    live, served, reg = listeners(), serve_map(), load_registry()
    known = set()
    services = []

    for s in reg.get("services", []):
        port = s.get("port")
        known.add(port)
        hit = live.get(port)
        # A skinned service answers on two ports: its own, and the skin_server
        # front that dresses it. The tile should open the dressed one — but only
        # if it is actually up, so a dead front degrades to the bare app rather
        # than to a dead link. Both ports count as known, or the front would
        # show up under "unregistered".
        face = port
        skin_port = (s.get("skin") or {}).get("port")
        if skin_port:
            known.add(skin_port)
            if live.get(skin_port):
                face = skin_port
        url = None
        if hit:
            if face in served:
                url = served[face]
            elif live[face]["cls"] in ("open", "tailnet"):
                url = f"http://{HOST}:{face}"
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
            # Registry-declared: this surface serves a real apple-touch-icon.
            # The install sheet lists only these, so it never promises a tile
            # that would come out as a screenshot of the page instead.
            "install": bool(s.get("install")),
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
<!-- The unprefixed form is what Chrome reads; the apple- one above is
     deprecated there but still the only one iOS honours. Both, therefore. -->
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="__MACHINE__">
<meta name="theme-color" content="#0f1216">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<title>__MACHINE__ // portal</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%230f1216'/><path d='M6 12h6l4 10 4-16 4 12h2' stroke='%2363b8b0' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
  :root{
    --bg:#0f1216; --panel:#161b21; --line:#262e37;
    --ink:#93a1ad; --bright:#dde5ec; --dim:#5a6772;
    --on:#63b8b0; --off:#2c353e; --warn:#f7b552;
    /* idle == a scheduled job resting between runs. Deliberately its own
       colour: not the green of "serving", not the grey of "dead". */
    --idle:#6b7f93; --bad:#d9483f;
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
    background:rgba(15,18,22,.93);backdrop-filter:blur(8px);
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
  /* Add to Home Screen cannot be triggered from script on iOS — Safari only
     offers it from the Share menu, and beforeinstallprompt does not exist
     there. So this is a checklist, not a button that installs: it opens each
     surface in turn and remembers which ones are done, because working through
     a dozen of them from memory is how you end up with four. */
  #ins{
    margin-left:8px;flex:none;width:30px;height:30px;padding:0;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--ink);cursor:pointer;line-height:0;
    display:inline-flex;align-items:center;justify-content:center;
  }
  #ins:active,#ins:hover{border-color:var(--on);color:var(--on)}
  /* Which surface answers `/`. A link and not a script toggle, so it also
     navigates to the surface it selects — the state is never something you
     have to read off a button, because you are looking at it. */
  #simple{
    margin-left:8px;flex:none;width:30px;height:30px;padding:0;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--ink);cursor:pointer;line-height:0;
    display:inline-flex;align-items:center;justify-content:center;
  }
  #simple:active,#simple:hover{border-color:var(--on);color:var(--on)}
  /* Lit = the simple screen is this device's home. Tapping it then hands `/`
     back to the board, without leaving the board you are already on. */
  #simple.on{border-color:var(--on);color:var(--on);background:rgba(99,184,176,.09)}
  #sheet{
    position:fixed;inset:0;z-index:20;display:none;overflow:auto;
    background:rgba(15,18,22,.88);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
    padding:24px 16px calc(32px + env(safe-area-inset-bottom));
  }
  #sheet.on{display:block}
  #sheet .card{
    max-width:520px;margin:0 auto;background:var(--panel);
    border:1px solid var(--line);border-radius:4px;padding:16px 16px 8px;
  }
  #sheet h3{
    margin:0 0 4px;font-size:11px;font-weight:600;letter-spacing:.22em;
    text-transform:uppercase;color:var(--bright);
  }
  #sheet .lead{color:var(--dim);font-size:11px;line-height:1.7;margin:0 0 14px}
  #sheet .lead b{color:var(--ink);font-weight:600}
  .irow{
    display:flex;align-items:center;gap:11px;padding:9px 4px;
    border-top:1px solid var(--line);color:var(--ink);
  }
  /* the tick is a sibling of the link, not inside it — a button nested in an
     anchor is invalid, and every tap would have navigated */
  .irow .go{
    display:flex;align-items:center;gap:11px;flex:1;min-width:0;
    color:inherit;text-decoration:none;
  }
  .irow .ico{width:19px;height:19px;flex:none;color:var(--on)}
  .irow .nm{flex:1;min-width:0;font-size:12.5px;color:var(--bright)}
  .irow .nm span{display:block;color:var(--dim);font-size:10.5px;letter-spacing:.06em}
  .irow.off{opacity:.4}
  .irow.off .ico{color:var(--off)}
  .tick{
    flex:none;width:22px;height:22px;border:1px solid var(--line);border-radius:50%;
    background:transparent;color:var(--off);cursor:pointer;padding:0;
    display:inline-flex;align-items:center;justify-content:center;line-height:0;
  }
  .tick.done{border-color:var(--on);color:var(--on)}
  #sheet .foot{
    display:flex;justify-content:space-between;align-items:center;gap:10px;
    border-top:1px solid var(--line);margin-top:6px;padding:11px 4px 8px;
    color:var(--dim);font-size:10.5px;letter-spacing:.06em;
  }
  #sheet .foot button{
    background:none;border:0;color:var(--on);font:inherit;
    text-decoration:underline;cursor:pointer;padding:0;
  }
  #sheet .empty{color:var(--dim);font-size:11px;line-height:1.7;padding:10px 4px 16px}
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
  .tile.up .lamp{background:var(--on);box-shadow:0 0 0 3px rgba(99,184,176,.14)}
  .tile.host .lamp{background:var(--warn);box-shadow:none}
  .name{color:var(--bright);font-weight:600;letter-spacing:.02em;line-height:1.25}
  .blurb{color:var(--dim);font-size:11px;line-height:1.35;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .foot{display:flex;justify-content:space-between;align-items:center;
    margin-top:auto;padding-top:8px;font-size:10.5px;color:var(--dim);letter-spacing:.06em}
  .badge{color:var(--warn);border:1px solid rgba(247,181,82,.3);
    border-radius:2px;padding:0 4px;font-size:9.5px;letter-spacing:.08em}
  .badge.api{color:var(--dim);border-color:var(--line)}
  .badge.bad{color:var(--bad);border-color:rgba(217,72,63,.35)}
  /* ── agents ──────────────────────────────────────────────────────────────
     A scheduled job that is not running right now is HEALTHY, so its lamp is
     calm, not dark. Only a non-zero exit earns red, and only an unloaded plist
     earns amber. Anything louder and the board stops being readable. */
  .tile.agent{min-height:92px}
  .tile.agent .ico{color:var(--dim)}
  .tile.agent .when{margin-top:2px;font-size:10px;letter-spacing:.06em;color:var(--dim)}
  /* An absent timestamp is stated, not hidden — but quietly, because on a
     board where most jobs are silent it is the common case, not a fault. */
  .tile.agent .when.none{opacity:.45}
  .tile.agent.run .lamp{background:var(--on);box-shadow:0 0 0 3px rgba(99,184,176,.14)}
  .tile.agent.run .ico{color:var(--on)}
  .tile.agent.ok .lamp{background:var(--idle)}
  .tile.agent.off{opacity:.55}
  .tile.agent.off .lamp{background:var(--warn)}
  .tile.agent.fail{border-color:rgba(217,72,63,.4)}
  .tile.agent.fail .lamp{background:var(--bad);box-shadow:0 0 0 3px rgba(217,72,63,.14)}
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
  <button id="ins" title="Add to Home Screen" aria-label="Add apps to Home Screen">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M12 8v7M8.5 11.5h7"/>
    </svg>
  </button>
  <!-- Four big keys, not a phone. The button beside this one is already a phone
       outline, and two phone glyphs a hair apart is a header you have to read
       twice; this says what the simple screen IS — fewer keys, larger. Drawn at
       17px against its neighbours' 14 because it is four separate shapes inset
       from the viewBox edge: same ink, more room to keep them apart. -->
  <a id="simple" class="__SIMPLE_CLASS__" href="__SIMPLE_HREF__"
     title="__SIMPLE_TITLE__" aria-label="__SIMPLE_TITLE__">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2.5" y="2.5" width="8.5" height="8.5" rx="1.5"/>
      <rect x="13" y="2.5" width="8.5" height="8.5" rx="1.5"/>
      <rect x="2.5" y="13" width="8.5" height="8.5" rx="1.5"/>
      <rect x="13" y="13" width="8.5" height="8.5" rx="1.5"/>
    </svg>
  </a>
</header>
<div id="hint"></div>
<main id="app"></main>
<div id="sheet"><div class="card">
  <h3>add to home screen</h3>
  <p class="lead">iOS only offers this from the Share menu, so nothing here can
    install for you. Open one, tap <b>Share &rarr; Add to Home Screen</b>, come
    back and tick it off.</p>
  <div id="ilist"></div>
  <div class="foot"><span id="idone">—</span>
    <span><button id="ireset">reset</button> · <button id="iclose">close</button></span>
  </div>
</div></div>
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
const I=__GLYPHS__;

const svg=k=>`<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${I[k]||I.server}</svg>`;
// registry text is operator-authored, but `proc` comes off lsof — escape both
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let last=0, DATA=null;
function render(d){
  last=d.scanned*1000; DATA=d;
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
      // launchd has no last-run time, so this is when the job last WROTE to
      // its log — a different claim, and labelled as one. Absent is rendered,
      // never filled in: "no log" means the plist declares no output path,
      // "no output" means it declares one and nothing has been written to it.
      const when = a.last_output ? `out ${rel(a.last_output)}`
                 : a.logged      ? 'no output'
                 : 'no log';
      const acts = d.agentActions ? `<div class="acts">
          <button data-act="start" data-label="${esc(a.label)}">run</button>
          <button data-act="stop"  data-label="${esc(a.label)}">stop</button>
        </div>` : '';
      h+=`<div class="tile agent ${a.health}">
        <div class="top">${svg(ico)}<span class="lamp"></span></div>
        <div><div class="name">${esc(a.name)}</div>
             <div class="blurb">${esc(a.program)}</div></div>
        <div class="foot"><span class="sched">${esc(s)}</span>${tag}</div>
        <div class="foot when${a.last_output?'':' none'}">${esc(when)}</div>
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
// Unix seconds -> a coarse age. Coarse on purpose: this is a proxy for when a
// job last ran, and reporting it to the second would dress a guess up as a
// measurement.
function rel(ts){
  const s=Math.max(0,Math.round(Date.now()/1000-ts));
  if(s<90) return 'just now';
  if(s<5400) return Math.round(s/60)+'m ago';
  if(s<172800) return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
}

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
// ── add to home screen ───────────────────────────────────────────────────────
// A checklist, not an installer. `beforeinstallprompt` does not exist on iOS
// and this board is built for a phone, so the honest thing is to hand over the
// list and remember your place in it. Which surfaces appear is registry-declared
// ("install": true) — the sheet never offers a tile that would still come out as
// a screenshot of its own page, the same rule the board follows for links it
// has not resolved.
const sheet = document.getElementById('sheet');
const ticked = () => { try { return JSON.parse(localStorage.getItem('fd-installed') || '[]'); }
                       catch (e) { return []; } };
const setTicked = a => localStorage.setItem('fd-installed', JSON.stringify(a));

function drawSheet(){
  if(!DATA) return;
  const done=ticked(), rows=(DATA.services||[]).filter(s=>s.install);
  const list=document.getElementById('ilist');
  if(!rows.length){
    list.innerHTML='<div class="empty">No service is marked installable yet. '
      +'Add <b>"install": true</b> to an entry in services.json once it serves '
      +'an apple-touch-icon — see the README.</div>';
    document.getElementById('idone').textContent='0 of 0';
    return;
  }
  let h='';
  for(const s of rows){
    // reachability comes off the same live scan the tiles use — a surface that
    // is down or loopback-bound is shown greyed with the reason, not offered
    const open=s.url&&s.linkable;
    const why=s.reach==='host'?'host only':(s.reach==='down'?'offline':':'+s.port);
    const inner=`${svg(s.icon)}<span class="nm">${esc(s.name)}<span>${why}</span></span>`;
    h+=`<div class="irow${open?'':' off'}">`
      +(open?`<a class="go" href="${esc(s.url)}" target="_blank" rel="noopener">${inner}</a>`
            :`<span class="go">${inner}</span>`)
      +`<button class="tick${done.includes(s.id)?' done':''}" data-id="${esc(s.id)}"`
      +` aria-label="Mark ${esc(s.name)} added">`
      +`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"`
      +` stroke-width="3" stroke-linecap="round" stroke-linejoin="round">`
      +`<path d="m5 13 5 5L20 7"/></svg></button></div>`;
  }
  list.innerHTML=h;
  const n=done.filter(id=>rows.some(s=>s.id===id)).length;
  document.getElementById('idone').textContent=n+' of '+rows.length+' added';
}

document.getElementById('ilist').addEventListener('click',e=>{
  const b=e.target.closest('.tick'); if(!b) return;
  const a=ticked(), i=a.indexOf(b.dataset.id);
  if(i<0) a.push(b.dataset.id); else a.splice(i,1);
  setTicked(a); drawSheet();
});
const closeSheet=()=>sheet.classList.remove('on');
document.getElementById('ins').addEventListener('click',()=>{drawSheet();sheet.classList.add('on')});
document.getElementById('iclose').addEventListener('click',closeSheet);
document.getElementById('ireset').addEventListener('click',()=>{setTicked([]);drawSheet()});
sheet.addEventListener('click',e=>{if(e.target===sheet)closeSheet()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSheet()});

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


# ── /call ────────────────────────────────────────────────────────────────────
# A spoken briefing, assembled from live state and read out in the operator's
# own cloned voice by the local TTS router on :8890.
#
# Why this and not a phone call: there is no telephony on this machine. No
# Twilio, no SignalWire, no number. A real PSTN call needs a cloud provider and
# a monthly line. What IS here is a full local voice stack — Chatterbox TTS with
# a voice reference at ~/wb-voice/refs/zayed_ref.wav, and whisper.cpp for the
# return leg — so a voice session over the tailnet costs nothing and leaves the
# machine. That is what this is. It is a callback, not a ring.
#
# The briefing is DETERMINISTIC. No model writes it: it is counts and names
# read out of the same scan the board renders and the same graph the lenses
# query. A model in this path would be a model that can invent an outage.
#
# :8890 is not on the tailnet, so the audio is proxied through here rather than
# linked. One origin, and the phone never needs a second port opened.
VOICE_URL = os.environ.get("WB_VOICE_URL", "http://127.0.0.1:8890/v1/audio/speech")
GRAPH_URL = os.environ.get("WB_GRAPH_URL", "http://127.0.0.1:4180")
SPEAK_MAX = 1200          # characters; a runaway briefing is a runaway TTS job


def _get_json(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        # An unreachable graph is a fact about the briefing, not a crash. The
        # caller says so out loud rather than reporting a healthy system it
        # could not actually see.
        return None


ROUTING = os.path.expanduser("~/.imsg-routing.json")


def fleet_state():
    """Every live tmux session as an agent, with what it is for and what it last said.

    The session LIST always comes from tmux, never from a file — the same rule
    imsg-router follows, and for the same reason: a hardcoded roster goes stale
    the first time a session is opened or killed, and then the fleet report is
    confidently wrong. Descriptions come from ~/.imsg-routing.json, which is
    also what the iMessage router reads, so Trace and the router cannot disagree
    about what a session is for.

    A session with no description is REPORTED as undescribed rather than hidden.
    An agent nobody has written a purpose for is the thing worth knowing about.
    """
    try:
        desc = json.load(open(ROUTING)).get("sessions") or {}
    except Exception:
        desc = {}
    raw = sh("tmux list-sessions -F '#{session_name}|#{session_attached}|#{session_activity}' 2>/dev/null")
    now, out = time.time(), []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, attached, activity = parts[0], parts[1] != "0", parts[2]
        try:
            idle = int(now - int(activity))
        except ValueError:
            idle = None
        # The last non-empty line of the pane. This is "reading the tunnel":
        # what the agent in there is actually showing right now. Truncated hard
        # — a pane can hold anything, including a stack trace.
        tail = ""
        for ln in reversed(sh(f"tmux capture-pane -p -t {name} -S -6 2>/dev/null").splitlines()):
            if ln.strip():
                tail = ln.strip()[:120]
                break
        out.append({
            "name": name,
            "attached": attached,
            "idle_secs": idle,
            "purpose": desc.get(name),
            "last_line": tail,
        })
    out.sort(key=lambda s: (s["purpose"] is None, s["name"].lower()))
    return out


# Patterns redacted before any pane content leaves this machine. A tmux pane is
# a scrollback of whatever the operator did in it — a cat of a .env, a curl with
# a bearer token, a printed key. That content is about to be posted to a cloud
# LLM, so it is scrubbed here, at the only point where the whole line is still
# visible. This is the same instinct the knowledge graph follows in refusing to
# store file bodies at all.
SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), "<redacted-key>"),
    (re.compile(r"(?i)\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}"), "<redacted-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"(?i)\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<redacted-jwt>"),
    (re.compile(r"(?i)(authorization|bearer)\s*[:=]?\s*\S{12,}"), r"\1 <redacted>"),
    # KEY=value / KEY: value where the key name smells like a credential.
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*\S+"),
     r"\1=<redacted>"),
    (re.compile(r"(?i)\bpostgres(?:ql)?://[^\s]*:[^\s@]*@"), "postgres://<redacted>@"),
]


def redact(text):
    for pat, sub in SECRET_PATTERNS:
        text = pat.sub(sub, text)
    return text


CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def transcript_for(cwd, turns=8):
    """The actual conversation in a tunnel, read from Claude Code's own log.

    THIS IS WHY capture-pane WAS NOT ENOUGH. Every agent pane runs in the
    alternate screen buffer (`alternate_on = 1`), so tmux holds no scrollback
    for it — the TUI owns the screen and repaints a viewport, which for the
    `pool` session is ten lines tall. Capturing it yields the input box and the
    status line, never the conversation. tmux is the wrong place to look.

    Claude Code writes each session to ~/.claude/projects/<encoded-cwd>/*.jsonl,
    one JSON record per line, and THAT is the conversation. The directory name
    is the working directory with every slash replaced by a dash.

    Returns the last `turns` exchanges, text only. Tool calls and file snapshots
    are skipped: they are most of the file by volume and none of it is what a
    human means by "what is it talking about".
    """
    if not cwd:
        return None
    encoded = cwd.replace("/", "-")
    d = os.path.join(CLAUDE_PROJECTS, encoded)
    if not os.path.isdir(d):
        return None
    logs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    if not logs:
        return None
    newest = max(logs, key=os.path.getmtime)

    out = []
    try:
        with open(newest, "r", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") not in ("user", "assistant"):
                    continue
                c = (r.get("message") or {}).get("content")
                if isinstance(c, list):
                    c = " ".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text")
                c = " ".join(str(c or "").split())
                if not c:
                    continue
                out.append({"role": r["type"], "text": redact(c)[:600]})
    except Exception:
        return None

    return {
        "source": os.path.basename(newest),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(os.path.getmtime(newest))),
        "total_turns": len(out),
        "turns": out[-int(turns):],
    }


def tunnel_context(name, lines=60):
    """Everything readable about one tunnel, structured.

    The earlier version returned ONE line of 120 characters, which is not
    context — it was enough to say a pane existed and nothing about what was
    happening in it. This returns the working directory, the running command,
    the git branch, and the tail of the scrollback, which together are what a
    human means by "what is that terminal doing".

    Scrollback is redacted on the way out (see SECRET_PATTERNS): the caller is
    a cloud model, and a pane is an unfiltered record of whatever was typed.
    """
    live = {f["name"] for f in fleet_state()}
    if name not in live:
        return {"found": False, "live": sorted(live)}

    def disp(fmt):
        return sh(f"tmux display-message -p -t {name} '{fmt}' 2>/dev/null").strip()

    cwd = disp("#{pane_current_path}")
    cmd = disp("#{pane_current_command}")
    panes = disp("#{window_panes}")
    branch = ""
    if cwd:
        branch = sh(f"git -C {cwd} rev-parse --abbrev-ref HEAD 2>/dev/null").strip()

    raw = sh(f"tmux capture-pane -p -t {name} -S -{int(lines)} 2>/dev/null")
    kept = [ln.rstrip() for ln in raw.splitlines()]
    # Box-drawing rules are most of a Claude Code pane by line count and carry
    # no information; dropping them roughly doubles the useful context that
    # fits in one tool response.
    kept = [ln for ln in kept if ln.strip() and not re.fullmatch(r"[\s\u2500-\u257f\-_=]+", ln)]

    try:
        desc = json.load(open(ROUTING)).get("sessions") or {}
    except Exception:
        desc = {}

    hit = next((f for f in fleet_state() if f["name"] == name), {})
    return {
        "found": True,
        "name": name,
        "purpose": desc.get(name),
        "idle_secs": hit.get("idle_secs"),
        "attached": hit.get("attached"),
        "cwd": cwd or None,
        "running": cmd or None,
        "git_branch": branch or None,
        "panes": int(panes) if panes.isdigit() else None,
        # An agent prompt vs a bare shell is the difference between a session
        # that can be given work and one that cannot.
        "has_agent": bool(cmd) and cmd.lower() not in {"zsh", "bash", "sh", "fish"},
        "scrollback": redact("\n".join(kept[-int(lines):])),
        "scrollback_lines": len(kept),
        # The pane is a viewport; this is the conversation. When both exist,
        # this is the one worth reading.
        "conversation": transcript_for(cwd),
        "pane_is_tui": disp("#{alternate_on}") == "1",
        "redacted": True,
    }


def agent_report(name):
    """Trace's read of ONE agent. Spoken, so it stays to a few sentences."""
    fleet = fleet_state()
    hit = next((f for f in fleet if f["name"] == name), None)
    if not hit:
        live = ", ".join(f["name"] for f in fleet[:8])
        return f"There is no session called {name}. Live right now: {live}."
    bits = [f"{hit['name']}."]
    bits.append(hit["purpose"] or "No purpose is written down for this one, which is worth fixing.")
    if hit["idle_secs"] is not None:
        mins = hit["idle_secs"] // 60
        bits.append("Active in the last minute." if mins < 1 else
                    f"Last activity {mins} minutes ago." if mins < 90 else
                    f"Quiet for {mins // 60} hours.")
    if hit["last_line"]:
        bits.append(f"Its pane currently ends with: {hit['last_line']}")
    return " ".join(bits)


OLLAMA = os.environ.get("WB_OLLAMA", "http://127.0.0.1:11434/api/generate")
ROUTER_MODEL = os.environ.get("WB_ROUTER_MODEL", "qwen3.8:27b-mlx")
# Upstream shipped the author's own number as the default here, which meant a
# fresh clone texted a stranger on its first notify. Read it from config.json
# instead and default to EMPTY: this is operator data, not code, and an
# unconfigured notify channel must stay silent rather than pick a recipient.
OPERATOR_PHONE = (os.environ.get("WB_OPERATOR_PHONE")
                  or CONF.get("operator", {}).get("phone", "")).strip()
# The Homebrew binary, NOT the ~/bin wrapper. TCC grants Automation per exact
# binary path, and the grant lives on /opt/homebrew/Cellar/imsg/.../imsg. The
# wrapper runs under zsh, which a launchd-spawned server does not inherit a
# grant for — so going through it fails with "authorization denied (code: 23)".
IMSG = "/opt/homebrew/bin/imsg"


ROUTER_PATH = os.path.expanduser("~/bin/imsg-router")
WHISPER_BIN = os.environ.get("WB_WHISPER", "/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = os.environ.get(
    "WB_WHISPER_MODEL", os.path.expanduser("~/eveng2/scripts/models/ggml-base.en.bin"))
_router = None


def router():
    """imsg-router, imported as a module rather than reimplemented.

    Its delivery is not trivial — it refuses a session that is not live, refuses
    a pane sitting at a bare shell (keys typed there run as shell commands
    instead of reaching an agent), types the payload, waits, then sends Enter,
    and appends the ticket footer that lets the agent close its own ticket. A
    second copy of that in this file would be a second dispatcher free to drift
    from the first, and the two would eventually disagree about what "delivered"
    means. One dispatcher, two front doors — iMessage and this.

    It has no import-time side effects: everything below its constants is a
    def, and execution is behind `if __name__ == "__main__"`.
    """
    global _router
    if _router is None:
        import importlib.util
        import importlib.machinery
        spec = importlib.util.spec_from_loader(
            "imsg_router",
            importlib.machinery.SourceFileLoader("imsg_router", ROUTER_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _router = mod
    return _router


def transcribe(audio_bytes, suffix=".webm"):
    """Browser audio -> text, via ffmpeg to 16k mono and whisper.cpp.

    Measured at half a second for a sentence on this box, which is what makes
    talking to it feel like talking rather than like filling in a form.
    """
    import tempfile
    if not os.path.exists(WHISPER_MODEL):
        return None, f"no whisper model at {WHISPER_MODEL}"
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "in" + suffix)
        wav = os.path.join(td, "in.wav")
        with open(raw, "wb") as fh:
            fh.write(audio_bytes)
        conv = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", raw,
             "-ar", "16000", "-ac", "1", wav],
            capture_output=True, text=True, timeout=60)
        if conv.returncode != 0 or not os.path.exists(wav):
            return None, "could not decode the recording"
        p = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-nt", "-np"],
            capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            return None, "transcription failed"
        # -nt drops timestamps; whisper still emits bracketed non-speech markers
        # like [BLANK_AUDIO], which are not words the operator said.
        text = re.sub(r"\[[^\]]*\]", " ", p.stdout)
        text = " ".join(text.split())
        return (text or None), (None if text else "nothing audible")


def dispatch(target, instruction):
    """Actually send an instruction into a tunnel. The only mutating path here.

    Deliberately requires an EXPLICIT target from the caller. The router's own
    policy note says a model guess can be wrong — it once sent a message naming
    'media' to 'ops' — so an inferred route is proposed and the operator
    confirms. This function is what the confirmation calls, never the classifier.
    """
    r = router()
    ticket_id = None
    try:
        t = subprocess.run(
            [os.path.expanduser("~/bin/req"), "file", instruction[:120],
             "--target", target, "--origin", "agent"],
            capture_output=True, text=True, timeout=25)
        m = re.search(r"REQ-\d+", (t.stdout or "") + (t.stderr or ""))
        ticket_id = m.group(0) if m else None
    except Exception:
        # A missing ticket must not block delivery; it is recorded as absent so
        # the reply can say the instruction went out untracked.
        ticket_id = None
    try:
        ok, detail = r.deliver(target, instruction, [], ticket_id)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "ticket": ticket_id}
    return {"ok": bool(ok), "detail": str(detail), "ticket": ticket_id}


def explicit_target(text):
    """(session, instruction) when the operator NAMED a session, else (None, text).

    Explicit beats inferred — the router's own policy, and the reason it fires
    a named target without proposing anything. The classifier is for when you
    did not say where it goes; running it when you did is the system second-
    guessing a decision you already made, which is what made the suggestions
    feel wrong so often.

    "media: redo the hero" and "media redo the hero" both address media.
    Longest name first, so 'pro' never shadows 'prod'. Reuses imsg-router's own
    separators so the two front doors parse an address the same way.
    """
    names = sorted((f["name"] for f in fleet_state()), key=len, reverse=True)
    stripped = (text or "").strip()
    low = stripped.lower()
    for n in names:
        nl = n.lower()
        if not low.startswith(nl):
            continue
        rest = stripped[len(n):]
        if not rest:
            continue
        if rest[0] in (":", "-", " ", ","):
            body = rest[1:].strip()
            if body:
                return n, body
    return None, stripped


def route_task(text):
    """Which tunnel should this go to? Trace proposes; he does not dispatch.

    Same model and same session-list-from-tmux rule as imsg-router, so a
    proposal here and a proposal over iMessage cannot disagree. PROPOSES ONLY:
    the router's own policy note says a model guess can be wrong — it once sent
    a message naming 'media' to 'ops' — so the guess is offered and the operator
    confirms. Nothing is typed into a pane from this surface.
    """
    fleet = fleet_state()
    if not fleet:
        return {"target": None, "why": "No tmux sessions are running, so there is nowhere to send it."}
    menu = "\n".join(
        f"- {f['name']}: {f['purpose'] or 'no description'}" for f in fleet)
    prompt = (
        "You route one instruction to exactly one tmux session. Reply with JSON only: "
        '{\"target\":\"<session name>\",\"why\":\"<one short sentence>\"}. '
        "The target MUST be one of these session names.\n\n"
        f"Sessions:\n{menu}\n\nInstruction: {text}\n"
    )
    body = json.dumps({"model": ROUTER_MODEL, "stream": False,
                       "format": "json", "prompt": prompt}).encode()
    try:
        req = urllib.request.Request(
            OLLAMA, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = json.loads(r.read().decode()).get("response", "{}")
        pick = json.loads(raw)
    except Exception as exc:
        return {"target": None,
                "why": f"The local router did not answer ({type(exc).__name__})."}
    names = {f["name"] for f in fleet}
    target = pick.get("target")
    if target not in names:
        # A model naming a session that does not exist is the failure mode the
        # live-list rule exists for. Refuse it rather than pass it on.
        return {"target": None,
                "why": f"It proposed '{target}', which is not a live session."}
    return {"target": target, "why": (pick.get("why") or "").strip()[:200]}


def send_text(body_text):
    """Text the operator. One recipient, fixed — this is a notify channel, not a sender."""
    if not OPERATOR_PHONE:
        # No recipient configured. Refusing beats falling back to any default:
        # the only wrong recipient a notify channel can have is someone else's.
        return False, "no operator phone configured (set operator.phone in config.json)"
    if not os.path.exists(IMSG):
        return False, "imsg wrapper not found"
    try:
        p = subprocess.run(
            [IMSG, "send", "--to", OPERATOR_PHONE, "--service", "imessage",
             "--text", body_text[:900]],
            capture_output=True, text=True, timeout=30)
        out = (p.stdout or "") + (p.stderr or "")
        ok = p.returncode == 0 and "denied" not in out.lower()
        if not ok and "authorization denied" in out.lower():
            # Named precisely, because the remedy is a one-time TCC grant and
            # not a code change: System Settings > Privacy & Security >
            # Automation, allow the portal's python to control Messages.
            out = ("Messages automation is not granted to the portal process "
                   "(TCC). Sending from a launchd job needs that grant.")
        return ok, out.strip()[:220]
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def brief_text():
    """The spoken briefing. Plain sentences — this is read aloud, not printed."""
    scan_now = cached_scan()
    svcs = scan_now.get("services", [])
    down = [s for s in svcs if not s.get("up")]
    parts = ["Trace here."]

    # The fleet comes first because it is the half that has people in it.
    fleet = fleet_state()
    if fleet:
        undesc = [f["name"] for f in fleet if not f["purpose"]]
        quiet = [f["name"] for f in fleet
                 if f["idle_secs"] is not None and f["idle_secs"] > 6 * 3600]
        parts.append(f"{len(fleet)} agent sessions are up.")
        if quiet:
            parts.append(f"Quiet for over six hours: {', '.join(quiet[:4])}.")
        if undesc:
            parts.append(f"{len(undesc)} have no purpose written down: {', '.join(undesc[:4])}.")

    if down:
        names = ", ".join(s.get("name") or s.get("id") for s in down[:5])
        more = f", and {len(down) - 5} more" if len(down) > 5 else ""
        parts.append(f"{len(down)} of {len(svcs)} services are down: {names}{more}.")
    else:
        parts.append(f"All {len(svcs)} services are up.")

    stats = _get_json(f"{GRAPH_URL}/api/stats")
    if stats is None:
        parts.append("The knowledge graph is unreachable, so I have nothing to say about it.")
    else:
        parts.append(
            f"The graph holds {stats.get('nodes', 0)} nodes and "
            f"{stats.get('edges', 0)} edges, with {stats.get('rejects', 0)} rejects.")
        ghosts = stats.get("ghosts") or 0
        contra = stats.get("contradictions") or 0
        if ghosts or contra:
            bits = []
            if ghosts:
                bits.append(f"{ghosts} cited documents that do not exist")
            if contra:
                bits.append(f"{contra} contradictions between canonical sources")
            parts.append("Worth a look: " + ", and ".join(bits) + ".")
        else:
            parts.append("No ghosts and no contradictions.")

    lex = _get_json(f"{GRAPH_URL}/api/ask/lexicon")
    if lex and not lex.get("honest_absence"):
        a = lex.get("answer") or {}
        checked, off = a.get("checked", 0), a.get("off_grammar", 0)
        parts.append(
            f"All {checked} agents follow the naming grammar."
            if not off else
            f"{off} of {checked} agent names are off grammar.")

    return " ".join(parts)


# ── which surface is home ────────────────────────────────────────────────────
# Two front doors now exist and one of them has to answer `/`. That choice is
# per DEVICE, not per machine: the board belongs on the desk where there is room
# for forty tiles, and the simple screen belongs on the phone. A setting in
# config.json would force one answer onto both, so this is a cookie.
#
# The canonical paths never lie — `/board` is always the board and `/phone` is
# always the simple screen, whatever the cookie says. Only `/` follows the
# preference, which is what the installed Home Screen tile opens (start_url is
# `/`), and so what "change to simple UI" actually has to change.
#
# Set through a GET that redirects rather than from script, because the simple
# screen is server-rendered and carries almost no JS — and a preference that
# needs JS to stick is a preference that fails on the surface most likely to be
# opened when something is already wrong.
HOME_COOKIE = "fd_home"
HOME_CHOICES = ("board", "simple")
HOME_MAX_AGE = 60 * 60 * 24 * 365


def home_pref(cookie_header):
    """Which surface this device wants at `/`. Board unless told otherwise."""
    if not cookie_header:
        return "board"
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return "board"          # a malformed jar is not worth a 500
    got = jar.get(HOME_COOKIE)
    value = got.value if got else ""
    return value if value in HOME_CHOICES else "board"


# ── /phone ───────────────────────────────────────────────────────────────────
# The board shows everything on the machine, which is what a board is for. This
# is the opposite surface: a clock and six buttons sized for a thumb, for the
# times you already know where you are going.
#
# The list is DATA, not markup, so changing the front screen is editing one line
# rather than editing a page. Ids resolve against the same registry the board
# uses, so a button's URL is still worked out live from lsof binds and
# `tailscale serve` — nothing here hardcodes a link that can rot.
#
# A service that is registered but down still gets its button, dimmed and
# unclickable, labelled `down`. Hiding it would make a dead service and an
# unregistered one look identical from the one screen most likely to be opened
# when something is wrong.
# The six keys on /phone, resolved against the same registry the board uses.
# Upstream hardcoded the author's own six. That is operator data — a fresh
# clone rendered six "not registered" stubs — so this fork reads it from
# config.json and falls back to upstream's list only when unset.
PHONE_APPS = (CONF.get("phone", {}).get("apps")
              or ["chat", "messages", "cockpit", "graph", "terminal", "pm"])

# Where the CALL key goes. Trace's conversational surface lives in the cockpit
# app, because that is where the ElevenLabs SDK, the admin session and the tool
# dispatcher already are — this server is stdlib Python and cannot host it.
#
# Resolved from the registry rather than hardcoded, like every other link on
# this board: the cockpit's reachable URL comes from lsof plus `tailscale
# serve`, and only its PATH is swapped. A pasted https:// link here would rot
# the first time the port or the tailnet name moved.
#
# If the cockpit is down the key falls back to /call — the local briefing, which
# needs nothing but this machine. Degrading to something that still works beats
# a dead button, and the label says which one you are getting.
# `?call=1` asked the cockpit for the CALL SCREEN rather than the Trace console.
# Without it the key landed on an admin surface whose own call button was a
# chip in the corner: a call screen handing off to a text UI containing a
# second, smaller CALL. One intent, one screen.
#
# NOW /admin/trace/local, WHICH IS A DIFFERENT VOICE STACK. ElevenLabs began
# refusing sessions on a billing problem (code 1002, "payment issue") and there
# was no way to see it from here — the API key lacks `user_read`, so the quota
# reads 401. A key that opens a call which dies on arrival is worse than one
# that opens a call which works, so it points at the stack that costs nothing:
# whisper.cpp for the ear, the operator's own cloned voice on :8890 for the
# mouth, and the same tool gate behind both.
#
# The trade is push-to-talk instead of full duplex — whisper transcribes a
# finished recording, so there is nothing to hear until you stop speaking.
# ?call=1 still works and is unchanged; it is one edit back if the account is
# topped up and full duplex is wanted again.
CALL_TARGET_ID = "cockpit"
CALL_TARGET_PATH = "/admin/trace/local"


# The CALL key reaches a voice stack that is entirely upstream-operator-specific:
# an Ollama router, ElevenLabs or whisper.cpp for the ear, a cloned voice on
# :8890, an `imsg` wrapper for the mouth, and a "cockpit" app to host the SDK
# this stdlib server cannot. On a machine with none of that, the key renders
# someone else's avatar over a link that goes nowhere useful. Opt-in, default
# off — the same treatment as operator.phone.
CALL_ENABLED = bool(CONF.get("call", {}).get("enabled", False))


def call_destination(by_id):
    """(href, label, sub) for the CALL key, or None when it is switched off."""
    if not CALL_ENABLED:
        return None
    s = by_id.get(CALL_TARGET_ID)
    if s and s.get("linkable") and s.get("url"):
        base = s["url"]
        registry_path = "/admin/console"
        if base.endswith(registry_path):
            base = base[: -len(registry_path)]
        return base + CALL_TARGET_PATH, "Call Trace", "live \u00b7 full duplex"
    return "/call", "Brief", "cockpit down \u00b7 local readout"

CALL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>Trace</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Trace">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="icon" href="data:,">
<style>
 *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
 html,body{height:100%}
 body{background:#0f1216;color:#dde5ec;font:14px/1.55 ui-monospace,"SF Mono",Menlo,monospace;
   display:flex;flex-direction:column;
   padding:max(16px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom))}
 header{display:flex;align-items:center;gap:10px;padding-bottom:10px}
 header b{color:#63b8b0;font-weight:400;letter-spacing:.2em;text-transform:uppercase;font-size:13px}
 header .sub{color:#5a6772;font-size:11px;letter-spacing:.1em}
 header a{margin-left:auto;color:#5a6772;text-decoration:none;font-size:11px;letter-spacing:.14em}
 .hide{display:none}

 .dock{border:2px solid #2f5a56;border-radius:14px;background:#0b1a17;padding:11px;
   display:flex;flex-direction:column;gap:9px;margin-bottom:11px}
 .dock.min{padding:7px 11px;flex-direction:row;align-items:center;gap:9px}
 .dock.min #st,.dock.min .ask{display:none}
 .row{display:flex;align-items:center;gap:8px}
 button{font:inherit;cursor:pointer}
 #go{flex:1;min-height:50px;border-radius:10px;border:2px solid #2f5a56;background:#0d221d;
   color:#63b8b0;font-size:15px;letter-spacing:.2em;text-transform:uppercase}
 #go:active{border-color:#63b8b0}
 #go[disabled]{opacity:.45}
 .dock.min #go{min-height:32px;font-size:12px;flex:0 0 88px}
 .chip{border:1px solid #2f5a56;background:transparent;color:#63b8b0;border-radius:8px;
   font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:7px 10px}
 .chip:active{background:#14262a}
 .chip[disabled]{opacity:.4}
 .chip.rec{background:#3a1220;border-color:#a33;color:#ff8fa3}
 #st{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#5a6772;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .lamp{width:9px;height:9px;border-radius:50%;background:#2c353e;flex:0 0 auto}
 .lamp.on{background:#63b8b0;animation:p 1.4s ease-in-out infinite}
 @keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
 .ask{display:flex;gap:7px}
 .ask input{flex:1;background:#0f1216;border:1px solid #2f5a56;border-radius:8px;
   color:#dde5ec;font:inherit;font-size:13px;padding:9px 10px;min-width:0}
 .ask input::placeholder{color:#2c353e}

 .tr{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:9px;padding:2px 0 10px}
 .msg{border-left:2px solid #2f5a56;padding:5px 0 5px 11px}
 .msg .who{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#63b8b0}
 .msg .when{color:#2c353e;margin-left:7px}
 .msg p{color:#93a1ad;margin-top:3px;white-space:pre-wrap}
 .msg.you{border-left-color:#2c353e}
 .msg.you .who{color:#5a6772}
 .msg .act{margin-top:7px;display:flex;gap:6px;flex-wrap:wrap}
 .empty{color:#2c353e;font-size:12px;text-align:center;padding:22px 0}

 h2{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#5a6772;margin:4px 0 7px}
 .fleet{display:flex;flex-wrap:wrap;gap:6px;padding-bottom:6px}
 .ag{border:1px solid #262e37;background:#161b21;color:#dde5ec;border-radius:8px;
   font-size:12px;padding:6px 10px;display:flex;align-items:center;gap:6px}
 .ag:active{border-color:#63b8b0}
 .ag[disabled]{opacity:.4}
 .ag i{font-style:normal;width:6px;height:6px;border-radius:50%;background:#63b8b0}
 .ag.q i{background:#2c353e}
 .ag.nd{border-style:dashed;color:#93a1ad}
</style></head><body>
 <header><span class="lamp" id="lamp"></span><b>Trace</b>
   <span class="sub" id="who">the fleet steward</span><a href="/phone">&lsaquo; home</a></header>

 <div class="dock" id="dock">
   <div class="row">
     <button id="go">Call</button>
     <button class="chip" id="talk">Talk</button>
     <button class="chip" id="stop" disabled>Stop</button>
     <button class="chip" id="mini">Min</button>
   </div>
   <form class="ask" id="askf" autocomplete="off">
     <input id="q" placeholder="say 'pool: fix the editor' to send it straight there" />
     <button class="chip" type="submit" id="send">Ask</button>
   </form>
   <div id="st">tap call for the fleet briefing</div>
 </div>

 <div id="body">
   <h2>Agents &mdash; tap one and Trace reads that tunnel</h2>
   <div class="fleet" id="fleet"></div>
   <h2>Transcript</h2>
 </div>
 <div class="tr" id="tr"><div class="empty">nothing yet</div></div>

<script>
 var go=D('go'), st=D('st'), tr=D('tr'), dock=D('dock'), lamp=D('lamp'),
     body=D('body'), mini=D('mini'), stop=D('stop'), who=D('who'),
     q=D('q'), askf=D('askf'), send=D('send'), talk=D('talk');
 function D(i){return document.getElementById(i)}
 var audio=new Audio(); audio.preload='auto';

 // iOS refuses play() outside the tap that started it. Awaiting anything first
 // loses the gesture and Safari throws NotAllowedError — "the request is not
 // allowed". So the element is unlocked SYNCHRONOUSLY on the first tap with a
 // silent wav; after that its src can be swapped from an async callback.
 var SILENCE='data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
 var unlocked=false;
 function unlock(){ if(unlocked) return;
   audio.src=SILENCE; var p=audio.play(); if(p&&p.catch) p.catch(function(){}); unlocked=true; }

 // ── one call at a time, and it ALWAYS ends ───────────────────────────────
 // The first cut had neither. Tapping a second agent while the first was in
 // flight left both writing to the transcript, and the superseded audio never
 // fired onended — so the Call button stayed disabled forever and the only way
 // out was reloading the page. Every call now takes a ticket, and finish()
 // runs on every exit path including the ones that throw.
 var ticket=0, blobUrl=null;
 function busy(on){
   go.disabled=on; send.disabled=on; stop.disabled=!on;
   if(!recording) talk.disabled=on;
   [].forEach.call(document.querySelectorAll('.ag'),function(b){b.disabled=on});
   lamp.classList.toggle('on',on);
 }
 function finish(msg){ busy(false); st.textContent=msg||'ready'; }
 function halt(){
   ticket++;                       // invalidates anything in flight
   try{audio.pause()}catch(e){}
   if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null}
   finish('stopped');
 }
 stop.onclick=halt;

 function stamp(){var d=new Date();
   return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
 function say(name,text,mine){
   var e=tr.querySelector('.empty'); if(e) e.remove();
   var d=document.createElement('div');
   d.className='msg'+(mine?' you':'');
   d.innerHTML='<div class="who">'+name+'<span class="when">'+stamp()+'</span></div>';
   var p=document.createElement('p'); p.textContent=text; d.appendChild(p);
   tr.appendChild(d); tr.scrollTop=tr.scrollHeight; return d;
 }

 // The unlock plays a zero-length silent wav, which fires onended immediately.
 // Without this guard that reset ran a few milliseconds INTO every call and
 // re-enabled the agent chips, so a second tap could still race the first —
 // the original lock-up wearing a different hat.
 function isSilence(){ return (audio.currentSrc||audio.src||'').indexOf('data:audio/wav')===0 }
 audio.onended=function(){ if(!isSilence()) finish('done'); };
 audio.onerror =function(){ if(!isSilence()) finish('audio failed'); };

 mini.onclick=function(){
   var m=dock.classList.toggle('min');
   body.classList.toggle('hide',m);
   mini.textContent=m?'Max':'Min';
 };

 async function speak(text,mine){
   var mine2=mine;
   st.textContent='synthesising';
   var r=await fetch('/api/speak',{method:'POST',
     headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})});
   if(mine2!==ticket) return;            // superseded while the TTS ran
   if(!r.ok){var j=await r.json().catch(function(){return{}});
     throw new Error(j.error||('tts '+r.status));}
   if(blobUrl) URL.revokeObjectURL(blobUrl);
   blobUrl=URL.createObjectURL(await r.blob());
   audio.src=blobUrl;
   st.textContent='speaking';
   await audio.play();
 }

 async function call(agent){
   unlock();
   var mine=++ticket; busy(true);
   st.textContent=agent?('reading '+agent):'assembling';
   if(agent) say('you','read '+agent,true);
   try{
     var url='/api/brief'+(agent?('?agent='+encodeURIComponent(agent)):'');
     var b=await (await fetch(url)).json();
     if(mine!==ticket) return;
     say('Trace',b.text);
     await speak(b.text,mine);
     if(mine!==ticket) return;
   }catch(e){ if(mine===ticket){ say('Trace','I could not finish that. '+e.message);
     finish('failed'); } }
 }
 go.onclick=function(){call(null)};

 // ── ask / route ──────────────────────────────────────────────────────────
 askf.onsubmit=function(ev){ ev.preventDefault();
   var text=q.value.trim(); if(text){ q.value=''; ask(text); } };

 async function ask(text){
   unlock();
   var mine=++ticket; busy(true);
   say('you',text,true); st.textContent='routing';
   try{
     var r=await (await fetch('/api/route',{method:'POST',
       headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})})).json();
     if(mine!==ticket) return;
     // Named a session? It just goes. That is the router's own policy —
     // explicit beats inferred, and proposing a target you already chose is the
     // system second-guessing you, which is what made the suggestions feel
     // wrong so often. Only a GUESS gets a confirmation step.
     if(r.explicit&&r.target){
       st.textContent='sending to '+r.target;
       var res=await (await fetch('/api/dispatch',{method:'POST',
         headers:{'Content-Type':'application/json'},
         body:JSON.stringify({target:r.target,text:r.text||text})})).json();
       var msg=res.ok
         ? ('Sent to '+r.target+(res.ticket?'. Ticket '+res.ticket+'.':'. No ticket filed.'))
         : ('Could not deliver that. '+res.detail);
       say('Trace',msg);
       await speak(msg,mine);
       return;
     }
     var line = r.target
       ? ('That goes to '+r.target+'. '+(r.why||''))
       : ('I cannot place that. '+(r.why||''));
     var node=say('Trace',line);
     // Trace proposes; the operator decides. Nothing is typed into a pane from
     // here — the iMessage router owns dispatch, and two dispatchers would be
     // two things that can send the same instruction twice.
     var act=document.createElement('div'); act.className='act';
     var t=document.createElement('button'); t.className='chip'; t.textContent='Text me this';
     t.onclick=async function(){
       t.disabled=true; t.textContent='sending';
       var res=await (await fetch('/api/text',{method:'POST',
         headers:{'Content-Type':'application/json'},
         body:JSON.stringify({text:'Trace: '+line+'\\n\\nTask: '+text})})).json();
       t.textContent=res.ok?'texted':'text failed';
     };
     act.appendChild(t);
     // Dispatch is a SEPARATE, explicit tap. The router's own policy is that a
     // model guess proposes and the operator confirms — this button is that
     // confirmation, and it is the only thing on this surface that can type
     // into a pane.
     if(r.target){
       var d=document.createElement('button'); d.className='chip';
       d.textContent='Send to '+r.target;
       d.onclick=async function(){
         d.disabled=true; d.textContent='sending';
         var res=await (await fetch('/api/dispatch',{method:'POST',
           headers:{'Content-Type':'application/json'},
           body:JSON.stringify({target:r.target,text:text})})).json();
         d.textContent=res.ok?('sent'+(res.ticket?' · '+res.ticket:'')):'refused';
         say('Trace', res.ok
           ? ('Delivered to '+r.target+(res.ticket?'. Ticket '+res.ticket+'.':'. No ticket was filed.'))
           : ('I could not deliver that. '+res.detail));
       };
       act.appendChild(d);
     }
     node.appendChild(act);
     await speak(line,mine);
   }catch(e){ if(mine===ticket){ say('Trace','Routing failed. '+e.message); finish('failed'); } }
 };

 // ── voice in ────────────────────────────────────────────────────────────
 // Tap to start, tap to stop. Hold-to-talk loses the recording every time a
 // notification steals the touch, which on a phone is often.
 var rec=null, chunks=[], recording=false;
 talk.onclick=async function(){
   unlock();
   if(recording){ rec && rec.state!=='inactive' && rec.stop(); return; }
   if(!navigator.mediaDevices||!window.MediaRecorder){
     say('Trace','This browser will not give me a microphone.'); return; }
   try{
     var stream=await navigator.mediaDevices.getUserMedia({audio:true});
     rec=new MediaRecorder(stream); chunks=[];
     rec.ondataavailable=function(e){ if(e.data.size) chunks.push(e.data); };
     rec.onstop=async function(){
       stream.getTracks().forEach(function(t){t.stop()});
       recording=false; talk.textContent='Talk'; talk.classList.remove('rec');
       var blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
       if(blob.size<1200){ st.textContent='too short'; return; }
       busy(true); st.textContent='transcribing';
       try{
         var r=await (await fetch('/api/listen',{method:'POST',
           headers:{'Content-Type':blob.type},body:blob})).json();
         busy(false);
         if(!r.text){ st.textContent=r.why||'nothing heard'; return; }
         ask(r.text);                    // straight into the same routing path
       }catch(e){ busy(false); st.textContent='transcription failed'; }
     };
     rec.start(); recording=true;
     talk.textContent='Stop rec'; talk.classList.add('rec');
     st.textContent='listening';
   }catch(e){
     say('Trace','Microphone permission was refused, so I cannot hear you.');
     st.textContent='no mic';
   }
 };

 (async function(){
   var f=(await (await fetch('/api/fleet')).json()).fleet||[];
   who.textContent=f.length+' tunnels';
   var el=D('fleet');
   f.forEach(function(a){
     var b=document.createElement('button');
     var quiet=a.idle_secs!=null&&a.idle_secs>6*3600;
     b.className='ag'+(quiet?' q':'')+(a.purpose?'':' nd');
     b.innerHTML='<i></i>';
     b.appendChild(document.createTextNode(a.name));
     b.title=(a.purpose||'no purpose written down')+(a.last_line?('\\n\\n'+a.last_line):'');
     b.onclick=function(){call(a.name)};
     el.appendChild(b);
   });
 })();
</script>
</body></html>"""



# The registry name is what the board shows; a few read better big and short.
# Overridable from config.json ("phone": {"labels": {...}}) for the same reason.
PHONE_LABELS = {"chat": "FLEET", "cockpit": "COCKPIT", "graph": "GRAPH",
                "pm": "BOARD", "messages": "MESSAGES", "terminal": "TERMINAL"}
PHONE_LABELS.update(CONF.get("phone", {}).get("labels", {}))

PHONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>__MACHINE__</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__MACHINE__">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="manifest" href="/phone.webmanifest">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%230f1216'/><path d='M6 12h6l4 10 4-16 4 12h2' stroke='%2363b8b0' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
 *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
 html,body{height:100%}
 body{background:#0f1216;color:#dde5ec;
   font:16px/1.4 ui-monospace,"SF Mono",Menlo,monospace;
   display:flex;flex-direction:column;
   padding:max(18px,env(safe-area-inset-top)) 18px max(18px,env(safe-area-inset-bottom))}
 /* The clock is the point of the screen, so it gets the room. */
 .clock{text-align:center;padding:22px 0 4px}
 .time{font-size:clamp(56px,19vw,104px);line-height:1;letter-spacing:.02em;
   color:#63b8b0;font-weight:400;font-variant-numeric:tabular-nums}
 .date{margin-top:10px;font-size:13px;letter-spacing:.22em;text-transform:uppercase;color:#5a6772}
 .grid{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:12px;
   align-content:center;padding:18px 0}
 a.key,span.key{display:flex;flex-direction:column;align-items:center;justify-content:center;
   gap:8px;min-height:104px;border:2px solid #262e37;border-radius:14px;
   background:#161b21;color:#dde5ec;text-decoration:none;
   font-size:15px;letter-spacing:.14em;text-transform:uppercase}
 a.key:active{border-color:#63b8b0;background:#1b2229}
 span.key{opacity:.34}
 .key svg{width:30px;height:30px;stroke:#63b8b0;stroke-width:1.6;fill:none;
   stroke-linecap:round;stroke-linejoin:round}
 span.key svg{stroke:#5a6772}
 .st{font-size:10px;letter-spacing:.16em;color:#5a6772}
 /* The call key gets the width and the only colour on the screen, because it
    is the one button that does something rather than going somewhere. */
 a.call{display:flex;align-items:center;justify-content:center;gap:11px;
   min-height:66px;border:2px solid #2f5a56;border-radius:14px;background:#0b1a17;
   color:#63b8b0;text-decoration:none;font-size:16px;letter-spacing:.2em;
   text-transform:uppercase;margin-bottom:12px}
 a.call:active{border-color:#63b8b0;background:#14262a}
 a.call em{font-style:normal;font-size:9.5px;letter-spacing:.14em;color:#2f7d6d;
   text-transform:uppercase}
 a.call .face{width:34px;height:34px;border-radius:50%;object-fit:cover;
   border:2px solid rgba(99,184,176,.45);
   animation:tbreathe 2.8s ease-in-out infinite}
 @keyframes tbreathe{0%,100%{box-shadow:0 0 10px rgba(99,184,176,.20)}
   50%{box-shadow:0 0 16px rgba(99,184,176,.40)}}
 .foot{text-align:center;font-size:11px;letter-spacing:.14em;color:#2c353e;padding-top:4px}
 .foot a{color:#5a6772;text-decoration:none}
 .foot a:active{color:#63b8b0}
 /* Lit when this screen already owns `/`, so the sentence reads as a state
    before it reads as a button. */
 .foot a.on{color:#63b8b0}
 .foot .sep{padding:0 8px;color:#262e37}

 /* Tapping CALL acknowledges here and connects THERE. The cockpit answers
    ?call=1 with the connecting screen — the same figure, the same rings — so
    drawing one here too was two overlays for one action, and the operator has
    to sit through both. This screen's job is now only to say the tap landed:
    the key lights, the sound plays, and the surface that is actually
    connecting is the one that shows connecting. */
 a.call.opening{border-color:#63b8b0;background:#14262a}
 a.call.opening span{opacity:.55}
</style></head><body>
 <div class="clock"><div class="time" id="t">--:--</div><div class="date" id="d">&nbsp;</div></div>
 <div class="grid">__KEYS__</div>
__CALL__ <div class="foot"><a href="/board">all __N__ services &rsaquo;</a><span class="sep">·</span>__HOME_TOGGLE__</div>

<script>
 function tick(){
   var n=new Date();
   var h=n.getHours(), m=String(n.getMinutes()).padStart(2,'0');
   document.getElementById('t').textContent=h+':'+m;
   document.getElementById('d').textContent=n.toLocaleDateString(undefined,
     {weekday:'long', day:'numeric', month:'long'});
 }
 tick(); setInterval(tick, 10000);


 // ── the tap ──────────────────────────────────────────────────────────────
 // Sound only. The screen this leads to draws the connecting state, so drawing
 // one here as well meant the operator sat through two of them for one action.
 //
 // The hold is 160ms: long enough for the key to light and the first blip to
 // land, short enough that it reads as the button responding rather than as a
 // wait. The line-opening sweep is deliberately gone with the overlay — a sound
 // describing a screen that is no longer here was the audio half of the same
 // duplication.
 (function(){
   var call = document.querySelector('a.call');
   if(!call) return;
   var href = call.getAttribute('href');
   var armed = false;

   // Built inside the tap and nowhere else: iOS starts every AudioContext
   // suspended and only resume() inside a user gesture lifts it.
   var AC = window.AudioContext || window.webkitAudioContext, actx = null;
   function blip(f, dur, t, type, peak){
     var o = actx.createOscillator(), g = actx.createGain();
     o.type = type; o.frequency.setValueAtTime(f, t);
     g.gain.setValueAtTime(0.0001, t);
     g.gain.linearRampToValueAtTime(peak, t + 0.012);
     g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
     o.connect(g); g.connect(actx.destination);
     o.start(t); o.stop(t + dur + 0.03);
   }
   function seize(){
     if(!AC) return;
     try{
       if(!actx) actx = new AC();
       if(actx.state === 'suspended') actx.resume();
       var t = actx.currentTime;
       blip(1180, .035, t, 'square', .07);          // the key going down
       blip(523.25, .30, t + .05, 'sine', .075);    // and the line taken
       blip(659.25, .30, t + .05, 'sine', .05);
     }catch(e){}
   }

   call.addEventListener('click', function(e){
     e.preventDefault();
     if(armed) return;              // a second tap is not a second call
     armed = true;
     call.classList.add('opening');
     seize();
     setTimeout(function(){ location.href = href; }, 160);
   });

   // Back from the cockpit restores this page from the back-forward cache with
   // the key still lit, on a screen the operator has finished with.
   window.addEventListener('pageshow', function(ev){
     if(ev.persisted){ armed = false; call.classList.remove('opening'); }
   });
 })();
</script>
</body></html>"""


# A refusal is the one failure mode a phone reports as nothing at all. Plain text
# gave the operator a blank-looking screen with no next action; this names the
# cause (wrong network) and the fix, in the portal's own visual language.
DENIED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>%(machine)s // off-tailnet</title>
<style>
 html,body{margin:0;background:#0f1216;color:#93a1ad;
   font:13px/1.6 ui-monospace,"SF Mono",Menlo,monospace}
 main{max-width:34rem;margin:0 auto;padding:14vh 22px}
 h1{color:#f7b552;font-size:12px;letter-spacing:.22em;text-transform:uppercase;margin:0 0 18px}
 p{margin:0 0 14px}b{color:#dde5ec;font-weight:600}
 code{color:#63b8b0}
 .box{border:1px solid #262e37;border-left:2px solid #f7b552;border-radius:3px;padding:14px}
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
    server_version = "fleetdeck"
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
        if path == "/api/tunnel":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            name = (q.get("name") or [""])[0]
            try:
                lines = max(10, min(200, int((q.get("lines") or ["60"])[0])))
            except ValueError:
                lines = 60
            if not name:
                return self._send(400, json.dumps({"error": "name is required"}),
                                  "application/json")
            out = tunnel_context(name, lines)
            return self._send(200 if out.get("found") else 404,
                              json.dumps(out), "application/json")

        if path == "/api/fleet":
            return self._send(200, json.dumps({"fleet": fleet_state()}),
                              "application/json")

        if path == "/api/brief":
            # ?agent=<session> narrows the briefing to one tunnel. The session
            # name is checked against the LIVE list inside agent_report, so an
            # arbitrary string cannot reach tmux.
            q = {}
            if "?" in self.path:
                from urllib.parse import parse_qs
                q = parse_qs(self.path.split("?", 1)[1])
            who = (q.get("agent") or [""])[0]
            text = agent_report(who) if who else brief_text()
            return self._send(200, json.dumps({"text": text, "agent": who or None}),
                              "application/json")

        if path == "/call":
            return self._send(200, CALL_PAGE, "text/html; charset=utf-8")

        if path == "/phone.webmanifest":
            return self._send(200, json.dumps({
                "name": MACHINE, "short_name": MACHINE,
                "start_url": "/phone", "scope": "/",
                "display": "fullscreen",
                "display_override": ["fullscreen", "standalone", "minimal-ui"],
                "background_color": "#0f1216", "theme_color": "#0f1216",
                "orientation": "portrait",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            }), "application/manifest+json")

        # `/home?ui=…` is the only thing that writes the preference. It answers
        # with a redirect to the surface it just selected, so choosing and
        # arriving are one tap and there is no state to disbelieve.
        if path == "/home":
            q = {}
            if "?" in self.path:
                from urllib.parse import parse_qs
                q = parse_qs(self.path.split("?", 1)[1])
            want = (q.get("ui") or [""])[0]
            if want not in HOME_CHOICES:
                return self._send(400, "ui must be board or simple\n", "text/plain")
            dest = "/phone" if want == "simple" else "/board"
            cookie = ("%s=%s; Path=/; Max-Age=%d; SameSite=Lax"
                      % (HOME_COOKIE, want, HOME_MAX_AGE))
            # 303: this was a GET that changed something, and the browser should
            # not re-issue it when the operator hits back.
            return self._send(303, "", "text/plain",
                              {"Location": dest, "Set-Cookie": cookie})

        if path in ("/phone", "/"):
            if path == "/" and home_pref(self.headers.get("Cookie")) != "simple":
                return self._board()
            return self._phone()

        if path == "/board":
            return self._board()

        if path == "/manifest.webmanifest":
            return self._send(200, json.dumps({
                "name": f"{BRAND} // {MACHINE}",
                "short_name": MACHINE,
                "start_url": "/",
                "scope": "/",
                # Ask for the whole screen and let the platform climb down.
                # Android honours `fullscreen` and drops the status bar
                # entirely; iOS ignores it and gives standalone, which with
                # black-translucent below is already its ceiling. Listing the
                # fallbacks explicitly means a browser that supports neither
                # lands on minimal-ui rather than a browser tab.
                "display": "fullscreen",
                "display_override": ["fullscreen", "standalone", "minimal-ui"],
                "background_color": "#0f1216",
                "theme_color": "#0f1216",
                "orientation": "portrait",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
                     "purpose": "maskable"},
                ],
            }), "application/manifest+json")

        # Trace's likeness, lifted from zaydr so he is one character across both
        # products rather than two things sharing a name.
        if path.startswith("/trace-") and path.endswith(".png"):
            icon = os.path.join(HERE, "assets", os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        if path.startswith("/icon-") and path.endswith(".png"):
            icon = os.path.join(HERE, "assets", os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        # One PNG per service, written by make-icons.py off the same glyph
        # library the board draws. Apps that can serve their own static files
        # should be given a copy (see `icon_dest` in services.json) so their
        # icon does not depend on this process; this route is the master, for
        # eyeballing the output and for surfaces that cannot host a file of
        # their own. basename() is the traversal guard — nothing outside
        # icons/ is reachable.
        if path.startswith("/icons/") and path.endswith(".png"):
            icon = os.path.join(ICONS, os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        if path in ("/api/status", "/api"):
            return self._send(200, json.dumps(cached_scan()), "application/json")

        self._send(404, "not here\n", "text/plain")

    # ── the two front doors ──────────────────────────────────────────────────
    # Both are reachable by a canonical path that always renders them, and `/`
    # picks between them from the cookie. Keeping them as methods rather than
    # branches in do_GET is what lets `/` do that without duplicating either.

    def _board(self):
        simple_is_home = home_pref(self.headers.get("Cookie")) == "simple"
        data = json.dumps(cached_scan()).replace("</", "<\\/")
        glyphs = json.dumps(glyph_map(), separators=(",", ":")).replace("</", "<\\/")
        page = (PAGE
                .replace("__BRAND__", esc_html(BRAND))
                .replace("__MACHINE__", esc_html(MACHINE))
                .replace("__SIMPLE_CLASS__", "on" if simple_is_home else "")
                .replace("__SIMPLE_HREF__",
                         "/home?ui=board" if simple_is_home else "/home?ui=simple")
                .replace("__SIMPLE_TITLE__",
                         "Simple screen is this device's home — tap for the board"
                         if simple_is_home else "Switch to the simple screen")
                .replace("__GLYPHS__", glyphs)
                .replace("window.__DATA__", f"JSON.parse({json.dumps(data)})"))
        return self._send(200, page, "text/html; charset=utf-8")

    def _phone(self):
        simple_is_home = home_pref(self.headers.get("Cookie")) == "simple"
        scan_now = cached_scan()
        by_id = {s["id"]: s for s in scan_now.get("services", [])}
        glyphs = glyph_map()
        keys = []
        for app_id in PHONE_APPS:
            s = by_id.get(app_id)
            if not s:
                # Named on the front screen and absent from the registry is an
                # editing mistake, not a state. Say so rather than quietly
                # rendering five buttons where six were asked for.
                keys.append(
                    '<span class="key"><span class="st">%s not registered</span></span>'
                    % esc_html(app_id))
                continue
            label = PHONE_LABELS.get(app_id, s.get("name") or app_id)
            icon = glyphs.get(s.get("icon"), glyphs.get("server", FALLBACK_GLYPH))
            svg = '<svg viewBox="0 0 24 24" aria-hidden="true">%s</svg>' % icon
            if s.get("linkable"):
                keys.append('<a class="key" href="%s">%s<span>%s</span></a>'
                            % (esc_html(s["url"]), svg, esc_html(label)))
            else:
                why = "down" if not s.get("up") else "no browser UI"
                keys.append('<span class="key">%s<span>%s</span>'
                            '<span class="st">%s</span></span>'
                            % (svg, esc_html(label), why))
        call = call_destination(by_id)
        if call:
            href, label, sub = call
            call_html = (' <a class="call" href="%s">'
                         '<img src="/trace-192.png" alt="" class="face">'
                         '<span>%s</span><em>%s</em></a>\n'
                         % (esc_html(href), esc_html(label), esc_html(sub)))
        else:
            # Switched off: emit nothing rather than a disabled-looking key. The
            # six keys reflow into the space on their own.
            call_html = ""
        toggle = ('<a class="on" href="/home?ui=board">unset as home</a>'
                  if simple_is_home
                  else '<a href="/home?ui=simple">set as home</a>')
        page = (PHONE_PAGE
                .replace("__MACHINE__", esc_html(MACHINE))
                .replace("__KEYS__", "".join(keys))
                .replace("__CALL__", call_html)
                .replace("__HOME_TOGGLE__", toggle)
                .replace("__N__", str(len(scan_now.get("services", [])))))
        return self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        """The one mutating route. Off unless agents.actions is switched on —
        see AGENT_ACTIONS. Even then it can only touch labels the scan already
        returned, so a caller cannot name an arbitrary launchd job."""
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not allowed(self.client_address[0]):
            return self._send(403, "no\n", "text/plain")

        # Speech is a POST because the text can be long, and a proxy rather than
        # a link because :8890 is not on the tailnet — the phone would have
        # nothing to fetch. Nothing here is stored: text in, audio out.
        if path == "/api/listen":
            # Raw audio body, not JSON — a base64 round trip of a voice clip
            # doubles the payload for nothing.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 8 * 1024 * 1024:
                    return self._send(400, json.dumps({"error": "empty or oversized clip"}),
                                      "application/json")
                blob = self.rfile.read(n)
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            text, why = transcribe(blob)
            if not text:
                return self._send(200, json.dumps({"text": None, "why": why or "nothing heard"}),
                                  "application/json")
            return self._send(200, json.dumps({"text": text}), "application/json")

        if path == "/api/dispatch":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            target = (body.get("target") or "").strip()
            instruction = (body.get("text") or "").strip()
            if not target or not instruction:
                return self._send(400, json.dumps({"error": "target and text are required"}),
                                  "application/json")
            # The target is checked against the LIVE session list, so a crafted
            # body cannot name a session that is not running.
            if target not in {f["name"] for f in fleet_state()}:
                return self._send(404, json.dumps(
                    {"ok": False, "detail": f"'{target}' is not a live session"}),
                    "application/json")
            out = dispatch(target, instruction[:1500])
            return self._send(200 if out["ok"] else 502, json.dumps(out),
                              "application/json")

        if path in ("/api/route", "/api/text"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "no text"}),
                                  "application/json")
            if path == "/api/route":
                named, body = explicit_target(text[:800])
                if named:
                    # No model call at all. You said where it goes.
                    return self._send(200, json.dumps({
                        "target": named, "text": body, "explicit": True,
                        "why": "You named it.",
                    }), "application/json")
                out = route_task(text[:800])
                out["explicit"] = False
                out["text"] = body
                return self._send(200, json.dumps(out), "application/json")
            ok, detail = send_text(text)
            return self._send(200 if ok else 502,
                              json.dumps({"ok": ok, "detail": detail}),
                              "application/json")

        if path == "/api/speak":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                text = (json.loads(self.rfile.read(n) or b"{}").get("text") or "").strip()
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            if not text:
                return self._send(400, json.dumps({"error": "no text"}),
                                  "application/json")
            # Bounded on purpose. TTS runs about three and a half seconds a
            # sentence on this box, so an unbounded body is an unbounded job.
            text = text[:SPEAK_MAX]
            payload = json.dumps({"model": "wb-voice", "voice": "", "input": text}).encode()
            req = urllib.request.Request(
                VOICE_URL, data=payload, headers={"Content-Type": "application/json"})
            try:
                # Generous: the first call after a restart pays ~20s of model
                # load. Later ones are a few seconds.
                with urllib.request.urlopen(req, timeout=90) as r:
                    return self._send(200, r.read(), "audio/mpeg")
            except Exception as exc:
                sys.stderr.write(f"speak failed: {exc}\n")
                sys.stderr.flush()
                return self._send(502, json.dumps(
                    {"error": f"voice router unreachable ({type(exc).__name__})"}),
                    "application/json")

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
