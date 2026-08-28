#!/usr/bin/env python3
"""fleetdeck adopt — paste a repo URL, get a running, registered tile.

Standing up an app by hand is a dozen steps and four traps, and the steps are
identical every time: clone somewhere launchd can read, install deps, pick a
port nothing else holds, bind loopback, front it with `tailscale serve`, write
a plist, bootstrap it, CHECK THE EXIT STATUS, register a tile, generate an
icon. Only three of those need a human. This automates the rest.

    fleetdeck adopt https://github.com/someone/thing      # CLI
    the Adopt tile on the board                           # or paste it there

TWO PHASES, AND THE SPLIT IS THE WHOLE POINT
--------------------------------------------
`inspect` clones to a scratch directory and only READS. It never runs anything
from the repo. It reports the stack, the entry command, the port the project
declares, the environment variables it needs, whether it has install lifecycle
scripts, its licence and whether its remote is third-party — plus a free port
this machine can actually give it.

`adopt` is the half that executes, and it only runs after you have seen that
report and said yes. This matters more than it looks: `npm install` runs
lifecycle scripts from every transitive dependency, and these surfaces have no
password — the tailnet bind is the only boundary. A one-click installer that
skipped the report would be a remote-code-execution button for anything that
can reach the board. So the gate is not a formality; it is the security model.

WHAT IT WILL NOT DO
-------------------
  · Install into ~/Documents, ~/Desktop or ~/Downloads. Those are TCC-protected;
    launchd cannot read them and the job fails looking like a bug in this tool.
  · Overwrite an existing directory, or take a port something already holds.
  · Adopt a Docker repo. It detects one and says so — a container is
    daemon-managed with its own restart policy, not a launchd job, and
    pretending otherwise would produce a tile that lies about what runs it.
  · Push anything, ever.

Stdlib only, like the rest of fleetdeck.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "services.json")
CFG_FILE = os.path.join(HERE, "config.json")
HOME = os.path.expanduser("~")

# launchd cannot read these without a Full Disk Access grant.
TCC_DIRS = (f"{HOME}/Documents", f"{HOME}/Desktop", f"{HOME}/Downloads")
# Where an adopted repo lands. ~/srv keeps them together and out of the way.
ROOT = os.environ.get("FLEETDECK_ADOPT_ROOT", f"{HOME}/srv")
# Ports handed out from here up, skipping anything registered or listening.
PORT_FROM = 4200
PORT_TO = 4999

URL_OK = re.compile(r"^(https://[\w.-]+/[\w.\-/]+?)(?:\.git)?/?$")


def sh(cmd, cwd=None, timeout=900):
    """Run and capture. Never shell=True — every argument here is either a
    literal or a path this module built."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default if default is not None else {}


def taken_ports():
    """Everything the registry claims plus everything actually listening. Both,
    because a port can be reserved-but-down or live-but-unregistered."""
    used = set()
    for s in load(REGISTRY, {}).get("services", []):
        for p in (s.get("port"), (s.get("skin") or {}).get("port")):
            if p:
                used.add(int(p))
    rc, out = sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    for line in out.splitlines()[1:]:
        m = re.search(r":(\d+)\s+\(LISTEN\)", line)
        if m:
            used.add(int(m.group(1)))
    return used


def free_port(preferred=None):
    used = taken_ports()
    if preferred and preferred not in used and _bindable(preferred):
        return preferred
    for p in range(PORT_FROM, PORT_TO):
        if p not in used and _bindable(p):
            return p
    return None


