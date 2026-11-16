"""
The Agilite CNC mark: the Agilite scorpion above the letters CNC.
Drawn from logos/agilite-scorpion.json and the BankGothic Bold font, with numpy, into an .ico and PNGs.
"""
import os
import struct

import numpy as np

import gcm_engine as gcm
import raster

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_FILE = os.path.join(HERE, "icons", "agilite-cnc-v2.ico")   # new name per design: Windows caches icons by path
gcm.LOGO_DIR = os.path.join(HERE, "logos")

ICON_LAYOUT = {"text_w": 1.12, "gap_k": 0.05}   # big letters so CNC reads on the taskbar
BAR = (24, 33, 44)        # the dark title bar colour (#18212c)
WHITE = (255, 255, 255)


def mark_polys(text_w=0.74, gap_k=0.10):
    """(scorpion polygons, CNC polygons) in mm, y up, scorpion above the letters, centred on x = 0."""
    sc_shapes, sw, sh, _ = gcm._mark_shapes({"type": "logo", "logo": "AGILITE scorpion", "size": 100.0,
                                             "registered": False})
    font = "BankGothic Bold" if "BankGothic Bold" in gcm.list_fonts() else "Arial Black"
    tx_shapes, tw, th, _ = gcm._mark_shapes({"type": "text", "text": "CNC", "font": font, "size": 30.0})

    def box(sh_):
        xs = [p[0] for s, _ in sh_ for p in s]
        ys = [p[1] for s, _ in sh_ for p in s]
        return min(xs), max(xs), min(ys), max(ys)
    a = box(sc_shapes)
    b = box(tx_shapes)
    # letters as wide as 74 % of the scorpion, a gap of 10 % of the scorpion height between them
    k = text_w * (a[1] - a[0]) / (b[1] - b[0])
    gap = gap_k * (a[3] - a[2])
    scor = [[(x - (a[0] + a[1]) / 2, y - a[2]) for x, y in s] for s, _ in sc_shapes]
    text = [[((x - (b[0] + b[1]) / 2) * k, (y - b[3]) * k - gap) for x, y in s] for s, _ in tx_shapes]
    return scor, text


def mark_mask(size_px, polys_scor, polys_text, pad=0.12, ss=4):
    """Anti-aliased coverage 0..1 (size, size) of the whole mark, fitted in a square with padding."""
    allp = [p for s in polys_scor + polys_text for p in s]
    xs, ys = [p[0] for p in allp], [p[1] for p in allp]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span = max(x1 - x0, y1 - y0) / (1 - 2 * pad)
    n = size_px * ss
    cell = span / n
    gx0 = (x0 + x1) / 2 - span / 2
    gy0 = (y0 + y1) / 2 - span / 2
    m = raster.fill_polys(polys_scor, gx0, gy0, cell, n, n, "nonzero")
    m |= raster.fill_polys(polys_text, gx0, gy0, cell, n, n, "nonzero")
    cov = m.reshape(size_px, ss, size_px, ss).mean(axis=(1, 3))
    return cov[::-1]          # row 0 = top


def icon_rgba(n, scor, text):
    cov = mark_mask(n, scor, text, pad=0.10 if n >= 64 else 0.06)
    # dark rounded square
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    r = n * 0.2
    dx = np.maximum(np.maximum(r - xx, xx - (n - r)), 0)
    dy = np.maximum(np.maximum(r - yy, yy - (n - r)), 0)
    inside = np.clip(r - np.hypot(dx, dy) + 0.5, 0, 1)
    rgb = np.empty((n, n, 3))
    rgb[:] = BAR
    rgb = rgb * (1 - cov[..., None]) + np.array(WHITE) * cov[..., None]
    a = inside * 255
    return np.dstack([rgb, a]).astype(np.uint8)


def _png_rgba(img):
    rows = [bytes(img[i].tobytes()) for i in range(img.shape[0])]
    return gcm._png(img.shape[0], rows)


def make_icon(path=ICON_FILE, force=False):
    if os.path.isfile(path) and not force:
        return path
    scor, text = mark_polys(**ICON_LAYOUT)
    sizes = (16, 24, 32, 40, 48, 64, 256)
    pngs = [_png_rgba(icon_rgba(n, scor, text)) for n in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    dirs = b""
    for n, data in zip(sizes, pngs):
        dirs += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
    with open(path, "wb") as f:
        f.write(head + dirs + b"".join(pngs))
    return path


def bar_logo_ppm(h):
    """The mark in white on the bar colour, h pixels tall, as PPM bytes (for the title bar)."""
    scor, text = mark_polys()
    cov = mark_mask(h, scor, text, pad=0.04)
    img = np.empty((h, h, 3))
    img[:] = BAR
    img = img * (1 - cov[..., None]) + np.array(WHITE) * cov[..., None]
    return raster.ppm(img.astype(np.uint8))


if __name__ == "__main__":
    print(make_icon(force=True))


def logo_png(h, rgb=WHITE):
    """The mark (scorpion above CNC) in one colour on a transparent square, h pixels, PNG bytes."""
    scor, text = mark_polys()
    cov = mark_mask(h, scor, text, pad=0.03)
    img = np.zeros((h, h, 4), np.uint8)
    img[..., :3] = rgb
    img[..., 3] = np.clip(cov * 255, 0, 255).astype(np.uint8)
    return _png_rgba(img)
