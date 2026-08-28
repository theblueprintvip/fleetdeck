#!/usr/bin/env python3
"""make-icons — one home-screen icon per service, from the board's own glyphs.

The portal installs to an iPhone home screen with a real icon. Nothing else on
a typical machine does: `apple-mobile-web-app-capable` alone buys standalone
mode, not an icon, so Add to Home Screen falls back to a screenshot of the page
and a row of your services comes out as a row of grey rectangles.

This renders the rest. Every icon comes from glyphs.json — the same file
portal_server.py interpolates into the board — so a tile and its home-screen
icon cannot drift apart. Restyling the whole fleet is an edit to BG/INK/STROKE
below and one run.

    ./make-icons.py              # every service with an icon
    ./make-icons.py chat pm      # just these

Nothing here is hardcoded per-machine: the surface list IS services.json.
Give an entry any of these and this script does the rest:

    "icon":      "chat"                  the glyph, from glyphs.json
    "icon_dest": "~/myapp/public/icons"  optional — also copy the PNGs here, so
                                         the app serves its own icon from its
                                         own origin and does not depend on the
                                         portal being up or the tailnet keeping
                                         its name. Point it at whatever
                                         directory that app serves statically.
    "maskable":  true                    also emit <id>-maskable-512.png at
                                         MASK_FILL, so Android's circle crop
                                         cannot clip a square-ish glyph.

Then link it from that app's <head> and mark the entry "install": true so it
appears in the portal's Add to Home Screen sheet:

    <link rel="apple-touch-icon" href="/icons/<id>-180.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="Whatever">

Optical fitting: glyphs do not share a bounding box. A waveform is 18x16 user
units, a compass is a full 18x18 circle, a terminal is 16x14. Scaling them all
by the nominal 24-unit viewBox leaves the flat ones looking shrunken next to
the round ones. So each glyph is measured in the browser with getBBox() and
scaled until its longest side fills the same fraction of the canvas — and
because that scale differs per glyph, stroke-width is recomputed in user units
to land on the same rendered pixel weight every time.

Rasteriser: headless Chrome, because it is the one thing already on most Macs
that can turn an SVG into a PNG. No rsvg-convert, ImageMagick or cairosvg
needed. `sips` (built into macOS) does the downscales. Set FLEETDECK_CHROME if
your browser lives somewhere unusual.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GLYPHS = os.path.join(HERE, "glyphs.json")
REGISTRY = os.path.join(HERE, "services.json")
OUT = os.path.join(HERE, "icons")
# Every glyph, pre-rendered and shipped in the repo. This is what makes a fresh
# clone work without a browser: with no Chromium, a service's icon is copied
# from here instead of rasterised. Regenerate with --library after changing a
# glyph or the palette; that one needs a browser.
LIBRARY = os.path.join(HERE, "assets", "glyphs")

# Any Chromium will do; the first one that exists wins.
CHROME_CANDIDATES = [
    os.environ.get("FLEETDECK_CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    shutil.which("chromium") or "",
    shutil.which("google-chrome") or "",
]

BG = "#05070a"      # --bg from the portal stylesheet
INK = "#4fe3c1"     # --on, the live lamp colour
STROKE = 26         # rendered stroke, px at 512
FILL = 0.73         # longest side of the glyph, as a fraction of the canvas
MASK_FILL = 0.55    # ditto for maskable, which Android crops to a circle
SIZES = (512, 192, 180)


def chrome():
    """The rasteriser, or None. Absent is not fatal — see LIBRARY.
    FLEETDECK_CHROME=none forces the shipped renders, which is also how you
    check what a browserless machine will get."""
    if os.environ.get("FLEETDECK_CHROME") == "none":
        return None
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def load(path):
    with open(path) as fh:
        return json.load(fh)


def render_glyph(paths, out512, fill=FILL, browser=None):
    """Rasterise one glyph at 512. The fitting maths runs in the page, where a
    real layout engine can measure the path — Chrome has finished executing it
    before --screenshot fires."""
    html = f"""<!doctype html><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;overflow:hidden;background:{BG}}}</style>
<svg id="s" xmlns="http://www.w3.org/2000/svg" width="512" height="512">
  <g id="g" fill="none" stroke="{INK}" stroke-linecap="round"
     stroke-linejoin="round">{paths}</g>
</svg>
<script>
  const g = document.getElementById('g'), b = g.getBBox();
  // square viewBox centred on the glyph, sized so its longest side is fill
  const box = Math.max(b.width, b.height) / {fill};
  document.getElementById('s').setAttribute('viewBox',
    (b.x + b.width / 2 - box / 2) + ' ' + (b.y + b.height / 2 - box / 2) +
    ' ' + box + ' ' + box);
  // user units shrink as box shrinks, so hold the rendered weight constant
  g.setAttribute('stroke-width', {STROKE} * box / 512);
