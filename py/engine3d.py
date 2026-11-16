"""
Agilite CNC 3D engine: from a solid part to safe G-code for the Stepcraft M700 (UCCNC).

The designer picks a material and a finish; everything else (bits, feeds, stepovers, the order of the work)
is decided here. The proven 2D program G-code Maker (gcm_engine) writes the G-code lines, measures the tools
and parks the machine, so the programs look and run like the ones the shop already trusts.

Frame: X/Y on the table, Z0 = the top of the stock, cuts go to negative Z.
Everything is planned on a top-down height grid of the part (3-axis: what is under an overhang counts as part).
Every tool position comes from the cutter-location (CL) surface: the lowest Z the tool tip can sit at, at each
grid point, without cutting into the part (plus the roughing allowance). Toolpaths ride that surface, so they
cannot gouge the part.

Public API
    load_part(path) -> Part
    place(part, up="auto") -> Part
    analyze(part, cell=None, bounds=None) -> dict
    cl_surface(heights, cell, tool, allowance=0.0) -> numpy grid
    build3d(part, material, finish, rack=None, machine=None, stock=None, margin=5.0, progress=None) -> gcm Job
    simulate3d(job, cell=None) -> {"grid", "origin", "cell"}
    leftover(job) -> {"grid", "origin", "cell"}
"""
import hashlib
import math
import os
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np

import gcm_engine as gcm
import presets as P

HERE = os.path.dirname(os.path.abspath(__file__))
STEP_PY = r"C:\Users\Lev Friedman\Desktop\agilite-pattern-flattener\.venv\Scripts\python.exe"
CACHE_DIR = os.path.join(tempfile.gettempdir(), "Agilite CNC STL cache")
FLAT, BALL, VBIT = P.FLAT, P.BALL, P.VBIT
BIG = 1.0e9


# ============================================================================ loading

class Part:
    """A solid as triangles (N, 3, 3) in mm. name: file name; up: the side that faces up after place()."""

    def __init__(self, tris, name="", path="", up="+Z"):
        self.tris = np.ascontiguousarray(tris, dtype=np.float64)
        self.name = name
        self.path = path
        self.up = up

    @property
    def bounds(self):
        v = self.tris.reshape(-1, 3)
        return v.min(axis=0), v.max(axis=0)

    @property
    def size(self):
        lo, hi = self.bounds
        return tuple(float(a) for a in hi - lo)


def _read_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * n == len(data) and n > 0:
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                                count=n, offset=84)
            return rec["v"].astype(np.float64)
    text = data.decode("latin-1", errors="replace")
    if "vertex" not in text:
        raise ValueError(f"{os.path.basename(path)} is not an STL file this program can read.")
    nums = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("vertex"):
            nums.extend(s.split()[1:4])
    v = np.array(nums, dtype=np.float64).reshape(-1, 3)
    v = v[: len(v) // 3 * 3]
    return v.reshape(-1, 3, 3)


def _step_to_stl(path):
    if not os.path.isfile(STEP_PY):
        raise RuntimeError("STEP files need the APF Python environment, which was not found at\n" + STEP_PY +
                           "\nSave the part as STL instead, or reinstall the APF tools.")
    os.makedirs(CACHE_DIR, exist_ok=True)
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:16]
    out = os.path.join(CACHE_DIR, f"{key}.stl")
    if os.path.isfile(out) and os.path.getsize(out) > 84:
        return out
    tmp = out + ".part"
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    r = subprocess.run([STEP_PY, os.path.join(HERE, "stepconv.py"), path, tmp, "0.02", "0.2"],
                       capture_output=True, text=True, timeout=300, creationflags=flags)
    if r.returncode != 0 or not os.path.isfile(tmp):
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError("The STEP file could not be converted: " + (msg[-1] if msg else "unknown error"))
    os.replace(tmp, out)
    return out


def load_part(path):
    """Reads an STL (binary or ASCII) or a STEP/STP file (converted through OpenCascade, cached)."""
    ext = os.path.splitext(path)[1].lower()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    stl = _step_to_stl(path) if ext in (".step", ".stp") else path
    tris = _read_stl(stl)
    # drop degenerate triangles
    e1, e2 = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    a = np.linalg.norm(np.cross(e1, e2), axis=1)
    tris = tris[a > 1e-12]
    if len(tris) == 0:
        raise ValueError(f"{os.path.basename(path)} holds no triangles.")
    return Part(tris, name=os.path.splitext(os.path.basename(path))[0], path=path)


# ============================================================================ placement

UPS = ("+Z", "-Z", "+X", "-X", "+Y", "-Y")


def _up_matrix(up):
    ax = {"X": 0, "Y": 1, "Z": 2}[up[1]]
    ez = np.zeros(3)
    ez[ax] = 1.0 if up[0] == "+" else -1.0
    ex = np.zeros(3)
    ex[(ax + 1) % 3] = 1.0
    if up == "-Z":
        ex = np.array([1.0, 0.0, 0.0])
    elif up == "+Z":
        ex = np.array([1.0, 0.0, 0.0])
    ey = np.cross(ez, ex)
    return np.array([ex, ey, ez])


def _orient_score(tris):
    """(undercut area, bottom flat area, height) for a part already rotated; lower undercut is better."""
    e1, e2 = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    cr = np.cross(e1, e2)
    ar = np.linalg.norm(cr, axis=1) / 2
    nz = cr[:, 2] / np.maximum(2 * ar, 1e-30)
    zmin = tris[:, :, 2].min()
    zlo = tris[:, :, 2].min(axis=1)
    down = nz < -0.02
    on_bottom = zlo > zmin + 0.01
    under = float(ar[down & on_bottom].sum())
    flat = float(ar[(nz < -0.999) & (tris[:, :, 2].max(axis=1) < zmin + 0.01)].sum())
    return under, flat, float(tris[:, :, 2].max() - zmin), float(ar.sum())


def place(part, up="auto"):
    """Turns the part so `up` faces up ("auto": fewest undercuts, then the biggest flat bottom), then moves it
    so its lower-left corner is X0 Y0 and its top is Z0 (the part spans Z -height..0)."""
    up = up or "auto"
    if up == "auto":
        best = None
        for u in UPS:
            t = part.tris @ _up_matrix(u).T
            under, flat, h, total = _orient_score(t)
            key = (round(under / max(total, 1e-9), 3), -round(flat / max(total, 1e-9), 3), round(h, 2), UPS.index(u))
            if best is None or key < best[0]:
                best = (key, u, t)
        _, up, t = best
    else:
        if up not in UPS:
            raise ValueError(f"Unknown side: {up}")
        t = part.tris @ _up_matrix(up).T
    v = t.reshape(-1, 3)
    lo, hi = v.min(axis=0), v.max(axis=0)
    t = t - np.array([lo[0], lo[1], hi[2]])
    return Part(t, name=part.name, path=part.path, up=up)


# ============================================================================ height grid

def _raster(tris, x0, y0, c, nx, ny, off=0.5, chunk=4_000_000):
    """Highest triangle Z at the sample points x0+(i+off)*c, y0+(j+off)*c; -inf where no triangle covers.
    Scanline: every triangle is cut by each sample row it spans, so the work is the covered area, not the
    bounding boxes. Vertical triangles are skipped (their top edge is covered by the neighbours)."""
    out = np.full(ny * nx, -np.inf)
    if len(tris) == 0:
        return out.reshape(ny, nx)
    A, B, C = tris[:, 0], tris[:, 1], tris[:, 2]
    d1, d2 = B - A, C - A
    det = d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]
    keep = np.abs(det) > 1e-12
    T = tris[keep]
    d1, d2, det = d1[keep], d2[keep], det[keep]
    # plane z = ax + by + k
    nrm = np.cross(d1, d2)
    pa = -nrm[:, 0] / nrm[:, 2]
    pb = -nrm[:, 1] / nrm[:, 2]
    pk = T[:, 0, 2] - pa * T[:, 0, 0] - pb * T[:, 0, 1]
    ys = T[:, :, 1]
    j0 = np.clip(np.ceil((ys.min(1) - y0) / c - off - 1e-9), 0, ny).astype(np.int64)
    j1 = np.clip(np.floor((ys.max(1) - y0) / c - off + 1e-9), -1, ny - 1).astype(np.int64)
    nrow = np.maximum(j1 - j0 + 1, 0)
    tid = np.repeat(np.arange(len(T)), nrow)
    jj = j0[tid] + (np.arange(int(nrow.sum())) - np.repeat(np.cumsum(nrow) - nrow, nrow))
    yv = y0 + (jj + off) * c
    xl = np.full(len(tid), np.inf)
    xr = np.full(len(tid), -np.inf)
    for a_, b_ in ((0, 1), (1, 2), (2, 0)):
        P, Q = T[tid, a_], T[tid, b_]
        lo, hi = np.minimum(P[:, 1], Q[:, 1]), np.maximum(P[:, 1], Q[:, 1])
        m = (yv >= lo - 1e-9) & (yv <= hi + 1e-9) & (hi - lo > 1e-12)
        t = np.where(m, (yv - P[:, 1]) / np.where(hi - lo > 1e-12, Q[:, 1] - P[:, 1], 1.0), 0.0)
        x = P[:, 0] + t * (Q[:, 0] - P[:, 0])
        xl = np.where(m, np.minimum(xl, x), xl)
        xr = np.where(m, np.maximum(xr, x), xr)
    i0 = np.clip(np.ceil((xl - x0) / c - off - 1e-9), 0, nx).astype(np.int64)
    i1 = np.clip(np.floor((xr - x0) / c - off + 1e-9), -1, nx - 1).astype(np.int64)
    ncol = np.where(np.isfinite(xl), np.maximum(i1 - i0 + 1, 0), 0)
    sel = np.nonzero(ncol)[0]
    ncol = ncol[sel]
    tt = tid[sel]
    zs = pa[tt] * (x0 + (i0[sel] + off) * c) + pb[tt] * yv[sel] + pk[tt]
    dz = pa[tt] * c
    base_idx = jj[sel] * nx + i0[sel]
    cum = np.cumsum(ncol)
    start = 0
    while start < len(sel):
        base = cum[start - 1] if start else 0
        end = max(int(np.searchsorted(cum, base + chunk, side="right")), start + 1)
        cs = ncol[start:end]
        k = np.arange(int(cs.sum())) - np.repeat(np.cumsum(cs) - cs, cs)
        z = np.repeat(zs[start:end], cs) + np.repeat(dz[start:end], cs) * k
        np.maximum.at(out, np.repeat(base_idx[start:end], cs) + k, z)
        start = end
    return out.reshape(ny, nx)


def auto_cell(w, h):
    """Grid cell (mm): fine for small parts, about 3 million cells for big ones."""
    c = math.sqrt(max(w * h, 1.0) / 3.0e6)
    return float(min(0.25, max(0.12, round(c, 3))))


def heights(tris, x0, y0, w, h, cell):
    """Two grids over the box: H = part top at the cell centres (-inf outside the part), and Hc = the highest of
    the centre, the four corners and any triangle corner in the cell (used for the tool positions, so a wall that
    crosses a cell between its samples still keeps the tool off)."""
    nx, ny = max(1, int(math.ceil(w / cell - 1e-9))), max(1, int(math.ceil(h / cell - 1e-9)))
    H = _raster(tris, x0, y0, cell, nx, ny, 0.5)
    N = _raster(tris, x0, y0, cell, nx + 1, ny + 1, 0.0)
    # the last row/column of centres may fall just past the box: a block's edge must not read as a gap
    if x0 + (nx - 0.5) * cell > x0 + w and nx > 1:
        H[:, -1] = np.where(np.isfinite(H[:, -1]), H[:, -1], H[:, -2])
    if y0 + (ny - 0.5) * cell > y0 + h and ny > 1:
        H[-1] = np.where(np.isfinite(H[-1]), H[-1], H[-2])
    corners = [N[:-1, :-1], N[1:, :-1], N[:-1, 1:], N[1:, 1:]]
    cmax = np.maximum.reduce(corners)
    cmin = np.minimum.reduce(corners)
    V = np.full(ny * nx, -np.inf)
    v = tris.reshape(-1, 3)
    ii = np.floor((v[:, 0] - x0) / cell).astype(np.int64)
    jj = np.floor((v[:, 1] - y0) / cell).astype(np.int64)
    ok = (ii >= 0) & (ii < nx) & (jj >= 0) & (jj < ny)
    np.maximum.at(V, jj[ok] * nx + ii[ok], v[ok, 2])
    V = V.reshape(ny, nx)
    # on smooth surfaces the centre sample is right (the surface runs straight between samples); where a wall or
    # an edge crosses the cell (corners far apart, or part of the cell off the part) the highest of the centre,
    # the corners and any triangle corner in the cell keeps the tool off it
    with np.errstate(invalid="ignore"):
        edge = ~np.isfinite(cmin) | ~np.isfinite(H) | ((cmax - cmin) > 5.3 * cell)
    Hc = np.where(edge, np.maximum(np.maximum(H, cmax), V), H)
    return H, Hc


# ---------------------------------------------------------------------------- fast window max/min

def _box_max(a, r, axis, fn=np.maximum, fill=-np.inf):
    """Max (or min) over a window of half-width r cells along one axis (sparse table, log steps)."""
    if r <= 0:
        return a.copy()
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r, r)
    p = np.pad(a, pad, constant_values=fill)
    width = 2 * r + 1
    cur, span = p, 1
    while span * 2 <= width:
        sl1 = [slice(None)] * a.ndim
        sl2 = [slice(None)] * a.ndim
        sl1[axis] = slice(0, cur.shape[axis] - span)
        sl2[axis] = slice(span, cur.shape[axis])
        cur = fn(cur[tuple(sl1)], cur[tuple(sl2)])
        span *= 2
    sl1 = [slice(None)] * a.ndim
    sl2 = [slice(None)] * a.ndim
    sl1[axis] = slice(0, n)
    sl2[axis] = slice(width - span, width - span + n)
    return fn(cur[tuple(sl1)], cur[tuple(sl2)])


def _square(a, r, fn=np.maximum, fill=-np.inf):
    return _box_max(_box_max(a, r, 0, fn, fill), r, 1, fn, fill)


def _disk(a, rad_cells, fn=np.maximum, fill=-np.inf):
    """Max (or min) over a flat disk of radius rad_cells (float) around every cell."""
    k = int(math.floor(rad_cells + 1e-9))
    if k <= 0:
        return a.copy()
    ny, nx = a.shape
    p = np.pad(a, ((0, 0), (k, k)), constant_values=fill)
    levels = [p]
    span = 1
    while span * 2 <= 2 * k + 1:
        prev = levels[-1]
        levels.append(fn(prev[:, :-span], prev[:, span:]))
        span *= 2
    res = np.full((ny, nx), -np.inf if fn is np.maximum else np.inf, dtype=a.dtype)
    for dy in range(-k, k + 1):
        if abs(dy) >= ny:
            continue
        hw = int(math.floor(math.sqrt(max(rad_cells * rad_cells - dy * dy, 0.0)) + 1e-9))
        width = 2 * hw + 1
        lv = int(math.floor(math.log2(width)))
        s = 1 << lv
        T = levels[lv]
        y0, y1 = max(0, dy), ny + min(0, dy)          # source rows
        a1 = T[y0:y1, k - hw: k - hw + nx]
        a2 = T[y0:y1, k + hw - s + 1: k + hw - s + 1 + nx]
        d = res[y0 - dy: y1 - dy]
        fn(d, fn(a1, a2), out=d)
    return res


# ============================================================================ tools and the CL surface

def _shank_start(t):
    """Height above the tip where the shank (wider than the cutter) begins."""
    if t.get("neck_d"):
        return float(t.get("reach", t.get("flute_len", 10.0)))
    return float(t.get("flute_len", 10.0))


def tool_profile(t, allow=0.0):
    """The tool as a height over the tip at each radius (grown by `allow` all round).
    Returns (rmax, q(r) numpy function, [(disk radius, constant height)])."""
    R = float(t["tool_d"]) / 2
    a = max(0.0, float(allow))
    kind = t.get("tool_type", FLAT)
    Rs = float(t.get("shank_d", t["tool_d"])) / 2
    L0 = _shank_start(t)
    disks = []
    if kind == BALL:
        def qc(r):
            return R - np.sqrt(np.maximum((R + a) ** 2 - r * r, 0.0))
        rc = R + a
    elif kind == VBIT:
        half = math.radians(float(t.get("tool_angle", 90.0)) / 2)
        tn, sn = math.tan(half), math.sin(half)

        def qc(r):
            return r / tn - a / sn
        rc = R + a
    else:
        def qc(r):
            return np.where(r <= R, -a, -np.sqrt(np.maximum(a * a - (r - R) ** 2, 0.0)))
        rc = R + a
        disks.append((R, -a))
    rmax = rc
    shank = Rs > R + 1e-6
    if shank:
        rmax = Rs + a
        disks.append((Rs + a, L0 - a))

    def q(r):
        r = np.asarray(r, dtype=np.float64)
        v = np.where(r <= rc + 1e-12, qc(np.minimum(r, rc)), np.inf)
        if shank:
            v = np.minimum(v, np.where(r <= Rs + a + 1e-12, L0 - a, np.inf))
        return v
    return rmax, q, disks


def _offsets(rmax, cell, q, disks):
    """Integer cell offsets inside the tool, their heights, and which of them the flat disks already cover."""
    k = int(math.floor(rmax / cell + 1e-9))
    di, dj = np.mgrid[-k:k + 1, -k:k + 1]
    di, dj = di.ravel(), dj.ravel()
    r = np.hypot(di, dj) * cell
    m = r <= rmax + 1e-9
    di, dj, r = di[m], dj[m], r[m]
    qq = q(r)
    covered = np.zeros(len(r), bool)
    for rad, v in disks:
        covered |= (r <= rad + 1e-9) & (np.abs(qq - v) < 1e-12)
    return di, dj, r, qq, covered, k


