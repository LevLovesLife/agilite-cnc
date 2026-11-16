"""
Agilite CNC: several parts on one sheet.

Every part (a DXF, or a flat STEP part's own outlines) is read with G-code Maker's reader, its copies are laid
out on the sheet row by row (biggest first, a set gap between them), and the lot is written as ONE drawing. The
proven 2D engine then cuts that drawing like any other: tabs, chamfers, text, pockets and surfacing all work across
the sheet without knowing there were several parts. Each part's layers keep their name with "P<n> " in front, so
a pocket layer of part 2 never takes its depth from part 1.
"""
import hashlib
import math
import os

import gcm_engine as gcm


def _bbox(shapes):
    xs = [p[0] for pts, _, _ in shapes for p in pts]
    ys = [p[1] for pts, _, _ in shapes for p in pts]
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def arrange(sizes, gap, width=None):
    """sizes: [(w, h)] -> [(x, y)] lower-left corners: rows (shelves), tallest first, the sheet about square unless
    a width is given. Returns (positions, (W, H)) in the input order."""
    if not sizes:
        return [], (0.0, 0.0)
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0], i))
    if width is None:
        area = sum((w + gap) * (h + gap) for w, h in sizes)
        width = max(max(w for w, _ in sizes), math.sqrt(area) * 1.1)
    pos = [None] * len(sizes)
    x = y = row_h = 0.0
    W = 0.0
    for i in order:
        w, h = sizes[i]
        if x > 0 and x + w > width + 1e-6:
            x, y, row_h = 0.0, y + row_h + gap, 0.0
        pos[i] = (x, y)
        x += w + gap
        row_h = max(row_h, h)
        W = max(W, x - gap)
    return pos, (W, y + row_h)


def compose(items, gap, out_dir, width=None):
    """items: [{"dxf", "layer_ops": {layer: op}, "copies": n, "name"}] -> (combined DXF path, combined layer ops,
    placements [{"item", "copy", "x", "y", "w", "h"}]). The file is only rewritten when its content changes, so
    the planner's caches (keyed by the file's time) still hit when nothing moved."""
    pieces, lops = [], {}
    for i, it in enumerate(items):
        n = max(0, int(it.get("copies", 1)))
        if not n:
            continue
        shapes, _ = gcm.read_dxf(it["dxf"])
        pre = f"P{i + 1} "
        ops = it.get("layer_ops") or {}
        keep = [(pts, c, L) for pts, c, L in shapes
                if (ops.get(L) or gcm.default_layer_op(L)).get("op") != "Skip"]
        x0, y0, x1, y1 = _bbox(keep)
        for L, op in ops.items():
            lops[pre + L] = op
        for k in range(n):
            pieces.append({"item": i, "copy": k, "shapes": keep, "pre": pre, "box": (x0, y0, x1, y1)})
    pos, size = arrange([(p["box"][2] - p["box"][0], p["box"][3] - p["box"][1]) for p in pieces], gap, width)
    L = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "4", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    placed = []
    for p, (px, py) in zip(pieces, pos):
        x0, y0, x1, y1 = p["box"]
        dx, dy = px - x0, py - y0
        placed.append({"item": p["item"], "copy": p["copy"], "x": px, "y": py, "w": x1 - x0, "h": y1 - y0})
        for pts, closed, layer in p["shapes"]:
            L += ["0", "LWPOLYLINE", "8", p["pre"] + layer, "90", str(len(pts)), "70", "1" if closed else "0"]
            for x, y in pts:
                L += ["10", f"{x + dx:.5f}", "20", f"{y + dy:.5f}"]
    L += ["0", "ENDSEC", "0", "EOF"]
    text = "\n".join(L) + "\n"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"sheet {hashlib.sha1(text.encode()).hexdigest()[:10]}.dxf")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return path, lops, placed, size
