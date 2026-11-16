"""
2D parts (DXF) for Agilite CNC: the proven G-code Maker engine (gcm_engine.build) does the cutting;
this file only chooses its settings and tools from the presets, the way a slicer would.
"""
import math
import os
import re

import gcm_engine as gcm
import presets as P

_TRIALS = {}   # bit trials per drawing and settings: moving the sensor or a text does not redo them
_TWOS = {}     # the two-bit trial (big bit outlines, small bit holes) per trial key
SKIP_WORDS = ("is skipped", "are not cut", "left out")


def base_settings(dxf, material, thickness, finish, machine=P.MACHINE, out_dir=None):
    s = dict(gcm.DEFAULTS)
    m = P.MATERIALS[material]
    f = P.FINISHES[finish]
    s.update({
        "dxf": dxf, "out_dir": out_dir or P.machine_folder(), "out_name": "",
        "thickness": float(thickness), "extra": 0.5, "material": m["gcm"],
        "tabs": 3 if m["hold"] == "tabs" else 0,
        "tab_len": 4.0, "tab_thick": round(max(0.4, min(1.0, 0.35 * float(thickness))), 2),
        "tabs_on_holes": True, "hole_tabs": 0, "direction": "Conventional", "origin": "Lower-left of drawing",
        "rpm_min": machine["rpm_min"], "rpm_max": machine["rpm_max"], "feed_max": machine["feed_max"],
        "safe_z": machine["safe_z"], "tc_mode": "One file, pause and measure", "tc_code": machine["tc_code"],
        "probe_dx": machine["probe_dx"], "probe_dy": machine["probe_dy"], "probe_d": machine["probe_d"],
        "probe_first": machine.get("sensor_mode") != "bed", "workflow_v2": True, "tools_v2": True,
        "end_z_mode": machine["end_z_mode"], "end_z": machine["end_z"], "end_xy": machine["end_xy"],
        "bed_sensor": P.bed_sensor(machine), "bed_macro": machine.get("bed_macro", "M20201"),
        "user_zero": machine.get("sensor_mode") == "bed",
        "finish": bool(f["finish_pass"]), "finish_allow": 0.1,
        "hole_tool": gcm.MAIN, "pocket_tool": gcm.MAIN, "engrave_tool": gcm.MAIN, "surf_tool": gcm.MAIN,
        "edge_tool": gcm.MAIN, "chamfer": False, "edge_break": False, "surf": False,
        "marks": [], "layer_ops": {}, "manual": {}, "recent": [], "skip_layers": "", "chamfer_sel": None,
    })
    return s


_DRAWINGS = {}   # (dxf, mtime, skip, layer ops, origin) -> ({shape key: drawn contour in DXF mm}, origin)
_KINDS = {}      # the same key -> (has pocket layers, has engraving layers)


def layer_kinds(s):
    """(has pocket layers, has engraving layers) of the drawing build() will cut."""
    drawing(s)
    for key, v in reversed(list(_KINDS.items())):
        if key[0] == s.get("dxf") and key[2:] == (s.get("skip_layers", ""), repr(s.get("layer_ops") or {}), s["origin"]):
            return v
    return False, False


def drawing(s):
    """The closed shapes build() will cut, by shape key (DXF coordinates), and the DXF point that becomes X0 Y0
    (worked out exactly as gcm_engine.build does it)."""
    dxf = s.get("dxf")
    try:
        key = (dxf, os.path.getmtime(dxf), s.get("skip_layers", ""), repr(s.get("layer_ops") or {}), s["origin"])
    except (OSError, TypeError):
        return {}, (0.0, 0.0)
    if key in _DRAWINGS:
        return _DRAWINGS[key]
    shapes, _ = gcm.read_dxf(dxf, s.get("skip_layers", "").split(","))
    cut, pockets, engrave, _, _ = gcm.split_layers(shapes, s.get("layer_ops") or {})
    allpts = ([p for c, _ in cut for p in c] + [p for cs in pockets.values() for c in cs for p in c]
              + [p for a, b in engrave.values() for c in a + b for p in c])
    xs = [p[0] for p in allpts] or [0.0]
    ys = [p[1] for p in allpts] or [0.0]
    if s["origin"].startswith("Lower"):
        o = (min(xs), min(ys))
    elif s["origin"].startswith("Centre"):
        o = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    else:
        o = (0.0, 0.0)
    shapes_ = {gcm.shape_key(c): c for c, _ in cut}
    for rk, _, c in gcm.rim_shapes(pockets):   # the pockets' top rims can be chamfered too
        shapes_[rk] = c
    out = (shapes_, o)
    _DRAWINGS[key] = out
    _KINDS[key] = (any(pockets.values()), any(a or b for a, b in engrave.values()))
    while len(_DRAWINGS) > 8:
        _KINDS.pop(next(iter(_DRAWINGS)), None)
        _DRAWINGS.pop(next(iter(_DRAWINGS)))
    return out


CORNER = math.radians(20.0)
STRAIGHT = math.radians(0.5)   # points of one straight run turn less than this