</script>"""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(html)
        page = fh.name
    try:
        subprocess.run(
            [browser, "--headless", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=1", f"--screenshot={out512}",
             "--window-size=512,512", f"file://{page}"],
            check=True, capture_output=True,
        )
    finally:
        os.unlink(page)


def sid_png(sid, size):
    return os.path.join(OUT, f"{sid}-{size}.png")


def downscale(sid):
    for size in SIZES[1:]:
        subprocess.run(["sips", "-z", str(size), str(size), sid_png(sid, 512),
                        "--out", sid_png(sid, size)],
                       check=True, capture_output=True)


def publish(sid, dest, maskable=False):
    """Copy the set into the app's own static directory, so it serves its own
    icon from its own origin."""
    dest = os.path.expanduser(dest)
    os.makedirs(dest, exist_ok=True)
    for size in SIZES:
        shutil.copy2(sid_png(sid, size), os.path.join(dest, f"{sid}-{size}.png"))
    if maskable:
        shutil.copy2(sid_png(sid, "maskable-512"),
                     os.path.join(dest, f"{sid}-maskable-512.png"))


def lib_png(key, size):
    return os.path.join(LIBRARY, f"{key}-{size}.png")


def build_library(glyphs, browser):
    """Pre-render every glyph, service-agnostic, into assets/glyphs.

    Shipped in the repo so a clone has the whole icon set on arrival and a
    machine with no browser still ends up with real icons. Maintainer command —
    run it after changing a glyph, BG, INK, STROKE or FILL, and commit the
    result."""
    if not browser:
        sys.exit("make-icons --library: needs a Chromium to rasterise. Install "
                 "one, or set FLEETDECK_CHROME=/path/to/browser.")
    os.makedirs(LIBRARY, exist_ok=True)
    for key, paths in sorted(glyphs.items()):
        render_glyph(paths, lib_png(key, 512), browser=browser)
        for size in SIZES[1:]:
            subprocess.run(["sips", "-z", str(size), str(size), lib_png(key, 512),
                            "--out", lib_png(key, size)],
                           check=True, capture_output=True)
        render_glyph(paths, lib_png(key, "maskable-512"), fill=MASK_FILL,
                     browser=browser)
        print(f"  {key}")
    print(f"{len(glyphs)} glyphs -> assets/glyphs/")


def from_library(key, sid, maskable):
    """No browser: copy the shipped render for this glyph. Returns False if the
    glyph is one the operator added themselves, which cannot be pre-rendered."""
    if not os.path.exists(lib_png(key, 512)):
        return False
    for size in SIZES:
        shutil.copy2(lib_png(key, size), sid_png(sid, size))
    if maskable and os.path.exists(lib_png(key, "maskable-512")):
        shutil.copy2(lib_png(key, "maskable-512"), sid_png(sid, "maskable-512"))
    return True


def main():
    glyphs = {k: v for k, v in load(GLYPHS).items() if not k.startswith("_")}
    browser = chrome()

    args = [a for a in sys.argv[1:] if a != "--library"]
    if "--library" in sys.argv[1:]:
        return build_library(glyphs, browser)

    # `_example` entries are documentation shipped in services.json. Rendering
    # them is harmless, but publishing one would mkdir a directory named in an
    # example on someone else's machine — so they are skipped unless asked for
    # by name.
    services = {s["id"]: s for s in load(REGISTRY).get("services", []) if s.get("id")}
    wanted = args or [k for k, s in services.items() if "_example" not in s]

    os.makedirs(OUT, exist_ok=True)
    if not services:
        sys.exit("make-icons: services.json has no services yet — register one "
                 "first, then run this.")
    if not browser:
        sys.stderr.write("make-icons: no Chromium — copying the shipped renders "
                         "from assets/glyphs instead of rasterising. Custom "
                         "glyphs and palette changes need a browser.\n")

    for sid in wanted:
        s = services.get(sid)
        if not s:
            sys.stderr.write(f"! {sid}: not in services.json — skipping\n")
            continue
        key = s.get("icon", "server")
        if key not in glyphs:
            sys.stderr.write(f"  ! {sid}: no glyph '{key}', using server\n")
            key = "server"
        paths = glyphs.get(key, "")
        maskable = bool(s.get("maskable"))

        if browser:
            render_glyph(paths, sid_png(sid, 512), browser=browser)
            downscale(sid)
            if maskable:
                render_glyph(paths, sid_png(sid, "maskable-512"),
                             fill=MASK_FILL, browser=browser)
            how = "glyph"
        elif from_library(key, sid, maskable):
            how = "shipped"
        else:
            sys.stderr.write(f"  ! {sid}: glyph '{key}' is not in the shipped "
                             f"library and there is no browser to draw it\n")
            continue

        note = ""
        if s.get("icon_dest"):
            publish(sid, s["icon_dest"], maskable)
            note = " -> " + s["icon_dest"]
        elif (s.get("skin") or {}).get("port"):
            note = " (skin_server serves it at /_skin/icon-*.png)"
        print(f"{sid:12} {how:8} {key:12}{note}")


if __name__ == "__main__":
    main()
