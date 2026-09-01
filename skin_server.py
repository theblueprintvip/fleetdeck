#!/usr/bin/env python3
"""fleetdeck-skin — dress an app you do not own, from outside it.

Half the tiles on a real board are third-party apps in containers: an LLM
front-end, a database studio, a mail catcher, a metasearch box. None of them
will ever ship an apple-touch-icon that matches your fleet, and none can be
edited — an image tagged `:latest` means anything written into the container is
gone on the next pull. Writing a branding script per image works exactly once
and then has to be maintained per image, forever.

So skin from outside instead. This proxies an app verbatim and rewrites only
its <head> on the way past:

    :8871  fleetdeck-skin  ──>  :8880  the app
             └─ <title>, apple-touch-icon, manifest, theme-color,
                and a stylesheet injected last, so it wins on specificity ties

The container is never touched, which is the whole point: image updates become
irrelevant, and skinning the next app is a `skin` block in services.json rather
than a new script.

    {"id": "tts", "port": 8880, "install": true,
     "skin": {"name": "Script", "port": 8871, "css": "script.css",
              "replace": {"TheirName": "Script"},
              "recolor": {"99, 102, 241": "79, 227, 193"}}}

  port      where this front listens. Give it a `tailscale serve` mapping and
            point the phone at that; the portal resolves the tile to the front
            automatically when it is up, and falls back to the bare app when
            it is not.
  name      <title> and the home-screen name.
  css       a file in skins/, injected last so it beats their stylesheets.
  icon_set  which icons/<name>-*.png to serve; defaults to the service id.
  replace   literal substitutions in text/html — for names an app paints into
            its own markup, which no stylesheet can reach.
  recolor   literal substitutions in text/css — for colours an app hardcoded
            past its own variables. A colour map survives a redesign that
            moves every element; a selector-specific override does not.

Rules this follows, because a proxy that gets them wrong is worse than none:

  · Only text/html (and text/css when `recolor` is set) is buffered and
    rewritten. Everything else is relayed byte-for-byte as it arrives, so a
    long or streaming response never sits in memory waiting to be complete.
  · Accept-Encoding is stripped on the way in. Upstream then sends plain text
    and there is no gzip to decode before the rewrite, and none to re-encode
    after it.
  · A 101 is spliced raw in both directions and never rewritten — a websocket
    app's `Connection: Upgrade` is load-bearing.
  · The injected <head> is idempotent — a page already carrying the marker is
    passed through untouched, so a re-proxied response cannot accumulate.

There is a second use worth knowing about: some dev servers (Vite, for one)
refuse a Host header they do not recognise, which makes them 403 behind
`tailscale serve` even though the port is reachable. This front rewrites Host
to 127.0.0.1:<origin>, which those servers accept — so it fixes that without
binding the app to 0.0.0.0 or patching a file you do not own.

Stdlib only, like every other server here. No build step, no deps.
"""

import io
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "services.json")
ICONS = os.path.join(HERE, "icons")
SKINS = os.path.join(HERE, "skins")

BIND = os.environ.get("FLEETDECK_SKIN_BIND", "127.0.0.1")
THEME = "#05070a"
MARK = "fleetdeck-skin"  # the idempotency marker

# Hop-by-hop headers: meaningless to forward, and Content-Length/Transfer-
# Encoding are recomputed because the body length changes when we inject.
DROP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "content-length"}


def skins():
    """Registry, read per request — adding a skin is an edit, not a restart.
    Only the listen ports are fixed at startup, because a socket must bind."""
    try:
        with open(REGISTRY) as fh:
            reg = json.load(fh)
    except Exception as exc:
        sys.stderr.write(f"fleetdeck-skin: registry unreadable ({exc})\n")
        return {}
    out = {}
    for s in reg.get("services", []):
        sk = s.get("skin")
        if sk and sk.get("port"):
            out[int(sk["port"])] = {"id": s["id"], "origin": int(s["port"]), **sk}
    return out


