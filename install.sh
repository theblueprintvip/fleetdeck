#!/bin/bash
# install.sh — reconcile this machine with this repo.
#
# The repo is the source of truth. Both python servers are executed IN PLACE
# from here (the generated plists point at these paths), so this only places
# the parts that must live elsewhere:
#
#   bin/fleetdeck             -> ~/bin/fleetdeck            (on PATH)
#   launchagents/*.plist.tmpl -> ~/Library/LaunchAgents/     (launchd only scans there)
#
# The .tmpl files are rendered with this repo's path, this machine's HOME, the
# python3 you actually have, and the label_prefix + ports from config.json.
#
# COPIES, not symlinks, on purpose. macOS attributes TCC grants to a binary's
# RESOLVED REAL PATH — that is how a `brew upgrade python` silently voided this
# project's grants once already. Symlinking a launchd job's program into a
# different real path invites the same failure, and launchd has its own history
# of refusing symlinked plists. `fleetdeck doctor` reports drift instead, which
# is the cheap half of the trade.
#
# Idempotent. Safe to re-run. Does not touch tmux sessions.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional: name the surfaces to install. `./install.sh` does all of them, which
# is what a fresh machine wants. `./install.sh portal` does one, which is what a
# machine already running something on the other ports needs — installing a
# second agent onto a bound port gets you a crash-loop and a surface the
# operator was using a moment ago.
WANT=("$@")
wanted() {
  [ ${#WANT[@]} -eq 0 ] && return 0
  for w in "${WANT[@]}"; do [ "fleetdeck-$w" = "$1" ] && return 0; done
  return 1
}
BIN="$HOME/bin"
LA="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"
# The python3 the plists will point at, for the life of those jobs.
#
# `command -v python3` is not good enough. A Homebrew python earlier on PATH is
# the common case and the fragile one: this repo already documents that a `brew
# upgrade python` silently voided a TCC grant once, which is why chat_server.py
# is shebanged /usr/bin/python3 — "Apple's path never moves". install.sh picking
# the Homebrew one anyway contradicts that lesson in the same repo.
#
# It gets worse than drift. A python@3.14 pulled in as a BUILD dependency of
# something else can be left behind broken (on macOS 12 its post-install fails
# and the interpreter is SIGKILLed on every run). Rendering that path into the
# plists gets two agents that crash-loop instantly, and the operator sees a bug
# in this tool rather than a bad interpreter.
#
# So: prefer Apple's, and whatever we end up with, prove it actually runs.
for cand in /usr/bin/python3 "$(command -v python3 || true)"; do
  [ -x "$cand" ] || continue
  "$cand" -c 'import json,sys' >/dev/null 2>&1 || continue
  PY="$cand"; break
done
if [ -z "${PY:-}" ]; then
  echo "  ✗ no working python3 found (tried /usr/bin/python3 and PATH)"
  exit 1
fi

echo "▩ fleetdeck $(cat "$HERE/VERSION" 2>/dev/null || echo '?') — installing from $HERE"
echo

# ── preflight ────────────────────────────────────────────────────────────────
case "$HERE" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
    echo "  ✗ REFUSING: $HERE is inside a TCC-protected folder."
    echo "    launchd cannot read Documents/Desktop/Downloads without a Full Disk"
    echo "    Access grant, and the jobs will fail in a way that looks like a bug"
    echo "    in this tool. Move the repo (e.g. ~/.config/fleetdeck) and re-run."
    exit 1 ;;
esac

miss=0
for b in tmux ttyd; do
  command -v "$b" >/dev/null 2>&1 || { echo "  ✗ missing: $b   (brew install $b)"; miss=1; }
done
[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ] \
  || echo "  ~ Tailscale.app not found — the portal will stay loopback-only until it is installed"
[ "$miss" = 0 ] || { echo; echo "install the missing tools, then re-run."; exit 1; }

# config.json and services.json are operator data and deliberately untracked —
# see .gitignore. Seed them from the shipped templates on a fresh clone, and
# never touch them again: re-running install.sh must not overwrite a board the
# operator has curated.
for f in config services; do
  if [ ! -f "$HERE/$f.json" ]; then
    cp "$HERE/$f.example.json" "$HERE/$f.json" && echo "  + $f.json (from $f.example.json)"
  fi
done
[ -f "$HERE/config.json" ] || { echo "  ✗ no config.json and no template to seed it"; exit 1; }

read -r PREFIX PORTAL_PORT CHAT_PORT TTYD_PORT ADOPT_PORT <<<"$("$PY" - "$HERE/config.json" <<'EOF'
import json,sys
d=json.load(open(sys.argv[1]))
p=d.get("ports",{})
print(d.get("label_prefix","com.example").rstrip("."),
      p.get("portal",8790), p.get("chat",8783), p.get("ttyd",8784),
      p.get("adopt",8793))
EOF
)"
echo "  prefix     $PREFIX"
echo "  ports      portal:$PORTAL_PORT chat:$CHAT_PORT ttyd:$TTYD_PORT adopt:$ADOPT_PORT (loopback)"
echo "  python3    $PY"
echo

# ── the CLI ──────────────────────────────────────────────────────────────────
mkdir -p "$BIN" "$LA"
tmp="$(mktemp)"
sed "s|__ROOT__|$HERE|g" "$HERE/bin/fleetdeck" > "$tmp"
if cmp -s "$tmp" "$BIN/fleetdeck"; then
  echo "  = $BIN/fleetdeck (current)"
else
  cp "$tmp" "$BIN/fleetdeck" && chmod +x "$BIN/fleetdeck" && echo "  + $BIN/fleetdeck"
fi
rm -f "$tmp"

# ── the plists ───────────────────────────────────────────────────────────────
for t in "$HERE"/launchagents/*.plist.tmpl; do
  base="$(basename "$t" .plist.tmpl)"
  wanted "$base" || continue
  label="$PREFIX.$base"
  out="$LA/$label.plist"
  tmp="$(mktemp)"
  sed -e "s|__ROOT__|$HERE|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__PREFIX__|$PREFIX|g" \
      -e "s|__PYTHON__|$PY|g" \
      -e "s|__PORTAL_PORT__|$PORTAL_PORT|g" \
      -e "s|__CHAT_PORT__|$CHAT_PORT|g" \
      -e "s|__TTYD_PORT__|$TTYD_PORT|g" \
      -e "s|__ADOPT_PORT__|$ADOPT_PORT|g" \
      "$t" > "$tmp"
  if cmp -s "$tmp" "$out"; then echo "  = $out (current)"
  else cp "$tmp" "$out" && echo "  + $out"; fi
  rm -f "$tmp"
done

chmod +x "$HERE"/*.py "$HERE"/bin/* 2>/dev/null

# The chat server's ttyd child runs with -a (a URL can shape its command line),
# so it must never be reachable by anything but our own proxy. Mint a credential
# for that hop even though the public surfaces have none.
if [ ! -s "$HERE/auth" ]; then
  pw="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(18))')"
  printf 'fleet:%s' "$pw" > "$HERE/auth"
  chmod 600 "$HERE/auth"
  echo "  + $HERE/auth (loopback ttyd credential)"
fi

echo
echo "loading agents…"
for t in "$HERE"/launchagents/*.plist.tmpl; do
  base="$(basename "$t" .plist.tmpl)"
  wanted "$base" || continue
  l="$PREFIX.$base"
  if launchctl print "gui/$UID_N/$l" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID_N/$l" 2>/dev/null
    # bootout is ASYNCHRONOUS. Bootstrapping before the old job has finished
    # tearing down fails, and a `|| kickstart` fallback cannot rescue it —
    # nothing is bootstrapped to kickstart. Wait for it to actually be gone.
    for _ in $(seq 20); do
      launchctl print "gui/$UID_N/$l" >/dev/null 2>&1 || break
      sleep 0.3
    done
  fi
  if err="$(launchctl bootstrap "gui/$UID_N" "$LA/$l.plist" 2>&1)"; then
    echo "  ✓ $l"
  else
    echo "  ! $l FAILED to load: ${err:-unknown}"
  fi
done

# The portal binds loopback; this is what makes it reachable at all.
TSBIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
if [ -x "$TSBIN" ]; then
  if "$TSBIN" serve status 2>/dev/null | grep -q ":$PORTAL_PORT"; then
    echo "  = tailscale serve :$PORTAL_PORT"
  elif "$TSBIN" serve --bg --https="$PORTAL_PORT" "http://127.0.0.1:$PORTAL_PORT" >/dev/null 2>&1; then
    echo "  + tailscale serve :$PORTAL_PORT"
  else
    echo "  ! tailscale serve :$PORTAL_PORT failed"
    echo "    HTTPS must be enabled for the tailnet (admin console > DNS > HTTPS Certificates)."
    echo "    Until then the portal answers only on 127.0.0.1:$PORTAL_PORT."
  fi
fi

# Home-screen icons, if anything in the registry wants one. Non-fatal: this
# needs a Chromium to rasterise SVG, and a machine without one should still end
# up with a working board — it just has no PNGs until Chrome is installed and
# `fleetdeck icons` is run.
if "$PY" "$HERE/make-icons.py" >/dev/null 2>&1; then
  echo "  + home-screen icons (fleetdeck icons to regenerate)"
else
  echo "  ~ home-screen icons skipped — run 'fleetdeck icons' once a Chromium"
  echo "    is available, or set FLEETDECK_CHROME. The board is unaffected."
fi

sleep 5
echo
exec "$BIN/fleetdeck" doctor