def _bindable(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def slug(url):
    name = URL_OK.match(url).group(1).rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


# ── inspect: reads only, executes nothing from the repo ──────────────────────

def inspect(url):
    if not URL_OK.match(url or ""):
        return {"ok": False, "error": "Not a repo URL. Expected "
                                      "https://host/owner/name"}
    name = slug(url)
    dest = os.path.join(ROOT, name)
    scratch = tempfile.mkdtemp(prefix="fleetdeck-adopt-")
    clone = os.path.join(scratch, "repo")

    rc, out = sh(["git", "clone", "--depth", "1", "--quiet", url, clone], timeout=600)
    if rc != 0:
        shutil.rmtree(scratch, ignore_errors=True)
        return {"ok": False, "error": f"clone failed: {out.strip()[:400]}"}

    r = {"ok": True, "url": url, "name": name, "dest": dest, "scratch": scratch,
         "warnings": [], "blockers": [], "env": [], "stack": "unknown",
         "entry": None, "declared_port": None, "install": None,
         "workdir": None}

    if os.path.exists(dest):
        r["blockers"].append(f"{dest} already exists — move it or pick another name")
    for d in TCC_DIRS:
        if dest.startswith(d):
            r["blockers"].append(f"{d} is TCC-protected; launchd cannot read it")

    pkg = load(os.path.join(clone, "package.json"), None)
    has = lambda f: os.path.exists(os.path.join(clone, f))

    # A Dockerfile is usually an OPTION a repo ships, not the only way to run
    # it — plenty of projects have one next to a perfectly good `npm start`.
    # So note it and keep looking; it only becomes a blocker further down, if
    # nothing native turned up.
    dockerish = has("Dockerfile") or has("compose.yml") or has("docker-compose.yml")

    # A repo is often several stacks at once — a package.json for tooling next
    # to the app.py that actually serves. Detecting by "which marker file
    # exists first" picks the wrong one and then reports it cannot start
    # something that starts fine. So score every candidate and take the first
    # that yields a RUNNABLE command; a stack with no way to start it loses to
    # one that has one.
    candidates = []

    if pkg:
        scripts = pkg.get("scripts", {}) or {}
        blob = (json.dumps(pkg.get("dependencies", {}))
                + json.dumps(pkg.get("devDependencies", {})) + json.dumps(scripts))
        stack = "node"
        for framework, marker in (("Next.js", "next"), ("Vite", "vite"),
                                  ("Astro", "astro"), ("Remix", "remix")):
            if marker in blob:
                stack = f"node · {framework}"
                break
        entry = next((f"npm run {c}" for c in ("dev", "start", "serve", "preview")
                      if c in scripts), None)
        install = "npm ci" if has("package-lock.json") else "npm install"
        notes = []
        # Lifecycle scripts execute on install, before any code of yours runs.
        risky = [k for k in ("preinstall", "install", "postinstall", "prepare")
                 if k in scripts]
        if risky:
            notes.append(f"runs {', '.join(risky)} on install — executes repo code")
        eng = (pkg.get("engines") or {}).get("node")
        if eng:
            _, ver = sh(["node", "--version"])
            notes.append(f"declares node {eng}; this machine has "
                         f"{ver.strip() or 'no node'}")
        candidates.append((stack, entry, install, notes))

    if has("pyproject.toml") or has("requirements.txt") or has("app.py"):
        entry = next((f"python3 {c}" for c in
                      ("app.py", "main.py", "server.py", "__main__.py")
                      if has(c)), None)
        install = ("pip3 install -r requirements.txt" if has("requirements.txt")
                   else "pip3 install -e ." if has("pyproject.toml") else None)
        candidates.append(("python", entry, install, []))

    if has("index.html"):
        candidates.append(("static", "python3 -m http.server", None, []))

    # A monorepo's root package.json is often just tooling — husky, prettier,
    # eslint — and the app that actually serves lives one directory down. So if
    # the root yields nothing runnable, look one level in. One level only:
    # deeper than that and it stops being a guess and starts being a search.
    if not any(c[1] for c in candidates):
        found = []
        for sub in sorted(os.listdir(clone)):
            subdir = os.path.join(clone, sub)
            if sub.startswith(".") or not os.path.isdir(subdir):
                continue
            spkg = load(os.path.join(subdir, "package.json"), None)
            if not spkg:
                continue
            sscripts = spkg.get("scripts", {}) or {}
            sentry = next((c for c in ("dev", "start", "serve", "preview")
                           if c in sscripts), None)
            if not sentry:
                continue
            cmd = sscripts[sentry]
            fw = next((f for f in ("vite", "next", "astro", "remix")
                       if f in cmd), None)
            # Score, do not take the first. A monorepo usually has several
            # workspaces with a `dev` script, and the alphabetically-first one
            # is as likely to be the CLI as the app — GitNexus ships
            # `gitnexus/` (bin: gitnexus, dev: tsx watch) before
            # `gitnexus-web/` (dev: vite). What separates them is that one's
            # dev script actually invokes a web framework and the other
            # declares a binary.
            score = (2 if fw else 0) - (2 if spkg.get("bin") else 0) \
                + (1 if re.search(r"web|app|client|ui|site", sub, re.I) else 0)
            stack = f"node · {fw.title() if fw else 'workspace'} · {sub}/"
            found.append((score, sub, stack, sentry))
        if found:
            found.sort(key=lambda x: -x[0])
            _, sub, stack, sentry = found[0]
            others = [f"{s}/" for _, s, _, _ in found[1:]]
            note = [f"the app is in {sub}/, not the repo root"]
            if others:
                note.append(f"other workspaces that could also start: "
                            f"{', '.join(others)}")
            r["workdir"] = sub
            # npm ci at the ROOT only installs the root's own deps unless the
            # root declares `workspaces`. This repo did not, so the app's
            # dependencies — vite included — were never fetched and the job
            # died on `vite: command not found`. Install where the app is.
            root_ws = bool((pkg or {}).get("workspaces"))
            candidates.append((stack, f"npm run {sentry}",
                               "npm ci" if root_ws else f"npm ci --prefix {sub}",
                               note))

    runnable = next((c for c in candidates if c[1]), None)
    chosen = runnable or (candidates[0] if candidates else None)
    if chosen:
        r["stack"], r["entry"], r["install"], notes = chosen
        r["warnings"].extend(notes)
        others = [c[0] for c in candidates if c is not chosen]
        if others:
            r["warnings"].append(f"also looks like {', '.join(others)} — "
                                 f"running it as {r['stack']}")

    # A declared port, if the project names one anywhere obvious.
    for f in (".env.example", ".env.sample", "vite.config.js", "package.json"):
        p = os.path.join(clone, f)
        if not os.path.exists(p):
            continue
        try:
            m = re.search(r"(?:^PORT=|port[\"' ]?[:=]\s*)(\d{2,5})",
                          open(p, errors="replace").read(), re.M | re.I)
        except Exception:
            continue
        if m:
            r["declared_port"] = int(m.group(1))
            break

    for f in (".env.example", ".env.sample", ".env.template"):
        p = os.path.join(clone, f)
        if os.path.exists(p):
            for line in open(p, errors="replace"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    # Empty or placeholder-looking values are the ones you must
                    # supply; anything with a real default is left alone.
                    if not v.strip() or "your_" in v or v.strip().endswith("_here"):
                        r["env"].append(k.strip())
            break

    r["port"] = free_port(r["declared_port"])
    if r["port"] is None:
        r["blockers"].append(f"no free port in {PORT_FROM}-{PORT_TO}")
    elif r["declared_port"] and r["port"] != r["declared_port"]:
        r["warnings"].append(f"declares port {r['declared_port']}, which is "
                             f"taken — assigned {r['port']}")

    # Vite pins server.allowedHosts to localhost/127.0.0.1/.local unless HOST
    # is 0.0.0.0. `tailscale serve` forwards the original Host, so the tailnet
    # name gets a flat 403 while loopback works fine — the tile looks alive
    # from the Mac and dead from the phone. Two surfaces on this box have been
    # caught by it. The fix is a skin front, which rewrites Host to
    # 127.0.0.1; binding 0.0.0.0 instead would hand the app's API keys to
    # anything that can reach the machine.
    if "Vite" in r["stack"]:
        r["warnings"].append(
            "Vite rejects an unrecognised Host, so it will 403 behind "
            "tailscale serve — give it a skin front (see skin_server.py) "
            "rather than binding 0.0.0.0")


    lic = next((f for f in ("LICENSE", "LICENSE.md", "COPYING")
                if has(f)), None)
    r["license"] = "none found"
    if lic:
        head = open(os.path.join(clone, lic), errors="replace").read(400)
        r["license"] = next((n for n in ("MIT", "Apache", "BSD", "GPL", "MPL")
                             if n.lower() in head.lower()), "present")

    owner = URL_OK.match(url).group(1).split("/")[-2]
    r["owner"] = owner
    # Deliberately not asserting "third-party" — this has no idea which owners
    # are yours. It states the fact and the policy, and leaves the judgement.
    r["warnings"].append(f"remote is {owner}/ — adopt clones, never pushes")

    if dockerish:
        if r["entry"]:
            r["warnings"].append("also ships a Dockerfile — adopt runs it "
                                 "natively, not in a container")
        else:
            r["stack"] = "docker"
            r["blockers"].append(
                "Docker is the only way this runs. A container is "
                "daemon-managed with its own restart policy, not a launchd "
                "job — bring it up by hand, then register the tile and skin "
                "it if you want your icon on it.")
    elif not r["entry"]:
        r["blockers"].append("could not work out how to start it — no start "
                             "script, no app.py, no index.html")
    return r


# ── adopt: the half that executes ────────────────────────────────────────────

def adopt(plan, env_values=None, glyph="server", group="apps", label_prefix=None):
    """Run the plan. Everything here executes; nothing here runs without a
    plan that a human has already read."""
    steps = []
    def step(ok, msg):
        steps.append({"ok": bool(ok), "msg": msg})
        return ok

    if plan.get("blockers"):
        return {"ok": False, "steps": [{"ok": False, "msg": b}
                                       for b in plan["blockers"]]}

    name, dest, port = plan["name"], plan["dest"], plan["port"]
    clone = os.path.join(plan["scratch"], "repo")
    if not os.path.isdir(clone):
        return {"ok": False, "steps": [{"ok": False,
                "msg": "scratch clone is gone — re-inspect first"}]}

    os.makedirs(ROOT, exist_ok=True)
    shutil.move(clone, dest)
    step(True, f"moved into {dest}")
    shutil.rmtree(plan["scratch"], ignore_errors=True)

    if env_values:
        with open(os.path.join(dest, ".env"), "w") as fh:
            for k, v in env_values.items():
                fh.write(f"{k}={v}\n")
        step(True, f".env written ({len(env_values)} values)")

    if plan.get("install"):
        rc, out = sh(plan["install"].split(), cwd=dest, timeout=1800)
        if not step(rc == 0, f"{plan['install']}"
                    + ("" if rc == 0 else f" failed: {out.strip()[-300:]}")):
            return {"ok": False, "steps": steps}

    workdir = os.path.join(dest, plan["workdir"]) if plan.get("workdir") else dest
    label = f"{label_prefix or 'com.example'}.{name}"
    plist = os.path.join(HOME, "Library", "LaunchAgents", f"{label}.plist")
    entry = plan["entry"]
    if plan["stack"] == "static":
        entry = f"python3 -m http.server {port} --bind 127.0.0.1"
    elif entry.startswith("npm run"):
        entry = f"{entry} -- --host 127.0.0.1 --port {port}"

    with open(plist, "w") as fh:
        fh.write(PLIST.format(label=label, dest=workdir, entry=_xml(entry),
                              port=port, home=HOME, name=name))
    step(True, f"plist {label}")

    sh(["launchctl", "bootout", f"gui/{os.getuid()}", plist])
    rc, out = sh(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist])
    step(rc == 0, "bootstrapped" if rc == 0 else f"bootstrap failed: {out.strip()[:200]}")

    # The check that would have caught a job dead on exit 127 for months.
    import time
    time.sleep(6)
    rc, out = sh(["launchctl", "list", label])
    exit_ok = '"LastExitStatus" = 0' in out
    running = '"PID"' in out
    step(running or exit_ok,
         "job healthy" if (running or exit_ok)
         else f"job is not running — check /tmp/{name}.log")

    ts = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if os.path.exists(ts):
        rc, _ = sh([ts, "serve", "--bg", f"--https={port}",
                    f"http://127.0.0.1:{port}"])
        step(rc == 0, f"tailscale serve :{port}"
             if rc == 0 else f"tailscale serve :{port} failed")

    reg = load(REGISTRY, {})
    reg.setdefault("services", []).append({
        "id": name, "group": group, "port": port, "icon": glyph,
        "name": name.replace("-", " ").title(),
        "blurb": f"adopted from {plan['owner']}",
    })
    with open(REGISTRY, "w") as fh:
        json.dump(reg, fh, indent=2)
    step(True, "registered a tile")

    rc, out = sh([sys.executable, os.path.join(HERE, "make-icons.py"), name])
    step(rc == 0, "icon generated" if rc == 0 else "icon skipped")

    return {"ok": all(s["ok"] for s in steps), "steps": steps,
            "url": f"http://127.0.0.1:{port}", "port": port, "name": name}


def _xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <!-- Bound to loopback and fronted by `tailscale serve`. A dev server often
       brokers API keys to whoever can reach it, so the tailnet is the boundary
       and 0.0.0.0 is not on the table. -->
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string><string>-lc</string>
    <string>cd "{dest}" &amp;&amp; exec {entry}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{home}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PORT</key><string>{port}</string>
    <key>HOST</key><string>127.0.0.1</string>
  </dict>
  <key>WorkingDirectory</key><string>{dest}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>/tmp/{name}.log</string>
  <key>StandardErrorPath</key><string>/tmp/{name}.log</string>
</dict>
</plist>
"""


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: adopt.py <repo-url> [--yes]\n"
                 "       inspects and prints the plan; --yes also adopts")
    url = sys.argv[1]
    plan = inspect(url)
    if not plan.get("ok"):
        sys.exit(f"✗ {plan.get('error')}")

    print(f"\n  ADOPT  {plan['url']}\n")
    print(f"    stack      {plan['stack']}")
    print(f"    entry      {plan['entry'] or '—'}")
    print(f"    install    {plan['install'] or 'nothing to install'}")
    print(f"    port       {plan['port']}")
    print(f"    dest       {plan['dest']}")
    print(f"    license    {plan['license']}")
    if plan["env"]:
        print(f"    env        {len(plan['env'])} needed: {', '.join(plan['env'])}")
    for w in plan["warnings"]:
        print(f"    warn       {w}")
    for b in plan["blockers"]:
        print(f"    BLOCKED    {b}")
    print()

    if plan["blockers"]:
        shutil.rmtree(plan["scratch"], ignore_errors=True)
        sys.exit(1)
    if "--yes" not in sys.argv:
        print("  Nothing has been executed. Re-run with --yes to adopt.\n")
        shutil.rmtree(plan["scratch"], ignore_errors=True)
        return

    cfg = load(CFG_FILE, {})
    res = adopt(plan, label_prefix=cfg.get("label_prefix"))
    print()
    for s in res["steps"]:
        print(f"    {'✓' if s['ok'] else '✗'} {s['msg']}")
    print(f"\n  {'done' if res['ok'] else 'finished with errors'} — "
          f"{res.get('url', '')}\n")


if __name__ == "__main__":
    main()