def head_block(sk):
    """What every skinned page gains. Same tags the surfaces we own carry, so
    a skinned tile installs identically to a native one."""
    sid, name = sk["id"], sk.get("name", sk["id"])
    css = (f'<link rel="stylesheet" href="/_skin/skin.css">' if sk.get("css") else "")
    return (
        f'<meta name="{MARK}" content="{sid}">'
        f'<meta name="apple-mobile-web-app-capable" content="yes">'
        # Chrome reads the unprefixed form; iOS only honours the apple- one.
        f'<meta name="mobile-web-app-capable" content="yes">'
        f'<meta name="apple-mobile-web-app-title" content="{name}">'
        f'<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f'<meta name="theme-color" content="{THEME}">'
        f'<link rel="apple-touch-icon" href="/_skin/icon-180.png">'
        f'<link rel="manifest" href="/_skin/manifest.webmanifest">'
        f'{css}'
    )


def recolor(css, sk):
    """Substitute colour literals in the app's own stylesheets.

    An override sheet can only reach colours the app routes through a variable.
    A well-behaved app routes most of them and then hardcodes the same literal
    in a couple of dozen rules, which no amount of `:root` overriding will
    catch. The alternative is one selector-specific rule per site — every one a
    bet on a class name that upstream is free to rename, in an image tagged
    :latest.

    A colour map does not care about structure. `{"99, 102, 241": "79, 227,
    193"}` survives a redesign that moves every element on the page, which is
    the whole reason this skin is a palette pass and not a restyle."""
    for src, dst in (sk.get("recolor") or {}).items():
        css = css.replace(src, dst)
    return css


def rewrite(html, sk):
    """Retitle, substitute, inject. The block goes last thing before </head> so
    the stylesheet loads after theirs and wins any specificity tie."""
    if f'name="{MARK}"' in html:
        return html
    # Names the app paints into the page itself, which no stylesheet can reach.
    for src, dst in (sk.get("replace") or {}).items():
        html = html.replace(src, dst)
    name = sk.get("name")
    if name:
        html = re.sub(r"<title>.*?</title>", f"<title>{name}</title>", html,
                      count=1, flags=re.S | re.I)
    block = head_block(sk)
    if re.search(r"</head>", html, re.I):
        return re.sub(r"</head>", block + "</head>", html, count=1, flags=re.I)
    # No </head> is malformed but not our problem to fail on — put it after
    # <head>, or at the top if there is not one of those either.
    if re.search(r"<head[^>]*>", html, re.I):
        return re.sub(r"(<head[^>]*>)", r"\1" + block, html, count=1, flags=re.I)
    return block + html


