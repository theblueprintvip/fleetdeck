# fleetdeck — `1.0.0`

Two browser surfaces for one Mac, reachable from a phone over Tailscale:

| surface | port | what it is |
|---|---|---|
| **portal** | 8790 | One tile per local **server** and per **launchd agent**. Installs to the home screen as a PWA. |
| **chat** | 8783 | The whole **tmux fleet as a Messages-style thread list**. Tap a session, get a live writable terminal. |

Plus one internal: **:8784**, a loopback-only `ttyd` that is the chat's terminal
backend. It is never reachable directly — `chat_server.py` proxies it under `/t`.

Stdlib Python only. No pip, no node, no build step.

```bash
./install.sh          # reconcile this machine with this repo (idempotent)
fleetdeck doctor      # full inventory: services, agents, drift, binding, urls
```

---

## Install

**1. Put the repo somewhere launchd can read.**

```bash
git clone <this-repo> ~/.config/fleetdeck && cd ~/.config/fleetdeck
```

> `install.sh` **refuses to run** from `~/Documents`, `~/Desktop` or
> `~/Downloads`. Those are TCC-protected: launchd cannot read them without a
> Full Disk Access grant, and the jobs fail in a way that looks like a bug in
> this tool. `~/.config` or `~/srv` is safe.

**2. Dependencies.**

```bash
brew install tmux ttyd
```

Tailscale.app is required for remote reach. Without it both surfaces still run,
but only on loopback.

**3. Edit `config.json`** — set `brand` and `label_prefix` at minimum:

```json
{
  "brand": "ACME",
  "machine": "",
  "label_prefix": "com.acme",
  "ports": { "portal": 8790, "chat": 8783, "ttyd": 8784 }
}
```

Leave `machine` empty. It is resolved from Tailscale at boot — a hardcoded
tailnet name is the single thing most likely to be wrong on someone else's box.

**4. Run it.**

```bash
./install.sh
```

It renders the plists with this repo's path, this machine's `$HOME`, your
`python3`, and your prefix; installs `~/bin/fleetdeck`; bootstraps both agents;
sets up the `tailscale serve` front; and ends by running `doctor`.

**5. Curate the board.** Open the portal. Everything listening that isn't in
`services.json` appears under **unregistered** at the bottom with its process
name and port. Promote the ones that matter:

```json
{ "id": "api", "group": "apps", "port": 3000, "icon": "server",
  "name": "API", "blurb": "staging api" }
```

Hot-reloaded — no restart. `services.json` ships nearly empty on purpose: a wall
of offline tiles for apps this machine doesn't run is worse than an honest
discovery list.

---

## The honesty rule

**A tile never shows a link the portal has not resolved from live system state.**
Every scan reads three sources fresh:

| source | answers |
|---|---|
| `lsof -nP -iTCP -sTCP:LISTEN` | is it listening, and what did it bind? |
| `tailscale serve status --json` | is a loopback service proxied to a real URL? |
| `launchctl list` + `~/Library/LaunchAgents/*.plist` | what is scheduled, and how did it last exit? |

Nothing about a URL is hardcoded. Move a service to a new port, add a serve
mapping, kill a container — the next scan tells the truth without an edit.

A service bound to `127.0.0.1` with no serve mapping gets a **`host only`**
badge, not a dead link. A service that answers HTTP but has no browser UI is
marked **`api`** and is deliberately **not clickable**, so a tap can never land
on a 404.

---

## Agents

This is the half a port scan cannot see. Most of an operator's fleet is
scheduled work — nudges, watchdogs, backups, digests — and none of it listens on
a socket, so `lsof` reports it as simply absent.

Reading **both** the plist (installed) and `launchctl list` (loaded) is what
makes `unloaded` visible, which is the most common way a launchd job is quietly
doing nothing at all.

| state | lamp | meaning |
|---|---|---|
| `run` | green | has a PID right now |
| `ok` | slate | loaded, last exit clean — **a resting periodic job is healthy** |
| `off` | amber | plist on disk, never bootstrapped → **doing nothing** |
| `fail` | red | last run exited non-zero |

A negative exit status is a **signal** (`-15` = SIGTERM on reload), not a
failure, and is folded into `ok`. Getting that wrong paints a healthy board red,
which is the fastest way to make an operator stop reading it.

Filtering, in `config.json`:

```json
"agents": {
  "show": true,
  "actions": false,
  "include": ["com.acme."],
  "exclude": []
}
```

