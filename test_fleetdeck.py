#!/usr/bin/env python3
"""Assertions for fleetdeck — the unit half runs offline, the live half proves
the deployed surface.

Weighted toward the agent classification, because that is where a wrong answer
is both easy to write and expensive: launchd reports a signal exit as a
negative number, and reading -15 as a failure paints a healthy board red —
including the portal's own tile, on the page it is serving.

    python3 test_fleetdeck.py           # unit + live
    python3 test_fleetdeck.py --unit    # unit only (no tailnet needed)
"""

import importlib.util
import json
import os
import plistlib
import ssl
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "portal_server", os.path.join(HERE, "portal_server.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

BASE = f"https://{P.HOST}:{P.PORT}"
_fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def agents_from(plists, state):
    """Run launch_agents() against a directory we control, so the assertions
    are about the rule rather than about whatever this machine happens to be
    running today."""
    with tempfile.TemporaryDirectory() as d:
        for label, body in plists.items():
            with open(os.path.join(d, f"{label}.plist"), "wb") as fh:
                plistlib.dump({"Label": label, **body}, fh)
        real_dir, real_state = P.AGENT_DIR, P.launchd_state
        P.AGENT_DIR = d
        P.launchd_state = lambda: state
        try:
            return {a["label"]: a for a in P.launch_agents()}
        finally:
            P.AGENT_DIR, P.launchd_state = real_dir, real_state


# ── health classification ────────────────────────────────────────────────────
# The regression most likely to ship, written first.

BASIC = {"ProgramArguments": ["/bin/true"]}

# The load-bearing one. `launchctl list` reports a signal exit as a negative
# number, and -15 is SIGTERM — what every agent shows after a reload. Reading
# that as a failure paints a healthy board red, the portal's own tile included.
# A live PID classifies as `run` rather than `ok`, which is the more precise of
# the two healthy states; what matters is that it is never `fail`.
a = agents_from({"com.acme.reloaded": BASIC},
                {"com.acme.reloaded": {"pid": 4242, "exit": -15}})
check("UNIT-1 live PID with exit -15 is healthy, never fail",
      a["com.acme.reloaded"]["health"] == "run",
      f"got {a['com.acme.reloaded']['health']}")

a = agents_from({"com.acme.broken": BASIC},
                {"com.acme.broken": {"pid": None, "exit": 1}})
check("UNIT-2 no PID with exit 1 is fail",
      a["com.acme.broken"]["health"] == "fail",
      f"got {a['com.acme.broken']['health']}")

a = agents_from({"com.acme.rested": BASIC},
                {"com.acme.rested": {"pid": None, "exit": 0}})
check("UNIT-3 no PID with exit 0 is not fail — a rested periodic job is healthy",
      a["com.acme.rested"]["health"] == "ok",
      f"got {a['com.acme.rested']['health']}")

a = agents_from({"com.acme.never": BASIC}, {})
check("UNIT-4 on disk but never bootstrapped is off",
      a["com.acme.never"]["health"] == "off",
      f"got {a['com.acme.never']['health']}")

a = agents_from({"com.acme.signal9": BASIC},
                {"com.acme.signal9": {"pid": None, "exit": -9}})
check("UNIT-5 a signal exit with no PID is still ok",
      a["com.acme.signal9"]["health"] == "ok",
      f"got {a['com.acme.signal9']['health']}")

# ── last output ──────────────────────────────────────────────────────────────
# Absence is a real answer and has to survive as one.

a = agents_from({"com.acme.silent": BASIC},
                {"com.acme.silent": {"pid": None, "exit": 0}})
t = a["com.acme.silent"]
check("UNIT-6 no StandardOutPath yields no timestamp",
      t["last_output"] is None and t["logged"] is False,
      f"last_output={t['last_output']} logged={t['logged']}")

with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as fh:
    fh.write(b"x")
    logfile = fh.name
a = agents_from({"com.acme.chatty": {**BASIC, "StandardOutPath": logfile}},
                {"com.acme.chatty": {"pid": None, "exit": 0}})
t = a["com.acme.chatty"]
check("UNIT-7 an existing log yields its mtime",
      isinstance(t["last_output"], float) and t["logged"] is True,
      f"last_output={t['last_output']}")
os.unlink(logfile)

a = agents_from({"com.acme.declared": {**BASIC,
                                       "StandardOutPath": "/tmp/definitely-not-here.log"}},
                {"com.acme.declared": {"pid": None, "exit": 0}})
t = a["com.acme.declared"]
check("UNIT-8 a declared but missing log is absence, not a fallback time",
      t["last_output"] is None and t["logged"] is True,
      f"last_output={t['last_output']} logged={t['logged']}")

# A plist that will not parse is a different thing from a job with nothing
# configured, and used to render identically to one. XML forbids `--` inside a
# comment; `plutil -lint` accepts it and plistlib does not, which is how two
# agents on this machine went blank over a hyphen.
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "com.acme.malformed.plist"), "w").write(
        '<?xml version="1.0"?><!-- a -- b --><plist version="1.0"><dict/></plist>')
    real_dir, real_state = P.AGENT_DIR, P.launchd_state
    P.AGENT_DIR, P.launchd_state = d, lambda: {}
    try:
        t = P.launch_agents()[0]
    finally:
        P.AGENT_DIR, P.launchd_state = real_dir, real_state