def dechunk(rd):
    """Read a chunked body. Only reached for text/html, which is the one case
    that has to be whole before it can be rewritten."""
    out = io.BytesIO()
    while True:
        line = rd.readline()
        if not line:
            break
        n = int(line.split(b";")[0].strip() or b"0", 16)
        if n == 0:
            rd.readline()  # trailing CRLF
            break
        out.write(rd.read(n))
        rd.readline()
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fleetdeck-skin"

    def log_message(self, *a):
        pass  # the app behind us keeps its own log; this one adds nothing

    # -- our own routes ----------------------------------------------------
    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def dressing(self, path, sk):
        """/_skin/* is answered here and never forwarded — the app underneath
        knows nothing about any of it."""
        if path == "/_skin/manifest.webmanifest":
            name = sk.get("name", sk["id"])
            return self._send(200, json.dumps({
                "name": name, "short_name": name, "start_url": "/",
                "scope": "/", "display": "standalone",
                "background_color": THEME, "theme_color": THEME,
                "icons": [{"src": f"/_skin/icon-{n}.png", "sizes": f"{n}x{n}",
                           "type": "image/png"} for n in (192, 512)],
            }), "application/manifest+json")
        m = re.fullmatch(r"/_skin/icon-(\d+)\.png", path)
        if m:
            icon = os.path.join(ICONS, f"{sk.get('icon_set', sk['id'])}-{m.group(1)}.png")
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, b"", "text/plain")
        if path == "/_skin/skin.css":
            css = os.path.join(SKINS, os.path.basename(sk.get("css", "")))
            if sk.get("css") and os.path.exists(css):
                with open(css, "rb") as fh:
                    return self._send(200, fh.read(), "text/css; charset=utf-8")
            return self._send(404, b"", "text/plain")
        return self._send(404, b"", "text/plain")

    # -- the proxy ---------------------------------------------------------
    def relay(self):
        sk = skins().get(self.server.server_address[1])
        if not sk:
            return self._send(503, "no skin bound to this port\n", "text/plain")

        path = self.path.split("?")[0]
        if path.startswith("/_skin/"):
            return self.dressing(path, sk)

        try:
            up = socket.create_connection(("127.0.0.1", sk["origin"]), timeout=10)
        except Exception as exc:
            self.close_connection = True
            return self._send(502, f"{sk['id']} is not answering on "
                                   f"{sk['origin']}: {exc}\n", "text/plain")

        upgrade = "upgrade" in self.headers.get("Connection", "").lower()
        head = io.StringIO()
        head.write(f"{self.command} {self.path} HTTP/1.1\r\n")
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "accept-encoding"):
                continue  # see the docstring: no gzip to undo before rewriting
            if lk in ("connection", "keep-alive") and not upgrade:
                continue
            head.write(f"{k}: {v}\r\n")
        head.write(f"Host: 127.0.0.1:{sk['origin']}\r\n")
        if not upgrade:
            head.write("Connection: close\r\n")
        head.write("\r\n")

        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)

        self.close_connection = True
        try:
            up.sendall(head.getvalue().encode("latin-1") + body)
            if upgrade:
                return self.splice(up)
            self.respond(up, sk)
        except Exception as exc:
            sys.stderr.write(f"fleetdeck-skin[{sk['id']}]: {exc}\n")
        finally:
            try:
                up.close()
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = relay

    def respond(self, up, sk):
        rd = up.makefile("rb")
        status = rd.readline()
        if not status:
            return
        headers, order = {}, []
        while True:
            line = rd.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
            order.append((k.strip(), v.strip()))

        ctype = headers.get("content-type", "").lower()
        chunked = "chunked" in headers.get("transfer-encoding", "").lower()
        is_html = "text/html" in ctype
        is_css = "text/css" in ctype and sk.get("recolor")

        if not (is_html or is_css):
            # Relay verbatim, framing included, streaming as it arrives —
            # synthesised audio must not be buffered to completion first.
            self.wfile.write(status)
            for k, v in order:
                self.wfile.write(f"{k}: {v}\r\n".encode("latin-1"))
            self.wfile.write(b"\r\n")
            while True:
                chunk = rd.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            return

        raw = dechunk(rd) if chunked else (
            rd.read(int(headers["content-length"])) if "content-length" in headers
            else rd.read())
        text = raw.decode("utf-8", "replace")
        out = (rewrite(text, sk) if is_html else recolor(text, sk)).encode()

        self.wfile.write(status)
        for k, v in order:
            if k.lower() in DROP:
                continue
            self.wfile.write(f"{k}: {v}\r\n".encode("latin-1"))
        self.wfile.write(f"Content-Length: {len(out)}\r\n".encode("latin-1"))
        self.wfile.write(b"Connection: close\r\n\r\n")
        self.wfile.write(out)

    def splice(self, up):
        """101 and onward: raw bytes both ways until one side hangs up."""
        self.connection.settimeout(None)
        up.settimeout(None)

        def pump(src, dst):
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except Exception:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass

        t = threading.Thread(target=pump, args=(self.connection, up), daemon=True)
        t.start()
        pump(up, self.connection)


def main():
    """Bind a listener per skinned service, and keep watching for more.

    A fresh install has no skins at all, and exiting on that would crash-loop
    forever under KeepAlive. So this idles instead, re-reads the registry every
    few seconds, and binds anything new — adding a `skin` block is an edit, not
    a restart, the same promise the registry makes everywhere else."""
    listening, complained = {}, {}
    announced = False
    while True:
        for port, sk in sorted(skins().items()):
            if port in listening:
                continue
            try:
                srv = ThreadingHTTPServer((BIND, port), Handler)
            except OSError as exc:
                # Say it once per port. This loop runs every few seconds, and a
                # port held by something else stays held — repeating the same
                # line forever would bury everything else in the log.
                if complained.get(port) != str(exc):
                    complained[port] = str(exc)
                    sys.stderr.write(f"fleetdeck-skin: cannot bind {BIND}:{port} "
                                     f"for {sk['id']} ({exc})\n")
                    sys.stderr.flush()
                continue
            complained.pop(port, None)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            listening[port] = sk
            print(f"fleetdeck-skin {BIND}:{port} -> :{sk['origin']} "
                  f"({sk['id']} as {sk.get('name', sk['id'])})", flush=True)
        if not listening and not announced:
            print("fleetdeck-skin: nothing to skin yet — add a \"skin\" block "
                  "to a service in services.json and it will bind here within "
                  "a few seconds. See the README.", flush=True)
            announced = True
        time.sleep(5)


if __name__ == "__main__":
    main()
