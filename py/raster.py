"""
Pictures for Agilite CNC, with numpy only: filling outlines into masks, and drawing a height grid
as a shaded, tilted 3D view (a "voxel space" renderer: every screen column is filled front to back).
"""
import math

import numpy as np


def fill_polys(polys, x0, y0, cell, nx, ny, rule="evenodd"):
    """Mask (ny, nx) of grid cells whose centres lie inside the polygons (lists of (x, y), mm).
    rule 'evenodd' (holes in a DXF part) or 'nonzero' (logo fonts)."""
    wind = np.zeros((ny, nx), np.int32)
    ys = y0 + (np.arange(ny) + 0.5) * cell
    for poly in polys:
        p = np.asarray(poly, float)
        if len(p) < 3:
            continue
        a, b = p, np.roll(p, -1, axis=0)
        for (ax, ay), (bx, by) in zip(a, b):
            if ay == by:
                continue
            up = by > ay
            lo, hi = (ay, by) if up else (by, ay)
            j0 = int(math.ceil((lo - y0) / cell - 0.5))
            j1 = int(math.ceil((hi - y0) / cell - 0.5))
            j0, j1 = max(j0, 0), min(j1, ny)
            if j0 >= j1:
                continue
            yy = ys[j0:j1]
            xx = ax + (yy - ay) * (bx - ax) / (by - ay)
            i = np.ceil((xx - x0) / cell - 0.5).astype(np.int64)
            i = np.clip(i, 0, nx)
            d = 1 if up else -1
            rows = np.arange(j0, j1)
            ok = i < nx
            # a crossing at column i: every cell from i to the right gets the winding change
            np.add.at(wind, (rows[ok], i[ok]), d if rule == "nonzero" else 1)
    acc = np.cumsum(wind, axis=1)
    return (acc != 0) if rule == "nonzero" else (acc % 2 == 1)


def shade(z, cell, light=(-0.45, -0.55, 0.70)):
    """Lambert shading 0..1 from a height grid (NaN = nothing there)."""
    zz = np.where(np.isnan(z), np.nanmin(z) if np.isfinite(z).any() else 0.0, z)
    gy, gx = np.gradient(zz, cell)
    n = np.stack([-gx, -gy, np.ones_like(zz)])
    n /= np.linalg.norm(n, axis=0)
    l = np.asarray(light, float)
    l /= np.linalg.norm(l)
    lam = np.clip((n * l[:, None, None]).sum(0), 0, 1)
    return 0.42 + 0.58 * lam


class View:
    """Camera for a grid: yaw (deg, around Z), elevation (deg; 90 = straight down), zoom (px per mm), pan (px)."""

    def __init__(self):
        self.yaw, self.elev, self.zoom, self.pan = -30.0, 52.0, None, (0.0, 0.0)

    def reset(self):
        self.yaw, self.elev, self.zoom, self.pan = -30.0, 52.0, None, (0.0, 0.0)