check("UNIT-11 an unparseable plist says so rather than rendering blank",
      t["unreadable"] and t["program"] == "plist will not parse",
      f"program={t['program']!r} unreadable={t['unreadable']!r}")

# ── curation ─────────────────────────────────────────────────────────────────

a = agents_from({"com.apple.somedaemon": BASIC, "com.acme.mine": BASIC},
                {"com.apple.somedaemon": {"pid": 1, "exit": 0},
                 "com.acme.mine": {"pid": 2, "exit": 0}})
check("UNIT-9 com.apple.* is never a tile",
      "com.apple.somedaemon" not in a and "com.acme.mine" in a,
      f"saw {sorted(a)}")

a = agents_from({"com.acme.z-ok": BASIC, "com.acme.a-fail": BASIC},
                {"com.acme.z-ok": {"pid": 9, "exit": 0},
                 "com.acme.a-fail": {"pid": None, "exit": 1}})
first = next(iter(a))
check("UNIT-10 failures sort first, before name",
      first == "com.acme.a-fail", f"first tile was {first}")

# ── live ─────────────────────────────────────────────────────────────────────

if "--unit" in sys.argv:
    print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED"))
    sys.exit(1 if _fails else 0)

ctx = ssl.create_default_context()


def get(path):
    return urllib.request.urlopen(BASE + path, timeout=20, context=ctx)


try:
    body = get("/api/status").read().decode()
    d = json.loads(body)
    check("LIVE-1 /api/status answers over the tailnet", True)
except Exception as e:
    check("LIVE-1 /api/status answers over the tailnet", False, str(e))
    d = {}

check("LIVE-2 the services grid still renders",
      len(d.get("services", [])) > 0,
      f"{len(d.get('services', []))} services")

ags = d.get("agents", [])
check("LIVE-3 agents are present", len(ags) > 0, f"{len(ags)} agents")
check("LIVE-4 no com.apple.* leaked into the board",
      not any(x["label"].startswith("com.apple.") for x in ags))
check("LIVE-5 every agent carries a last_output key, null or float",
      all("last_output" in x and (x["last_output"] is None
                                  or isinstance(x["last_output"], float))
          for x in ags))
check("LIVE-6 no running agent is classified fail",
      not any(x["health"] == "fail" and x["pid"] for x in ags),
      str([x["label"] for x in ags if x["health"] == "fail" and x["pid"]]))
check("LIVE-7 failures sort ahead of healthy tiles",
      [x["health"] for x in ags] == sorted(
          [x["health"] for x in ags],
          key=lambda h: {"fail": 0, "off": 1, "run": 2, "ok": 3}[h]))

# Every browser-facing tile should resolve to https. A plain-http origin is not
# a secure context, so Add to Home Screen degrades on exactly the tiles worth
# installing, and anything bound wider than loopback is also answering on the
# local Wi-Fi — a network that is not a boundary. `kind: api` tiles are exempt:
# they are never linked, so no browser ever lands on one.
insecure = [s for s in d.get("services", [])
            if (s.get("url") or "").startswith("http://")
            and s.get("kind") != "api" and s.get("linkable")]
check("LIVE-9 no linkable tile is served over plain http",
      not insecure,
      ", ".join(f"{s['name']} {s['url']}" for s in insecure))

try:
    page = get("/").read().decode()
    check("LIVE-8 the board renders with glyphs interpolated",
          "__GLYPHS__" not in page and "const I={" in page)
except Exception as e:
    check("LIVE-8 the board renders with glyphs interpolated", False, str(e))

print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED: "
                                            + ", ".join(_fails)))
sys.exit(1 if _fails else 0)