def contour_edges(points):
    """Splits a closed contour into the edges a designer would click: broken only at corners over 20 degrees and
    where a real straight (at least 1 mm, or 3 chords) meets a curve. A curve stays one edge however its
    tessellation came out (a STEP slice's arcs have uneven chords and doubled points), so a click on a slot's
    round end or a hole takes the whole of it.
    Returns [[i0, i1], ...]: point index ranges along the contour, inclusive, wrapping (i1 < i0 wraps round;
    [i0, i0] is the whole loop, for a shape that is one single edge, such as a circle)."""
    n = len(points)
    if n < 3:
        return [[0, 0]]
    x0, y0 = points[0]
    p = [(x - x0, y - y0) for x, y in points]
    seg = [math.hypot(p[(k + 1) % n][0] - p[k][0], p[(k + 1) % n][1] - p[k][1]) for k in range(n)]
    span = max(max(q[0] for q in p) - min(q[0] for q in p), max(q[1] for q in p) - min(q[1] for q in p), 1e-9)
    tiny = 1e-4 * span
    turn = [0.0] * n
    for k in range(n):
        if seg[k] <= tiny or seg[k - 1] <= tiny:
            continue      # a doubled point: no direction of its own
        a, b, c = p[k - 1], p[k], p[(k + 1) % n]
        t = math.atan2(c[1] - b[1], c[0] - b[0]) - math.atan2(b[1] - a[1], b[0] - a[0])
        turn[k] = (t + math.pi) % (2 * math.pi) - math.pi
    # flat: segment k turns less than STRAIGHT at both its ends
    chords = sorted(x for x in seg if x > tiny) or [1.0]
    med = chords[len(chords) // 2]
    # (a single chord far longer than the rest is a line whatever its ends turn: a slot's side meets the round
    # end's first chord at half a chord's angle)
    flat = [(abs(turn[k]) < STRAIGHT * 2 and abs(turn[(k + 1) % n]) < STRAIGHT * 2) or seg[k] >= max(1.0, 6 * med)
            for k in range(n)]
    # straight runs that count: long enough to be a real line, not a near-flat bit of a big arc
    straight = [False] * n
    if not all(flat):
        k0 = flat.index(False)
        k = 0
        while k < n:
            i = (k0 + k) % n
            if flat[i]:
                m = 0
                while m < n and flat[(i + m) % n]:
                    m += 1
                L = sum(seg[(i + j) % n] for j in range(m))
                if L >= max(1.0, 3 * med) or (m == 1 and seg[i] >= max(1.0, 6 * med)):
                    for j in range(m):
                        straight[(i + j) % n] = True
                k += m
            else:
                k += 1
    else:
        straight = [True] * n
    breaks = set()
    for k in range(n):   # point k sits between segment k - 1 and segment k
        if abs(turn[k]) > CORNER or straight[k] != straight[k - 1]:
            breaks.add(k)
    if not breaks:
        return [[0, 0]]
    b = sorted(breaks)
    if len(b) == 1:
        return [[b[0], b[0]]]
    return [[b[j], b[(j + 1) % len(b)]] for j in range(len(b))]


def _contour_edges_old(points):
    """The first splitter (kept for reference): straight runs and arcs, broken at corners over 20 degrees and
    where a straight meets a curve or one arc meets another.
    Returns [[i0, i1], ...]: point index ranges along the contour, inclusive, wrapping (i1 < i0 wraps round;
    [i0, i0] is the whole loop, for a shape that is one single edge, such as a circle)."""
    n = len(points)
    if n < 3:
        return [[0, 0]]
    x0, y0 = points[0]
    p = [(x - x0, y - y0) for x, y in points]   # local: the same result in job and DXF coordinates
    xs, ys = [q[0] for q in p], [q[1] for q in p]
    tol = max(0.005, 2e-4 * max(max(xs) - min(xs), max(ys) - min(ys)))
    turn = []
    seg = [math.hypot(p[(k + 1) % n][0] - p[k][0], p[(k + 1) % n][1] - p[k][1]) for k in range(n)]
    for k in range(n):
        a, b, c = p[k - 1], p[k], p[(k + 1) % n]
        t = math.atan2(c[1] - b[1], c[0] - b[0]) - math.atan2(b[1] - a[1], b[0] - a[0])
        turn.append((t + math.pi) % (2 * math.pi) - math.pi)

    def fits(i, m):
        """Do the m segments from point i make one straight run or one arc?"""
        if m <= 1:
            return True
        if any(abs(turn[(i + j) % n]) > CORNER for j in range(1, m)):
            return False
        q = [p[(i + j) % n] for j in range(m + 1)]
        a, b = q[0], q[-1]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L > 1e-9 and all(abs(turn[(i + j) % n]) < STRAIGHT for j in range(1, m)) and all(abs((v[0] - a[0]) * (b[1] - a[1]) - (v[1] - a[1]) * (b[0] - a[0])) / L <= tol for v in q):
            return True
        sg = [turn[(i + j) % n] for j in range(1, m)]
        ls = [seg[(i + j) % n] for j in range(m)]
        if max(ls) > 2.0 * min(ls):   # an arc's chords are alike; a line next to a chord is not one arc
            return False
        if not (all(t > 1e-6 for t in sg) or all(t < -1e-6 for t in sg)) or abs(sum(sg)) > 2 * math.pi + 1e-6:
            return False
        u, v, w = q[0], q[max(1, m // 3)], q[max(2, (2 * m) // 3)]
        d = 2 * (u[0] * (v[1] - w[1]) + v[0] * (w[1] - u[1]) + w[0] * (u[1] - v[1]))
        if abs(d) < 1e-12:
            return False
        uu, vv, ww = u[0] ** 2 + u[1] ** 2, v[0] ** 2 + v[1] ** 2, w[0] ** 2 + w[1] ** 2
        cx = (uu * (v[1] - w[1]) + vv * (w[1] - u[1]) + ww * (u[1] - v[1])) / d
        cy = (uu * (w[0] - v[0]) + vv * (u[0] - w[0]) + ww * (v[0] - u[0])) / d
        r = math.hypot(u[0] - cx, u[1] - cy)
        return all(abs(math.hypot(z[0] - cx, z[1] - cy) - r) <= tol for z in q)

    def grow(i, limit):
        m = 1
        while m < limit and fits(i, m + 1):
            m += 1
        return m

    corners = [k for k in range(n) if abs(turn[k]) > CORNER]
    # where segment lengths jump (a line meeting an arc's chords) is also a sure edge end. The first such point,
    # not the biggest jump: symmetric drawings have equal jumps, and float noise must not change the numbering
    jumps = [k for k in range(n) if max(seg[k], seg[k - 1]) > 2.0 * min(seg[k], seg[k - 1])]
    if corners:
        start = corners[0]
    elif jumps:
        start = jumps[0]
    else:
        m0 = grow(0, n)
        if m0 >= n:
            return [[0, 0]]
        start = m0 % n
    edges, i, done = [], start, 0
    while done < n:
        m = grow(i, n - done)
        edges.append([i, (i + m) % n])
        i, done = (i + m) % n, done + m
    if len(edges) == 1:
        return [[start, start]]
    return edges


def _skipped(job):
    return sum(1 for w in job.warnings if any(k in w for k in SKIP_WORDS))


def _too_wide(job):
    return sum(1 for w in job.warnings if "wider than drawn" in w)


def _run(s, tools, main, holes):
    s = dict(s)
    s["tool_name"] = main["name"]
    s["hole_tool"] = gcm.MAIN if holes is main else holes["name"]
    s["pocket_tool"] = s["hole_tool"]
    if s.get("engrave_tool") in (None, "", gcm.MAIN):
        s["engrave_tool"] = s["hole_tool"]
    return gcm.build(s, tools)


def _best_bit(rack, kind, machine, prefer_small=False, max_d=None):
    bits = [b for b in rack if b["tool_type"] == kind and b.get("shank_d", b["tool_d"]) <= machine["collet_max_shank"]
            and (max_d is None or b["tool_d"] <= max_d)]
    if not bits:
        return None
    return sorted(bits, key=lambda b: (not b.get("owned", True), b["tool_d"] if prefer_small else -b["tool_d"]))[0]


def apply_options(s, options, rack, material, machine):
    """The add-ons a designer can put on a part (sensor spot, tabs, text and logos, chamfer, round edges,
    surfacing) as G-code Maker settings, each with an automatically chosen bit. Returns extra tool dicts."""
    o = options or {}
    extra = []
    sen = o.get("sensor") or {}
    if "dx" in sen and "dy" in sen:
        s["probe_dx"], s["probe_dy"] = float(sen["dx"]), float(sen["dy"])
    if o.get("extra") is not None:   # mm the through-cuts go below the sheet (G-code Maker's "extra")
        s["extra"] = max(0.0, float(o["extra"]))
    tabs = o.get("tabs") or {}
    man = tabs.get("manual")
    if isinstance(man, dict):
        # tabs the designer placed, per shape key, in job coordinates; G-code Maker keeps them in DXF ones
        _, (ox, oy) = drawing(s)
        s["manual"] = {gcm.manual_key(s["dxf"]): {
            "tabs": {str(k): [[float(x) + ox, float(y) + oy] for x, y in (pts or [])] for k, pts in man.items()},
            "starts": {}}}
    if "count" in tabs:
        s["tabs"] = max(0, int(tabs["count"]))
    if "windows" in tabs:   # automatic tabs per internal window (0 = none; manual tabs still go on any window)
        s["hole_tabs"] = max(0, int(tabs["windows"] or 0))
    if "len" in tabs:
        s["tab_len"] = float(tabs["len"])
    if "thick" in tabs:
        s["tab_thick"] = float(tabs["thick"])
    marks, need_engrave_v = [], False
    for a in o.get("addons") or []:
        if a.get("hidden"):
            continue
        k = a.get("kind")
        if k in ("text", "logo"):
            m = gcm.new_mark(k)
            for key in ("text", "font", "bold", "size", "x", "y", "angle", "align", "cut", "depth", "logo",
                        "registered"):
                if key in a and a[key] is not None:
                    m[key] = a[key]
            marks.append(m)
            if m.get("cut", "On the line") == "On the line":
                need_engrave_v = True
        elif k == "chamfer" and not o.get("chamfers"):   # chamfers per edge replace the all-edges one
            v = _best_bit(rack, P.VBIT, machine)
            if v:
                t = P.tool_for(v, material, machine)
                extra.append(t)
                s.update(chamfer=True, chamfer_tool=t["name"], chamfer_w=float(a.get("width", 0.5)))
        elif k == "edge":
            b = _best_bit(rack, P.BALL, machine, prefer_small=False, max_d=6.0)
            if b:
                t = P.tool_for(b, material, machine)
                extra.append(t)
                s.update(edge_break=True, edge_tool=t["name"], edge_size=float(a.get("size", 0.5)))
        elif k == "surface":
            # ONE value says how much is surfaced off: part_t (the finished thickness, "Leaves" in the panel).
            # The add-on only brings the stepover and margin; an old-style add-on depth (no part_t sent) is
            # turned into part_t here, so the two can never both apply.
            if o.get("part_t") is None and a.get("depth") is not None:
                o = dict(o, part_t=max(0.0, s["thickness"] - float(a["depth"])))
            if a.get("over") is not None:   # set by the designer; else the surfacing bit's own (or 45 %)
                s["surf_over_set"] = float(a["over"])
            s.update(surf_over=float(a.get("over") or 45.0), surf_margin=float(a.get("margin", 5.0)),
                     surf_area={"rect": "Drawing + margin", "around": "Part + margin"}.get(a.get("area"), "Part outline"),
                     surf_pattern={"one": "One way", "rings": "Rings"}.get(a.get("pattern"), "Back and forth"))
    if o.get("chamfers"):
        sel = chamfer_selection(s, o["chamfers"])
        v = _best_bit(rack, P.VBIT, machine) if sel else None
        if v:
            t = P.tool_for(v, material, machine)
            extra.append(t)
            s.update(chamfer=True, chamfer_tool=t["name"], chamfer_sel=sel,
                     chamfer_w=max(w for ent in sel.values() for w, _ in ent))
    s["marks"] = marks
    if marks:
        if need_engrave_v:
            b = _best_bit(rack, P.VBIT, machine)
        else:
            b = _best_bit(rack, P.FLAT, machine, prefer_small=True)
        if b:
            t = P.tool_for(b, material, machine)
            extra.append(t)
            s["engrave_tool"] = t["name"]
    # stock thicker than the part: surface the difference off the top first, then cut the part from what is left
    pt = o.get("part_t")
    if pt is not None and 0 < float(pt) < s["thickness"] - 0.005:
        # a surfacing bit (an owned one) faces the top fastest; else the biggest flat end mill
        b = _best_bit(rack, P.SURF, machine)
        if b is None or not b.get("owned", True):
            b = _best_bit(rack, P.FLAT, machine) or b
        if b:
            t = P.tool_for(b, material, machine)
            extra.append(t)
            # shaped to the part: only the part and the strip its outline bit cuts beside it are faced off (the
            # engine grows the outline by that bit, or by the margin when it is wider)
            s.update(surf=True, surf_tool=t["name"])
            if not any(a.get("kind") == "surface" and not a.get("hidden") for a in o.get("addons") or []):
                s.update(surf_over=45.0, surf_area="Part outline", surf_pattern="Back and forth")
            s["surf_depth"] = round(s["thickness"] - float(pt), 4)
    else:
        s["surf"] = False
    apply_params(s, o.get("params"))
    return extra


# Every number the Settings tab can change, Bambu-style: unset = the preset's own value (material, finish, bit).
# key: (type, min, max). Percentages scale the bit's own speeds; ramp_set 0 = the bit's own ramp angle.
PARAMS = {
    "direction": ("choice", ("Conventional", "Climb")),
    "feed_pct": ("num", 20.0, 200.0), "step_pct": ("num", 20.0, 300.0), "rpm_pct": ("num", 40.0, 150.0),
    "ramp_set": ("num", 0.0, 90.0),
    "finish": ("bool",), "finish_allow": ("num", 0.02, 1.0),
    "slot_center": ("bool",), "slot_allow": ("num", 0.0, 2.0), "pocket_over": ("num", 10.0, 90.0),
    "safe_z": ("num", 1.0, 50.0), "end_z": ("num", 0.0, 150.0),
    "end_z_mode": ("choice", ("Machine top (G53 Z-3)", "Custom height")),
}


def apply_params(s, params):
    """The designer's changed settings, checked and kept inside sensible limits; unknown keys are ignored."""
    for k, v in (params or {}).items():
        spec = PARAMS.get(k)
        if spec is None or v is None:
            continue
        try:
            if spec[0] == "choice":
                if v in spec[1]:
                    s[k] = v
            elif spec[0] == "bool":
                s[k] = bool(v)
            else:
                s[k] = max(spec[1], min(spec[2], float(v)))
        except (TypeError, ValueError):
            continue


def load_checks(s, used):
    """The chip load, cutting speed and depth per pass of every end mill the plan uses, with the designer's feed /
    depth / spindle percentages applied, judged like G-code Maker's speed check. Returns ({tool name: info},
    [warnings]); info = {fz, aim, vc, vc_range, step, step_aim, state: ok | thin | heavy, deep: bool}."""
    info, warns = {}, []
    for t, *_ in used or []:
        if t.get("tool_type") == gcm.VBIT:
            continue   # a V-bit's chip depends on how deep it runs; G-code Maker's table doesn't cover it
        q = dict(s, tool_d=float(t["tool_d"]), flutes=int(t.get("flutes", 2)), tool_mat=t.get("tool_mat", "Carbide"),
                 tool_type=t.get("tool_type", P.FLAT))
        sug = gcm.suggest(q)
        if t.get("tool_type") == P.SURF:   # an insert cutter: its own chip and depth, not the end mill table's
            sd = P.SURF_DATA.get(s.get("material"), P.SURF_DATA["Wood / MDF / plywood"])
            sug = dict(sug, fz_target=sd["fz"], step=sd["step"])
        sp = gcm.tool_speeds(t, s)
        z = max(1, int(t.get("flutes", 2)))
        fz = sp["feed"] / max(1.0, sp["rpm"] * z)
        aim = sug["fz_target"]
        vc = math.pi * float(t["tool_d"]) * sp["rpm"] / 1000
        state = "thin" if fz < 0.6 * aim else "heavy" if fz > 1.6 * aim else "ok"
        deep = sp["step"] > sug["step"] * 1.5
        info[t["name"]] = {"fz": round(fz, 4), "aim": round(aim, 4), "vc": round(vc), "vc_range": [round(v) for v in sug["vc_range"]],
                           "step": sp["step"], "step_aim": sug["step"], "state": state, "deep": deep,
                           "rpm": sp["rpm"], "feed": sp["feed"]}
        lab = gcm._tool_label(t)
        if state == "heavy":
            warns.append(f"The {lab} takes a heavy chip ({fz:.3f} mm per tooth, aim {aim:.3f}): it may chatter or "
                         f"break. Lower the feed rate or raise the spindle speed (Settings).")
        elif state == "thin":
            warns.append(f"The {lab} takes a thin chip ({fz:.3f} mm per tooth, aim {aim:.3f}): it rubs and heats up "
                         f"instead of cutting. Raise the feed rate or lower the spindle speed (Settings).")
        if deep:
            warns.append(f"The {lab} cuts {sp['step']:.2f} mm per pass, deep for it in this material (about "
                         f"{sug['step']:.2f} mm is right). Lower the depth per pass (Settings).")
        if float(t.get("shank_d") or 0) > 6.0 + 1e-6:
            warns.append(f"The {t['name']} has a {t['shank_d']:g} mm shank: put an {t['shank_d']:g} mm collet in "
                         f"the spindle for it.")
    return info, warns


def params_used(s):
    """The value every Settings-tab number has in this plan (so the panel shows real numbers, not blanks)."""
    out = {}
    for k in PARAMS:
        if k in s:
            out[k] = s[k]
    for k in ("feed_pct", "step_pct", "rpm_pct"):
        out[k] = float(s.get(k) or 100.0)
    out["ramp_set"] = float(s.get("ramp_set") or 0.0)
    return out


def chamfer_selection(s, chamfers):
    """options["chamfers"] = [{"key": shape key, "edges": [edge index, ...] or "all", "width": mm}, ...] as
    G-code Maker's chamfer_sel: {shape key: [(width, [(i0, i1) point ranges] or None for the whole loop)]}."""
    shapes, _ = drawing(s)
    sel = {}
    for ch in chamfers or []:
        key = str(ch.get("key", ""))
        try:
            w = float(ch.get("width", 0.5) or 0)
        except (TypeError, ValueError):
            continue
        if key not in shapes or w <= 0:
            continue
        edges = contour_edges(shapes[key])
        want = ch.get("edges", "all")
        if want == "all" or want is None:
            idx = list(range(len(edges)))
        else:
            idx = sorted({int(e) for e in want if 0 <= int(e) < len(edges)})
        if not idx:
            continue
        ranges = None if len(idx) == len(edges) else [tuple(edges[e]) for e in idx]
        sel.setdefault(key, []).append((w, ranges))
    return sel


def plan(dxf, material, thickness, finish, rack=None, machine=P.MACHINE, out_dir=None, progress=None, options=None,
         layer_ops=None):
    """Returns (job, settings, tools_used) where tools_used = [(tool dict, reason, [what it cuts])].
    options["strategy"] in ("fewest", "recommended", "fastest") and options["bits"] = {op key: rack bit name}
    choose the bits; without them the plan is exactly the recommended one. The job also carries op_cards
    (the operations in run order, each with its bit and the bits it could use) and strategies."""
    rack = rack if rack is not None else P.load_rack()
    rack = P.rack_for(rack, material)   # a bit that doesn't cut this material (a wood-only surfacing bit) is left out
    opts = options or {}
    s = base_settings(dxf, material, thickness, finish, machine, out_dir)
    if layer_ops:
        s["layer_ops"] = dict(layer_ops)
    extra_tools = apply_options(s, options, rack, material, machine)
    sen = (options or {}).get("sensor") or {}
    if s.get("surf") and not ("dx" in sen and "dy" in sen):
        # the puck must not sit on the surfaced (lower) top: by default it goes outside the surfaced area
        s["probe_dx"] = s["probe_dy"] = -(float(s.get("surf_margin") or 5.0) + s["probe_d"] / 2 + 3.0)
    depth = float(thickness) + s["extra"]
    flats = [b for b in rack if b["tool_type"] == P.FLAT and b.get("shank_d", b["tool_d"]) <= machine["collet_max_shank"]]
    reach_ok = [b for b in flats if b.get("flute_len", 99) >= depth and b.get("reach", 99) >= depth]
    pool = reach_ok or flats
    # one bit of each diameter: the owned one first, then the one with more flutes for wood, fewer for aluminium
    best = {}
    for b in sorted(pool, key=lambda b: (not b.get("owned", True),
                                          b["flutes"] if P.MATERIALS[material]["gcm"] == "Aluminium" else -b["flutes"])):
        best.setdefault(round(b["tool_d"], 3), b)
    cands = [P.tool_for(b, material, machine) for b in sorted(best.values(), key=lambda b: -b["tool_d"])]
    if not cands:
        job = gcm.Job()
        job.warnings.append("There is no flat end mill in the end mill rack. Add one under Expert > End mills.")
        return job, s, []
    tools = list(cands) + [t for t in extra_tools if t["name"] not in {c["name"] for c in cands}]
    tried = []
    # the bit trials only need the part itself: the add-ons come in for the final build
    # manual tabs do not change which bit fits: they stay out of the trials (and so out of their cache key)
    trial = dict(s, surf=False, chamfer=False, edge_break=False, marks=[], manual={}, chamfer_sel=None)
    addons = trial != s
    try:
        key = repr((sorted((k, repr(v)) for k, v in trial.items() if k not in ("probe_dx", "probe_dy", "out_dir")),
                    # the speeds too: Lev's own speeds for an end mill change its trial
                    [(c["name"], tuple(c[k] for k in gcm.SPEED_KEYS)) for c in cands], os.path.getmtime(dxf)))
    except OSError:
        key = None
    if key is not None and key in _TRIALS:
        tried = _TRIALS[key]
        cands_iter = []
    else:
        cands_iter = cands
    for i, t in enumerate(cands_iter):
        if progress:
            progress(i / (len(cands) + 2), f"Trying the {gcm._tool_label(t)}")
        job = _run(trial, tools, t, t)
        tried.append((t, job, _skipped(job), _too_wide(job)))
    if key is not None:
        _TRIALS[key] = tried
        while len(_TRIALS) > 8:
            _TRIALS.pop(next(iter(_TRIALS)))
    fits_all = [x for x in tried if x[2] == 0 and x[3] == 0]
    biggest = tried[0]
    two_c = None
    if fits_all:
        one = fits_all[0]          # biggest bit that cuts everything
        options = [("one", one[0], one[0], one[1], one[1].time_min)]
        if biggest[0] is not one[0]:
            # the big bit cuts the outlines, the small one the holes and pockets: worth a tool change?
            # (the two-bit trial is cached with the one-bit ones, so switching strategies does not redo it)
            tkey = None if key is None else (key, biggest[0]["name"], one[0]["name"])
            two = _TWOS.get(tkey) if tkey is not None else None
            if two is None:
                two = _run(trial, tools, biggest[0], one[0])
                if tkey is not None:
                    _TWOS[tkey] = two
                    while len(_TWOS) > 8:
                        _TWOS.pop(next(iter(_TWOS)))
            if _skipped(two) == 0:
                options.append(("two", biggest[0], one[0], two, two.time_min + machine["tool_change_min"]))
                two_c = (biggest[0], one[0], two.time_min)
        kind, main, holes, job, _ = min(options, key=lambda o: o[4])
        one_c = (one[0], one[0], one[1].time_min)
    else:
        x = min(tried, key=lambda x: (x[2], x[3], -x[0]["tool_d"]))
        kind, main, holes, job = "one", x[0], x[0], x[1]
        one_c = (x[0], x[0], x[1].time_min)
    rec_c = two_c if (two_c and kind == "two") else one_c
    fast_c = min([one_c] + ([two_c] if two_c else []), key=lambda c: c[2])   # no cost for a bit change
    combos = {"fewest": one_c, "recommended": rec_c, "fastest": fast_c}
    strat = opts.get("strategy") if opts.get("strategy") in combos else "recommended"
    picks = {k: v for k, v in (opts.get("bits") or {}).items() if k in OP_KEYS and v}
    custom = strat != "recommended" or bool(picks)
    if not custom and s.get("surf") and s.get("surf_tool") not in (None, "", gcm.MAIN, main["name"]):
        # surfacing with the cut-out bit, unless the bigger bit saves more than a bit change costs (Lev: the
        # simplest, fastest way every time; a small part's skim is never worth a change)
        big = next((t for t in tools if t["name"] == s["surf_tool"]), None)
        if big is not None and _surf_minutes(s, main) - _surf_minutes(s, big) < machine["tool_change_min"]:
            s = dict(s, surf_tool=main["name"])
            addons = True
    if custom:
        s0 = s
        assign, picked = _assignment(strat, combos[strat], s, tools, picks, rack, material, machine)
        s = _settings_for(s, assign)
        main, holes = assign["outline"], assign["holes"]
        kind = "one" if main["name"] == holes["name"] else "two"
        job = gcm.build(s, tools)
    elif addons or not cands_iter:
        job = _run(s, tools, main, holes)
    if progress:
        progress(1.0, "Done")
    if custom:
        used = _used_for(job, s, tools, strat, picked)
        _finish(job, s, s0, tools, rack, material, machine, strat, combos, picks)
        s["tool_name"], s["hole_tool"] = main["name"], (gcm.MAIN if holes["name"] == main["name"] else holes["name"])
        for t, _, _ in used:
            if not t.get("owned", True):
                job.warnings.insert(0, f"Buy: {t['name']} (a standard end mill). The plan uses it; it isn't in the rack yet.")
        _add_load(job, s, used)
        return job, s, used
    used = []
    small_gap = min((t["tool_d"] for t, _, sk, tw in tried if sk == 0 and tw == 0), default=None)
    if kind == "one":
        bigger = [t for t in cands if t["tool_d"] > main["tool_d"]]
        if bigger and small_gap is not None:
            why = (f"The {gcm._tool_label(main)} is the biggest end mill that fits every hole, slot and corner of this part. "
                   f"The {gcm._tool_label(bigger[-1])} would not fit them all.")
        elif bigger:
            why = (f"No end mill in the rack fits every feature; the {gcm._tool_label(main)} fits the most. "
                   f"The ones it can't cut are listed under Checks.")
        else:
            why = f"The {gcm._tool_label(main)} is the biggest flat end mill in the rack, so it cuts fastest."
        used.append((main, why, ["holes and slots", "pockets", "outlines"]))
    else:
        used.append((main, f"The {gcm._tool_label(main)} cuts the outline fast; bigger end mills cut faster and break less.",
                     ["outlines"]))
        used.append((holes, f"The {gcm._tool_label(holes)} fits the small holes and slots the big end mill can't. "
                            f"Changing end mills costs about {machine['tool_change_min']:g} minutes, and still saves time.",
                     ["holes and slots", "pockets"]))
    have = {t["name"] for t, _, _ in used}
    reasons = {
        "chamfer_tool": ("chamfer", "A V-bit cuts the chamfer along the top edges after the part is cut out."),
        "edge_tool": ("edge break", "The ball nose rounds the top edges after the cut-out, so they are smooth to hold."),
        "surf_tool": ("surfacing", "The biggest flat end mill skims the top flat first, so every depth is measured "
                                   "from a true surface."),
        "engrave_tool": ("text and logos", "Engraves the text and logos. A V-bit draws fine single lines; "
                                           "a small flat end mill cuts outlined and filled letters."),
    }
    on = {"chamfer_tool": s.get("chamfer"), "edge_tool": s.get("edge_break"), "surf_tool": s.get("surf"),
          "engrave_tool": bool(s.get("marks"))}
    for key, (what, why) in reasons.items():
        name = s.get(key)
        if not on[key] or not name or name == gcm.MAIN:
            continue
        t = next((x for x in tools if x["name"] == name), None)
        if t is None:
            continue
        if name in have:
            for i, (tt, w, ops) in enumerate(used):
                if tt["name"] == name and what not in ops:
                    used[i] = (tt, w, ops + [what])
            continue
        used.append((t, why, [what]))
        have.add(name)
    for t, _, _ in used:
        if not t.get("owned", True):
            job.warnings.insert(0, f"Buy: {t['name']} (a standard end mill). The plan uses it; it isn't in the rack yet.")
    s["tool_name"], s["hole_tool"] = main["name"], (gcm.MAIN if holes is main else holes["name"])
    _finish(job, s, s, tools, rack, material, machine, "recommended", combos, {})
    _add_load(job, s, used)
    return job, s, used


def _add_load(job, s, used):
    job.chip, warns = load_checks(s, used)
    job.warnings.extend(warns)


def _surf_minutes(s, t):
    """About how long surfacing takes with bit t: back-and-forth rows over the drawing plus the margin, one pass
    per depth step, at the bit's feed."""
    shapes, _ = drawing(s)
    pts = [p for c in shapes.values() for p in c]
    if not pts or not t.get("feed"):
        return 0.0
    m = float(s.get("surf_margin") or 0.0)
    w = max(p[0] for p in pts) - min(p[0] for p in pts) + 2 * m
    h = max(p[1] for p in pts) - min(p[1] for p in pts) + 2 * m
    w, h = max(w, h), min(w, h)
    so = max(0.1, t["tool_d"] * float(s.get("surf_over_set") or t.get("surf_over") or s.get("surf_over") or 45.0) / 100)
    passes = max(1, math.ceil(float(s.get("surf_depth") or 0.0) / max(0.05, float(t.get("step") or 0.5)) - 1e-9))
    return passes * (h / so + 1) * (w + t["tool_d"]) / float(t["feed"])


# ---------------------------------------------------------------- bit strategies and the operations list
OP_KEYS = ("surface", "pockets", "holes", "outline", "chamfer", "engrave", "edge")
OP_LABELS = {"surface": "Surfacing", "pockets": "Pockets", "holes": "Holes and slots", "outline": "Outline cut",
             "chamfer": "Chamfer", "engrave": "Text and logos", "edge": "Round the edges"}
OP_WHAT = {"surface": "surfacing", "pockets": "pockets", "holes": "holes and slots", "outline": "outlines",
           "chamfer": "chamfer", "engrave": "text and logos", "edge": "edge break"}
OP_BITS = {"surface": (P.SURF, P.FLAT), "pockets": (P.FLAT,), "holes": (P.FLAT,), "outline": (P.FLAT,),
           "chamfer": (P.VBIT,), "engrave": (P.FLAT, P.VBIT), "edge": (P.BALL,)}
STAGE_OP = {"surfacing": "surface", "pockets": "pockets", "holes and slots": "holes", "outlines": "outline",
            "engraving": "engrave", "chamfer": "chamfer", "edge break": "edge"}
STRATEGIES = (("fewest", "Fewest end mill changes"), ("recommended", "Recommended"), ("fastest", "Fastest"))
_STAGE = re.compile(r"^T(\d+) (.*?): (.+?)( \(finishing pass\))?$")


def _tool_named(name, tools, rack, material, machine):
    """The tool dict for a bit name: from the plan's tools, else made from the rack (added to tools)."""
    t = next((t for t in tools if t["name"] == name), None)
    if t is None:
        b = next((b for b in rack if b.get("name") == name), None)
        if b is None:
            return None
        t = P.tool_for(b, material, machine)
        tools.append(t)
    return t


def _assignment(strat, combo, s, tools, picks, rack, material, machine):
    """The bit for each operation the part has, by strategy, then the designer's own picks on top.
    Returns ({op key: tool dict}, {op keys the designer picked})."""
    main, holes = combo[0], combo[1]
    byname = lambda n: next((t for t in tools if t["name"] == n), None)
    a = {"outline": main, "holes": holes, "pockets": holes}
    engrave_tied = s.get("engrave_tool") in (None, "", gcm.MAIN)   # layer engraving with the holes bit
    if s.get("surf"):
        a["surface"] = main if strat == "fewest" else (byname(s.get("surf_tool")) or main)
    eg = holes if engrave_tied else (byname(s["engrave_tool"]) or holes)
    if strat == "fewest" and eg["tool_type"] == P.FLAT:
        eg = main   # a flat bit engraves: the one bit does it too
    a["engrave"] = eg
    if s.get("chamfer") and byname(s.get("chamfer_tool")):
        a["chamfer"] = byname(s["chamfer_tool"])
    if s.get("edge_break") and byname(s.get("edge_tool")):
        a["edge"] = byname(s["edge_tool"])
    picked = set()
    for k in OP_KEYS:   # (holes before pockets and engraving: those follow a picked holes bit)
        name = picks.get(k)
        if not name or k not in a:
            continue
        t = _tool_named(name, tools, rack, material, machine)
        if t is None or t["tool_type"] not in OP_BITS[k]:
            continue
        a[k] = t
        picked.add(k)
        if k == "holes":
            if "pockets" not in picks:
                a["pockets"] = t
            if engrave_tied and "engrave" not in picks:
                a["engrave"] = t
    return a, picked


def _settings_for(s, a):
    """G-code Maker settings with one bit per operation (MAIN where it is the outline bit)."""
    s = dict(s)
    main = a["outline"]["name"]
    nm = lambda t: gcm.MAIN if t["name"] == main else t["name"]
    s["tool_name"] = main
    s["hole_tool"], s["pocket_tool"], s["engrave_tool"] = nm(a["holes"]), nm(a["pockets"]), nm(a["engrave"])
    for k, sk in (("surface", "surf_tool"), ("chamfer", "chamfer_tool"), ("edge", "edge_tool")):
        if k in a:
            s[sk] = nm(a[k])
    return s


def _stage_ops(job, s):
    """[(op key, tool name, minutes, T number)] per stage of the job, in run order."""
    has_pockets, _ = layer_kinds(s) if s.get("dxf") else (False, False)
    eng = s.get("engrave_tool")
    # the pocket bit by name ("Same as outlines" is the outline bit), so a pocket cut with the engraving bit is
    # still called Pockets; only filled text and logos make a pockets stage into Text and logos
    pk = s.get("pocket_tool")
    pk = s.get("tool_name") if pk in (None, "", gcm.MAIN) else pk
    filled = any(isinstance(m_, dict) and m_.get("cut") == "Filled" for m_ in s.get("marks") or [])
    cum = [0.0]
    for x in job.seg_info:
        cum.append(cum[-1] + x[0])
    bounds = list(job.stages) + [(len(job.segs), "")]
    out = []
    for (i0, label), (i1, _) in zip(bounds[:-1], bounds[1:]):
        m = _STAGE.match(label)
        if not m:
            continue
        k = STAGE_OP.get(m.group(3))
        if k is None:
            continue
        if k == "pockets" and (not has_pockets or (filled and eng not in (None, "", gcm.MAIN) and m.group(2) == eng
                                                   and pk != eng)):
            k = "engrave"   # filled text and logos are cut as pockets
        mins = cum[min(i1, len(cum) - 1)] - cum[min(i0, len(cum) - 1)]
        out.append((k, m.group(2), mins, int(m.group(1))))
    return out


def _choices(k, rack, machine):
    bits = [b for b in rack if b.get("tool_type") in OP_BITS[k]
            and b.get("shank_d", b["tool_d"]) <= machine["collet_max_shank"]]
    bits.sort(key=lambda b: (not b.get("owned", True), -b["tool_d"], b["name"]))
    return [{"name": b["name"], "label": b["name"], "owned": b.get("owned", True) is not False} for b in bits]


def _used_for(job, s, tools, strat, picked):
    """The bit list (tools_used) of a plan made with a strategy or picked bits, in the order the bits run."""
    order, ops = [], {}
    for k, name, _, _ in _stage_ops(job, s):
        if name not in ops:
            order.append(name)
            ops[name] = []
        if OP_WHAT[k] not in [OP_WHAT[x] for x in ops[name]]:
            ops[name].append(k)
    used = []
    for name in order:
        t = next((t for t in tools if t["name"] == name), None)
        if t is None:
            continue
        lab = gcm._tool_label(t)
        mine = [k for k in ops[name] if k in picked]
        if mine:
            why = f"You picked the {lab} for the {', '.join(OP_WHAT[k] for k in mine)}."
        elif strat == "fewest":
            why = (f"Fewest end mill changes: the {lab} does every cut a flat end mill can, so the job runs without stopping "
                   f"for an end mill change. Slower than an end mill per cut.")
        elif strat == "fastest":
            why = f"Fastest: the {lab} is the quickest end mill for these cuts, whatever the end mill changes cost."
        else:
            why = f"The {lab} is the recommended end mill for these cuts."
        used.append((t, why, [OP_WHAT[k] for k in ops[name]]))
    return used


def _finish(job, s, s0, tools, rack, material, machine, strat, combos, picks):
    """Puts op_cards, strategies and strategy on the job (for the page). s = the settings the job was built
    with, s0 = the settings before any strategy (what the other strategies are estimated from)."""
    stages = _stage_ops(job, s)
    cards, seen = [], {}
    for k, name, mins, tno in stages:
        if k in seen:
            seen[k]["minutes"] += mins
            continue
        t = next((t for t in tools if t["name"] == name), {"name": name, "tool_d": 0, "tool_type": ""})
        c = {"key": k, "label": OP_LABELS[k], "bit": name, "bit_label": gcm._tool_label(t) if t.get("tool_d") else name,
             "t": tno, "owned": t.get("owned", True) is not False, "picked": k in picks,
             "choices": _choices(k, rack, machine), "minutes": mins}
        seen[k] = c
        cards.append(c)
    for c in cards:
        c["minutes"] = round(c["minutes"], 3)
    job.op_cards = cards
    job.strategy = strat
    # Exact time and bit changes for the strategy this plan was made with. The others are ESTIMATES, not built:
    # this plan's time, plus the difference between the bit trials of their outline/holes bits (cached, so free),
    # plus the surfacing and engraving time scaled by the bit diameter squared where their bit differs (checked
    # on the ladderlock with surfacing: within about 1 min of building them). Picked bits are only in the exact one.
    tc = machine["tool_change_min"]
    nbits = len({tno for _, _, _, tno in stages})
    base_sel = combos[strat][2]
    mins = {c["key"]: c["minutes"] for c in cards}
    d_of = {c["key"]: next((t["tool_d"] for t in tools if t["name"] == c["bit"]), 0) for c in cards}
    out = []
    for key, label in STRATEGIES:
        if key == strat:
            out.append({"key": key, "label": label, "time_min": round(job.time_min, 3),
                        "changes": max(0, nbits - 1), "exact": True})
            continue
        a, _ = _assignment(key, combos[key], s0, list(tools), {}, rack, material, machine)
        t = job.time_min + combos[key][2] - base_sel
        for k in ("surface", "engrave"):
            if k in mins and k in a and d_of.get(k) and a[k]["tool_d"] and a[k]["name"] != next(
                    (c["bit"] for c in cards if c["key"] == k), None):
                # rows a bit's width apart and a feed that grows with the bit: about diameter squared
                t += mins[k] * ((d_of[k] / a[k]["tool_d"]) ** 2 - 1.0)
        present = {k for k in mins}
        names = {a[k]["name"] for k in present if k in a}
        out.append({"key": key, "label": label, "time_min": round(max(0.0, t), 3),
                    "changes": max(0, len(names) - 1), "exact": False})
    for x in out:
        x["total_min"] = round(x["time_min"] + x["changes"] * tc, 3)
    job.strategies = out


def stock_of(job, s, margin=8.0):
    """The sheet the job needs: the drawing plus margin, and the sensor spot on it."""
    pts = [p for c in job.contours + job.open_paths for p in c]
    if not pts:
        return {"x0": 0.0, "y0": 0.0, "w": 100.0, "h": 100.0, "t": float(s["thickness"])}
    x0 = min(p[0] for p in pts) - margin
    y0 = min(p[1] for p in pts) - margin
    x1 = max(p[0] for p in pts) + margin
    y1 = max(p[1] for p in pts) + margin
    if job.probe and not s.get("user_zero"):   # bed sensor: no puck on the stock, so no room kept for it
        px, py, pr = job.probe
        x0, y0 = min(x0, px - pr - 3), min(y0, py - pr - 3)
        x1, y1 = max(x1, px + pr + 3), max(y1, py + pr + 3)
    return {"x0": x0, "y0": y0, "w": x1 - x0, "h": y1 - y0, "t": float(s["thickness"])}


def file_name(dxf):
    return os.path.splitext(os.path.basename(dxf))[0]