def render(z, rgb, x0, y0, cell, bottom, view, W, H, bg=(244, 245, 247), wall_dark=0.68, step=1):
    """z (ny, nx) heights in mm (NaN = empty), rgb (ny, nx, 3) floats 0..255 (already shaded),
    grid lower-left corner (x0, y0), cell size. bottom = Z of the stock bottom (walls go down to it).
    Returns (image uint8 (H, W, 3), project) where project(xs, ys, zs) -> screen (u, v) arrays.
    step > 1 renders at a lower resolution (while dragging) and scales up."""
    ny, nx = z.shape
    Wr, Hr = max(8, W // step), max(8, H // step)
    cx, cy = x0 + nx * cell / 2, y0 + ny * cell / 2
    ztop = np.nanmax(z) if np.isfinite(z).any() else 0.0
    zmid = (ztop + bottom) / 2
    ya, el = math.radians(view.yaw), math.radians(max(5.0, min(90.0, view.elev)))
    se, ce = math.sin(el), math.cos(el)
    ex, ey = math.cos(ya), math.sin(ya)          # screen-right direction on the ground
    fx, fy = -math.sin(ya), math.cos(ya)         # away-from-viewer direction on the ground
    if view.zoom is None:   # fit: the whole grid on screen with a margin
        diag = math.hypot(nx * cell, ny * cell)
        hz = diag * se + (ztop - bottom) * ce
        view.zoom = 0.86 * min(W / max(diag, 1e-6), H / max(hz, 1e-6))
    s = view.zoom / step
    px, py = view.pan[0] / step, view.pan[1] / step

    def project(xs, ys, zs, st=step):
        xs, ys, zs = np.asarray(xs, float), np.asarray(ys, float), np.asarray(zs, float)
        dx, dy = xs - cx, ys - cy
        a = dx * ex + dy * ey
        b = dx * fx + dy * fy
        sc = view.zoom / st
        u = W / st / 2 + view.pan[0] / st + a * sc
        v = H / st / 2 + view.pan[1] / st - (b * se + (zs - zmid) * ce) * sc
        return u * st, v * st

    # sample the grid along camera-aligned lines: column a per screen pixel, depth b in fine steps
    us = (np.arange(Wr) + 0.5 - Wr / 2 - px) / s
    half = math.hypot(nx * cell, ny * cell) / 2
    db = min(cell, 1.0 / s / max(se, 0.25)) * 0.9
    nb = int(2 * half / db) + 2
    nb = min(nb, 2400)
    db = 2 * half / (nb - 1)
    bs = -half + np.arange(nb) * db                      # near (negative b) to far
    A, B = np.meshgrid(us, bs)                           # (nb, Wr)
    X, Y = cx + A * ex + B * fx, cy + A * ey + B * fy
    I = np.floor((X - x0) / cell).astype(np.int64)
    J = np.floor((Y - y0) / cell).astype(np.int64)
    ok = (I >= 0) & (I < nx) & (J >= 0) & (J < ny)
    Ic, Jc = np.clip(I, 0, nx - 1), np.clip(J, 0, ny - 1)
    Z = np.where(ok, z[Jc, Ic], np.nan)
    has = np.isfinite(Z)
    # screen row of each sample's top (down = larger)
    row = Hr / 2 + py - (B * se + (np.where(has, Z, -1e9) - zmid) * ce) * s
    row = np.where(has, row, np.inf)
    # front to back: a sample shows on the rows between its top and the lowest top in front of it
    cm = np.minimum.accumulate(row, axis=0)               # (nb, Wr) non-increasing down the depth axis
    img = np.empty((Hr, Wr, 3), np.float32)
    img[:] = bg
    col = rgb[Jc, Ic]                                     # (nb, Wr, 3)
    # base of the stock under each sample: the wall runs down to here
    rows_r = np.arange(Hr) + 0.5
    face_px = db * se * s + 1.0
    for c in range(Wr):
        m = cm[:, c]
        if not np.isfinite(m[-1]) and not np.isfinite(m).any():
            continue
        # pixel r shows the nearest sample whose cumulative top is <= r
        neg = -m                                          # non-decreasing
        k = np.searchsorted(neg, -rows_r, side="left")    # first index with m <= r
        # only where that sample really exists and the pixel is above the sample's bottom wall
        valid = k < nb
        kk = np.minimum(k, nb - 1)
        top = row[kk, c]
        base_row = Hr / 2 + py - (bs[kk] * se + (bottom - zmid) * ce) * s
        valid &= np.isfinite(top) & (rows_r <= base_row + 0.5)
        if not valid.any():
            continue
        cc = col[kk, c].astype(np.float32)
        wall = (rows_r - top) > face_px
        cc[wall] *= wall_dark
        img[valid, c] = cc[valid]
    out = np.clip(img, 0, 255).astype(np.uint8)
    if step > 1:
        out = np.repeat(np.repeat(out, step, axis=0), step, axis=1)[:H, :W]
        if out.shape[0] < H or out.shape[1] < W:
            pad = np.empty((H, W, 3), np.uint8)
            pad[:] = bg
            pad[:out.shape[0], :out.shape[1]] = out
            out = pad
    return out, project


def ppm(img):
    """uint8 (H, W, 3) to PPM bytes for tk.PhotoImage(data=..., format='PPM')."""
    h, w = img.shape[:2]
    return b"P6 %d %d 255\n" % (w, h) + np.ascontiguousarray(img).tobytes()
