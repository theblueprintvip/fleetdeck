#!/usr/bin/env python3
"""fleetdeck audit — prove the three claims the portal makes, one at a time.

A green lamp on the portal means only "something is listening on that port".
That is weaker than it looks, so this checks each claim separately:

  OPEN      does it actually answer HTTP, and does it answer WITHOUT a password?
            A 401 with a WWW-Authenticate header is a gate; anything 2xx/3xx/404
            from the server itself is open. Connection refused means the lamp is
            lying.
  MANAGED   is there a LaunchAgent behind it? Found structurally, not by name:
            take the listening PID, walk its parent chain, and look for a PID
            that launchctl reports owning. Docker containers never map this way
            — their supervisor is Docker's restart policy, so those are resolved
            against `docker inspect` instead and reported as such.
  LAMP      does the portal's own status agree with what just happened?

Read-only. Nothing here starts, stops, or edits a service.
"""

import concurrent.futures as cf
import json
import re
import ssl
import subprocess
import urllib.request

import json as _json, os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
try:
    _P = _json.load(open(_os.path.join(_HERE, "config.json")))["ports"]["portal"]
except Exception:
    _P = 8790
PORTAL = _os.environ.get("FLEETDECK_PORTAL", f"http://127.0.0.1:{_P}")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ── process → launchd ─────────────────────────────────────────────────────────

def port_pids():
    out, m = sh("lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null"), {}
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) < 3 or not f[-1].startswith("("):
            continue
        addr = f[-2]
        _, _, port = addr.rpartition(":")
        if port.isdigit():
            m.setdefault(int(port), (int(f[1]), f[0]))
    return m


def ppid_map():
    m = {}
    for line in sh("ps -eo pid=,ppid=").splitlines():
        p = line.split()
        if len(p) == 2:
            m[int(p[0])] = int(p[1])
    return m


def launchd_pids():
    """label -> pid, for every loaded agent that currently has one."""
    m = {}
    for line in sh("launchctl list").splitlines()[1:]:
        p = line.split("\t")
        if len(p) >= 3 and p[0].strip().isdigit():
            m[int(p[0])] = p[2].strip()
    return m


def docker_ports():
    """host port -> (container, restart policy)."""
    out = sh("/opt/homebrew/bin/docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null")
    m = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, ports = line.split("\t", 1)
        for hp in re.findall(r":(\d+)->", ports):
            m[int(hp)] = name
    if m:
        names = " ".join(sorted(set(m.values())))
        pol = sh(f"/opt/homebrew/bin/docker inspect -f "
                 f"'{{{{.Name}}}} {{{{.HostConfig.RestartPolicy.Name}}}}' {names} 2>/dev/null")
        policies = {}
        for line in pol.splitlines():
            p = line.split()
            if len(p) == 2:
                policies[p[0].lstrip("/")] = p[1]
        return {k: (v, policies.get(v, "?")) for k, v in m.items()}
    return {}


def owner(port, pids, parents, ld, dk):
    """Which supervisor restarts this port after a reboot?"""
    if port in dk:
        name, pol = dk[port]
        ok = pol in ("always", "unless-stopped")
        return ("docker", f"{name} ({pol})", ok)
    hit = pids.get(port)
    if not hit:
        return ("none", "not listening", False)
    pid = hit[0]
    seen = 0
    while pid and pid > 1 and seen < 12:
        if pid in ld:
            return ("launchd", ld[pid], True)
        pid = parents.get(pid, 0)
        seen += 1
    return ("none", f"unsupervised (pid {hit[0]}, {hit[1]})", False)


# ── http ──────────────────────────────────────────────────────────────────────