`include` empty = every non-vendor agent (Apple, Google, Adobe, Homebrew, Docker
and friends are filtered out — they're the OS, not the fleet).

### `actions` — read this before turning it on

`actions: true` adds **run/stop** buttons to every agent tile. These surfaces
have **no password** — the tailnet bind is the only boundary. Read-only, that's
a defensible posture. Start/stop is a much bigger claim, so it is **off by
default**.

The `POST /api/agent` route additionally refuses any label the scan did not
discover, so a caller cannot name an arbitrary launchd job even with actions on.

---

## Security posture

There is no password anywhere in the request path. What protects these surfaces:

- **portal** binds `127.0.0.1` and is fronted by `tailscale serve` with a real
  Let's Encrypt cert. Requests arrive from loopback; the tailnet boundary is
  enforced by Tailscale at the proxy. The per-request CGNAT check
  (`100.64/10`, `fd7a:115c:a1e0::/48`) stays as defence in depth.
- **chat** binds the tailnet address directly and **degrades closed to
  loopback** if Tailscale is down. That bind is the only thing between the
  tailnet and a writable shell, so it must never fall open.
- **ttyd** on `:8784` keeps a credential even on loopback, because it runs with
  `-a` (a URL can shape its command line) and must answer nothing but the chat
  server's proxy.

`FLEETDECK_OPEN=1` lifts the portal's IP check. Don't, except to debug.

**This is not production-grade.** It is tailnet-gated single-operator tooling.
Before putting it in front of anyone else you want real auth (Cloudflare Access
or Tailscale Serve + an identity header), an audit of the `/t` proxy, and rate
limiting.

---

## The traps

Every one of these cost real debugging time upstream. They're in the code as
comments too.

**Serve the portal on plain HTTP and iPhone Safari breaks three ways at once**,
all of which the phone reports identically as "it won't load": Safari upgrades
typed hostnames to HTTPS first and takes a hard TLS failure; a plain-HTTP origin
isn't a secure context so Add to Home Screen degrades; and off-tailnet requests
get a bare 403. The `tailscale serve` front fixes all three. Don't remove it.

**`launchctl bootout` is asynchronous.** Bootstrapping before the old job has
finished tearing down fails, and a `|| kickstart` fallback cannot rescue it —
there's nothing bootstrapped to kickstart. Both `install.sh` and the CLI poll
until the job is actually gone.

**TCC grants attach to a binary's resolved real path.** That's why `install.sh`
**copies** rather than symlinks, and why a `brew upgrade python` can silently
void a Full Disk Access grant. It's also why the chat server's shebang is
`/usr/bin/python3` (Apple's path never moves) and not the Homebrew one.

**`tmux` renders one window at one size.** A phone attaching to a session that
is also tiled on a monitor is a genuine conflict, not a bug. Two modes, toggled
in the chat's list header:

| mode | attach | effect |
|---|---|---|
| **LIVE** (default) | `new-session -A -s <name>` | browser becomes newest client, `window-size latest` hands it the size. A desktop tile reshapes to the phone while the tab is open and **snaps back when it closes**. |
| **PEEK** | `attach -f ignore-size,read-only -t <name>` | session keeps its geometry, drops keystrokes. Watch an agent without touching it. Cost: tmux pads unused rows with `·`. |

**Don't hardcode `/opt/homebrew`.** Intel Macs put brew in `/usr/local`, and
launchd runs with a PATH containing neither. `chat_server.py` resolves `tmux`
and `ttyd` via `shutil.which()` with both as fallbacks; the plists set an
explicit PATH.

**`install.sh` substitutes `__ROOT__` everywhere in the CLI.** If you add a
literal `__ROOT__` to a comparison or a comment it will be rewritten too. The
drift check builds the token at runtime (`printf '__%s__' ROOT`) for exactly
this reason.

---

## Layout

```
config.json          identity, ports, agent filters      ← edit this
services.json        the tile registry, hot-reloaded     ← and this
portal_server.py     discovery + page + PWA + agent API
chat_server.py       tmux thread list + ttyd proxy
audit.py             proves each tile's claim separately
bin/fleetdeck        CLI (rendered into ~/bin on install)
launchagents/*.tmpl  plist templates
assets/icon-*.png    home-screen icons                   ← replace with theirs
ttyd-index.html      ttyd's index + the viewport meta it omits
install.sh           reconcile machine with repo
auth                 loopback ttyd credential (git-ignored, minted on install)
```

## CLI

```
fleetdeck start [portal|chat]     bootstrap + set up the serve front
fleetdeck stop  [portal|chat]
fleetdeck restart
fleetdeck status                  up/down, pids, urls
fleetdeck doctor                  status + deps + serve map + drift + agent health
fleetdeck url                     the two links (portal is https, chat is http)
fleetdeck log [n]
fleetdeck edit                    $EDITOR services.json
fleetdeck audit                   verify every tile's claim
```

## Uninstall

```bash
UID_N=$(id -u); PREFIX=$(python3 -c 'import json;print(json.load(open("config.json"))["label_prefix"])')
for l in $PREFIX.fleetdeck-portal $PREFIX.fleetdeck-chat; do
  launchctl bootout "gui/$UID_N/$l" 2>/dev/null
  rm -f ~/Library/LaunchAgents/$l.plist
done
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=8790 off
rm -f ~/bin/fleetdeck
```
