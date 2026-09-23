#!/usr/bin/env python3
"""Generate the paper-audiobook icon set from one set of coordinates.

Why this file exists
--------------------
There is no SVG rasteriser on this machine (no rsvg-convert, no inkscape, no
ImageMagick, no cairosvg, no headless Chromium), so the SVGs and the PNGs cannot
be produced by a single tool. Instead they are produced by *one set of
coordinates*: GEOMETRY below is the only place the artwork is described, and both
`--emit` (writing the files) and `--check` (re-deriving them and diffing) read
it. The SVG text and the PNG bytes therefore cannot drift apart without
`--check` failing.

The mark
--------
A portrait page with a play triangle knocked out of it: the page is the paper,
the triangle is the audio, and the portrait proportions keep it from reading as a
video player. Deliberately two shapes and no dog-eared corner -- anything more
turns to mush at 16px, which is the size that actually matters in a tab.

Colours come from the app's own theme in `static/index.html` (`:root`): the light
-accent green on the paper ground. Icons are fixed artwork, so there is no
`prefers-color-scheme` variant -- launchers cache one image.

Run it
------
    python3 make_icons.py            # write static/icons/ + manifest
    python3 make_icons.py --check    # re-derive and audit; non-zero on drift

Needs Pillow. That is a *build-time* dependency only: the generated files are
committed, so nothing in the running app imports this module and `requirements.txt`
is untouched. The app venv deliberately does not carry Pillow.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

try:
    from PIL import Image, ImageDraw
except ModuleNotFoundError:  # pragma: no cover - build-time tool
    sys.exit("make_icons.py needs Pillow (build-time only): pip install pillow")

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "static" / "icons"

# --- the artwork, in a 512x512 coordinate space -------------------------------
TITLE = "Paper Audiobook"
ACCENT = "#0F6B5A"  # --accent, light theme
PAPER = "#F4F6F2"  # --paper, light theme
BG_RX = 112  # rounded-square radius of the app-icon ground
PAGE = (150, 110, 362, 402)  # portrait page: 212 x 292
PAGE_RX = 22
TRIANGLE = ((222, 206), (222, 306), (322, 256))  # play mark, optically centred
CANVAS = 512

# How much of the canvas the *glyph* fills. Chrome crops maskable icons to
# whatever shape the launcher wants and only guarantees the central 80% circle,
# so a maskable glyph is inset; iOS masks the touch icon itself, so it only needs
# to be full-bleed. `centre` in --check asserts the result really is centred,
# which is the mistake Horizon's maskable icon makes.
INSET = {"icon": 1.0, "maskable": 0.82, "apple-touch": 0.92}
SUPERSAMPLE = 8  # draw big, then LANCZOS down: cheap antialiasing


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _scale(points, k: float) -> list[tuple[float, float]]:
    """Scale a point list about the canvas centre."""
    c = CANVAS / 2
    return [(c + (x - c) * k, c + (y - c) * k) for x, y in points]


def _rect_points(r, k: float):
    x0, y0, x1, y1 = r
    return _scale([(x0, y0), (x1, y1)], k)


def render(size: int, inset: float, rounded: bool) -> Image.Image:
    """One raster, drawn at `size` from the constants above."""
    ss = size * SUPERSAMPLE
    scale = ss / CANVAS
    im = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    if rounded:
        d.rounded_rectangle(
            [0, 0, ss - 1, ss - 1], radius=BG_RX * scale, fill=(*_hex_rgb(ACCENT), 255)
        )
    else:
        d.rectangle([0, 0, ss, ss], fill=(*_hex_rgb(ACCENT), 255))

    p0, p1 = _rect_points(PAGE, inset)
    x0, y0 = p0
    x1, y1 = p1
    d.rounded_rectangle(
        [x0 * scale, y0 * scale, x1 * scale, y1 * scale],
        radius=PAGE_RX * inset * scale,
        fill=(*_hex_rgb(PAPER), 255),
    )
    d.polygon(
        [(x * scale, y * scale) for x, y in _scale(TRIANGLE, inset)],
        fill=(*_hex_rgb(ACCENT), 255),
    )
    return im.resize((size, size), Image.LANCZOS)


def svg_text() -> str:
    """The same geometry as markup. `--check` parses these numbers back out."""
    x0, y0, x1, y1 = PAGE
    (tx0, ty0), (tx1, ty1), (tx2, ty2) = TRIANGLE
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}"'
        f' role="img" aria-label="{TITLE}">\n'
        f"  <title>{TITLE}</title>\n"
        f'  <rect width="{CANVAS}" height="{CANVAS}" rx="{BG_RX}" fill="{ACCENT}"/>\n'
        f'  <rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"'
        f' rx="{PAGE_RX}" fill="{PAPER}"/>\n'
        f'  <path d="M{tx0} {ty0} L{tx2} {ty2} L{tx1} {ty1} Z" fill="{ACCENT}"/>\n'
        "</svg>\n"
    )


def manifest() -> dict:
    return {
        "id": "/",
        "name": TITLE,
        "short_name": "Audiobook",
        "description": "Turn academic PDFs into narrated audio you can organise, browse and listen to.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": ACCENT,
        "background_color": PAPER,
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {
                "src": "/static/icons/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
            {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
    }


def emit() -> list[pathlib.Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []

    def put(name: str, im: Image.Image) -> None:
        p = OUT / name
        im.save(p, optimize=True)
        written.append(p)

    (OUT / "icon.svg").write_text(svg_text())
    written.append(OUT / "icon.svg")

    put("icon-192.png", render(192, INSET["icon"], rounded=True))
    put("icon-512.png", render(512, INSET["icon"], rounded=True))
    put("favicon-32.png", render(32, INSET["icon"], rounded=True))
    put("icon-maskable-512.png", render(512, INSET["maskable"], rounded=False))
    put("apple-touch-icon.png", render(180, INSET["apple-touch"], rounded=False))

    # A .ico carries its own small frames, and Pillow will happily downsample one
    # image into all of them. Supersampling each size separately gives a crisper
    # 16px, so the frames are built here and handed in via append_images.
    frames = {s: render(s, INSET["icon"], rounded=True) for s in (16, 32, 48)}
    ico = OUT / "favicon.ico"
    frames[48].save(ico, sizes=[(16, 16), (32, 32), (48, 48)], append_images=[frames[16], frames[32]])
    written.append(ico)

    (OUT / "manifest.webmanifest").write_text(json.dumps(manifest(), indent=2) + "\n")
    written.append(OUT / "manifest.webmanifest")
    return written


# --- checks ------------------------------------------------------------------


def _alpha_stats(im: Image.Image) -> tuple[int, int]:
    hist = im.convert("RGBA").split()[3].histogram()
    return hist[0], sum(hist[255:])  # fully-transparent px, fully-opaque px


def _content_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of everything that is not the flat background colour.

    The background is the most common colour, not the pixel under the centre: an
    inset maskable glyph is centred by construction, so the centre pixel is
    usually inside the artwork.
    """
    im = im.convert("RGBA")
    w, h = im.size
    px = im.load()
    counts = im.getcolors(maxcolors=1 << 20) or []
    bg = max(counts, key=lambda c: c[0])[1][:3] if counts else px[0, 0][:3]
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 10 and (r, g, b) != bg:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _mean_diff(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 999.0
    pa, pb = a.convert("RGBA").load(), b.convert("RGBA").load()
    total = 0
    for y in range(a.size[1]):
        for x in range(a.size[0]):
            total += sum(abs(pa[x, y][i] - pb[x, y][i]) for i in range(4)) / 4
    return total / (a.size[0] * a.size[1])


def check() -> int:
    failures: list[str] = []

    # 1. The SVG markup still describes the same geometry as the constants.
    svg = (OUT / "icon.svg").read_text()
    nums = [float(n) for n in re.findall(r'(?:width|height|x|y|rx)="(-?[\d.]+)"', svg)]
    x0, y0, x1, y1 = PAGE
    expected = [CANVAS, CANVAS, BG_RX, x0, y0, x1 - x0, y1 - y0, PAGE_RX]
    if nums != expected:
        failures.append(f"icon.svg geometry {nums} != constants {expected}")
    for tx, ty in TRIANGLE:
        if f"{tx} {ty}" not in svg:
            failures.append(f"triangle point {tx},{ty} missing from icon.svg")

    # 2. The rasters still match the constants (catches a stale generated file).
    for name, size, inset, rounded in [
        ("icon-512.png", 512, INSET["icon"], True),
        ("icon-192.png", 192, INSET["icon"], True),
        ("favicon-32.png", 32, INSET["icon"], True),
        ("icon-maskable-512.png", 512, INSET["maskable"], False),
        ("apple-touch-icon.png", 180, INSET["apple-touch"], False),
    ]:
        on_disk = Image.open(OUT / name)
        d = _mean_diff(on_disk, render(size, inset, rounded))
        if on_disk.size != (size, size):
            failures.append(f"{name}: {on_disk.size} != {(size, size)}")
        if d > 4.0:
            failures.append(f"{name}: mean diff vs constants {d:.1f}/255 (>4)")

    # 3. iOS composites transparent apple-touch pixels onto BLACK, so that file
    #    must be fully opaque. This is the defect Horizon's copy has.
    for name in ("apple-touch-icon.png", "icon-maskable-512.png"):
        clear, _ = _alpha_stats(Image.open(OUT / name))
        if clear:
            failures.append(f"{name}: {clear} transparent px -- must be fully opaque")

    # 4. Maskable content must be centred, or launchers crop it lopsided.
    c = CANVAS / 2
    x0, y0, x1, y1 = _content_bbox(Image.open(OUT / "icon-maskable-512.png"))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    far = max(((x - c) ** 2 + (y - c) ** 2) ** 0.5 for x in (x0, x1) for y in (y0, y1))
    if abs(cx - c) > 2 or abs(cy - c) > 2:
        failures.append(f"maskable content centre ({cx:.1f},{cy:.1f}) != ({c},{c})")
    if far > 0.4 * CANVAS:
        failures.append(f"maskable content reaches {far:.1f}px, past the safe radius {0.4 * CANVAS:.1f}px")

    # 5. The .ico really carries all three frames.
    sizes = set(Image.open(OUT / "favicon.ico").info.get("sizes") or ())
    if not {(16, 16), (32, 32), (48, 48)} <= sizes:
        failures.append(f"favicon.ico frames {sorted(sizes)} missing 16/32/48")
    manifest_icons = {i["src"] for i in json.loads((OUT / "manifest.webmanifest").read_text())["icons"]}
    for src in manifest_icons:
        if not (ROOT / src.lstrip("/")).is_file():
            failures.append(f"manifest references missing file {src}")

    for f in failures:
        print(f"FAIL  {f}")
    if not failures:
        print(f"OK    {len(list(OUT.iterdir()))} files in static/icons/ match the constants")
    return 1 if failures else 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    for p in emit():
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size:,} bytes)")
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