def probe(url):
    """(code, gated, note). 'gated' == a real auth challenge."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
            return r.status, False, ""
    except urllib.error.HTTPError as e:
        gated = e.code == 401 or bool(e.headers.get("WWW-Authenticate"))
        return e.code, gated, "auth challenge" if gated else ""
    except Exception as e:
        return 0, False, type(e).__name__


def main():
    with urllib.request.urlopen(f"{PORTAL}/api/status", timeout=10) as r:
        data = json.load(r)

    pids, parents, ld, dk = port_pids(), ppid_map(), launchd_pids(), docker_ports()
    svcs = data["services"]

    def check(s):
        url = s["url"] or f"http://127.0.0.1:{s['port']}/"
        code, gated, note = (0, False, "not listening") if not s["up"] else probe(url)
        kind, who, ok = owner(s["port"], pids, parents, ld, dk)
        # THE assertion this file exists for. A tile the operator can tap must
        # land on a working page. Badging something "api" while leaving it
        # tappable shipped him a tile that opened
        # {"error":"Unexpected endpoint or method. (GET /)"} — caught by him, not
        # by this audit. Any tappable tile not returning 200 is now a failure.
        tappable = bool(s.get("linkable") and s["url"])
        return {**s, "code": code, "gated": gated, "note": note,
                "sup_kind": kind, "sup": who, "sup_ok": ok,
                "tappable": tappable, "tap_ok": (not tappable) or code == 200}

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        rows = list(ex.map(check, svcs))

    order = {"link": 0, "host": 1, "down": 2}
    rows.sort(key=lambda r: (order[r["reach"]], r["group"], r["name"]))

    print(f"{'SERVICE':<13}{'PORT':<7}{'OPEN':<22}{'MANAGED BY':<34}{'LAMP'}")
    print("─" * 104)
    for r in rows:
        if not r["up"]:
            open_s = "— offline"
        elif r["code"] == 0:
            open_s = f"BROKEN ({r['note']})"
        elif r["gated"]:
            open_s = f"GATED {r['code']}"
        elif not r["tappable"]:
            open_s = f"{r['code']} not-linked"
        elif r["code"] != 200:
            open_s = f"TAP BROKEN {r['code']}"
        else:
            open_s = "open 200"

        sup = r["sup"] if r["sup_kind"] != "none" else f"NONE — {r['sup']}"
        # the lamp is honest when "listening" matches "answers"
        lamp = "ok" if (r["up"] == (r["code"] != 0)) else "WRONG"
        print(f"{r['name']:<13}:{r['port']:<6}{open_s:<22}{sup:<34}{lamp}")

    print()
    live = [r for r in rows if r["up"]]
    print(f"{len(svcs)} registered · {len(live)} listening")
    print(f"  answering HTTP : {sum(1 for r in live if r['code'])}/{len(live)}")
    print(f"  password-gated : {sum(1 for r in live if r['gated'])}")
    print(f"  supervised     : {sum(1 for r in live if r['sup_ok'])}/{len(live)}"
          f"  (launchd {sum(1 for r in live if r['sup_kind']=='launchd')},"
          f" docker {sum(1 for r in live if r['sup_kind']=='docker')})")
    bad = [r["name"] for r in rows if r["up"] != (r["code"] != 0)]
    print(f"  lamp accuracy  : {len(svcs)-len(bad)}/{len(svcs)}"
          + (f"  WRONG: {', '.join(bad)}" if bad else ""))
    taps = [r for r in rows if r["tappable"]]
    broke = [f"{r['name']} ({r['code']})" for r in taps if not r["tap_ok"]]
    print(f"  tappable tiles : {len(taps)-len(broke)}/{len(taps)} land on 200"
          + (f"  BROKEN: {', '.join(broke)}" if broke else ""))
    orphans = [r["name"] for r in live if not r["sup_ok"]]
    if orphans:
        print(f"\n  no supervisor (will not return after a reboot): {', '.join(orphans)}")
    gated = [r["name"] for r in live if r["gated"]]
    if gated:
        print(f"  still asking for a password: {', '.join(gated)}")


if __name__ == "__main__":
    main()