def _dilate(G, cell, prof, tile=64):
    """max over the tool footprint of G(x+o) - q(|o|): the lowest tip height that keeps the tool off G.
    G must be finite inside (beyond the grid counts as empty). float32 in, float32 out."""
    rmax, q, disks = prof
    G = np.ascontiguousarray(G, dtype=np.float32)
    ny, nx = G.shape
    res = np.full((ny, nx), -np.inf, dtype=np.float32)
    for rad, v in disks:
        np.maximum(res, _disk(G, rad / cell) - np.float32(v), out=res)
    di, dj, r, qq, cov, _ = _offsets(rmax, cell, q, disks)
    vary = ~cov
    if not vary.any():
        return res
    di, dj, r, qq = di[vary], dj[vary], r[vary], qq[vary]
    k = int(max(np.abs(di).max(), np.abs(dj).max()))
    qmin = float(qq.min())
    lmax = _square(G, k)
    lmin = _square(G, k, np.minimum, np.inf)
    with np.errstate(invalid="ignore"):
        nontriv = (lmax - lmin) > 1e-7
    del lmax, lmin
    vres = G - np.float32(qmin)
    T = tile if k <= 24 else 128
    nty, ntx = -(-ny // T), -(-nx // T)
    pad_t = np.zeros((nty * T, ntx * T), bool)
    pad_t[:ny, :nx] = nontriv
    act = pad_t.reshape(nty, T, ntx, T).any(axis=(1, 3))
    tiles = np.argwhere(act)
    if len(tiles):
        Gp = np.full((nty * T + 2 * k, ntx * T + 2 * k), -np.inf, dtype=np.float32)
        Gp[k:k + ny, k:k + nx] = G
        W = T + 2 * k
        # process in batches so the stack stays small
        per = max(1, int(6_000_000 // (W * W)))
        order = np.lexsort((r,))
        di, dj, qq = di[order], dj[order], qq[order]
        keys = np.round(qq, 9)
        brk = np.nonzero(np.diff(keys))[0] + 1
        groups = np.split(np.arange(len(qq)), brk)
        for b0 in range(0, len(tiles), per):
            tb = tiles[b0:b0 + per]
            S = np.stack([Gp[ti * T: ti * T + W, tj * T: tj * T + W] for ti, tj in tb])
            acc = np.full((len(tb), T, T), -np.inf, dtype=np.float32)
            m = np.empty_like(acc)
            for g in groups:
                first = True
                for idx in g:
                    a_, b_ = k + int(di[idx]), k + int(dj[idx])
                    v = S[:, a_:a_ + T, b_:b_ + T]
                    if first:
                        m[...] = v
                        first = False
                    else:
                        np.maximum(m, v, out=m)
                m -= np.float32(qq[g[0]])
                np.maximum(acc, m, out=acc)
            for n, (ti, tj) in enumerate(tb):
                y0, x0 = ti * T, tj * T
                y1, x1 = min(ny, y0 + T), min(nx, x0 + T)
                vres[y0:y1, x0:x1] = acc[n, :y1 - y0, :x1 - x0]
    np.maximum(res, vres, out=res)
    return res


def _erode(G, cell, prof):
    """min over the footprint of G(x-o) + q(|o|): the surface a tool leaves when its tip visits G."""
    return -_dilate(-np.asarray(G, dtype=np.float32), cell, prof)


def cl_surface(Hc, cell, tool, allowance=0.0):
    """Lowest tip Z at every cell that keeps the tool (cutter, neck and shank) `allowance` off the part.
    Hc must be finite (fill the outside with the floor height first)."""
    return _dilate(Hc, cell, tool_profile(tool, allowance))


# ============================================================================ analysis

def _down_tris(tris):
    """Downward-facing triangles that are not on the bottom plane (the overhangs a 3-axis machine cannot see)."""
    e1, e2 = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    cr = np.cross(e1, e2)
    nz = cr[:, 2] / np.maximum(np.linalg.norm(cr, axis=1), 1e-30)
    zmin = tris[:, :, 2].min()
    return tris[(nz < -0.02) & (tris[:, :, 2].min(axis=1) > zmin + 0.01)]


def _flat_cells(H, tol=2e-4):
    f = np.isfinite(H)
    out = f.copy()
    for sh in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        n = np.roll(H, sh, axis=(0, 1))
        with np.errstate(invalid="ignore"):
            out &= np.isfinite(n) & (np.abs(n - H) <= tol)
    return out


def _floors(H, cell, min_area=3.0):
    """Flat horizontal areas: [(z, area mm²)], highest first."""
    fl = _flat_cells(H)
    if not fl.any():
        return [], fl
    v = H[fl]
    key = np.round(v / 0.01).astype(np.int64)
    u, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    out = []
    for i, k in enumerate(u):
        a = cnt[i] * cell * cell
        if a >= min_area:
            out.append((float(np.median(v[inv == i])), float(a)))
    # neighbouring bins of the same floor (rounding) merge
    out.sort(key=lambda t: -t[0])
    merged = []
    for z, a in out:
        if merged and abs(merged[-1][0] - z) < 0.012:
            z0, a0 = merged[-1]
            merged[-1] = ((z0 * a0 + z * a) / (a0 + a), a0 + a)
        else:
            merged.append((z, a))
    return merged, fl


def _inside_radius(H, cell):
    """Smallest concave radius on the smooth part of the surface (mm), or None."""
    s = 2
    h = s * cell
    Hf = np.where(np.isfinite(H), H, np.nan)
    c = Hf[s:-s, s:-s]
    if c.size == 0:
        return None
    xm, xp = Hf[s:-s, :-2 * s], Hf[s:-s, 2 * s:]
    ym, yp = Hf[:-2 * s, s:-s], Hf[2 * s:, s:-s]
    with np.errstate(invalid="ignore"):
        gx, gy = (xp - xm) / (2 * h), (yp - ym) / (2 * h)
        lim = 1.7 * h   # no step steeper than 60 degrees anywhere in the stencil
        ok = (np.isfinite(xm + xp + ym + yp + c) & (np.abs(xp - c) < lim) & (np.abs(c - xm) < lim)
              & (np.abs(yp - c) < lim) & (np.abs(c - ym) < lim))
        hxx = (xp - 2 * c + xm) / (h * h)
        hyy = (yp - 2 * c + ym) / (h * h)
        hxy = (Hf[2 * s:, 2 * s:] - Hf[2 * s:, :-2 * s] - Hf[:-2 * s, 2 * s:] + Hf[:-2 * s, :-2 * s]) / (4 * h * h)
        tr, det = hxx + hyy, hxx * hyy - hxy * hxy
        kmax = tr / 2 + np.sqrt(np.maximum(tr * tr / 4 - det, 0))
        k = kmax / (1 + gx * gx + gy * gy) ** 1.5
        sel = ok & np.isfinite(k) & (k > 0.02)
    if sel.sum() < 20:
        return None
    r = 1.0 / np.percentile(k[sel], 99.0)
    return float(r)


def _narrowest_gap(H, cell):
    """Rough width of the narrowest slot or hole opening into the part (mm), or None."""
    f = 1
    while cell * f < 0.45:
        f += 1
    ny, nx = H.shape
    Hs = H[: ny // f * f, : nx // f * f].reshape(ny // f, f, nx // f, f)
    with np.errstate(invalid="ignore"):
        top = np.nanmax(np.where(np.isfinite(H), H, np.nan)) if np.isfinite(H).any() else 0.0
        solid = (np.where(np.isfinite(Hs), Hs, -np.inf) >= top - 0.2).any(axis=(1, 3))
    mask = ~solid
    if not mask.any() or not solid.any():
        return None
    cs = cell * f
    prev = 0.0
    for d in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0):
        rad = d / 2 / cs
        if d / 4 / cs < 1:
            prev = d
            continue
        m = mask.astype(np.float32)
        er = _disk(m, rad, np.minimum, 0.0)
        op = _disk(er, rad, np.maximum, 0.0) > 0.5
        lost = mask & ~op
        core = _disk(m, d / 4 / cs, np.minimum, 0.0) > 0.5
        if (lost & core).sum() >= 2:
            return (prev + d) / 2
        prev = d
    return None


def analyze(part, cell=None, bounds=None):
    """Top-down look at a placed part. bounds = (x0, y0, w, h) of the grid (default: the part's box).
    Returns a dict: grid (ny, nx) part top per cell (NaN outside the part), grid_c (the conservative grid used
    for tool positions, -inf outside), origin, cell, slope (degrees), undercut (bool mask), undercut_area,
    footprint (bool), depth (deepest point below the top, mm), size (w, d, h), floors [(z, area)],
    min_inside_r, narrowest_gap, is_block (the part is a full block: its sides are the box sides)."""
    tris = part.tris
    lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
    if bounds is None:
        bounds = (float(lo[0]), float(lo[1]), float(hi[0] - lo[0]), float(hi[1] - lo[1]))
    x0, y0, w, h = bounds
    if cell is None:
        cell = auto_cell(w, h)
    H, Hc = heights(tris, x0, y0, w, h, cell)
    ny, nx = H.shape
    fp = np.isfinite(H)
    nzt, _ = _normals_z(tris)
    slo = tris[(nzt > 0.02) & (nzt < 0.999)]
    Sg = _raster(slo, x0, y0, cell, nx, ny, 0.5) if len(slo) else np.full((ny, nx), -np.inf)
    with np.errstate(invalid="ignore"):
        curved = np.isfinite(Sg) & fp & (Sg >= H - 0.01)
    D = _down_tris(tris)
    und = np.isfinite(_raster(D, x0, y0, cell, nx, ny, 0.5)) if len(D) else np.zeros_like(fp)
    Hn = np.where(fp, H, np.nan)
    with np.errstate(invalid="ignore"):
        gy, gx = np.gradient(np.where(fp, H, float(lo[2])), cell)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    slope[~fp] = np.nan
    floors, flat = _floors(H, cell)
    top = float(hi[2])
    # a full block: the footprint fills the box and the rim is at the top
    pw, ph = float(hi[0] - lo[0]), float(hi[1] - lo[1])
    in_box = (abs(x0 - lo[0]) < 1e-6 and abs(y0 - lo[1]) < 1e-6 and abs(w - pw) < 1e-6 and abs(h - ph) < 1e-6)
    rim = np.concatenate([H[0], H[-1], H[:, 0], H[:, -1]])
    is_block = bool(in_box and fp.all() and np.all(np.isfinite(rim)) and np.all(rim >= top - 0.01))
    res = {
        "grid": Hn, "grid_c": Hc, "origin": (x0, y0), "cell": cell, "slope": slope,
        "undercut": und, "undercut_area": float(und.sum() * cell * cell), "footprint": fp,
        "depth": float(top - np.nanmin(Hn)) if fp.any() else 0.0,
        "size": (pw, ph, float(hi[2] - lo[2])),
        "floors": floors, "flat": flat, "top": top,
        "min_inside_r": _inside_radius(H, cell),
        "narrowest_gap": _narrowest_gap(H, cell),
        "is_block": is_block,
        "curved": curved,
        "curved_area": float(curved.sum() * cell * cell),
    }
    return res


# ============================================================================ simulation

def _tool_of(t):
    """A gcm/engine tool dict made complete enough for the profile (2D jobs carry fewer keys)."""
    d = dict(t)
    d.setdefault("tool_type", FLAT)
    d["tool_d"] = float(d.get("tool_d", 3.0))
    d.setdefault("shank_d", d["tool_d"])
    d.setdefault("flute_len", max(10.0, 3 * d["tool_d"]))
    return d


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = np.where(L2 > 1e-18, ((px - ax) * dx + (py - ay) * dy) / np.where(L2 > 1e-18, L2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    return np.hypot(px - ax - t * dx, py - ay - t * dy)


def _strips(flat, ox, oy, c, nx, ny, A, B, z0, z1, tool, caps_a, caps_b, chunk=3_000_000):
    """Exact cut of the tool moving in a straight line from (A, z0) to (B, z1), one row per move: min into flat
    (ny*nx). Cells are visited through a lattice laid along each move (0.7 cell apart, so every cell centre
    within the tool radius is hit). caps_a/caps_b: True where the end needs the full disk (ends of runs and
    sharp turns); elsewhere the next move's strip continues the cut. Level moves are exact; sloped moves take
    the lowest of several tool positions along the stretch above each cell."""
    if len(A) == 0:
        return
    R = float(tool["tool_d"]) / 2
    kind = tool.get("tool_type", FLAT)
    rm, q, _ = tool_profile(tool, 0.0)
    rc = R      # only the cutter cuts in the simulation (the shank would be a crash, not a cut)
    s = 0.7 * c
    nw = 2 * int(math.ceil(rc / s)) + 1
    d = B - A
    L = np.hypot(d[:, 0], d[:, 1])
    u = np.where(L[:, None] > 1e-9, d / np.maximum(L, 1e-9)[:, None], np.array([1.0, 0.0]))
    n = np.stack([-u[:, 1], u[:, 0]], 1)
    ea = np.where(caps_a < 0, rc, caps_a * rc + s)
    eb = np.where(caps_b < 0, rc, caps_b * rc + s)
    sloped = np.abs(z1 - z0) > 1e-6
    # each move's rectangle (along -ea..L+eb, across -rc..rc), cut into cell rows (scanline)
    c0 = A - ea[:, None] * u - rc * n
    c1 = A + (L + eb)[:, None] * u - rc * n
    c2 = A + (L + eb)[:, None] * u + rc * n
    c3 = A - ea[:, None] * u + rc * n
    Q = np.stack([c0, c1, c2, c3], 1)
    ys = Q[:, :, 1]
    j0 = np.clip(np.ceil((ys.min(1) - oy) / c - 0.5 - 1e-9), 0, ny).astype(np.int64)
    j1 = np.clip(np.floor((ys.max(1) - oy) / c - 0.5 + 1e-9), -1, ny - 1).astype(np.int64)
    nrow = np.maximum(j1 - j0 + 1, 0)
    rid = np.repeat(np.arange(len(A)), nrow)
    jr = j0[rid] + (np.arange(int(nrow.sum())) - np.repeat(np.cumsum(nrow) - nrow, nrow))
    yv = oy + (jr + 0.5) * c
    xl = np.full(len(rid), np.inf)
    xr = np.full(len(rid), -np.inf)
    for a_, b_ in ((0, 1), (1, 2), (2, 3), (3, 0)):
        Pa, Pb = Q[rid, a_], Q[rid, b_]
        lo_, hi_ = np.minimum(Pa[:, 1], Pb[:, 1]), np.maximum(Pa[:, 1], Pb[:, 1])
        m = (yv >= lo_ - 1e-9) & (yv <= hi_ + 1e-9)
        dyv = Pb[:, 1] - Pa[:, 1]
        t = np.where(np.abs(dyv) > 1e-12, (yv - Pa[:, 1]) / np.where(np.abs(dyv) > 1e-12, dyv, 1.0), 0.0)
        x = Pa[:, 0] + np.clip(t, 0, 1) * (Pb[:, 0] - Pa[:, 0])
        xl = np.where(m, np.minimum(xl, np.where(np.abs(dyv) > 1e-12, x, np.minimum(Pa[:, 0], Pb[:, 0]))), xl)
        xr = np.where(m, np.maximum(xr, np.where(np.abs(dyv) > 1e-12, x, np.maximum(Pa[:, 0], Pb[:, 0]))), xr)
    i0 = np.clip(np.ceil((xl - ox) / c - 0.5 - 1e-9), 0, nx).astype(np.int64)
    i1 = np.clip(np.floor((xr - ox) / c - 0.5 + 1e-9), -1, nx - 1).astype(np.int64)
    ncol = np.where(np.isfinite(xl), np.maximum(i1 - i0 + 1, 0), 0)
    keep = np.nonzero(ncol)[0]
    rid, jr, i0, ncol = rid[keep], jr[keep], i0[keep], ncol[keep]
    cum = np.cumsum(ncol)
    st = 0
    while st < len(rid):
        base = cum[st - 1] if st else 0
        en = max(int(np.searchsorted(cum, base + chunk, side="right")), st + 1)
        cs = ncol[st:en]
        k = np.arange(int(cs.sum())) - np.repeat(np.cumsum(cs) - cs, cs)
        r = np.repeat(rid[st:en], cs)
        ii = np.repeat(i0[st:en], cs) + k
        jj = np.repeat(jr[st:en], cs)
        cx, cy = ox + (ii + 0.5) * c - A[r, 0], oy + (jj + 0.5) * c - A[r, 1]
        Lr = L[r]
        along = cx * u[r, 0] + cy * u[r, 1]
        perp2 = np.maximum(cx * cx + cy * cy - along * along, 0.0)
        inside = perp2 <= rc * rc + 1e-12
        h = np.sqrt(np.maximum(rc * rc - perp2, 0.0))
        ta = np.clip(along - h, 0.0, Lr)
        tb = np.clip(along + h, 0.0, Lr)
        inside &= (along + h >= -1e-9) & (along - h <= Lr + 1e-9)
        zs0, zs1 = z0[r], z1[r]
        slope = np.where(Lr > 1e-9, (zs1 - zs0) / np.maximum(Lr, 1e-9), 0.0)
        tp = np.clip(along, 0.0, Lr)
        if kind == FLAT:
            # the flat bottom covers the cell for t in [ta, tb]: the lowest Z there
            v = zs0 + np.minimum(slope * ta, slope * tb)
            # vertical moves (L = 0): the lower end
            v = np.where(Lr > 1e-9, v, np.minimum(zs0, zs1))
        else:
            def f(t):
                dd = np.sqrt(perp2 + (along - t) ** 2)
                return zs0 + slope * t + q(np.minimum(dd, rc))
            v = f(tp)
            sl = np.nonzero(sloped[r] & inside)[0]
            if len(sl):
                vs = v[sl]
                for frac in (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0):
                    t_ = ta[sl] + (tb[sl] - ta[sl]) * frac
                    dd = np.sqrt(perp2[sl] + (along[sl] - t_) ** 2)
                    vs = np.minimum(vs, zs0[sl] + slope[sl] * t_ + q(np.minimum(dd, rc)))
                v[sl] = vs
            v = np.where(Lr > 1e-9, v, np.minimum(zs0, zs1) + q(np.minimum(np.sqrt(perp2 + along * along), rc)))
        m = inside & np.isfinite(v)
        np.minimum.at(flat, (jj * nx + ii)[m], v[m])
        st = en


def _on_grid(S, ox, oy, c):
    """Moves whose ends sit on cell centres and that run along a row, a column or a 45 degree diagonal."""
    fa = (S[:, 0:2] - (ox, oy)) / c - 0.5
    fb = (S[:, 3:5] - (ox, oy)) / c - 0.5
    on = (np.all(np.abs(fa - np.round(fa)) < 1e-4, 1) & np.all(np.abs(fb - np.round(fb)) < 1e-4, 1))
    dij = np.abs(np.round(fb) - np.round(fa))
    on &= (dij[:, 0] == 0) | (dij[:, 1] == 0) | (dij[:, 0] == dij[:, 1])
    return on, dij


def _sample_snap(flat, ox, oy, c, nx, ny, S):
    """Tip positions along on-grid moves (every cell centre they pass): min tip Z into flat."""
    if len(S) == 0:
        return
    a, b = S[:, 0:3], S[:, 3:6]
    grid, dij = _on_grid(S, ox, oy, c)
    nseg = np.maximum(np.maximum(dij[:, 0], dij[:, 1]), 1).astype(np.int64)
    cnt = nseg + 1
    cum = np.cumsum(cnt)
    st = 0
    while st < len(S):
        base = cum[st - 1] if st else 0
        en = max(int(np.searchsorted(cum, base + 4_000_000, side="right")), st + 1)
        cs = cnt[st:en]
        r = np.repeat(np.arange(st, en), cs)
        k = np.arange(int(cs.sum())) - np.repeat(np.cumsum(cs) - cs, cs)
        t = k / nseg[r]
        p = a[r] + (b[r] - a[r]) * t[:, None]
        ii = np.rint((p[:, 0] - ox) / c - 0.5).astype(np.int64)
        jj = np.rint((p[:, 1] - oy) / c - 0.5).astype(np.int64)
        ok = (ii >= 0) & (ii < nx) & (jj >= 0) & (jj < ny)
        np.minimum.at(flat, (jj * nx + ii)[ok], p[ok, 2])
        st = en


def _sim_cell(job):
    pg = getattr(job, "part_grid", None)
    if pg and pg.get("cell"):
        return float(pg["cell"])
    st = job.stock
    return max(0.08, auto_cell(st["w"], st["h"]))


def _sim_moves(segs, z_top=0.0):
    """Moves grouped per tool: {name: (tool, (n, 6) array)}; moves above the stock are left out."""
    by = {}
    for sg in segs:
        if min(sg[2], sg[5]) >= z_top - 1e-9:
            continue
        tl = sg[7]
        by.setdefault(tl.get("name", str(id(tl))), [tl, []])[1].append(sg[:6])
    return {k: (v[0], np.array(v[1], dtype=np.float64)) for k, v in by.items()}


def _sample_any(flat, ox, oy, c, nx, ny, S):
    """Tip positions every half cell along any move, put on the nearest cell (fast, about half a cell off)."""
    if len(S) == 0:
        return
    a, b = S[:, 0:3], S[:, 3:6]
    L = np.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1])
    nseg = np.maximum(np.ceil(L / (0.5 * c)), 1).astype(np.int64)
    cnt = nseg + 1
    cum = np.cumsum(cnt)
    st = 0
    while st < len(S):
        base = cum[st - 1] if st else 0
        en = max(int(np.searchsorted(cum, base + 4_000_000, side="right")), st + 1)
        cs = cnt[st:en]
        r = np.repeat(np.arange(st, en), cs)
        k = np.arange(int(cs.sum())) - np.repeat(np.cumsum(cs) - cs, cs)
        t = k / nseg[r]
        p = a[r] + (b[r] - a[r]) * t[:, None]
        ii = np.floor((p[:, 0] - ox) / c).astype(np.int64)
        jj = np.floor((p[:, 1] - oy) / c).astype(np.int64)
        ok = (ii >= 0) & (ii < nx) & (jj >= 0) & (jj < ny)
        np.minimum.at(flat, (jj * nx + ii)[ok], p[ok, 2])
        st = en


def _cut_surface(S, tool, ox, oy, c, nx, ny, approx=False):
    """Lowest surface the tool leaves when it makes the moves S (n, 6): (ny, nx) float32, +inf where untouched.
    Moves that run on cell centres go through the fast grid erosion; every other move is cut exactly."""
    tool = _tool_of(tool)
    rm, q, disks = tool_profile(tool, 0.0)
    R = tool["tool_d"] / 2
    cut_prof = tool_profile(dict(tool, shank_d=tool["tool_d"]), 0.0)
    out = np.full((ny, nx), np.inf, dtype=np.float32)
    if len(S) == 0:
        return out
    on, _ = _on_grid(S, ox, oy, c)
    T = np.full(ny * nx, np.inf)
    if approx:
        _sample_any(T, ox, oy, c, nx, ny, S)
        on[:] = True
    else:
        _sample_snap(T, ox, oy, c, nx, ny, S[on])
    if np.isfinite(T).any():
        np.minimum(out, _erode(T.reshape(ny, nx), c, cut_prof), out=out)
    ex = np.nonzero(~on)[0]
    if len(ex):
        # the same level move made again lower (passes down a contour): only the lowest one cuts
        Se = S[ex]
        lvl = np.abs(Se[:, 5] - Se[:, 2]) < 1e-6
        if lvl.sum() > 1:
            key = np.round(Se[:, [0, 1, 3, 4]], 5)
            swap = (key[:, 0] > key[:, 2]) | ((key[:, 0] == key[:, 2]) & (key[:, 1] > key[:, 3]))
            key[swap] = key[swap][:, [2, 3, 0, 1]]
            idx = np.nonzero(lvl)[0]
            kk = key[idx]
            order = np.lexsort((Se[idx, 2], kk[:, 3], kk[:, 2], kk[:, 1], kk[:, 0]))
            ks = kk[order]
            first = np.ones(len(order), bool)
            first[1:] = np.any(ks[1:] != ks[:-1], axis=1)
            drop = np.zeros(len(Se), bool)
            drop[idx[order[~first]]] = True
            ex = ex[~drop]
        A = S[ex][:, 0:2].copy()
        B = S[ex][:, 3:5].copy()
        z0, z1 = S[ex][:, 2].copy(), S[ex][:, 5].copy()
        # a move continues the previous one when it starts where that ended, without a sharp turn
        d0 = B - A
        L0 = np.hypot(d0[:, 0], d0[:, 1])
        with np.errstate(invalid="ignore", divide="ignore"):
            cosang = (d0[1:] * d0[:-1]).sum(1) / np.maximum(L0[1:] * L0[:-1], 1e-12)
        joined = ((np.hypot(B[:-1, 0] - A[1:, 0], B[:-1, 1] - A[1:, 1]) < 1e-6) & (np.abs(z1[:-1] - z0[1:]) < 1e-6)
                  & (cosang > 0.5) & (L0[1:] > 1e-9) & (L0[:-1] > 1e-9))
        # joined ends reach a little past the joint (tan of half the turn, times the radius) to fill the outer
        # wedge of the turn; free ends and sharp turns get the whole disk (-1)
        half = np.arccos(np.clip(np.nan_to_num(cosang, nan=1.0), -1, 1)) / 2
        ext = np.where(joined, np.tan(half), -1.0)
        caps_a = np.concatenate([[-1.0], ext])
        caps_b = np.concatenate([ext, [-1.0]])
        F = np.full(ny * nx, np.inf, dtype=np.float64)
        _strips(F, ox, oy, c, nx, ny, A, B, z0, z1, tool, caps_a, caps_b)
        np.minimum(out, F.reshape(ny, nx).astype(np.float32), out=out)
    return out


def simulate3d(job, cell=None):
    """Stock top after every move of the job: {"grid" (ny, nx), "origin" (x0, y0), "cell"}.
    The stock starts flat at Z0 over job.stock and ends at -t (cuts below the stock bottom read as -t).
    Works for 3D jobs and for 2D jobs from gcm_engine.build() once job.stock is set."""
    st = job.stock
    if cell is None:
        cell = _sim_cell(job)
    cache = getattr(job, "_sim", None)
    if cache and abs(cache["cell"] - cell) < 1e-12:
        return cache
    c = float(cell)
    ox, oy = float(st["x0"]), float(st["y0"])
    pg = getattr(job, "part_grid", None)
    if pg and abs(pg["cell"] - c) < 1e-12:
        ny, nx = pg["grid"].shape
        ox, oy = pg["origin"]
    else:
        nx = max(1, int(math.ceil(st["w"] / c - 1e-9)))
        ny = max(1, int(math.ceil(st["h"] / c - 1e-9)))
    sim = np.zeros((ny, nx), dtype=np.float32)
    rough = np.zeros(len(job.segs), bool)
    for op in getattr(job, "ops", None) or []:
        if op.get("kind") in ("rough",) and "seg_range" in op:
            a_, b_ = op["seg_range"]
            rough[a_:b_] = True
    for flag in (True, False):
        segs = [sg for sg, r_ in zip(job.segs, rough) if r_ == flag]
        for name, (tl, S) in _sim_moves(segs).items():
            np.minimum(sim, _cut_surface(S, tl, ox, oy, c, nx, ny, approx=flag), out=sim)
    np.maximum(sim, np.float32(-float(st["t"])), out=sim)
    res = {"grid": sim, "origin": (ox, oy), "cell": c}
    try:
        job._sim = res
    except Exception:
        pass
    return res


def leftover(job):
    """Simulated stock minus the part: positive = material left, negative = a gouge; NaN where there is no part."""
    sim = simulate3d(job)
    pg = getattr(job, "part_grid", None)
    if not pg or pg["grid"].shape != sim["grid"].shape:
        return {"grid": np.full(sim["grid"].shape, np.nan), "origin": sim["origin"], "cell": sim["cell"]}
    lo = sim["grid"].astype(np.float64) - pg["grid"]
    return {"grid": lo, "origin": sim["origin"], "cell": sim["cell"]}


# ============================================================================ path helpers (grid index space)
# Index coordinates: (u, v) = (column, row) as floats; world x = ox + (u + 0.5) * c, y = oy + (v + 0.5) * c.

def _march(F, level, cliff=None):
    """Contours of F at `level` (marching squares), the high side on the right of the direction of travel
    (so a tool following them climb-mills with a clockwise spindle). F is padded with a high border, so every
    contour closes. Returns [(P (n, 2) index coords, low (n, 2) int cell of the low side, t (n,), closed)].
    The low cell is the side where F <= level (where the tool may be)."""
    ny, nx = F.shape
    HI = np.float64(1e6)
    G = np.full((ny + 2, nx + 2), HI)
    G[1:-1, 1:-1] = F
    Ny, Nx = G.shape
    b = G > level
    b0, b1, b2, b3 = b[:-1, :-1], b[:-1, 1:], b[1:, 1:], b[1:, :-1]
    bits = [b0, b1, b2, b3]
    starts = [(~bits[k]) & bits[(k + 1) % 4] for k in range(4)]
    ends = [bits[k] & ~bits[(k + 1) % 4] for k in range(4)]
    ns = starts[0].astype(np.int8) + starts[1] + starts[2] + starts[3]
    sq = np.nonzero(ns)
    if len(sq[0]) == 0:
        return []
    si, sj = sq
    nss = ns[si, sj]

    def eid(i, j, k):
        if k == 0:
            return 2 * (i * Nx + j)
        if k == 1:
            return 2 * (i * Nx + j + 1) + 1
        if k == 2:
            return 2 * ((i + 1) * Nx + j)
        return 2 * (i * Nx + j) + 1

    S = np.stack([starts[k][si, sj] for k in range(4)], 1)
    E = np.stack([ends[k][si, sj] for k in range(4)], 1)
    seg_s, seg_e = [], []
    one = nss == 1
    i1, j1 = si[one], sj[one]
    ks, ke = np.argmax(S[one], 1), np.argmax(E[one], 1)
    for k in range(4):
        m = ks == k
        seg_s.append(eid(i1[m], j1[m], k))
    for k in range(4):
        m = ke == k
        seg_e.append((np.nonzero(m)[0], eid(i1[m], j1[m], k)))
    s1 = np.empty(len(i1), np.int64)
    e1 = np.empty(len(i1), np.int64)
    for k in range(4):
        m = ks == k
        s1[m] = eid(i1[m], j1[m], k)
        m2 = ke == k
        e1[m2] = eid(i1[m2], j1[m2], k)
    seg_s, seg_e = [s1], [e1]
    two = nss == 2
    if two.any():
        i2, j2 = si[two], sj[two]
        cen = (G[i2, j2] + G[i2, j2 + 1] + G[i2 + 1, j2 + 1] + G[i2 + 1, j2]) / 4 > level
        Sk = S[two]
        for k in range(4):
            m = Sk[:, k]
            if not m.any():
                continue
            ii, jj, cc = i2[m], j2[m], cen[m]
            ke_ = np.where(cc, (k - 1) % 4, (k + 1) % 4)
            seg_s.append(eid(ii, jj, k))
            ee = np.empty(len(ii), np.int64)
            for kk in range(4):
                mm = ke_ == kk
                ee[mm] = eid(ii[mm], jj[mm], kk)
            seg_e.append(ee)
    ss = np.concatenate(seg_s)
    es = np.concatenate(seg_e)
    succ = dict(zip(ss.tolist(), es.tolist()))
    # chain
    loops = []
    seen = set()
    endset = set(es.tolist())
    order = [s for s in ss.tolist() if s not in endset] + ss.tolist()
    for s0 in order:
        if s0 in seen:
            continue
        chain = [s0]
        seen.add(s0)
        cur = s0
        closed = False
        while True:
            nx_ = succ.get(cur)
            if nx_ is None:
                break
            if nx_ == s0:
                closed = True
                break
            if nx_ in seen:
                chain.append(nx_)
                break
            chain.append(nx_)
            seen.add(nx_)
            cur = nx_
        loops.append((np.array(chain, np.int64), closed))
    out = []
    for ids, closed in loops:
        cellk = ids // 2
        vert = (ids % 2) == 1
        gi, gj = cellk // Nx, cellk % Nx
        ai, aj = gi, gj
        bi = np.where(vert, gi + 1, gi)
        bj = np.where(vert, gj, gj + 1)
        fa, fb = G[ai, aj], G[bi, bj]
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.clip((level - fa) / (fb - fa), 0.0, 1.0)
        t = np.where(np.isfinite(t), t, 0.5)
        a_low = fa <= level
        lowi = np.where(a_low, ai, bi) - 1
        lowj = np.where(a_low, aj, bj) - 1
        # index coords of the crossing (minus the pad)
        u = aj - 1 + np.where(vert, 0.0, t)
        v = ai - 1 + np.where(vert, t, 0.0)
        P = np.stack([u, v], 1)
        low = np.stack([np.clip(lowj, 0, nx - 1), np.clip(lowi, 0, ny - 1)], 1)
        hii = np.where(a_low, bi, ai) - 1
        hij = np.where(a_low, bj, aj) - 1
        hi = np.stack([hij, hii], 1)
        # t measured from the low cell (0 = on the low cell centre)
        tl = np.where(a_low, t, 1.0 - t)
        out.append((P, low, hi, tl, closed))
    return out


def _dedupe_pts(P, closed=False, eps=1e-6):
    if len(P) < 2:
        return P
    d = np.hypot(*(np.diff(P[:, :2], axis=0).T))
    keep = np.concatenate([[True], d > eps])
    P = P[keep]
    if closed and len(P) > 2 and np.hypot(*(P[-1, :2] - P[0, :2])) <= eps:
        P = P[:-1]
    return P


def _dp(P, tol):
    """Douglas-Peucker on 3D points (keeps ends)."""
    n = len(P)
    if n < 3:
        return P
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        A, B = P[a], P[b]
        seg = B - A
        L2 = float(seg @ seg)
        Q = P[a + 1:b] - A
        if L2 < 1e-18:
            d = np.linalg.norm(Q, axis=1)
        else:
            t = np.clip(Q @ seg / L2, 0, 1)
            d = np.linalg.norm(Q - t[:, None] * seg, axis=1)
        k = int(np.argmax(d))
        if d[k] > tol:
            m = a + 1 + k
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return P[keep]


def _thin_open(P, tol):
    """Drops points while every original point stays within tol of the simplified line (3D). Vectorised:
    rounds of removing every other point where the chord past it still fits."""
    n = len(P)
    if n < 3:
        return P
    keep = np.arange(n)
    idle = 0
    for parity in (1, 0) * 40:
        if len(keep) < 3 or idle >= 2:
            break
        cand = np.arange(1, len(keep) - 1)
        cand = cand[(cand % 2) == parity]
        if len(cand) == 0:
            continue
        a, b = keep[cand - 1], keep[cand + 1]
        # every original point strictly between a and b against the chord a-b
        cnt = b - a - 1
        r = np.repeat(np.arange(len(cand)), cnt)
        k = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        pi = a[r] + 1 + k
        A, B = P[a[r]], P[b[r]]
        seg = B - A
        L2 = np.maximum((seg * seg).sum(1), 1e-18)
        t = np.clip(((P[pi] - A) * seg).sum(1) / L2, 0, 1)
        err = np.linalg.norm(P[pi] - A - t[:, None] * seg, axis=1)
        emax = np.zeros(len(cand))
        np.maximum.at(emax, r, err)
        rm_ = cand[emax <= tol]
        if len(rm_) == 0:
            idle += 1
            continue
        idle = 0
        m = np.ones(len(keep), bool)
        m[rm_] = False
        keep = keep[m]
    return P[keep]


def _dp(P, tol):
    return _thin_open(P, tol)


def _thin(P, tol, closed=False):
    if closed and len(P) > 3:
        Q = np.vstack([P, P[:1]])
        Q = _thin_open(Q, tol)
        return Q[:-1]
    return _thin_open(P, tol)


def _line_cells(a, b):
    """Cells (u, v int) along the straight line between index points a and b (every half cell)."""
    d = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    n = max(1, int(math.ceil(d * 2)))
    t = np.linspace(0, 1, n + 1)
    u = np.rint(a[0] + (b[0] - a[0]) * t).astype(np.int64)
    v = np.rint(a[1] + (b[1] - a[1]) * t).astype(np.int64)
    return u, v


def _line_max(G, a, b, pad=0):
    """Highest value of G under the line from a to b (index coords), widened by `pad` cells."""
    ny, nx = G.shape
    d = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    n = max(1, int(math.ceil(d * 2)))
    t = np.linspace(0, 1, n + 1)
    u = a[0] + (b[0] - a[0]) * t
    v = a[1] + (b[1] - a[1]) * t
    us = np.concatenate([np.floor(u), np.ceil(u)]).astype(np.int64)
    vs = np.concatenate([np.floor(v), np.ceil(v)]).astype(np.int64)
    us, vs = np.meshgrid(us, vs, indexing="ij") if False else (np.concatenate([us, us]), np.concatenate([vs, vs[::-1]]))
    us = np.clip(us, 0, nx - 1)
    vs = np.clip(vs, 0, ny - 1)
    m = float(G[vs, us].max())
    if pad:
        m = max(m, float(G[np.clip(vs + pad, 0, ny - 1), us].max()), float(G[np.clip(vs - pad, 0, ny - 1), us].max()),
                float(G[vs, np.clip(us + pad, 0, nx - 1)].max()), float(G[vs, np.clip(us - pad, 0, nx - 1)].max()))
    return m


def _row_runs(M, rows):
    """Runs of True along each chosen row of M: {row: [(a, b), ...]} (inclusive column indices)."""
    out = {}
    if len(rows) == 0:
        return out
    Ms = M[rows]
    p = np.zeros((len(rows), M.shape[1] + 2), np.int8)
    p[:, 1:-1] = Ms
    d = np.diff(p, axis=1)
    rs, cs = np.nonzero(d == 1)
    re, ce = np.nonzero(d == -1)
    for r, a, b in zip(rs.tolist(), cs.tolist(), (ce - 1).tolist()):
        out.setdefault(int(rows[r]), []).append((a, b))
    return out


def _zigzag(runs, rows, link_ok):
    """Joins row runs into back-and-forth chains. runs {row: [(a, b)]}, rows sorted.
    Returns [list of (u, v) index points]."""
    used = {r: [False] * len(runs.get(r, [])) for r in rows}
    pos_of_row = {r: k for k, r in enumerate(rows)}
    chains = []
    last = None
    while True:
        # the next unused run: the one nearest the last chain's end (else the first)
        best = None
        for r in rows:
            for k, (a, b) in enumerate(runs.get(r, [])):
                if used[r][k]:
                    continue
                if last is None:
                    best = (0, r, k)
                    break
                dd = min(abs(a - last[0]), abs(b - last[0])) + abs(r - last[1])
                if best is None or dd < best[0]:
                    best = (dd, r, k)
            if last is None and best is not None:
                break
        if best is None:
            break
        _, r, k = best
        a, b = runs[r][k]
        used[r][k] = True
        if last is not None and abs(b - last[0]) < abs(a - last[0]):
            a, b = b, a
        pts = [(a, r), (b, r)]
        cur = (b, r)
        while True:
            pi = pos_of_row[cur[1]]
            nxt = None
            for step in (1, -1):
                q = pi + step
                if q < 0 or q >= len(rows):
                    continue
                r2 = rows[q]
                for k2, (a2, b2) in enumerate(runs.get(r2, [])):
                    if used[r2][k2]:
                        continue
                    lo, hi = min(a2, b2), max(a2, b2)
                    tgt = min(max(cur[0], lo), hi)
                    gap = abs(r2 - cur[1])
                    if abs(tgt - cur[0]) <= 2 * gap + 2 and link_ok(cur, (tgt, r2)):
                        nxt = (r2, k2, tgt, lo, hi)
                        break
                if nxt:
                    break
            if nxt is None:
                break
            r2, k2, tgt, lo, hi = nxt
            used[r2][k2] = True
            near, far = (lo, hi) if abs(tgt - lo) <= abs(tgt - hi) else (hi, lo)
            pts.append((tgt, r2))
            if near != tgt:
                pts.append((near, r2))
            pts.append((far, r2))
            cur = (far, r2)
        chains.append(pts)
        last = cur
    return chains


def _order_nearest(items, start, key_start, key_end, reversible=None):
    """Greedy nearest-neighbour order of paths. key_start/key_end give (x, y); returns ordered items
    (reversed where reversible(item) and the end is nearer)."""
    left = list(items)
    out = []
    pos = start
    while left:
        bi, brev, bd = 0, False, None
        for i, it in enumerate(left):
            s = key_start(it)
            d = (s[0] - pos[0]) ** 2 + (s[1] - pos[1]) ** 2
            if bd is None or d < bd:
                bi, brev, bd = i, False, d
            if reversible and reversible(it):
                e = key_end(it)
                d2 = (e[0] - pos[0]) ** 2 + (e[1] - pos[1]) ** 2
                if d2 < bd:
                    bi, brev, bd = i, True, d2
        it = left.pop(bi)
        if brev:
            it = it.reversed()
        out.append(it)
        pos = key_end(it)
    return out


class Path:
    """One continuous cutting move list: pts (n, 3) world, closed flag, z_top (material top at the entry)."""

    def __init__(self, pts, closed=False, entry_top=0.0, kind=""):
        self.pts = np.asarray(pts, dtype=np.float64)
        self.closed = closed
        self.entry_top = entry_top
        self.kind = kind

    def reversed(self):
        return Path(self.pts[::-1].copy(), self.closed, self.entry_top, self.kind)

    def rotated_to(self, x, y):
        """A closed path starting at its point nearest (x, y)."""
        if not self.closed or len(self.pts) < 3:
            return self
        k = int(np.argmin((self.pts[:, 0] - x) ** 2 + (self.pts[:, 1] - y) ** 2))
        return Path(np.vstack([self.pts[k:], self.pts[:k]]), True, self.entry_top, self.kind)

    @property
    def length(self):
        P = self.pts if not self.closed else np.vstack([self.pts, self.pts[:1]])
        return float(np.hypot(*np.diff(P[:, :2], axis=0).T).sum()) if len(P) > 1 else 0.0


# ============================================================================ tool choice

def _label(t):
    return gcm._tool_label(t)


def _rack_tools(rack, material, machine):
    out = []
    for b in rack:
        if b.get("tool_type") not in (FLAT, BALL, VBIT, P.SURF) or not P.bit_cuts(b, material):
            continue
        t = P.tool_for(b, material, machine)
        t.setdefault("reach", b.get("flute_len", 10.0))
        t.setdefault("flute_len", b.get("flute_len", 10.0))
        t.setdefault("shank_d", b.get("shank_d", b["tool_d"]))
        t["owned"] = bool(b.get("owned", True))
        out.append(t)
    return out


def _pick_by_reach(cands, depth):
    """(chosen, the best owned bit that is too short or None). Owned bits first, then the biggest diameter,
    then the shortest reach (stiffer)."""
    ok = [t for t in cands if t["reach"] >= depth - 1e-6]
    owned_ok = [t for t in ok if t["owned"]]
    pool = owned_ok or ok
    short_owned = sorted([t for t in cands if t["owned"] and t["reach"] < depth - 1e-6],
                         key=lambda t: (-t["tool_d"], -t["reach"]))
    if not pool:
        if not cands:
            return None, None
        best = max(cands, key=lambda t: (t["reach"], t["tool_d"]))
        return best, None
    best = max(pool, key=lambda t: (t["tool_d"], -t["reach"], t["flutes"]))
    alt = short_owned[0] if (short_owned and not best["owned"]) else None
    if alt is None and short_owned and short_owned[0]["tool_d"] > best["tool_d"]:
        alt = short_owned[0]
    return best, alt


def _reach_reason(t, alt, depth, what):
    s = f"{t['name']}: the biggest {what} that reaches the {depth:.1f} mm deep part"
    if alt is not None:
        s += f" (the {alt['name']} reaches only {alt['reach']:g} mm)"
    if not t["owned"]:
        s += "; it is not in the rack yet"
    return s + "."


# ============================================================================ the planner

class _Grid:
    def __init__(self, ox, oy, c, ny, nx):
        self.ox, self.oy, self.c, self.ny, self.nx = ox, oy, c, ny, nx

    def xy(self, u, v):
        return self.ox + (np.asarray(u) + 0.5) * self.c, self.oy + (np.asarray(v) + 0.5) * self.c

    def uv(self, x, y):
        return (np.asarray(x) - self.ox) / self.c - 0.5, (np.asarray(y) - self.oy) / self.c - 0.5


def _drop_exact(Hc, g, prof, X, Y):
    """Exact lowest tip Z at off-grid points (X, Y) against the grid samples of Hc (drop cutter)."""
    rm, q, _ = prof
    c = g.c
    K = int(math.floor(rm / c)) + 1
    X = np.asarray(X, dtype=np.float64)
    Y = np.asarray(Y, dtype=np.float64)
    out = np.full(len(X), -np.inf)
    if len(X) == 0:
        return out
    ny, nx = Hc.shape
    di, dj = np.mgrid[-K:K + 2, -K:K + 2]
    di, dj = di.ravel(), dj.ravel()
    u = (X - g.ox) / c - 0.5
    v = (Y - g.oy) / c - 0.5
    bu, bv = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
    per = max(1, 3_000_000 // len(di))
    for a in range(0, len(X), per):
        cu = bu[a:a + per, None] + dj[None, :]
        cv = bv[a:a + per, None] + di[None, :]
        d = np.hypot(g.ox + (cu + 0.5) * c - X[a:a + per, None], g.oy + (cv + 0.5) * c - Y[a:a + per, None])
        ok = (d <= rm) & (cu >= 0) & (cu < nx) & (cv >= 0) & (cv < ny)
        val = Hc[np.clip(cv, 0, ny - 1), np.clip(cu, 0, nx - 1)] - q(np.minimum(d, rm))
        out[a:a + per] = np.where(ok, val, -np.inf).max(axis=1)
    return out


def _fat_profile(prof, d):
    """The tool grown by d all round its side: q_fat(r) = q(r - d). A CL surface made with it is an upper bound
    of the true CL anywhere within d of the cell centre."""
    rm, q, disks = prof

    def qf(r):
        return q(np.maximum(np.asarray(r, dtype=np.float64) - d, 0.0))
    return rm + d, qf, [(rad + d, v) for rad, v in disks]


def _cl_window(Hc, c, prof, win):
    """CL surface for one window of the grid, worked out with a margin so it is exact inside the window."""
    y0, y1, x0, x1 = win
    pad = int(math.ceil(prof[0] / c)) + 1
    ny, nx = Hc.shape
    a0, a1, b0, b1 = max(0, y0 - pad), min(ny, y1 + pad), max(0, x0 - pad), min(nx, x1 + pad)
    W = _dilate(Hc[a0:a1, b0:b1], c, prof)
    return W[y0 - a0: y0 - a0 + (y1 - y0), x0 - b0: x0 - b0 + (x1 - x0)]


def _contour_paths(F, level, g, keep=None, fat=None, snap=False, min_len=0.6, bias=0.1, sub=None):
    """Contours of F at level as world polylines (z = level), high side on the right.
    snap: put every point on the centre of its low cell (for masks). keep: bool grid; points whose low cell is
    outside it are cut out. fat: the CL surface of the tool grown by half a cell (same window as F): across a
    cliff of F a point may sit at most half a cell off its low cell, and only where fat says that is safe.
    sub: (v0, u0) offset when F is a window of the full grid."""
    v0, u0 = sub or (0, 0)
    out = []
    c = g.c
    ny, nx = F.shape
    for Pidx, low, hi, tl, closed in _march(F, level):
        if snap:
            uv = low.astype(np.float64)
        else:
            tt = np.maximum(tl - bias, 0.0)
            lu, lv = low[:, 0].astype(np.float64), low[:, 1].astype(np.float64)
            su, sv = (hi[:, 0] - low[:, 0]).astype(np.float64), (hi[:, 1] - low[:, 1]).astype(np.float64)
            hu, hv = np.clip(hi[:, 0], 0, nx - 1), np.clip(hi[:, 1], 0, ny - 1)
            # steeper than about 63 degrees between the two cells: the straight-line guess may sit inside a
            # wall top, so the point only moves off its low cell where the half-cell-fat tool says it is safe
            d1 = np.abs(F[hv, hu] - F[low[:, 1], low[:, 0]])
            jump = (d1 > 2.0 * c)
            if fat is not None and jump.any():
                ok = fat[low[:, 1], low[:, 0]] <= level + 1e-6
                tt = np.where(jump, np.where(ok, np.minimum(tt, 0.5), 0.0), tt)
            elif jump.any():
                tt = np.where(jump, 0.0, tt)
            uv = np.stack([lu + su * tt, lv + sv * tt], 1)
        if keep is not None:
            inside = keep[low[:, 1], low[:, 0]]
        else:
            inside = np.ones(len(uv), bool)
        X, Y = g.xy(uv[:, 0] + u0, uv[:, 1] + v0)
        pts = np.stack([X, Y, np.full(len(X), level)], 1)
        pieces = []
        if inside.all():
            pieces.append((pts, closed))
        else:
            m = inside.astype(np.int8)
            if closed and m.any():
                k = int(np.argmin(m))      # start the walk at an outside point
                pts, m = np.vstack([pts[k:], pts[:k]]), np.concatenate([m[k:], m[:k]])
            d = np.diff(np.concatenate([[0], m, [0]]))
            for a, b in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
                pieces.append((pts[a:b], False))
        for pp, cl in pieces:
            pp = _dedupe_pts(pp, cl)
            if len(pp) < 2:
                if len(pp) == 1 and snap:
                    out.append(Path(pp, False))
                continue
            p = Path(pp, cl)
            if p.length >= min_len or snap:
                out.append(p)
    return out


def _ramp(o, pts, closed, z0, z1, tan):
    """Feeds down from z0 to z1 along the start of the path (round a loop, back and forth on a line),
    ending on the path's first point at z1."""
    if z0 - z1 < 1e-6:
        o.g1(pts[0][0], pts[0][1], z1)
        return
    P2 = [(float(p[0]), float(p[1])) for p in pts]
    D = (z0 - z1) / max(tan, 1e-3)
    if closed and len(P2) >= 3:
        L = Path(np.array(pts), True).length
        if L < 0.3:
            o.g1(P2[0][0], P2[0][1], z1)
            return
        gcm._walk(o, P2, True, 0.0, D, z0, z1)
        rest = math.ceil(D / L - 1e-9) * L
        if rest - D > 1e-6:
            gcm._walk(o, P2, True, D, rest, z1, z1)
        return
    # open: the first up-to-20 mm of the path, forward and back
    acc, sub = 0.0, [P2[0]]
    for a, b in zip(P2[:-1], P2[1:]):
        dd = math.hypot(b[0] - a[0], b[1] - a[1])
        if acc + dd >= 20.0:
            f = (20.0 - acc) / dd if dd > 0 else 0
            sub.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            acc = 20.0
            break
        sub.append(b)
        acc += dd
    L = acc
    if L < 0.3:
        o.g1(P2[0][0], P2[0][1], z1)
        return
    rev = sub[::-1]
    z = z0
    fwd = True
    while z > z1 + 1e-9:
        dz = min(z - z1, L * tan)
        legL = dz / tan
        seq = sub if fwd else rev
        gcm._walk(o, seq, False, 0.0, legL, z, z - dz)
        z -= dz
        if legL < L - 1e-9:
            # finish this leg at depth, so the next leg starts from an end
            gcm._walk(o, seq, False, legL, L, z, z)
        fwd = not fwd
    if not fwd:
        gcm._walk(o, rev, False, 0.0, L, z1, z1)


class _Builder:
    """Collects one program: tools in order, their operations, and writes the G-code through gcm._Out."""

    def __init__(self, machine):
        self.m = machine
        self.o = gcm._Out()
        self.safe = float(machine["safe_z"])
        self.ops = []

    def at(self):
        o = self.o
        if not all(o.k.values()):
            return None
        return (o.p["x"], o.p["y"], o.p["z"])

    def lift_safe(self):
        self.o.g0(z=self.safe)

    def go_down_to(self, x, y, z_air, z):
        """From safe height: rapid to x y, rapid down to z_air (above the material there), feed to z."""
        o = self.o
        if self.at() is None or o.p["z"] < self.safe - 1e-6:
            o.g0(z=self.safe)
        o.g0(x=x, y=y)
        if z_air < self.safe - 1e-6:
            o.g0(z=max(z_air, z))
        if z < max(z_air, z) - 1e-9 or o.p["z"] > z + 1e-9:
            o.g1(x, y, z)

    def move_at(self, x, y, z):
        """Sideways at height z: rapid above the stock, feed below it."""
        if z >= 0:
            self.o.g0(x=x, y=y)
        else:
            self.o.g1(x, y, z)

    def cut(self, pts, closed=False, start=0):
        o = self.o
        for p in pts[start:]:
            o.g1(float(p[0]), float(p[1]), float(p[2]))
        if closed and len(pts) > 2:
            o.g1(float(pts[0][0]), float(pts[0][1]), float(pts[0][2]))


def _link_fin(b, nxt, CL, SD, g, dmax, z_safe):
    """Moves to the start of the next finishing path: straight across when the CL surface and the stock allow,
    else up over everything in the way (the stock grown by the tool), across, and down."""
    o = b.o
    cur = b.at()
    x1, y1, z1 = float(nxt[0]), float(nxt[1]), float(nxt[2])
    u1, v1 = g.uv(x1, y1)
    sd1 = float(SD[int(np.clip(round(float(v1)), 0, g.ny - 1)), int(np.clip(round(float(u1)), 0, g.nx - 1))])
    if cur is None or cur[2] > z_safe - 1e-6:
        b.go_down_to(x1, y1, min(z_safe, sd1 + 0.5), z1)
        return
    x0, y0, z0 = cur
    u0, v0 = g.uv(x0, y0)
    d = math.hypot(x1 - x0, y1 - y0)
    zh = max(z0, z1)
    if d <= dmax:
        clm = _line_max(CL, (float(u0), float(v0)), (float(u1), float(v1)))
        sdm = _line_max(SD, (float(u0), float(v0)), (float(u1), float(v1)))
        if clm <= zh + 1e-6 and sdm <= zh + 1.0:
            if z0 < zh:
                o.g1(x0, y0, zh)
            o.g1(x1, y1, zh)
            if z1 < zh:
                o.g1(x1, y1, z1)
            return
    zl = min(z_safe, max(_line_max(SD, (float(u0), float(v0)), (float(u1), float(v1)), pad=1), z0, z1) + 0.5)
    o.g0(z=zl)
    b.move_at(x1, y1, zl)
    za = max(z1, min(zl, sd1 + 0.5))
    if za < zl - 1e-6:
        o.g0(z=za)
    o.g1(x1, y1, z1)


def _raster_paths(CL, region, g, step_cells, along_x, thin_tol, cliff):
    """Parallel rows over region on the CL surface (points on cell centres), joined into zigzags where the
    link stays in the region. Returns [Path]."""
    M = region if along_x else region.T
    C = CL if along_x else CL.T
    nrow = M.shape[0]
    rows = np.nonzero(M.any(axis=1))[0]
    if len(rows) == 0:
        return []
    first = rows[0]
    sel = np.arange(first, rows[-1] + 1, step_cells)
    if sel[-1] != rows[-1]:
        sel = np.append(sel, rows[-1])
    # split runs where the surface jumps (a cliff between two cells)
    J = np.abs(np.diff(C, axis=1)) > cliff
    runs0 = _row_runs(M, sel)
    runs = {}
    for r, lst in runs0.items():
        for a, b in lst:
            cuts = np.nonzero(J[r, a:b])[0]
            s = a
            for k in cuts.tolist():
                runs.setdefault(r, []).append((s, a + k))
                s = a + k + 1
            runs.setdefault(r, []).append((s, b))

    def link_ok(p, q):
        uu, vv = _line_cells(p, q)
        if not M[np.clip(vv, 0, nrow - 1), np.clip(uu, 0, M.shape[1] - 1)].all():
            return False
        z = C[vv, uu]
        return bool(np.all(np.abs(np.diff(z)) <= cliff))

    chains = _zigzag(runs, list(sel), link_ok)
    out = []
    for ch in chains:
        us, vs = [], []
        for (a, r), (b, r2) in zip(ch[:-1], ch[1:]):
            if r == r2:
                n = abs(b - a)
                uu = np.linspace(a, b, n + 1).round().astype(np.int64)
                vv = np.full(len(uu), r)
            else:
                uu, vv = _line_cells((a, r), (b, r2))
            if us:
                uu, vv = uu[1:], vv[1:]
            us.append(uu)
            vs.append(vv)
        if not us:
            a, r = ch[0]
            us, vs = [np.array([a])], [np.array([r])]
        uu, vv = np.concatenate(us), np.concatenate(vs)
        z = C[vv, uu].astype(np.float64)
        if along_x:
            X, Y = g.xy(uu, vv)
        else:
            X, Y = g.xy(vv, uu)
        pts = np.stack([X, Y, z], 1)
        pts = _dedupe_pts(pts)
        pts = _thin(pts, thin_tol)
        out.append(Path(pts, False))
    return out


def _bbox(mask, pad, shape):
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    ny, nx = shape
    return (max(0, ys.min() - pad), min(ny, ys.max() + pad + 1), max(0, xs.min() - pad), min(nx, xs.max() + pad + 1))


# ============================================================================ build3d

def _measure_fn(b, s, probe):
    px, py = probe

    def pause(line):
        b.o.raw(line)
        if line.upper().startswith("M0"):
            msg = line[2:].strip().strip("()").strip()
            b.o.pauses.append((len(b.o.segs), msg or "Paused: Cycle Start to carry on"))

    def measure(t, n, first):
        o = b.o
        if first and s.get("user_zero"):   # the operator zeroed T1 on the stock before starting
            return
        if not first and s.get("bed_sensor"):   # bit changes measured on the fixed bed sensor
            gcm.bed_change(o, s, t, n, pause)
            return
        o.raw(f"(---- {'Start' if first else 'Tool change'}: T{n} {gcm._cmt(t['name'])}, measured at the sensor "
              f"spot X{px:.1f} Y{py:.1f} ----)")
        if not first:
            o.g0(z=s["safe_z"])
        gcm.show_lift(o, t)
        o.machine(z=-3.0)
        o.raw("M5")
        o.g0(x=px, y=py)
        gcm.show_sensor(o, px, py, t)
        for line in s["tc_code"].split(";"):
            if first:
                for w in ("end mill", "bit"):   # (older settings still say "bit")
                    line = line.replace(f"Put in T{{tool}} {{name}}, sensor under the {w}", "Sensor under T{tool} {name}")
            line = line.strip().replace("{tool}", str(n)).replace("{name}", gcm._cmt(t["name"]))
            if line:
                pause(line)
        o.forget()
    return measure


def _rough_step(t, material):
    gm = P.MATERIALS[material]["gcm"]
    k = 0.25 if gm == "Aluminium" else (0.75 if gm.startswith("Wood") else 0.4)
    return max(0.2, min(k * t["tool_d"], t.get("flute_len", 10.0) - 0.5))


def _emit_rough_level(b, g, items, items2, z, z_prev, R_prev, tan, ny, nx):
    """One roughing level: zigzags then the wall contours, each entered by a ramp from just above the
    material; between paths the tool stays low where the level above is already cleared."""
    for p in items + items2:
        x0, y0 = float(p.pts[0, 0]), float(p.pts[0, 1])
        cur = b.at()
        z_air = z_prev + 1.0
        linked = False
        if cur is not None and R_prev is not None and cur[2] < b.safe - 1e-6:
            u0, v0 = g.uv(cur[0], cur[1])
            u1, v1 = g.uv(x0, y0)
            uu, vv = _line_cells((float(u0), float(v0)), (float(u1), float(v1)))
            if R_prev[np.clip(vv, 0, ny - 1), np.clip(uu, 0, nx - 1)].all():
                if cur[2] < z_air:
                    b.o.g0(z=z_air)
                b.move_at(x0, y0, max(cur[2], z_air))
                linked = True
        if not linked:
            b.go_down_to(x0, y0, z_air, z_air)
        _ramp(b.o, p.pts, p.closed, b.o.p["z"], z, tan)
        b.cut(p.pts, p.closed, start=1 if not p.closed else 0)


def _emit_floor_level(b, g, items, zf, CLf, allow, tan, ny, nx):
    """A flat floor: zigzag at the floor depth, then the walls round it, all with the flat tool."""
    Fl = CLf <= zf + 0.003
    for p in items:
        x0, y0 = float(p.pts[0, 0]), float(p.pts[0, 1])
        cur = b.at()
        ok = False
        if cur is not None and abs(cur[2] - zf) < 1e-6:
            u0, v0 = g.uv(cur[0], cur[1])
            u1, v1 = g.uv(x0, y0)
            uu, vv = _line_cells((float(u0), float(v0)), (float(u1), float(v1)))
            if Fl[np.clip(vv, 0, ny - 1), np.clip(uu, 0, nx - 1)].all():
                b.o.g1(x0, y0, zf)
                ok = True
        if not ok:
            b.go_down_to(x0, y0, zf + allow + 1.0, zf + allow + 1.0)
            _ramp(b.o, p.pts, p.closed, b.o.p["z"], zf, tan)
        b.cut(p.pts, p.closed, start=1 if not p.closed else 0)
    b.lift_safe()


# ---------------------------------------------------------------------------- operations of a 3D job
# Stable op keys (options["bits"] = {key: rack bit name}, options["order"] = [keys], op cards in the plan JSON):
#   surface        Surfacing: faces the stock top down to the part (stock thicker than the part) or a skim
#   holes          Holes: small round vertical holes, helical (or plunge + circle), one at a time, full depth
#   rough          Roughing: region by region (cavity, pocket, ...), each from the top to its full depth
#   finish_flat    Finishing of the flat floors and the walls round them (pocket floors)
#   finish_curved  Finishing of the gentle curved surfaces, in parallel rows
#   finish_steep   Finishing of the steep walls, in level loops (waterline)
#   rest           Rest: tight corners the finishing ball cannot reach, with a smaller ball
#   chamfer        Chamfer along the rims of everything that breaks the top face
#   engrave        Text and logos on the flat top face
#   cutout         Cut-out round the outline (a part that is not the whole block)
OP3_KEYS = ("surface", "holes", "rough", "finish_flat", "finish_curved", "finish_steep", "rest", "chamfer",
            "engrave", "cutout")
OP3_LABELS = {"surface": "Surfacing", "holes": "Holes", "rough": "Roughing", "finish_flat": "Finishing: flat floors",
              "finish_curved": "Finishing: curved", "finish_steep": "Finishing: steep walls",
              "rest": "Rest: tight corners", "chamfer": "Chamfer", "engrave": "Text and logos", "cutout": "Cut-out"}
OP3_BITS = {"surface": (FLAT,), "holes": (FLAT,), "rough": (FLAT,), "finish_flat": (FLAT, BALL),
            "finish_curved": (BALL, FLAT), "finish_steep": (BALL, FLAT), "rest": (BALL,), "chamfer": (VBIT,),
            "engrave": (FLAT, VBIT), "cutout": (FLAT,)}
OP3_NAME = {"surfacing": "surface", "holes (helical)": "holes", "roughing": "rough",
            "flat floors and walls (pocket)": "finish_flat", "finishing (parallel)": "finish_curved",
            "finishing (waterline)": "finish_steep", "detail (rest, parallel)": "rest",
            "detail (rest, waterline)": "rest", "chamfer": "chamfer", "engraving": "engrave",
            "outline cut-out": "cutout"}
FIN3 = ("finish_flat", "finish_curved", "finish_steep", "rest")
STRATEGIES3 = (("fewest", "Fewest end mill changes"), ("recommended", "Recommended"), ("fastest", "Fastest"))


def order3(order=None):
    """The run order of the op keys: Surfacing first and Cut-out last always; Holes before (default) or after
    Roughing; Chamfer and Text before or after (default) the finishing, as options["order"] lists them."""
    o = [k for k in (order or []) if k in OP3_KEYS]
    pos = {k: i for i, k in enumerate(o)}
    holes_first = not ("holes" in pos and "rough" in pos and pos["holes"] > pos["rough"])
    fin = [pos[k] for k in FIN3 if k in pos]

    def before_fin(k):
        return bool(fin) and k in pos and pos[k] < min(fin)
    marks = ["chamfer", "engrave"]
    if "chamfer" in pos and "engrave" in pos and pos["engrave"] < pos["chamfer"]:
        marks = ["engrave", "chamfer"]
    seq = ["surface"] + (["holes", "rough"] if holes_first else ["rough", "holes"])
    seq += [k for k in marks if before_fin(k)] + list(FIN3) + [k for k in marks if not before_fin(k)] + ["cutout"]
    return seq


def _components(mask):
    """4-connected regions of a bool grid, without scipy: (label grid, 1.. n; [(r0, r1, c0, c1, cells)] per label)."""
    ny, nx = mask.shape
    d = np.diff(np.pad(mask.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    rs, cs = np.nonzero(d == 1)
    _, ce = np.nonzero(d == -1)
    n = len(rs)
    lab = np.zeros((ny, nx), np.int32)
    if n == 0:
        return lab, []
    parent = np.arange(n)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    start = np.searchsorted(rs, np.arange(ny + 1))
    for r in range(1, ny):
        i, i1, j, j1 = start[r - 1], start[r], start[r], start[r + 1]
        while i < i1 and j < j1:
            if cs[i] < ce[j] and cs[j] < ce[i]:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            if ce[i] < ce[j]:
                i += 1
            else:
                j += 1
    roots = np.array([find(i) for i in range(n)])
    uniq, idx = np.unique(roots, return_inverse=True)
    stats = [[ny, 0, nx, 0, 0] for _ in uniq]
    for k in range(n):
        L = idx[k]
        r, c0, c1 = int(rs[k]), int(cs[k]), int(ce[k])
        lab[r, c0:c1] = L + 1
        s = stats[L]
        s[0], s[1], s[2], s[3] = min(s[0], r), max(s[1], r + 1), min(s[2], c0), max(s[3], c1)
        s[4] += c1 - c0
    return lab, [tuple(s) for s in stats]


def _find_holes(lab, stats, Hfill, top_z, cell, verts):
    """Small round vertical holes with a flat bottom: [{x, y, r, z (floor), depth, k (label)}]."""
    ny, nx = lab.shape
    out = []
    for k, (r0, r1, c0, c1, n) in enumerate(stats):
        if r0 == 0 or c0 == 0 or r1 >= ny or c1 >= nx:
            continue
        w, h = (c1 - c0) * cell, (r1 - r0) * cell
        d = (w + h) / 2
        if not 1.5 <= d <= 25.0 or abs(w - h) > max(2.5 * cell, 0.06 * d):
            continue
        A = n * cell * cell
        if not 0.70 <= A / (w * h) <= 0.86:
            continue
        sub = lab[r0:r1, c0:c1] == k + 1
        Hv = Hfill[r0:r1, c0:c1][sub]
        zf = float(np.median(Hv))
        if float(np.mean(np.abs(Hv - zf) < 0.03)) < 0.9 or zf > top_z - 0.5:
            continue
        vv, uu = np.nonzero(sub)
        cx, cy = (c0 + uu.mean() + 0.5) * cell, (r0 + vv.mean() + 0.5) * cell
        r_eq = math.sqrt(A / math.pi)
        sel = ((np.abs(verts[:, 0] - cx) < 1.5 * r_eq) & (np.abs(verts[:, 1] - cy) < 1.5 * r_eq)
               & (verts[:, 2] > zf - 0.01) & (verts[:, 2] < top_z + 0.01))
        dd = np.hypot(verts[sel, 0] - cx, verts[sel, 1] - cy)
        dd = dd[(dd > 0.6 * r_eq) & (dd < 1.4 * r_eq)]
        r = float(np.median(dd)) if len(dd) >= 6 else r_eq
        out.append({"x": float(cx), "y": float(cy), "r": r - 0.005, "z": zf, "depth": top_z - zf, "k": k + 1})
    return out


def _helix_hole(b, t, hole, top_z, so):
    """One hole to full depth: helical rings from the middle out (or plunge + a circle when the hole is barely
    bigger than the bit), climb milling (counter-clockwise), then up out of it."""
    o = b.o
    rt = t["tool_d"] / 2
    R = hole["r"] - rt
    cx, cy, zf = hole["x"], hole["y"], hole["z"]
    tan = math.tan(math.radians(t.get("ramp_angle", 10.0)))
    stp = max(0.1, t.get("step", 1.0))
    if R < 0.2 * rt:
        rings = [max(R, 0.0)]
    else:
        n = max(0, int(math.ceil((R - 0.9 * rt) / max(so, 0.1))))
        rings = [R] if R <= 0.9 * rt else list(np.linspace(min(0.9 * rt, R), R, n + 1))
    z_air = top_z + 1.0
    first = True
    for ri in rings:
        x0 = cx + ri
        if first:
            b.go_down_to(x0, cy, z_air, z_air)
            first = False
        else:
            o.g0(z=z_air)
            b.move_at(x0, cy, z_air)
        if ri < 0.05:   # a plain plunge, in pecks
            z = top_z
            while z > zf + 1e-9:
                z = max(zf, z - stp)
                o.g1(cx, cy, z)
                if z > zf + 1e-9:
                    o.g0(z=z + 0.5)
            continue
        o.g1(x0, cy, top_z)
        circ = 2 * math.pi * ri
        pitch = max(0.05, min(stp, circ * tan))
        nseg = max(24, int(math.ceil(circ / 0.5)))
        z, a = top_z, 0.0
        da = 2 * math.pi / nseg
        while z > zf + 1e-9:
            a += da
            z = max(zf, z - pitch / nseg)
            o.g1(cx + ri * math.cos(a), cy + ri * math.sin(a), z)
        for k in range(1, nseg + 1):    # one full lap on the floor
            o.g1(cx + ri * math.cos(a + k * da), cy + ri * math.sin(a + k * da), zf)
    o.g0(z=z_air)
    b.lift_safe()


def _split_on(mask, g, pts, closed):
    """Pieces of a path whose points lie on mask cells."""
    u, v = g.uv(pts[:, 0], pts[:, 1])
    iu = np.clip(np.round(u).astype(int), 0, g.nx - 1)
    iv = np.clip(np.round(v).astype(int), 0, g.ny - 1)
    ok = mask[iv, iu]
    if ok.all():
        return [(pts, closed)], False
    out = []
    d = np.diff(np.concatenate([[0], ok.astype(np.int8), [0]]))
    for a, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
        if e - a >= 2:
            out.append((pts[a:e], False))
    return out, True


def _path_cut(b, pts, closed, top_z, z, t):
    """Down to z along the path (ramped, in steps of the bit's depth per pass), along it, and up."""
    tan = math.tan(math.radians(t.get("ramp_angle", 10.0)))
    stp = max(0.05, t.get("step", 1.0))
    P2 = np.asarray(pts, dtype=np.float64).copy()
    b.go_down_to(float(P2[0, 0]), float(P2[0, 1]), top_z + 1.0, top_z + 1.0)
    zc = top_z
    while zc > z + 1e-9:
        zn = max(z, zc - stp)
        P2[:, 2] = zn
        _ramp(b.o, P2, closed, b.o.p["z"], zn, tan)
        b.cut(P2, closed, start=0 if closed else 1)
        if not closed and zn > z + 1e-9:
            b.o.g0(z=top_z + 1.0)
            b.move_at(float(P2[0, 0]), float(P2[0, 1]), top_z + 1.0)
        zc = zn
    b.o.g0(z=top_z + 1.0)
    b.lift_safe()


def _choices3(k, rack, machine):
    bits = [b for b in rack if b.get("tool_type") in OP3_BITS[k]
            and b.get("shank_d", b["tool_d"]) <= machine.get("collet_max_shank", 99)]
    bits.sort(key=lambda b: (not b.get("owned", True), -b["tool_d"], b["name"]))
    return [{"name": b["name"], "label": b["name"], "owned": b.get("owned", True) is not False} for b in bits]


def _op_factor(k, a, b):
    """About how the time of op k scales when bit b does it instead of bit a."""
    if a is None or b is None or a["name"] == b["name"]:
        return 1.0
    fr = max(a["feed"], 1e-6) / max(b["feed"], 1e-6)
    if k in ("surface", "rough", "finish_flat"):
        return fr * a["tool_d"] / max(b["tool_d"], 1e-6)
    if k in ("finish_curved", "finish_steep", "rest"):
        return fr * math.sqrt(a["tool_d"] / max(b["tool_d"], 1e-6))
    return fr


def build3d(part, material, finish, rack=None, machine=None, stock=None, margin=5.0, progress=None, options=None):
    """The whole job for a placed part (see place()). Returns a gcm_engine.Job with the usual fields plus
    tools_used [(tool, reason, [op names])], ops [{name, tool, kind, key, seg_range}], op_cards (the plan JSON's
    ops), strategies, strategy, part_grid {grid, origin, cell}, stock {x0, y0, w, h, t} and part_offset (dx, dy):
    the program frame is the placed part moved by part_offset, so the stock's lower-left corner is X0 Y0.

    options (all optional): thickness (stock height, mm; the part sits on the stock bottom and the extra is
    surfaced off first; Z0 stays the stock top), part_t (finished height: less than the part's own height skims
    the top), strategy (fewest | recommended | fastest), bits {op key: rack bit name}, order [op keys] (see
    order3), addons [{kind: surface (over, margin) | chamfer (width) | text | logo}], chamfers [{width}]."""
    t_start = time.time()
    machine = dict(machine or P.MACHINE)
    rack = rack if rack is not None else P.load_rack()
    rack = P.rack_for(rack, material)   # bits that don't cut this material (a wood-only surfacing bit) stay out
    mat = P.MATERIALS[material]
    fin = P.FINISHES[finish]
    opts = options or {}

    def prog(f, text):
        if progress:
            try:
                progress(float(f), text)
            except Exception:
                pass

    if mat["kind"] == "sheet" and _is_25d(part):
        return _build_sheet(part, material, finish, rack, machine, stock, margin, progress)

    job = gcm.Job()
    prog(0.02, "Looking at the part")
    lo, hi = part.bounds
    pw, ph, Hp = float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(hi[2] - lo[2])
    probe_a = analyze(part)
    block = probe_a["is_block"]
    if stock is None:
        if block:
            stock = {"x0": float(lo[0]), "y0": float(lo[1]), "w": pw, "h": ph, "t": Hp}
        else:
            stock = {"x0": float(lo[0]) - margin, "y0": float(lo[1]) - margin, "w": pw + 2 * margin,
                     "h": ph + 2 * margin, "t": Hp + 1.0}
        if opts.get("thickness"):
            stock["t"] = float(opts["thickness"])
    stock = {k: float(stock[k]) for k in ("x0", "y0", "w", "h", "t")}
    # the part sits on the stock bottom: what is above it is surfaced off first (Z0 stays the stock top)
    off = max(0.0, stock["t"] - Hp) if opts.get("thickness") else 0.0
    leaves = Hp
    if opts.get("part_t") is not None:
        leaves = max(0.1, min(Hp, float(opts["part_t"])))
    skim = Hp - leaves
    surf_d = off + skim
    top_z = -surf_d
    dx, dy = -stock["x0"], -stock["y0"]
    tris = part.tris + np.array([dx, dy, -off])
    st = {"x0": 0.0, "y0": 0.0, "w": stock["w"], "h": stock["h"], "t": stock["t"]}
    job.stock = st
    job.part_offset = (dx, dy)
    job.part_t, job.model_t = leaves, Hp
    if stock["t"] < Hp - 0.01:
        job.warnings.append(f"The stock ({stock['t']:.1f} mm) is thinner than the part ({Hp:.1f} mm): "
                            f"only its top {stock['t']:.1f} mm are cut.")
    if skim > 0.004:
        job.warnings.append(f"Surfacing takes the part {skim:.2f} mm thinner than the model ({leaves:g} instead of "
                            f"{Hp:g} mm).")
    if block and abs(dx) < 1e-9 and abs(dy) < 1e-9 and abs(st["w"] - pw) < 1e-9 and abs(st["h"] - ph) < 1e-9:
        a = dict(probe_a)
    else:
        a = analyze(Part(part.tris + np.array([dx, dy, 0.0])), bounds=(0.0, 0.0, st["w"], st["h"]))
    if off > 0 or skim > 0:
        # the same look at the part, moved down by off and cut flat at the surfaced top
        a["grid"] = np.minimum(a["grid"] - off, top_z)
        a["grid_c"] = np.minimum(a["grid_c"] - off, top_z).astype(np.float32)
        a["floors"] = [(z - off, ar) for z, ar in a["floors"] if z - off < top_z - 0.01]
        a["top"] = top_z
    c = a["cell"]
    H = a["grid"]
    ny, nx = H.shape
    g = _Grid(0.0, 0.0, c, ny, nx)
    zbot = max(-(Hp + off), -st["t"])
    Hfill = np.where(np.isfinite(H), H, zbot)
    Hc = np.where(np.isfinite(a["grid_c"]), a["grid_c"], zbot)
    Hc = np.maximum(Hc, -st["t"]).astype(np.float32)
    Hfill = np.maximum(Hfill, -st["t"])
    probe = (st["x0"] + machine["probe_dx"], st["y0"] + machine["probe_dy"])
    pr_ = machine["probe_d"] / 2
    if not block:
        # around a part that is not the whole block only a band next to it is cleared; the rest of the
        # stock stays at full height (the surfaced top), and so does the sensor spot
        band = max([float(b["tool_d"]) for b in rack] + [6.0]) + 2.0
        far = ~(_disk(a["footprint"].astype(np.float32), band / c) > 0.5)
        yy, xx = np.ogrid[:ny, :nx]
        sens = np.hypot((xx + 0.5) * c - probe[0], (yy + 0.5) * c - probe[1]) <= pr_ + 2.0
        keep_top = (far | sens) & ~a["footprint"]
        Hfill = np.where(keep_top, top_z, Hfill)
        Hc = np.where(keep_top, np.float32(top_z), Hc).astype(np.float32)
    job.part_grid = {"grid": H, "origin": (0.0, 0.0), "cell": c}
    job.analysis = {k: v for k, v in a.items() if not isinstance(v, np.ndarray)}
    floors, _ = _floors(Hfill, c)
    floors = [(z, ar) for z, ar in floors if z < top_z - 0.01]
    if a["undercut_area"] > 1.0:
        job.warnings.append(f"{a['undercut_area']:.0f} mm² of the part is under an overhang: a 3-axis machine "
                            f"cannot reach it from the top, so it stays solid. Try another side up.")

    tools = _rack_tools(rack, material, machine)
    flats = [t for t in tools if t["tool_type"] == FLAT]
    balls = [t for t in tools if t["tool_type"] == BALL]
    vbits = [t for t in tools if t["tool_type"] == VBIT]
    byname = {t["name"]: t for t in tools}
    depth = float(top_z - Hfill.min())
    if not flats:
        job.warnings.append("The rack has no flat end mill, so nothing can be roughed.")
        return job
    strat = opts.get("strategy") if opts.get("strategy") in ("fewest", "recommended", "fastest") else "recommended"
    picks = {}
    for k, nm in (opts.get("bits") or {}).items():
        t = byname.get(nm)
        if k in OP3_BITS and t is not None and t["tool_type"] in OP3_BITS[k]:
            picks[k] = t
        elif k in OP3_BITS and nm:
            job.warnings.append(f"The {nm} cannot do the {OP3_LABELS[k].lower()}; the plan keeps its own end mill.")
    tr0, alt = _pick_by_reach(flats, depth)
    reasons = {}
    rough_reason = _reach_reason(tr0, alt, depth, "flat end mill")
    if tr0["reach"] < depth - 1e-6:
        rough_reason = f"{tr0['name']}: the longest flat end mill in the rack ({tr0['reach']:g} mm reach)."
    allow = float(mat["allowance"])
    scal = float(fin["scallop"])
    so_frac = float(mat["max_stepover"])
    # the designer's own numbers from the Settings tab (Bambu-style: the preset's value unless changed)
    prm = opts.get("params") or {}

    def _num(k, cur, lo, hi, scale=1.0):
        try:
            return cur if prm.get(k) is None else max(lo, min(hi, float(prm[k]) / scale))
        except (TypeError, ValueError):
            return cur
    allow = _num("allowance", allow, 0.0, 3.0)
    scal = _num("scallop", scal, 0.002, 0.5)
    so_frac = _num("rough_over", so_frac, 0.05, 0.95, 100.0)
    job.params_used = {"allowance": round(allow, 3), "scallop": round(scal, 4), "rough_over": round(so_frac * 100)}

    # ---------------------------------------------------------------- features: holes and regions
    prog(0.05, "Finding the features")
    need = Hfill < top_z - 0.005
    lab, stats = _components(need)
    holes_all = _find_holes(lab, stats, Hfill, top_z, c, tris.reshape(-1, 3))

    def fits_hole(t, h):
        return t["tool_d"] <= 2 * h["r"] - 0.2 and t["reach"] >= h["depth"] - 1e-6

    def hole_bit(pool, prefer):
        if not holes_all:
            return None
        if prefer is not None and all(fits_hole(prefer, h) for h in holes_all):
            return prefer
        ok = [t for t in pool if all(fits_hole(t, h) for h in holes_all)]
        if not ok:
            ok = [t for t in pool if any(fits_hole(t, h) for h in holes_all)]
        return max(ok, key=lambda t: (t["owned"], t["tool_d"], -t["reach"])) if ok else None

    def assign(strategy):
        """The bit for each op key, by strategy (before the designer's picks); None = the engine's own choice."""
        rough = tr0
        holes_t = hole_bit(flats, rough)
        if strategy == "fewest" and holes_t is not None and holes_t is not rough \
                and holes_t["reach"] >= depth - 1e-6:
            rough = holes_t       # one flat bit that fits the holes does all the flat work
        # surfacing: an owned surfacing bit, else the biggest flat end mill
        surfs = [t for t in tools if t["tool_type"] == P.SURF and t["owned"]]
        bf = max(surfs or flats, key=lambda t: (t["tool_d"], t is rough, t["owned"], -t["reach"]))
        surf_t = rough if strategy == "fewest" else bf
        if strategy == "recommended" and surf_t is not rough and surf_d > 0:
            # a bigger bit only if it saves more than the bit change costs
            area = st["w"] * st["h"]
            npass = math.ceil(surf_d / max(0.1, rough["step"]))
            t_r = area / (so_frac * rough["tool_d"]) / rough["feed"] * npass
            npass2 = math.ceil(surf_d / max(0.1, surf_t["step"]))
            t_b = area / (so_frac * surf_t["tool_d"]) / surf_t["feed"] * npass2
            if t_r - t_b < machine["tool_change_min"]:
                surf_t = rough
        if strategy == "fastest" and holes_all:
            ok = [t for t in flats if all(fits_hole(t, h) for h in holes_all)]
            if ok:
                holes_t = max(ok, key=lambda t: (t["tool_d"], t is rough, t["owned"], -t["reach"]))
        return {"surface": surf_t, "holes": holes_t, "rough": rough, "finish_flat": rough, "cutout": rough,
                "finish_curved": None, "finish_steep": None, "rest": None if strategy != "fewest" else False,
                "chamfer": None, "engrave": None}

    A = assign(strat)
    A0 = {k: v for k, v in A.items()}
    for k, t in picks.items():
        A[k] = t
    tr = A["rough"]
    t_floor = A["finish_flat"] or tr
    t_cut = A["cutout"] or tr
    t_surf = A["surface"] or tr
    th = A["holes"]
    if "rough" in picks and "finish_flat" not in picks:
        t_floor = tr
    if "rough" in picks and "cutout" not in picks:
        t_cut = tr
    if t_floor["tool_type"] == BALL:
        job.warnings.append(f"The {t_floor['name']} finishes the flat floors: a ball nose leaves ridges between "
                            f"its rows on a flat floor. A flat end mill does it better.")
    so_r = so_frac * tr["tool_d"]
    step_r = _rough_step(tr, material)
    tr["step3d"] = step_r

    holes = [h for h in holes_all if th is not None and fits_hole(th, h)]
    if holes_all and len(holes) < len(holes_all):
        job.warnings.append(f"{len(holes_all) - len(holes)} of the {len(holes_all)} round holes are too small or too "
                            f"deep for the {th['name'] if th else 'flat end mills'}: they are roughed and finished like "
                            f"the other features.")
    Hm = np.zeros_like(need)
    for h in holes:
        Hm |= lab == h["k"]
    Hm_d = _disk(Hm.astype(np.float32), 1.5) > 0.5 if holes else Hm

    # ---------------------------------------------------------------- roughing, region by region
    prog(0.08, f"Roughing with the {_label(tr)}")
    prof_r = tool_profile(tr, allow)
    CLr = _dilate(Hc, c, prof_r)
    unreached_r = CLr < top_z - tr["reach"] - 1e-6
    CLr = np.maximum(CLr, np.float32(top_z - tr["reach"]))
    CLr = np.maximum(CLr, np.float32(-st["t"]))
    if unreached_r.any():
        job.warnings.append(f"{unreached_r.sum() * c * c:.0f} mm² lies deeper than the {tr['name']} reaches "
                            f"({tr['reach']:g} mm): it is left for a longer end mill.")
    need_r = need & ~Hm
    zmin_r = float(CLr[need_r].min()) if need_r.any() else top_z
    levels = set()
    z = top_z - step_r
    while z > zmin_r + 1e-6:
        levels.add(round(z, 4))
        z -= step_r
    levels.add(round(zmin_r, 4))
    for zf, _ in floors:
        if zf + allow > zmin_r - 1e-6 and zf + allow < top_z - 1e-6:
            levels.add(round(zf + allow, 4))
    fl_lv = sorted({round(zf + allow, 4) for zf, _ in floors})
    lv = sorted(levels, reverse=True)
    lv = [z for z in lv if not any(abs(z - f) < 0.25 and z != f for f in fl_lv)]
    lv = [z for z in lv if z < top_z - 1e-6]
    m_rows = max(1, int(math.floor(so_r / c)))
    long_x = True
    bb = _bbox(need, 0, need.shape)
    if bb:
        long_x = (bb[3] - bb[2]) >= (bb[1] - bb[0])
    # the regions: the connected areas below the top that are not holes, each roughed top to bottom
    regions = []
    for k, (r0, r1, c0, c1, n) in enumerate(stats):
        if any(h["k"] == k + 1 for h in holes):
            continue
        pad = 2
        win = (max(0, r0 - pad), min(ny, r1 + pad), max(0, c0 - pad), min(nx, c1 + pad))
        y0_, y1_, x0_, x1_ = win
        Lw = lab[y0_:y1_, x0_:x1_] == k + 1
        CLw = CLr[y0_:y1_, x0_:x1_]
        lvls = []
        for z in lv:
            R = (CLw <= z + 1e-5) & Lw
            if R.any():
                lvls.append((z, R))
        if lvls:
            regions.append({"k": k + 1, "win": win, "levels": lvls,
                            "c": ((x0_ + x1_) / 2 * c, (y0_ + y1_) / 2 * c)})
    # nearest region next, from the lower-left corner
    ordered, here = [], (0.0, 0.0)
    left = list(regions)
    while left:
        nxt = min(left, key=lambda r: math.hypot(r["c"][0] - here[0], r["c"][1] - here[1]))
        left.remove(nxt)
        ordered.append(nxt)
        here = nxt["c"]
    regions = ordered
    rough_cache = {}

    def emit_rough(b):
        tan = math.tan(math.radians(b.o.t["ramp_angle"]))
        for reg in regions:
            y0_, y1_, x0_, x1_ = reg["win"]
            gw = _Grid(x0_ * c, y0_ * c, c, y1_ - y0_, x1_ - x0_)
            z_prev, R_prev = top_z, None
            for z, R in reg["levels"]:
                key = (reg["k"], z)
                if key not in rough_cache:
                    paths = _raster_paths(np.where(R, z, 1e3).astype(np.float32), R, gw, m_rows, long_x, 0.002, 1e9)
                    for p in paths:
                        p.pts[:, 2] = z
                    wy0, wy1, wx0, wx1 = _bbox(R, 2, R.shape)
                    Fm = np.where(R[wy0:wy1, wx0:wx1], 0.0, 1.0)
                    conts = _contour_paths(Fm, 0.5, gw, snap=True, sub=(wy0, wx0))
                    for p in conts:
                        p.pts[:, 2] = z
                        p.pts = _thin(p.pts, 0.01, p.closed)
                    cur = b.at()
                    start = (cur[0], cur[1]) if cur else (0.0, 0.0)
                    items = _order_nearest(paths, start, lambda p: p.pts[0], lambda p: p.pts[-1], lambda p: True)
                    items2 = list(_order_nearest(conts, items[-1].pts[-1] if items else start, lambda p: p.pts[0],
                                                 lambda p: p.pts[-1], lambda p: not p.closed))
                    rough_cache[key] = (items, items2)
                items, items2 = rough_cache[key]
                _emit_rough_level(b, gw, items, items2, z, z_prev, R_prev, tan, gw.ny, gw.nx)
                z_prev, R_prev = z, R
            b.lift_safe()

    # ---------------------------------------------------------------- holes, one at a time
    def emit_holes(b):
        t = b.o.t
        so_h = so_frac * t["tool_d"]
        left_, here_ = list(holes), (0.0, 0.0)
        while left_:
            h = min(left_, key=lambda h: math.hypot(h["x"] - here_[0], h["y"] - here_[1]))
            left_.remove(h)
            _helix_hole(b, t, h, top_z, so_h)
            here_ = (h["x"], h["y"])

    # ---------------------------------------------------------------- flat floors
    prof_f = tool_profile(t_floor, 0.0)
    CLf = np.maximum(_dilate(Hc, c, prof_f), np.float32(top_z - t_floor["reach"]))
    floor_jobs = []
    for zf, ar in floors:
        if zf < top_z - t_floor["reach"] - 1e-6:
            continue
        F = (np.abs(CLf - zf) <= 0.003) & ~Hm_d
        if F.sum() < 3:
            continue
        floor_jobs.append((zf, F))
    floor_cache = {}

    def emit_floors(b):
        tan = math.tan(math.radians(b.o.t["ramp_angle"]))
        m_rf = max(1, int(math.floor(so_frac * t_floor["tool_d"] / c)))
        for zf, F in floor_jobs:
            if zf not in floor_cache:
                rows = _raster_paths(np.where(F, zf, 1e3).astype(np.float32), F, g, m_rf, long_x, 0.002, 1e9)
                for p in rows:
                    p.pts[:, 2] = zf
                near = _disk(F.astype(np.float32), 2.0) > 0.5
                win = _bbox(near, 3, F.shape)
                conts = []
                if win:
                    y0_, y1_, x0_, x1_ = win
                    fat = _cl_window(Hc, c, _fat_profile(prof_f, 0.5 * c), win)
                    conts = _contour_paths(CLf[y0_:y1_, x0_:x1_].astype(np.float64), zf + 0.003, g,
                                           keep=near[y0_:y1_, x0_:x1_], fat=fat, sub=(y0_, x0_))
                    for p in conts:
                        p.pts[:, 2] = zf
                        p.pts = _thin(p.pts, 0.003, p.closed)
                items = _order_nearest(rows, (0, 0), lambda p: p.pts[0], lambda p: p.pts[-1], lambda p: True)
                last = items[-1].pts[-1] if items else (0, 0)
                items += _order_nearest(conts, last, lambda p: p.pts[0], lambda p: p.pts[-1], lambda p: not p.closed)
                floor_cache[zf] = items
            _emit_floor_level(b, g, floor_cache[zf], zf, CLf, allow, tan, ny, nx)
        b.lift_safe()

    # ---------------------------------------------------------------- surfacing
    surf_add = next((x for x in opts.get("addons") or [] if x.get("kind") == "surface" and not x.get("hidden")), {})
    surf_over = float(surf_add.get("over", 45.0) or 45.0)

    def emit_surface(b):
        t = b.o.t
        r = t["tool_d"] / 2
        over = surf_over if surf_add.get("over") is not None else float(t.get("surf_over") or surf_over)
        so = max(0.1, t["tool_d"] * over / 100)
        stp = max(0.1, t.get("step", 1.0))
        along_x = st["w"] >= st["h"]
        La, Lb = (st["w"], st["h"]) if along_x else (st["h"], st["w"])
        rows = np.linspace(0.0, Lb, int(math.ceil(Lb / so)) + 1)
        a0, a1 = -r - 1.0, La + r + 1.0
        xy = (lambda a_, b_: (a_, b_)) if along_x else (lambda a_, b_: (b_, a_))
        z = 0.0
        while z > -surf_d + 1e-9:
            z = max(-surf_d, z - stp)
            x0_, y0_ = xy(a0, rows[0])
            b.go_down_to(x0_, y0_, z, z)
            for i, yb in enumerate(rows):
                ea, eb = (a0, a1) if i % 2 == 0 else (a1, a0)
                b.o.g1(*xy(ea, yb), z)
                b.o.g1(*xy(eb, yb), z)
            b.lift_safe()

    # ---------------------------------------------------------------- stock after the flat tools, for the ball
    prog(0.3, "Working out what the flat tools leave")
    flat_ops = [(tr, emit_rough)] if regions else []
    if floor_jobs:
        flat_ops.append((t_floor, emit_floors))
    b = _Builder(machine)
    b.o.p.update(x=0.0, y=0.0, z=b.safe)
    b.o.k = {"x": True, "y": True, "z": True}
    SR = np.full((ny, nx), np.float32(top_z))
    for t_, fn in flat_ops:
        b.o.t = t_
        s0 = len(b.o.segs)
        fn(b)
        S = np.array([sg[:6] for sg in b.o.segs[s0:]], dtype=np.float64) if len(b.o.segs) > s0 else np.zeros((0, 6))
        if len(S):
            SR = np.minimum(SR, _cut_surface(S[np.minimum(S[:, 2], S[:, 5]) < top_z], t_, 0.0, 0.0, c, nx, ny,
                                             approx=True))
    SR = np.where(Hm, Hfill, SR).astype(np.float32)      # the holes are cut to size
    SR = np.maximum(SR, np.float32(-st["t"]))
    tol_need = max(0.01, scal)
    curved_near = _disk(a["curved"].astype(np.float32), 1.5) > 0.5
    need_b = ((SR - Hfill) > tol_need) & curved_near & ~Hm_d
    if need_b.sum() * c * c < 2.0:
        need_b[:] = False      # flat floors and walls only: the flat end mill has done them
    finish_jobs = []    # (tool, reason, [(name, kind, paths)], CL)

    def plan_finish(tool, region_need, CLfull, Hc_, prof, name_par, name_wl, rest=False, want=("par", "wl")):
        """Parallel rows on the shallow part and waterlines on the steep part of region_need, for one tool."""
        R = tool["tool_d"] / 2
        s = 2 * math.sqrt(max(2 * R * scal - scal * scal, 1e-9))
        Mt = _disk(region_need.astype(np.float32), R / c + 1.0) > 0.5
        win = _bbox(Mt, int(R / c) + 3, Mt.shape)
        if not win:
            return []
        y0_, y1_, x0_, x1_ = win
        CLw = CLfull[y0_:y1_, x0_:x1_]
        Mw = Mt[y0_:y1_, x0_:x1_]
        gy_, gx_ = np.gradient(CLw.astype(np.float64), c)
        slope = np.degrees(np.arctan(np.hypot(gx_, gy_)))
        shallow = Mw & (slope <= 50.0)
        steep = Mw & (slope >= 40.0)
        gw = _Grid(x0_ * c, y0_ * c, c, y1_ - y0_, x1_ - x0_)
        out = []
        m = max(1, int(math.floor(s / c)))
        ww, hh = x1_ - x0_, y1_ - y0_
        if "par" in want:
            par = _raster_paths(CLw, shallow, gw, m, ww >= hh, 0.005, c * math.tan(math.radians(60)))
            if par:
                out.append((name_par, "finish", par))
        if steep.any() and "wl" in want:
            dz = s * math.sin(math.radians(40.0))
            zt, zb = float(CLw[steep].max()), float(CLw[steep].min())
            keep_m = _disk(steep.astype(np.float32), 1.0) > 0.5
            wl = []
            zz = zt - dz / 2
            fat = _cl_window(Hc_, c, _fat_profile(prof, 0.5 * c), win)
            CLd = CLw.astype(np.float64)
            with np.errstate(invalid="ignore"):
                rmin = np.nanmin(np.where(keep_m, CLd, np.inf), 1)
                cmin = np.nanmin(np.where(keep_m, CLd, np.inf), 0)
            while zz > zb - 1e-6:
                rr = np.nonzero(rmin <= zz)[0]
                cc = np.nonzero(cmin <= zz)[0]
                if len(rr) == 0 or len(cc) == 0:
                    zz -= dz
                    wl.append([])
                    continue
                a0, a1 = max(0, rr[0] - 3), min(CLd.shape[0], rr[-1] + 4)
                b0, b1 = max(0, cc[0] - 3), min(CLd.shape[1], cc[-1] + 4)
                ps = _contour_paths(CLd[a0:a1, b0:b1], zz, g, keep=keep_m[a0:a1, b0:b1], fat=fat[a0:a1, b0:b1],
                                    sub=(y0_ + a0, x0_ + b0))
                for p in ps:
                    p.pts = _thin(p.pts, 0.005, p.closed)
                wl.append(ps)
                zz -= dz
            items = []
            last = (0.0, 0.0)
            for ps in wl:
                ordered_ = _order_nearest(ps, last, lambda p: p.pts[0], lambda p: p.pts[-1], lambda p: not p.closed)
                for p in ordered_:
                    p = p.rotated_to(*last)
                    items.append(p)
                    last = (p.pts[-1][0], p.pts[-1][1]) if not p.closed else (p.pts[0][0], p.pts[0][1])
            if items:
                out.append((name_wl, "finish", items))
        return out

    tb = None
    unhandled = unreached_r | a["undercut"]
    fin_pool = balls + ([t for t in flats] if any(picks.get(k) and picks[k]["tool_type"] == FLAT
                                                   for k in ("finish_curved", "finish_steep")) else [])
    if not balls and not any(k in picks for k in ("finish_curved", "finish_steep")):
        unhandled = unhandled | need_b
    if need_b.any() and fin_pool:
        prog(0.4, "Choosing the ball nose")
        depth_b = float(top_z - Hfill[need_b].min())
        rmin = a["min_inside_r"]
        if not balls:
            tb, reason_b = None, ""
        else:
            okb = [t for t in balls if t["reach"] >= depth_b - 1e-6]
            if not okb:
                tb = max(balls, key=lambda t: (t["reach"], t["tool_d"]))
                reason_b = (f"{tb['name']}: the longest ball nose in the rack ({tb['reach']:g} mm reach; the surfaces "
                            f"go {depth_b:.1f} mm deep).")
            else:
                big = max(okb, key=lambda t: (t["tool_d"], t["owned"], -t["reach"]))
                fits = [t for t in okb if rmin is None or t["tool_d"] / 2 <= rmin + 0.05]
                tb = big
                short = sorted([t for t in balls if t["reach"] < depth_b - 1e-6 and t["owned"]],
                               key=lambda t: -t["tool_d"])
                reason_b = f"{big['name']}: the biggest ball nose that reaches the {depth_b:.1f} mm deep surfaces"
                if fits and big not in fits:
                    small = max(fits, key=lambda t: (t["tool_d"], t["owned"]))
                    area_mm2 = float(need_b.sum() * c * c)
                    sb = 2 * math.sqrt(2 * big["tool_d"] / 2 * scal)
                    ss = 2 * math.sqrt(2 * small["tool_d"] / 2 * scal)
                    extra = area_mm2 / ss / small["feed"] - area_mm2 / sb / big["feed"]
                    if extra < machine["tool_change_min"]:
                        tb = small
                        reason_b = (f"{small['name']}: it fits the {rmin:.1f} mm inside radius and reaches the "
                                    f"{depth_b:.1f} mm deep surfaces")
                if short and short[0]["tool_d"] >= tb["tool_d"] - 1e-9 and short[0] is not tb:
                    reason_b += f"; the {short[0]['name']} only reaches {short[0]['reach']:g} mm"
                if not tb["owned"]:
                    reason_b += "; it is not in the rack yet"
                reason_b += "."
        A0["finish_curved"] = A0["finish_steep"] = tb
        tc_ = picks.get("finish_curved") or tb
        tw_ = picks.get("finish_steep") or tb
        if tc_ is None:
            tc_ = tw_
        if tw_ is None:
            tw_ = tc_
        for t_ in {id(tc_): tc_, id(tw_): tw_}.values():
            if t_["tool_type"] == FLAT:
                job.warnings.append(f"The {t_['name']} finishes curved surfaces: a flat end mill leaves steps on "
                                    f"them. A ball nose does it better.")
        prog(0.45, f"Finishing with the {_label(tc_)}")
        cls = {}
        for t_ in (tc_, tw_):
            if t_["name"] in cls:
                continue
            prof_b = tool_profile(t_, 0.0)
            CLb = _dilate(Hc, c, prof_b)
            unreached_b = need_b & (CLb < top_z - t_["reach"] - 1e-6)
            unhandled |= unreached_b
            CLb = np.maximum(CLb, np.float32(top_z - t_["reach"]))
            if unreached_b.any():
                job.warnings.append(f"{unreached_b.sum() * c * c:.0f} mm² of surface is deeper than the "
                                    f"{t_['name']} reaches ({t_['reach']:g} mm) and is left rough.")
            want = tuple(w for w, tt in (("par", tc_), ("wl", tw_)) if tt["name"] == t_["name"])
            fj = plan_finish(t_, need_b, CLb, Hc, prof_b, "finishing (parallel)", "finishing (waterline)", want=want)
            cls[t_["name"]] = (prof_b, CLb, unreached_b)
            if fj:
                why = reason_b if (tb is not None and t_["name"] == tb["name"]) else \
                    f"{t_['name']}: you picked it for the finishing."
                finish_jobs.append((t_, why, fj, CLb))
        # rest: where the finishing ball cannot reach (inside radii smaller than the ball), a smaller ball goes in
        prof_b, CLb, unreached_b = cls[tc_["name"]]
        prog(0.6, "Looking for corners the ball cannot reach")
        recon = _erode(CLb, c, prof_b)
        rest = need_b & ((recon - Hc) > max(0.05, 3 * scal)) & ~unreached_b
        rest &= curved_near
        if 0 < rest.sum() * c * c < 2.0:
            unhandled |= rest
        if rest.sum() * c * c >= 2.0:
            depth_rest = float(top_z - Hfill[rest].min())
            smaller = [t for t in balls if t["tool_d"] < tc_["tool_d"] - 1e-6]
            ok_s = [t for t in smaller if t["reach"] >= depth_rest - 1e-6]
            ts = picks.get("rest")
            if ts is None and ok_s:
                ts = max(ok_s, key=lambda t: (t["tool_d"], t["owned"], -t["reach"]))
            A0["rest"] = ts if ok_s else None
            if A.get("rest") is False and "rest" not in picks:
                unhandled |= rest
                job.warnings.append(f"Fewest end mill changes: {rest.sum() * c * c:.0f} mm² of tight corners the "
                                    f"{_label(tc_)} cannot reach is left (Recommended cleans them with a smaller "
                                    f"ball nose).")
            elif not fin.get("finish_pass", True) and "rest" not in picks:
                unhandled |= rest
                job.warnings.append(f"Fast finish: {rest.sum() * c * c:.0f} mm² of tight corners the "
                                    f"{_label(tc_)} cannot reach is left (Standard or Fine cleans them with a "
                                    f"smaller ball nose).")
            elif ts is not None:
                if "rest" in picks:
                    reason_s = f"{ts['name']}: you picked it for the tight corners."
                else:
                    reason_s = (f"{ts['name']}: the {_label(tc_)} cannot get into {rest.sum() * c * c:.0f} mm² of "
                                f"tight corners; this is the biggest smaller ball nose that reaches them "
                                f"({depth_rest:.1f} mm deep)" + ("" if ts["owned"] else "; it is not in the rack yet")
                                + ".")
                prof_s = tool_profile(ts, 0.0)
                win = _bbox(rest, int((prof_s[0] + ts["tool_d"]) / c) + 4, rest.shape)
                y0_, y1_, x0_, x1_ = win
                CLs = np.full(Hc.shape, np.float32(1e3))
                CLs[y0_:y1_, x0_:x1_] = _cl_window(Hc, c, prof_s, win)
                unreached_s = rest & (CLs < top_z - ts["reach"] - 1e-6)
                CLs = np.maximum(CLs, np.float32(top_z - ts["reach"]))
                rec_s = np.full(Hc.shape, np.float32(1e3))
                rec_s[y0_:y1_, x0_:x1_] = _erode(CLs[y0_:y1_, x0_:x1_], c, prof_s)
                unhandled |= unreached_s | (rest & ((rec_s - Hc) > max(0.05, 3 * scal)))
                sj = plan_finish(ts, rest, CLs, Hc, prof_s, "detail (rest, parallel)",
                                 "detail (rest, waterline)", rest=True)
                if sj:
                    finish_jobs.append((ts, reason_s, sj, CLs))
            else:
                unhandled |= rest
                job.warnings.append(f"{rest.sum() * c * c:.0f} mm² of tight corners is smaller than the "
                                    f"{_label(tc_)} can reach and the rack has no smaller ball nose for them.")
    elif need_b.any():
        job.warnings.append("The rack has no ball nose: curved surfaces are left with the roughing steps.")

    # where the finish is expected to be within the scallop (for the preview and the tests)
    job.finish_mask = (Hfill < top_z - 0.005) & a["footprint"] & ~(_disk(unhandled.astype(np.float32), 1.5) > 0.5)

    # ---------------------------------------------------------------- the top face: chamfer, text and logos
    top_m = a["footprint"] & (Hfill >= top_z - 0.01)
    widths = [float(x.get("width", 0.5) or 0) for x in opts.get("addons") or []
              if x.get("kind") == "chamfer" and not x.get("hidden")]
    widths += [float(x.get("width", 0.5) or 0) for x in opts.get("chamfers") or []]
    ch_w = max(widths) if widths else 0.0
    t_ch = picks.get("chamfer") or (max(vbits, key=lambda t: (t["owned"], t["tool_d"])) if vbits else None)
    rims = []
    if ch_w > 0:
        if t_ch is None:
            job.warnings.append("The chamfer needs a V-bit, and the rack has none.")
        else:
            A0["chamfer"] = t_ch if "chamfer" not in picks else A0["chamfer"]
            Fm = np.where(top_m, c, 0.0)
            rims = [p for p in _contour_paths(Fm, 0.5 * c, g, min_len=1.0) if p.closed or p.length > 1.0]
            for p in rims:
                p.pts = _thin(p.pts, 0.01, p.closed)
            if not rims:
                job.warnings.append("Nothing breaks the top face, so there is no edge to chamfer.")
    ch_depth = 0.0
    if rims:
        ch_depth = ch_w / max(math.tan(math.radians(t_ch.get("tool_angle", 90.0) / 2)), 0.05)

    def emit_chamfer(b):
        for p in _order_nearest(rims, (0.0, 0.0), lambda p: p.pts[0], lambda p: p.pts[-1], lambda p: not p.closed):
            _path_cut(b, p.pts, p.closed, top_z, top_z - ch_depth, b.o.t)

    marks_in = [x for x in opts.get("addons") or [] if x.get("kind") in ("text", "logo") and not x.get("hidden")]
    eng_paths = []
    need_v = False
    if marks_in:
        top_in = _disk(top_m.astype(np.float32), 1.0, np.minimum, 0.0) > 0.5
        for mi, x in enumerate(marks_in):
            m = gcm.new_mark(x["kind"])
            for key in ("text", "font", "bold", "size", "x", "y", "angle", "align", "cut", "depth", "logo",
                        "registered"):
                if key in x and x[key] is not None:
                    m[key] = x[key]
            shapes, w, h, problems = gcm.mark_shapes(m)
            label = (" ".join(str(m.get("text", "")).replace("|", " ").split())[:24] if m.get("type") != "logo"
                     else m.get("logo", "logo"))
            job.warnings += [f"Mark \"{label}\": {p_}" for p_ in problems]
            if not shapes:
                continue
            placed = gcm.place_mark(shapes, m)
            job.mark_paths += placed
            job.marks.append({"i": mi, "x": gcm.mark_num(m, "x"), "y": gcm.mark_num(m, "y"), "w": w, "h": h,
                              "angle": gcm.mark_num(m, "angle"), "label": label})
            cut = m.get("cut", "On the line")
            if cut == "On the line" or any(not cl for _, cl in placed):
                need_v = True
            elif cut == "Filled":
                job.warnings.append(f"Mark \"{label}\": filled marks are cut as outlines on a block.")
            dg = gcm.mark_num(m, "depth", 0.2)
            off_top = False
            for pts, cl in placed:
                P3 = np.array([[px, py, 0.0] for px, py in pts], dtype=np.float64)
                if len(P3) < 2:
                    continue
                pieces, cut_off = _split_on(top_in, g, P3, bool(cl))
                off_top |= cut_off
                eng_paths += [(pp, cc, dg) for pp, cc in pieces]
            if off_top:
                job.warnings.append(f"Mark \"{label}\" runs off the flat top face: only the part on the top is "
                                    f"engraved.")
    t_eng = None
    if eng_paths:
        if need_v and vbits:
            t_eng = max(vbits, key=lambda t: (t["owned"], t["tool_d"]))
        else:
            t_eng = min(flats, key=lambda t: (not t["owned"], t["tool_d"]))
        if strat == "fewest" and t_eng["tool_type"] == FLAT:
            t_eng = tr
        A0["engrave"] = t_eng
        t_eng = picks.get("engrave") or t_eng

    def emit_engrave(b):
        for pp, cl, dg in eng_paths:
            _path_cut(b, pp, cl, top_z, top_z - dg, b.o.t)

    # ---------------------------------------------------------------- cut-out
    cut_loops = []
    z_part_bot = -(Hp + off)
    if not block and st["t"] > Hp - 0.01 - 1e-9:
        fp = a["footprint"].astype(np.float32)
        D = _disk(fp, t_cut["tool_d"] / 2 / c + 0.75) > 0.5
        if st["t"] > Hp + off + 0.01:
            nearD = _disk(D.astype(np.float32), 2.0) > 0.5     # (not the stock's own edge)
            cut_loops = _contour_paths(np.where(D, 1.0, 0.0), 0.5, g, snap=True, keep=nearD)
            for p in cut_loops:
                p.pts = _thin(p.pts, 0.01, p.closed)

    def emit_cutout(b):
        t = b.o.t
        tan = math.tan(math.radians(t["ramp_angle"]))
        stp = max(0.1, t["step"])
        z_from = max(z_part_bot, -st["t"])
        for p in cut_loops:
            if len(p.pts) < 2:
                continue
            P2 = p.pts.copy()
            b.go_down_to(float(P2[0, 0]), float(P2[0, 1]), z_from + 1.0, z_from + 1.0)
            z = z_from
            while z > -st["t"] + 1e-6:
                zn = max(-st["t"], z - stp)
                _ramp(b.o, P2, p.closed, b.o.p["z"], zn, tan)
                P2[:, 2] = zn
                b.cut(P2, p.closed, start=0)
                z = zn
            b.lift_safe()

    # ---------------------------------------------------------------- the operations, in run order
    A0["finish_flat"] = A0["finish_flat"] or tr
    ops = {}   # key -> [(tool, name, kind, emit)]
    if surf_d > 1e-6:
        ops["surface"] = [(t_surf, "surfacing", "surface", emit_surface)]
    if holes:
        ops["holes"] = [(th, "holes (helical)", "holes", emit_holes)]
    if regions:
        ops["rough"] = [(tr, "roughing", "rough", emit_rough)]
    if floor_jobs:
        ops["finish_flat"] = [(t_floor, "flat floors and walls (pocket)", "floor", emit_floors)]
    for tool, why, fj, CLt in finish_jobs:
        for n, k, paths in fj:
            key = OP3_NAME[n]
            ops.setdefault(key, []).append((tool, n, k, _fin_emitter(paths, CLt, SR, g, tool,
                                                                      b_safe=machine["safe_z"])))
    if rims:
        ops["chamfer"] = [(t_ch, "chamfer", "chamfer", emit_chamfer)]
    if eng_paths and t_eng is not None:
        ops["engrave"] = [(t_eng, "engraving", "engrave", emit_engrave)]
    if cut_loops:
        ops["cutout"] = [(t_cut, "outline cut-out", "cutout", emit_cutout)]
    seq = order3(opts.get("order"))
    flat_seq = [(key, *x) for key in seq for x in ops.get(key, [])]
    groups = []   # (tool, [(name, kind, emit, key)])
    for key, t_, n, k, f in flat_seq:
        if groups and groups[-1][0]["name"] == t_["name"]:
            groups[-1][1].append((n, k, f, key))
        else:
            groups.append((t_, [(n, k, f, key)]))
    # why each bit
    for k_, t_ in picks.items():
        reasons.setdefault(t_["name"], f"{t_['name']}: you picked it for the {OP3_LABELS[k_].lower()}.")
    reasons.setdefault(tr["name"], rough_reason if tr is tr0 else
                       f"{tr['name']}: one flat end mill for the roughing and the holes (fewest end mill changes).")
    for tool, why, fj, CLt in finish_jobs:
        reasons.setdefault(tool["name"], why)
    if th is not None and holes:
        reasons.setdefault(th["name"], f"{th['name']}: the biggest flat end mill that fits the "
                                       f"{min(2 * h['r'] for h in holes):.1f} mm holes.")
    reasons.setdefault(t_surf["name"], f"{t_surf['name']}: the biggest flat end mill faces the top quickly.")
    if t_ch is not None:
        reasons.setdefault(t_ch["name"], f"{t_ch['name']}: a V-bit cuts the chamfer along the top edges.")
    if t_eng is not None:
        reasons.setdefault(t_eng["name"], f"{t_eng['name']}: engraves the text and logos on the top face.")
    reasons.setdefault(t_cut["name"], f"{t_cut['name']}: cuts the part free round its outline.")
    used, seen_u = [], {}
    for t_, lst in groups:
        if t_["name"] not in seen_u:
            seen_u[t_["name"]] = [t_, reasons.get(t_["name"]) or f"{t_['name']}.", []]
            used.append(seen_u[t_["name"]])
        for n, k, f, key in lst:
            if n not in seen_u[t_["name"]][2]:
                seen_u[t_["name"]][2].append(n)

    # ---------------------------------------------------------------- write the program
    prog(0.75, "Writing the G-code")
    s = dict(gcm.DEFAULTS)
    s.update({k: machine[k] for k in ("safe_z", "end_z_mode", "end_z", "end_xy", "tc_code", "probe_dx", "probe_dy",
                                      "probe_d", "bed_macro") if k in machine})
    s["bed_sensor"] = P.bed_sensor(machine)
    s["user_zero"] = machine.get("sensor_mode") == "bed"
    s.setdefault("end_x", 0.0)
    s.setdefault("end_y", 0.0)
    job.probe = (probe[0], probe[1], machine["probe_d"] / 2)
    if not groups:
        groups = [(tr, [])]
    b = _Builder(machine)
    o = b.o
    name = part.name or "part"
    tno = {}
    for t_, _ in groups:
        tno.setdefault(t_["name"], len(tno) + 1)
    o.raw(f"(Agilite CNC - {gcm._cmt(name)})")
    if part.path:
        o.raw(f"(Source: {gcm._cmt(part.path)})")
    listed = set()
    for t_, lst in groups:
        if t_["name"] in listed:
            continue
        listed.add(t_["name"])
        names = [n for t2, l2 in groups if t2["name"] == t_["name"] for n, k, f, key in l2]
        o.raw(f"(T{tno[t_['name']]} {gcm._cmt(t_['name'])} = {gcm._cmt(_label(t_))}: "
              f"{', '.join(dict.fromkeys(names))})")
        o.raw(f"(   {int(t_['rpm'])} rpm, feed {t_['feed']:g}, ramp {t_['ramp_feed']:g}, plunge {t_['plunge_feed']:g}"
              f" mm/min)")
    o.raw(f"(Material: {gcm._cmt(material)}; stock {st['w']:.1f} x {st['h']:.1f} x {st['t']:.1f} mm; "
          f"finish {finish}, scallop {scal:.3f} mm)")
    if surf_d > 1e-6:
        o.raw(f"(The top {surf_d:.2f} mm of the stock is surfaced off first; the part is {leaves:.2f} mm high)")
    o.raw("(Origin: the lower-left corner of the stock; Z0 = top of the stock)")
    o.raw("(Before starting: home the machine, put T1 in, zero X and Y on the stock, "
          + ("zero Z on the stock top.)" if s["user_zero"] else "Z is measured by the program.)"))
    o.raw("TIME_PLACEHOLDER")
    o.raw("G21 G90 G17 G94")
    measure = _measure_fn(b, s, probe)
    run_groups = []
    job.ops = []
    for gi, (t_, lst) in enumerate(groups):
        n_ = tno[t_["name"]]
        t_before, p_before = o.time, len(o.pauses)
        measure(t_, n_, first=(gi == 0))
        o.t = t_
        o.raw(f"S{int(t_['rpm'])}")   # on its own line: UCCNC ignores the S on an "M3 S..." line (gcm_engine)
        o.raw("M3")
        b.stock_after = SR
        for nm, kind, fn, key in lst:
            seg0 = len(o.segs)
            o.stages.append((seg0, f"T{n_} {t_['name']}: {nm}"))
            o.raw(f"(T{n_}: {gcm._cmt(nm)})")
            fn(b)
            job.ops.append({"name": nm, "tool": t_, "kind": kind, "key": key, "seg_range": (seg0, len(o.segs)),
                            "t": n_})
        run_groups.append({"n": n_, "tool": t_, "ops": [n for n, k, f, key in lst],
                           "pauses": [m_ for _, m_ in o.pauses[p_before:]], "time": o.time - t_before})
        prog(0.75 + 0.2 * (gi + 1) / len(groups), "Writing the G-code")
    gcm._park(o, s, final=True)
    o.lines[o.lines.index("TIME_PLACEHOLDER")] = f"(Estimated time {gcm._fmt_time(o.time)})"
    job.files = [("", o.lines)]
    job.lines = o.lines
    job.segs = o.segs
    job.seg_info = [(dt, f_, 0, li) for dt, f_, li in o.info]
    job.pauses = [(i_, msg, 0) for i_, msg in o.pauses]
    job.stages = list(o.stages)
    job.time_min = o.time
    job.tools_used = [(t_, r_, ops_) for t_, r_, ops_ in used]
    if not any(min(sg[2], sg[5]) < -1e-6 for sg in job.segs):
        job.warnings.append("Nothing to cut: the part already fills the stock from the top.")
    for t_, r_, ops_ in job.tools_used:
        if not t_.get("owned", True):
            job.warnings.append(f"Buy: {t_['name']} (needed for {', '.join(ops_)}).")
    bad = [sg for sg in job.segs if sg[6] and min(sg[2], sg[5]) < -1e-6 and math.hypot(sg[3] - sg[0], sg[4] - sg[1]) > 1e-3]
    if bad:
        job.warnings.append(f"{len(bad)} rapid move(s) go sideways below the top of the stock, e.g. at "
                            f"X{bad[0][3]:.2f} Y{bad[0][4]:.2f} Z{bad[0][5]:.2f}. Please report this; do not run it.")
    # the sensor must sit on stock the job never cuts (Z0 is the top of the stock)
    hit = None
    puck_changes = not s["bed_sensor"] and len(groups) > 1
    puck = puck_changes or not s["user_zero"]
    if not puck:
        job.probe = None
    # the puck spot is only used when no bed sensor measures the changes; in bed mode (user_zero) there is no puck
    # spot at all, the job just waits for the bed sensor's position, so its spot is never checked
    for sg in (job.segs if puck and not s["user_zero"] else []):
        if sg[6] or min(sg[2], sg[5]) > -1e-3:
            continue
        dd = gcm._seg_dist(probe, (sg[0], sg[1]), (sg[3], sg[4])) - sg[7]["tool_d"] / 2
        if dd < pr_:
            hit = sg
            break
    if hit is not None:
        job.warnings.insert(0, f"The sensor spot (the {machine['probe_d']:g} mm circle) lies on stock this job cuts, "
                               f"so the tools would be measured on a lower surface. Move the sensor spot onto "
                               f"untouched stock" + (", or measure the end mill changes on the bed sensor."
                                                     if surf_d > 0 else "."))
    xs = [p for sg in job.segs for p in (sg[0], sg[3])]
    ys = [p for sg in job.segs for p in (sg[1], sg[4])]
    if xs and (max(xs) - min(xs) > machine["x_max"] or max(ys) - min(ys) > machine["y_max"]):
        job.warnings.append(f"The job spans {max(xs) - min(xs):.0f} x {max(ys) - min(ys):.0f} mm, more than the "
                            f"machine's {machine['x_max']:g} x {machine['y_max']:g} mm.")
    # run steps
    steps = []
    held = "screwed down at the corners" if mat["hold"] == "screws" else "held down"
    steps.append(f"Fix the {st['w']:.0f} x {st['h']:.0f} x {st['t']:.1f} mm {material.lower()} stock to the table, "
                 f"{held}, clamps and screws outside the cut area.")
    bed_ = s["bed_sensor"] and len(groups) > 1
    if puck:
        steps.append(f"Keep the sensor spot free: a {machine['probe_d']:g} mm circle centred "
                     f"{abs(machine['probe_dx']):g} mm {'left of' if machine['probe_dx'] < 0 else 'right of'} and "
                     f"{abs(machine['probe_dy']):g} mm {'below' if machine['probe_dy'] < 0 else 'above'} the stock's "
                     f"lower-left corner.")
    steps.append("Home the machine" + (" (end mill changes go to the bed sensor in machine coordinates)." if bed_ else
                                        " (the program lifts to the machine top for every tool change)."))
    steps.append(f"Put T1 ({groups[0][0]['name']}) in the spindle.")
    steps.append("Jog to the lower-left corner of the stock and zero X and Y. Do not zero X or Y again until the "
                 "job is done.")
    if s["user_zero"]:
        steps.append("Zero Z: set the tool sensor on the stock over the area that gets cut, measure with M31, then put the sensor back in its holder.")
    steps.append("Load the G-code file in UCCNC and press Cycle Start.")
    for gi_, rg in enumerate(run_groups):
        if rg["pauses"]:
            steps.append("The machine lifts, stops the spindle and goes to the bed sensor." if bed_ and gi_
                         else "The machine lifts, stops the spindle and goes to the sensor spot.")
        for m_ in rg["pauses"]:
            steps.append(f"Pause: {m_.rstrip('.')}.")
        steps.append(f"T{rg['n']} cuts: {', '.join(rg['ops'])} (about {max(1, round(rg['time']))} min).")
    steps.append("Done: the spindle stops and the machine parks at X0 Y0" +
                 (". The part is cut free at the end: hold it down so it cannot move." if cut_loops else "."))
    job.run_steps = steps
    job.summary = [
        f"Part: {pw:.1f} x {ph:.1f} x {Hp:.1f} mm, {a['depth']:.1f} mm deep from the top",
        f"Stock: {st['w']:.1f} x {st['h']:.1f} x {st['t']:.1f} mm {material.lower()}"
        + (" (the part is the whole block)" if block else ""),
        f"Finish: {finish} (scallop {scal:.3f} mm); roughing leaves {allow:.2f} mm",
    ]
    if surf_d > 1e-6:
        job.summary.insert(2, f"Surfacing: {surf_d:.2f} mm off the top first")
    if holes:
        job.summary.append(f"Holes: {len(holes)} round hole{'s' if len(holes) > 1 else ''}, one at a time to full "
                           f"depth")
    for t_, r_, ops_ in job.tools_used:
        job.summary.append(f"T{tno[t_['name']]} {t_['name']}: {', '.join(ops_)}  ({int(t_['rpm'])} rpm, feed "
                           f"{t_['feed']:g})")
        job.summary.append(f"   {r_}")
    if len(groups) > 1:
        job.summary.append(f"One file, {len(groups) - 1} tool change{'s' if len(groups) > 2 else ''}: the machine "
                           f"stops at the sensor spot and measures each tool")
    job.summary.append(f"Estimated time: {gcm._fmt_time(job.time_min)}   ({len(job.lines)} lines)")
    # ---------------------------------------------------------------- the ops and strategies for the page
    info = job.seg_info
    cum = np.cumsum([x[0] for x in info]) if info else np.zeros(0)
    cards, seen = [], {}
    for op in job.ops:
        a_, b_ = op["seg_range"]
        t0 = float(cum[a_ - 1]) if a_ > 0 and len(cum) else 0.0
        t1 = float(cum[b_ - 1]) if b_ > 0 and len(cum) else 0.0
        k = op["key"]
        if k in seen:
            seen[k]["minutes"] += t1 - t0
            continue
        t_ = op["tool"]
        cd = {"key": k, "label": OP3_LABELS[k], "bit": t_["name"], "bit_label": _label(t_), "t": op["t"],
              "owned": t_.get("owned", True) is not False, "picked": k in picks,
              "choices": _choices3(k, rack, machine), "minutes": t1 - t0,
              "movable": k in ("holes", "chamfer", "engrave")}
        seen[k] = cd
        cards.append(cd)
    for cd in cards:
        cd["minutes"] = round(cd["minutes"], 3)
    job.op_cards = cards
    job.op_order = [cd["key"] for cd in cards]
    job.strategy = strat
    tc = machine["tool_change_min"]
    mins = {cd["key"]: cd["minutes"] for cd in cards}
    cur = {cd["key"]: byname.get(cd["bit"]) for cd in cards}
    out = []
    for key, label in STRATEGIES3:
        if key == strat:
            out.append({"key": key, "label": label, "time_min": round(job.time_min, 3),
                        "changes": max(0, len(groups) - 1), "exact": True})
            continue
        alt_a = assign(key)
        t_est = job.time_min
        names = []
        for k, m_ in mins.items():
            nb = alt_a.get(k)
            if nb is False:          # fewest: no rest pass
                t_est -= m_
                continue
            if nb is None:
                nb = A0.get(k) or cur[k]
            t_est += m_ * (_op_factor(k, cur[k], nb) - 1.0)
            names.append(nb["name"])
        nchg = sum(1 for i in range(1, len(names)) if names[i] != names[i - 1])
        out.append({"key": key, "label": label, "time_min": round(max(0.0, t_est), 3), "changes": nchg,
                    "exact": False})
    for x in out:
        x["total_min"] = round(x["time_min"] + x["changes"] * tc, 3)
    job.strategies = out
    job.build_seconds = time.time() - t_start
    prog(1.0, "Done")
    return job


def _fin_emitter(paths, CLt, SR, g, tool, b_safe):
    R = tool["tool_d"] / 2
    SD = _disk(SR, R / g.c + 1.0)

    def emit(b):
        o = b.o
        s_ = 2 * R
        for p in paths:
            nxt = p.pts[0]
            _link_fin(b, nxt, CLt, SD, g, max(3 * s_, 3.0), b_safe)
            b.cut(p.pts, p.closed, start=1)
        b.lift_safe()
    return emit


# ============================================================================ 2.5D sheet parts (through G-code Maker)

def _normals_z(tris):
    cr = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(cr, axis=1)
    return cr[:, 2] / np.maximum(ln, 1e-30), ln / 2


WALL_LEAN = 0.05   # mm: a wall leaning less than this over its height is vertical


def _odd_faces(tris, nz):
    """Faces that are neither flat-horizontal nor vertical. A wall counts as vertical when it leans less than
    WALL_LEAN mm over its height: an STL meshes a hole's top and bottom circles apart, so its walls lean a little
    (Lev 2026-11-16: the QD ladderlock STL's walls lean 0.5 deg, 0.01 mm, and it was read as a 3D part)."""
    a = np.abs(nz)
    h = tris[:, :, 2].max(1) - tris[:, :, 2].min(1)
    lean = h * a / np.sqrt(np.maximum(1.0 - a * a, 1e-12))
    vertical = (a <= 0.002) | ((a <= 0.05) & (lean <= WALL_LEAN))
    return (a < 0.999) & ~vertical


def _is_25d(part):
    """Every face is flat-horizontal or vertical (a plate with holes, slots and stepped pockets)."""
    nz, ar = _normals_z(part.tris)
    odd = _odd_faces(part.tris, nz)
    return float(ar[odd].sum()) <= 1e-3 * float(ar.sum())


def _chain_segments(segs, tol=1e-4):
    """Joins 2D segments [(a, b)] into closed loops (lists of points)."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))
    adj = {}
    for i, (a, b) in enumerate(segs):
        adj.setdefault(key(a), []).append((i, 0))
        adj.setdefault(key(b), []).append((i, 1))
    used = [False] * len(segs)
    loops = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        a, b = segs[i]
        loop = [a, b]
        start = key(a)
        cur = key(b)
        while cur != start:
            nxt = None
            for j, end in adj.get(cur, []):
                if not used[j]:
                    nxt = (j, end)
                    break
            if nxt is None:
                break
            j, end = nxt
            used[j] = True
            p = segs[j][1 - end]
            loop.append(p)
            cur = key(p)
        if cur == start and len(loop) > 3:
            loops.append([(float(x), float(y)) for x, y in loop[:-1]])
    return loops


def _slice(tris, z0):
    """Closed loops where the plane Z = z0 cuts the mesh."""
    zs = tris[:, :, 2]
    m = (zs.min(1) < z0) & (zs.max(1) > z0)
    segs = []
    for t in tris[m]:
        pts = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            a, b = t[i], t[j]
            if (a[2] - z0) * (b[2] - z0) < 0:
                f = (z0 - a[2]) / (b[2] - a[2])
                pts.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
        if len(pts) == 2:
            segs.append((pts[0], pts[1]))
    return [gcm._dedupe(l, 1e-6) for l in _chain_segments(segs)]


def _boundary_loops(tris):
    """Outline loops of a set of (horizontal) triangles: the edges used by one triangle only."""
    cnt = {}
    for t in tris:
        for i, j in ((0, 1), (1, 2), (2, 0)):
            a = (round(t[i][0], 5), round(t[i][1], 5))
            b = (round(t[j][0], 5), round(t[j][1], 5))
            k = (a, b) if a <= b else (b, a)
            cnt[k] = cnt.get(k, 0) + 1
    segs = [k for k, v in cnt.items() if v == 1]
    return [gcm._dedupe(l, 1e-6) for l in _chain_segments(segs)]


def _lobes_into_holes(loops, holes, zf, top_at):
    """Floor pieces that open into a through hole, joined with that hole into one pocket shape.
    Lev 2026-11-16: the QD ladderlock's recess is eight 3.6 mm lobes round a 9.6 mm through hole; each lobe on
    its own is too short for any end mill, and stretching it into the hole pinched it to a spike, so not even a
    2 mm end mill cut them. The true pocket is the flower: the hole's outline with each lobe's wall in place of
    the stretch of hole it opens onto. Returns (flowers, the loops left as they were)."""
    if not holes:
        return [], loops
    hs = [h if gcm.area(h) > 0 else h[::-1] for h in holes]
    lobes = {}
    rest = []
    for c in loops:
        poly = c if gcm.area(c) > 0 else c[::-1]
        n = len(poly)
        kind = []
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1.0
            pr = ((a[0] + b[0]) / 2 + dy / L * 0.12, (a[1] + b[1]) / 2 - dx / L * 0.12)   # just outside the floor
            if top_at(pr) > zf + 0.01:
                kind.append(-1)          # a wall
            else:
                kind.append(next((k for k, h in enumerate(hs) if gcm.inside(pr, h)), -2))
        opens = {k for k in kind if k >= 0}
        runs = sum(1 for i in range(n) if (kind[i] == -1) != (kind[i - 1] == -1))
        if len(opens) != 1 or -2 in kind or -1 not in kind or runs != 2:
            rest.append(c)
            continue
        # the wall run, start to end, in the floor's counter-clockwise direction (the flower's direction too)
        i0 = next(i for i in range(n) if kind[i] == -1 and kind[i - 1] != -1)
        chain = [poly[i0]]
        i = i0
        while kind[i % n] == -1:
            chain.append(poly[(i + 1) % n])
            i += 1
        lobes.setdefault(opens.pop(), []).append(chain)
    flowers = []
    for k, chains in lobes.items():
        h = hs[k]
        m = len(h)
        cum = [0.0]
        for i in range(m):
            cum.append(cum[-1] + math.dist(h[i], h[(i + 1) % m]))
        per = cum[-1]

        def at(p):   # where p lies along the hole's outline (arc length)
            best = None
            for i in range(m):
                a, b = h[i], h[(i + 1) % m]
                dx, dy = b[0] - a[0], b[1] - a[1]
                L2 = dx * dx + dy * dy or 1e-12
                t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
                d = math.dist(p, (a[0] + t * dx, a[1] + t * dy))
                if best is None or d < best[0]:
                    best = (d, cum[i] + t * math.sqrt(L2))
            return best[1]
        spans = sorted(((at(ch[0]), at(ch[-1]), ch) for ch in chains), key=lambda x: x[0])
        out = []
        ok = True
        for j, (s0, s1, ch) in enumerate(spans):
            out += ch
            nxt = spans[(j + 1) % len(spans)][0]
            if s1 < s0:
                s1 += per                    # the lobe straddles where the outline starts
            nxt = nxt + per if j + 1 == len(spans) or nxt < s1 - per * 0.5 else nxt
            if nxt < s1 - 1e-6:
                ok = False                   # lobes overlapping along the hole: leave it all as it was
                break
            # the hole's own corners from this lobe's end to the next lobe's start
            out += [h[i % m] for i in range(m * 3) if s1 + 1e-6 < cum[i % m] + per * (i // m) < nxt - 1e-6]
        if ok and len(out) >= 3:
            flowers.append(gcm._dedupe(out, 1e-6))
        else:
            for _, _, ch in spans:
                pass
            rest += [c for c in loops if c not in rest and c not in flowers]
    return flowers, rest


def _line_hit(p1, n1, p2, n2):
    """Intersection of the lines p1 + t*n1 and p2 + s*n2, or None when parallel."""
    d = n1[0] * n2[1] - n1[1] * n2[0]
    if abs(d) < 1e-9:
        return None
    t = ((p2[0] - p1[0]) * n2[1] - (p2[1] - p1[1]) * n2[0]) / d
    return (p1[0] + t * n1[0], p1[1] + t * n1[1])


def _pocket_loops(loops, zf, top_at, ext):
    """The floor at zf as pocket shapes for G-code Maker. Edges where the floor ends at a wall stay; edges where
    it runs off the part (an open step) are pushed out by `ext`, so the bit clears right to the edge.
    Holes in the floor that drop to a lower level are left out (the pocket simply covers them)."""
    out = []
    lvl = [sum(1 for k, o in enumerate(loops) if k != i and gcm.inside(c[0], o)) for i, c in enumerate(loops)]
    for li, c in enumerate(loops):
        poly = c if gcm.area(c) > 0 else c[::-1]
        hole = lvl[li] % 2 == 1
        n = len(poly)
        normals, wall = [], []
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1.0
            nv = (dy / L, -dx / L)       # right of a CCW edge = outside the loop
            if hole:
                nv = (-nv[0], -nv[1])    # a hole: the floor is outside it, so "away from the floor" is inward
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            probe = (mid[0] + nv[0] * 0.12, mid[1] + nv[1] * 0.12)
            normals.append(nv)
            wall.append(top_at(probe) > zf + 0.01)
        if hole and not any(wall):
            continue
        if all(wall):
            out.append(poly)
            continue
        new = []
        for i in range(n):
            ia = (i - 1) % n     # edge ending at vertex i
            ib = i               # edge starting at vertex i
            pa, pb = poly[ia], poly[i]
            va = (pb[0] - pa[0], pb[1] - pa[1])
            vb = (poly[(i + 1) % n][0] - pb[0], poly[(i + 1) % n][1] - pb[1])
            oa = 0.0 if wall[ia] else ext
            ob = 0.0 if wall[ib] else ext
            if oa == 0 and ob == 0:
                new.append(pb)
                continue
            p1 = (pb[0] + normals[ia][0] * oa, pb[1] + normals[ia][1] * oa)
            p2 = (pb[0] + normals[ib][0] * ob, pb[1] + normals[ib][1] * ob)
            hit = _line_hit(p1, va, p2, vb)
            if hit is None or math.hypot(hit[0] - pb[0], hit[1] - pb[1]) > 4 * ext:
                new.append(p1)
                new.append(p2)
            else:
                new.append(hit)
        out.append(new)
    return out


def _write_dxf(path, layers):
    """layers: {name: [closed loops]} -> a minimal R12-style DXF of closed LWPOLYLINEs (mm)."""
    L = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "4", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    for name, loops in layers.items():
        for lp in loops:
            L += ["0", "LWPOLYLINE", "8", name, "90", str(len(lp)), "70", "1"]
            for x, y in lp:
                L += ["10", f"{x:.5f}", "20", f"{y:.5f}"]
    L += ["0", "ENDSEC", "0", "EOF"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def is_flat(part):
    """A plate: every face flat-horizontal or vertical, so the 2D pipeline (tabs, chamfers, text...) can cut it;
    top edges broken by a small fillet or chamfer still count (the 2D pipeline cuts that break as a chamfer)."""
    return _is_25d(part) or edge_break(part) > 0


def edge_break(part, bmax=2.0):
    """The width (mm) of the fillets or chamfers that break a plate's top edges, 0 when there are none or the part
    is not a plate with only those. Every sloped face must hang off a horizontal level (the top or a pocket floor's
    rim) and reach at most bmax below it, facing up (a convex break of a top edge, never a floor fillet). The width
    is the average step-in of the outlines just under the level against just below the break."""
    tris = part.tris
    nz, ar = _normals_z(tris)
    odd = _odd_faces(tris, nz)
    total = float(ar.sum()) or 1.0
    if float(ar[odd].sum()) <= 1e-3 * total:
        return 0.0
    lo, hi = part.bounds
    T = float(hi[2] - lo[2])
    zc_up = tris[nz > 0.999][:, :, 2].mean(1) if (nz > 0.999).any() else np.zeros(0)
    levels = np.unique(np.round(np.concatenate([zc_up, [float(hi[2])]]), 3))
    zmax, zmin = tris[:, :, 2].max(1), tris[:, :, 2].min(1)
    bad, deepest = 0.0, 0.0
    for i in np.nonzero(odd)[0]:
        # the level this face breaks: the nearest one at or above it (a model may split one bevel into strips)
        above = levels[levels >= zmax[i] - 0.02]
        L = float(above.min()) if len(above) else float(hi[2])
        h = float(L - zmin[i])
        # a face that ends on a lower level is a floor fillet (needs a ball nose), not a broken top edge
        on_floor = bool(np.any((np.abs(levels - zmin[i]) < 0.02) & (levels < L - 0.02)))
        if nz[i] <= 0 or on_floor or h > min(bmax, 0.6 * T):
            bad += float(ar[i])
        else:
            deepest = max(deepest, h)
    if bad > 1e-3 * total or deepest <= 0:
        return 0.0
    top = float(hi[2])
    below = _slice(tris, top - deepest - 0.02)
    under = _slice(tris, top - 0.01)
    if below and under and len(below) == len(under):
        # outlines shrink and holes grow at the top: the solid area drops by about width x the edges' length
        per = sum(sum(math.dist(c[k], c[(k + 1) % len(c)]) for k in range(len(c))) for c in below)
        w = abs(_net_area(below) - _net_area(under)) / per if per > 0 else deepest
    else:
        w = deepest
    return round(max(0.05, min(w, bmax)), 2)


def _net_area(loops):
    """Solid area of nested loops: outlines count, holes take away (by nesting depth, whatever their winding)."""
    tot = 0.0
    for i, c in enumerate(loops):
        a = abs(gcm.area(c))
        depth = sum(1 for k, o in enumerate(loops) if k != i and abs(gcm.area(o)) > a and gcm.inside(c[0], o))
        tot += a if depth % 2 == 0 else -a
    return tot


def sheet_dxf(part, path, ext=7.0):
    """Writes a flat part's outlines as a DXF at path: layer CUT = through cuts, POCKET n = stepped floors
    (open sides extended by ext mm so any bit up to ext - 1 mm clears them). Returns (layer_ops, thickness)."""
    tris = part.tris
    lo, hi = part.bounds
    T = float(hi[2] - lo[2])
    through = _slice(tris, float(lo[2]) + min(0.05, T / 10))
    nz, ar = _normals_z(tris)
    up = (nz > 0.999)
    zc = tris[:, :, 2].mean(1)
    levels = {}
    for i in np.nonzero(up & (zc < float(hi[2]) - 0.01) & (zc > float(lo[2]) + 0.01))[0]:
        levels.setdefault(round(float(zc[i]), 3), []).append(tris[i])
    a = analyze(part, cell=None)
    Hg, cg, og = a["grid"], a["cell"], a["origin"]

    def top_at(p):
        i = int(math.floor((p[0] - og[0]) / cg))
        j = int(math.floor((p[1] - og[1]) / cg))
        if 0 <= j < Hg.shape[0] and 0 <= i < Hg.shape[1] and np.isfinite(Hg[j, i]):
            return float(Hg[j, i])
        return -1e9
    layers = {"CUT": through}
    lops = {"CUT": {"op": "Cut through", "depth": ""}}
    through_holes = [c for i, c in enumerate(through)
                     if sum(1 for j, o in enumerate(through) if j != i and gcm.inside(c[0], o)) % 2]
    for k, (zf, ts) in enumerate(sorted(levels.items(), reverse=True)):
        nm = f"POCKET {k + 1}"
        flowers, rest = _lobes_into_holes(_boundary_loops(np.array(ts)), through_holes, zf, top_at)
        layers[nm] = flowers + _pocket_loops(rest, zf, top_at, ext)
        lops[nm] = {"op": "Pocket", "depth": f"{float(hi[2] - zf):.4f}"}
    _write_dxf(path, layers)
    return lops, T


def _build_sheet(part, material, finish, rack, machine, stock, margin, progress):
    """A flat part from sheet: through holes and slots, stepped pockets, the outline cut out with tabs.
    Built by G-code Maker's 2D engine from the part's own outlines (sliced from the solid)."""
    t0 = time.time()

    def prog(f, text):
        if progress:
            try:
                progress(float(f), text)
            except Exception:
                pass
    prog(0.05, "Reading the outlines of the flat part")
    mat = P.MATERIALS[material]
    tris = part.tris
    lo, hi = part.bounds
    T = float(hi[2] - lo[2])
    extra = 0.5
    through = _slice(tris, float(lo[2]) + min(0.05, T / 10))
    nz, ar = _normals_z(tris)
    up = (nz > 0.999)
    zc = tris[:, :, 2].mean(1)
    levels = {}
    for i in np.nonzero(up & (zc < float(hi[2]) - 0.01) & (zc > float(lo[2]) + 0.01))[0]:
        levels.setdefault(round(float(zc[i]), 3), []).append(tris[i])
    a = analyze(part, cell=None)
    Hg, cg, og = a["grid"], a["cell"], a["origin"]

    def top_at(p):
        i = int(math.floor((p[0] - og[0]) / cg))
        j = int(math.floor((p[1] - og[1]) / cg))
        if 0 <= j < Hg.shape[0] and 0 <= i < Hg.shape[1] and np.isfinite(Hg[j, i]):
            return float(Hg[j, i])
        return -1e9

    tools = [t for t in _rack_tools(rack, material, machine) if t["tool_type"] == FLAT]
    need_reach = T + extra
    holes = [c for i, c in enumerate(through) if sum(1 for k, o in enumerate(through) if k != i and gcm.inside(c[0], o)) % 2]
    floors = []
    for zf, ts in sorted(levels.items(), reverse=True):
        floors.append((zf, _boundary_loops(np.array(ts))))

    def fits(t):
        r = t["tool_d"] / 2 + 0.01
        for c in holes:
            if not gcm.offset(c, -r):
                return False
        for zf, loops in floors:
            for c in _pocket_loops(loops, zf, top_at, t["tool_d"] + 1.0):
                if gcm.area(c) > 0 and not gcm.offset(c, -r):
                    return False
        return True

    reach_ok = [t for t in tools if t["reach"] >= need_reach - 1e-6]
    pool = reach_ok or tools
    order = sorted(pool, key=lambda t: (not t["owned"], -t["tool_d"], t["reach"]))
    fit = [t for t in order if fits(t)]
    if fit:
        owned_fit = [t for t in fit if t["owned"]]
        tool = max(owned_fit or fit, key=lambda t: (t["tool_d"], -t["reach"], t["flutes"]))
        bigger = [t for t in pool if t["tool_d"] > tool["tool_d"] + 1e-9]
        reason = (f"{tool['name']}: the biggest flat end mill that fits every hole, slot and pocket"
                  + (f" (the {max(bigger, key=lambda t: t['tool_d'])['tool_d']:g} mm does not fit the smallest)"
                     if bigger else "") + ("" if tool["owned"] else "; it is not in the rack yet") + ".")
    else:
        tool = min(order, key=lambda t: t["tool_d"]) if order else None
        reason = f"{tool['name']}: the smallest flat end mill in the rack; some features are smaller still." if tool else ""
    if tool is None:
        job = gcm.Job()
        job.warnings.append("The rack has no flat end mill for cutting sheet.")
        return job
    ext = tool["tool_d"] + 1.0
    layers = {"CUT": through}
    lops = {"CUT": {"op": "Cut through", "depth": ""}}
    for k, (zf, loops) in enumerate(floors):
        depth = -zf if hi[2] == 0 else float(hi[2] - zf)
        nm = f"POCKET {k + 1}"
        layers[nm] = _pocket_loops(loops, zf, top_at, ext)
        lops[nm] = {"op": "Pocket", "depth": f"{depth:.4f}"}
    # the program frame: X0 Y0 = the stock's lower-left corner (as on 3D jobs); the sensor spot is given from
    # that corner too, and the sheet grows down-left when the spot would fall off it
    pr = machine["probe_d"] / 2
    if stock is None:
        stock = {"x0": float(lo[0]) - margin, "y0": float(lo[1]) - margin,
                 "w": float(hi[0] - lo[0]) + 2 * margin, "h": float(hi[1] - lo[1]) + 2 * margin, "t": T}
    stock = {k: float(stock[k]) for k in ("x0", "y0", "w", "h", "t")}
    sx, sy = float(machine["probe_dx"]), float(machine["probe_dy"])
    if sx - pr < 0:
        grow = pr + 2.0 - sx
        stock["x0"] -= grow
        stock["w"] += grow
        sx = pr + 2.0
    if sy - pr < 0:
        grow = pr + 2.0 - sy
        stock["y0"] -= grow
        stock["h"] += grow
        sy = pr + 2.0
    off = (-stock["x0"], -stock["y0"])
    layers = {k: [[(x + off[0], y + off[1]) for x, y in lp] for lp in v] for k, v in layers.items()}
    minx = min(p[0] for v in layers.values() for lp in v for p in lp)
    miny = min(p[1] for v in layers.values() for lp in v for p in lp)
    prog(0.3, "Planning the cuts with G-code Maker")
    os.makedirs(CACHE_DIR, exist_ok=True)
    dxf = os.path.join(CACHE_DIR, f"sheet_{os.getpid()}_{abs(hash(part.name)) % 100000}.dxf")
    _write_dxf(dxf, layers)
    s = dict(gcm.DEFAULTS)
    gt = {k: tool[k] for k in ("name", "tool_d", "tool_type", "flutes", "tool_mat", "tool_angle", "rpm", "feed",
                               "ramp_feed", "plunge_feed", "step", "ramp_angle")}
    s.update({
        "dxf": dxf, "out_dir": CACHE_DIR, "out_name": part.name or "part", "tool_name": tool["name"],
        "tool_d": tool["tool_d"], "tool_type": FLAT, "flutes": tool["flutes"], "tool_mat": tool["tool_mat"],
        "rpm": tool["rpm"], "feed": tool["feed"], "ramp_feed": tool["ramp_feed"], "plunge_feed": tool["plunge_feed"],
        "step": tool["step"], "ramp_angle": tool["ramp_angle"],
        "thickness": T, "extra": extra, "material": mat["gcm"], "layer_ops": lops, "skip_layers": "",
        "origin": "DXF origin (0,0)", "tc_mode": "One file, pause and measure", "probe_first": True,
        "rpm_min": machine["rpm_min"], "rpm_max": machine["rpm_max"], "feed_max": machine["feed_max"],
        "tabs": 3 if mat.get("hold") == "tabs" else 0, "tab_thick": min(1.0, max(0.3, T * 0.4)),
        "pocket_tool": gcm.MAIN, "hole_tool": gcm.MAIN, "manual": {}, "marks": [], "surf": False,
    })
    for k in ("safe_z", "end_z_mode", "end_z", "end_xy", "tc_code", "probe_d"):
        if k in machine:
            s[k] = machine[k]
    s["probe_dx"], s["probe_dy"] = sx - minx, sy - miny    # G-code Maker measures from the drawing's corner
    job = gcm.build(s, [gt])
    prog(0.9, "Finishing up")
    job.stock = {"x0": 0.0, "y0": 0.0, "w": stock["w"], "h": stock["h"], "t": T}
    job.part_offset = off
    st = job.stock
    ga = analyze(Part(part.tris + np.array([off[0], off[1], 0.0])), cell=max(0.1, auto_cell(st["w"], st["h"])),
                 bounds=(0.0, 0.0, st["w"], st["h"]))
    steps = []
    for line in job.run_steps:
        if line.startswith("Keep the sensor spot free"):
            line = (f"Keep the sensor spot free: a {machine['probe_d']:g} mm circle centred {sx:g} mm right of and "
                    f"{sy:g} mm above the stock's lower-left corner, on stock that is not cut.")
        line = line.replace("the DXF's 0,0 point on the stock", "the lower-left corner of the stock")
        steps.append(line)
    job.run_steps = steps
    job.part_grid = {"grid": ga["grid"], "origin": ga["origin"], "cell": ga["cell"]}
    job.analysis = {k: v for k, v in a.items() if not isinstance(v, np.ndarray)}
    names = []
    job.ops = []
    for k, (si, label) in enumerate(job.stages):
        end = job.stages[k + 1][0] if k + 1 < len(job.stages) else len(job.segs)
        nm = label.split(": ", 1)[-1]
        kind = ("pocket" if "pocket" in nm else "hole" if "hole" in nm else "outline" if "outline" in nm else "other")
        job.ops.append({"name": nm, "tool": gt, "kind": kind, "seg_range": (si, end)})
        names.append(nm)
    gt["owned"] = tool["owned"]
    gt["reach"] = tool["reach"]
    gt["flute_len"] = tool["flute_len"]
    gt["shank_d"] = tool.get("shank_d", tool["tool_d"])
    job.tools_used = [(gt, reason, list(dict.fromkeys(names)))]
    if not tool["owned"]:
        job.warnings.append(f"Buy: {tool['name']} (needed for {', '.join(dict.fromkeys(names))}).")
    job.flat_part = True
    job.build_seconds = time.time() - t0
    prog(1.0, "Done")
    return job
