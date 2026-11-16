"""
G-code Maker - turns a DXF of flat parts into G-code for the Stepcraft M700 (UCCNC).

Outer outlines are cut on the outside of the line, holes and slots on the inside of
the line. Every cut ramps down step by step to the full depth, and the outlines keep
holding tabs. Holes are cut first, then the outlines. Only standard Python is used.

Double-click this file to open the window.
Command line (for testing):  pythonw "G-code Maker.pyw" --cli in.dxf out.ngc [key=value ...]
"""
import bisect
import json
import math
import os
import re
import sys

APP = "G-code Maker"
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "G-code Maker settings.json")
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Desktop", "G-code")
TOL = 0.01          # how closely arcs are followed, mm
RAPID = 2500.0      # rapid speed used only for the time estimate, mm/min

DEFAULTS = {
    "dxf": "",
    "out_dir": DEFAULT_OUT,
    "out_name": "",
    "tool_d": 4.0,
    "tool_type": "Ball nose",
    "rpm": 24000,
    "feed": 500.0,
    "ramp_feed": 250.0,
    "plunge_feed": 100.0,
    "thickness": 3.3,
    "extra": 2.2,
    "step": 0.3,
    "ramp_angle": 10.0,
    "safe_z": 5.0,
    "tabs": 3,
    "tab_len": 4.0,
    "tab_thick": 1.0,
    "tabs_on_holes": False,
    "direction": "Conventional",
    "origin": "Lower-left of drawing",
    "skip_layers": "",
    "tool_name": "4 mm ball nose 2F carbide",
    "flutes": 2,
    "tool_mat": "Carbide",
    "material": "Aluminium",
    "rpm_min": 5000,
    "rpm_max": 24000,
    "feed_max": 3000.0,
    "tool_angle": 90.0,
    "hole_tool": "Same as outlines",
    "chamfer": False,
    "chamfer_tool": "",
    "chamfer_w": 0.5,
    "edge_break": False,
    "edge_tool": "Same as outlines",
    "edge_size": 0.5,
    "tc_mode": "One file, pause and measure",
    "probe_dx": -20.0,   # the sensor spot: its centre, from the drawing's lower-left corner
    "probe_dy": -20.0,
    "probe_d": 32.0,     # the sensor's base (Lev's sensor: 32 mm)
    "probe_first": True, # measure the first tool too (the program starts at the sensor spot)
    "workflow_v2": False,
    "tools_v2": False,   # every library tool has its own speeds and feeds (see migrate_tools)
    "tc_code": "M0 (Put in T{tool} {name}, sensor under the end mill, then Cycle Start); M31; "
               "M0 (Take the sensor away, then Cycle Start)",
    "end_z_mode": "Custom height",
    "end_z": 20.0,
    "end_xy": "X0 Y0",
    "end_x": 0.0,
    "end_y": 0.0,
    "slot_center": True,
    "slot_allow": 0.3,
    "surf": False,
    "surf_tool": "Same as outlines",
    "surf_depth": 0.2,
    "surf_over": 50.0,
    "surf_area": "Drawing + margin",
    "surf_margin": 5.0,
    "surf_x": 0.0,
    "surf_y": 0.0,
    "surf_w": 100.0,
    "surf_h": 100.0,
    "surf_pattern": "Back and forth",
    "pocket_tool": "Same as outlines",
    "pocket_over": 40.0,
    "engrave_tool": "Same as outlines",
    "finish": False,
    "finish_allow": 0.1,
    "marks": [],         # typed texts and logos: see new_mark()
    "recent": [],        # the last drawings opened, newest first
    "layer_ops": {},     # layer name -> {"op": one of LAYER_OPS, "depth": "1.0"}
    "manual": {},        # DXF path -> {"tabs": {shape key: [[x, y], ...]}, "starts": {shape key: [x, y]}}
}
DICT_KEYS = ("layer_ops", "manual", "marks", "recent")
# what belongs to one drawing (remembered per DXF in the jobs file); the rest is the machine's and the app's
JOB_KEYS = ("out_name", "thickness", "extra", "material", "tabs", "tab_len", "tab_thick", "tabs_on_holes",
            "direction", "origin", "skip_layers", "tool_name", "hole_tool", "chamfer", "chamfer_tool", "chamfer_w",
            "edge_break", "edge_tool", "edge_size", "probe_dx", "probe_dy", "slot_center", "slot_allow", "surf",
            "surf_tool", "surf_depth", "surf_over", "surf_area", "surf_margin", "surf_x", "surf_y", "surf_w",
            "surf_h", "surf_pattern", "pocket_tool", "pocket_over", "engrave_tool", "finish", "finish_allow", "marks")

# Single-line font for engraving typed text: each character is strokes on a grid 4 wide and 6 tall
# (capitals; lower case is cut as capitals). One line per stroke: "x,y x,y ...", strokes split by "|".
FONT = {
    "A": "0,0 2,6 4,0|0.67,2 3.33,2",
    "B": "0,0 0,6 3,6 4,5.2 4,3.8 3,3 0,3|3,3 4,2.2 4,0.8 3,0 0,0",
    "C": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1",
    "D": "0,0 0,6 2.5,6 4,4.5 4,1.5 2.5,0 0,0",
    "E": "4,6 0,6 0,0 4,0|0,3 3,3",
    "F": "4,6 0,6 0,0|0,3 3,3",
    "G": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1 4,3 2.5,3",
    "H": "0,0 0,6|4,0 4,6|0,3 4,3",
    "I": "1,6 3,6|2,6 2,0|1,0 3,0",
    "J": "4,6 4,1 3,0 1,0 0,1",
    "K": "0,0 0,6|4,6 0,2|1.3,3.3 4,0",
    "L": "0,6 0,0 4,0",
    "M": "0,0 0,6 2,3 4,6 4,0",
    "N": "0,0 0,6 4,0 4,6",
    "O": "1,0 0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0",
    "P": "0,0 0,6 3,6 4,5 4,4 3,3 0,3",
    "Q": "1,0 0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0|2.5,1.5 4,0",
    "R": "0,0 0,6 3,6 4,5 4,4 3,3 0,3|2,3 4,0",
    "S": "4,5 3,6 1,6 0,5 0,4 1,3 3,3 4,2 4,1 3,0 1,0 0,1",
    "T": "0,6 4,6|2,6 2,0",
    "U": "0,6 0,1 1,0 3,0 4,1 4,6",
    "V": "0,6 2,0 4,6",
    "W": "0,6 1,0 2,4 3,0 4,6",
    "X": "0,0 4,6|0,6 4,0",
    "Y": "0,6 2,3 4,6|2,3 2,0",
    "Z": "0,6 4,6 0,0 4,0",
    "0": "1,0 0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0|1,1 3,5",
    "1": "1,5 2,6 2,0|1,0 3,0",
    "2": "0,5 1,6 3,6 4,5 4,4 0,0 4,0",
    "3": "0,5 1,6 3,6 4,5 4,4 3,3 1.5,3|3,3 4,2 4,1 3,0 1,0 0,1",
    "4": "3,0 3,6 0,2 4,2",
    "5": "4,6 0,6 0,3.5 3,3.5 4,2.5 4,1 3,0 1,0 0,1",
    "6": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1 4,2.5 3,3.5 0,3.5",
    "7": "0,6 4,6 1.5,0",
    "8": "1,3 0,4 0,5 1,6 3,6 4,5 4,4 3,3 1,3 0,2 0,1 1,0 3,0 4,1 4,2 3,3",
    "9": "0,1 1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,3.5 1,2.5 4,2.5",
    "-": "0.5,3 3.5,3",
    "+": "2,1 2,5|0,3 4,3",
    "=": "0,2 4,2|0,4 4,4",
    ".": "1.7,0 2.3,0 2.3,0.6 1.7,0.6 1.7,0",
    ",": "2.3,0.6 2.3,0 1.6,-1",
    ":": "1.7,1 2.3,1 2.3,1.6 1.7,1.6 1.7,1|1.7,4 2.3,4 2.3,4.6 1.7,4.6 1.7,4",
    "/": "0,0 4,6",
    "#": "1.2,0.5 1.7,5.5|2.7,0.5 3.2,5.5|0,2 4,2|0,4 4,4",
    "(": "3,6 2,5 1.5,3 2,1 3,0",
    ")": "1,6 2,5 2.5,3 2,1 1,0",
    "_": "0,-0.6 4,-0.6",
    "'": "2,6 2,4.5",
    "\"": "1.3,6 1.3,4.5|2.7,6 2.7,4.5",
    "°": "1.5,6 2.5,6 2.5,5 1.5,5 1.5,6",
    "&": "4,0 1,4.5 1,5.5 1.5,6 2.5,6 3,5.5 3,4.5 0,2 0,1 1,0 2.5,0 4,2",
}


def text_strokes(text, h, x, y, angle):
    """Strokes (open lines, mm) for typed text: h = capital height, (x, y) = lower left of the last line,
    lines split by "|" stack upward. Returns (strokes, characters that have no shape)."""
    sc = h / 6.0
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    lines = [ln.strip() for ln in text.split("|")]
    out, missing = [], set()
    for li, ln in enumerate(lines):
        by = (len(lines) - 1 - li) * 10.0   # line spacing: 10 grid units
        cx = 0.0
        for ch in ln.upper():
            if ch == " ":
                cx += 4.0
                continue
            g = FONT.get(ch)
            if g is None:
                missing.add(ch)
                cx += 5.5
                continue
            for stroke in g.split("|"):
                pts = []
                for pair in stroke.split():
                    u, w = (float(t) for t in pair.split(","))
                    px, py = (cx + u) * sc, (by + w) * sc
                    pts.append((x + px * ca - py * sa, y + px * sa + py * ca))
                out.append(pts)
            cx += 5.5
    return out, missing


# ------------------------------------------------------------ system fonts and logos

SINGLE_LINE = "Single line (engraving)"
LOGO_DIR = os.path.join(HERE, "logos")
LOGOS = {"AGILITE scorpion": "agilite-scorpion", "AGILITE wordmark": "agilite-wordmark",
         "AGILITE stacked": "agilite-stacked"}
CUT_STYLES_LINE = ["On the line"]
CUT_STYLES_SHAPE = ["Outline", "Filled"]
ALIGNS = ["Left", "Centre", "Right"]
_PINNED_FONTS = {"bankgothicbt-light": "Bank Gothic Light", "bank gothic light": "Bank Gothic Light",
                 "bankgothic bold": "BankGothic Bold"}
_font_list = None
_font_cache = {}
_logo_cache = {}


def list_fonts():
    """{family: {"regular": path, "bold": path, ...}} of the installed .ttf/.otf fonts (listed as APF+ lists
    them), Bank Gothic first."""
    global _font_list
    if _font_list is not None:
        return _font_list
    entries = []
    try:
        import winreg
        windir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                k = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            except OSError:
                continue
            with k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    name, value, _ = winreg.EnumValue(k, i)
                    p = str(value)
                    entries.append((str(name), p if os.path.isabs(p) else os.path.join(windir, p)))
    except ImportError:
        pass
    fams = {}
    for reg, path in entries:
        if not path.lower().endswith((".ttf", ".otf")):
            continue
        name = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", reg).strip()
        fam, face = name, "regular"
        if name.lower() in _PINNED_FONTS:
            fam = _PINNED_FONTS[name.lower()]
        else:
            for suf, fc in (("Bold Italic", "bolditalic"), ("Bold Oblique", "bolditalic"), ("Bold", "bold"),
                            ("Italic", "italic"), ("Oblique", "italic"), ("Regular", "regular")):
                if name.endswith(" " + suf):
                    fam, face = name[:-len(suf) - 1].strip(), fc
                    break
        fams.setdefault(fam, {}).setdefault(face, path)
    for f in fams.values():
        f.setdefault("regular", next(iter(f.values())))
    pinned = [f for f in dict.fromkeys(_PINNED_FONTS.values()) if f in fams]
    order = pinned + sorted((f for f in fams if f not in pinned), key=str.lower)
    _font_list = {f: fams[f] for f in order}
    return _font_list


class FontFile:
    """Glyph outlines of a .ttf (quadratic) or .otf/CFF (cubic) font, read with the standard library.
    Outlines stay in font units as path commands, flattened once a size is known."""

    def __init__(self, path):
        import struct
        self.S = struct
        with open(path, "rb") as f:
            d = f.read()
        self.d = d
        n = struct.unpack(">H", d[4:6])[0]
        self.t = {}
        for i in range(n):
            tg, _, off, ln = struct.unpack(">4sIII", d[12 + 16 * i:28 + 16 * i])
            self.t[tg.decode("latin-1")] = (off, ln)
        h = self.t["head"][0]
        self.upem = struct.unpack(">H", d[h + 18:h + 20])[0]
        self.loca_long = struct.unpack(">h", d[h + 50:h + 52])[0] == 1
        self.nglyphs = struct.unpack(">H", d[self.t["maxp"][0] + 4:self.t["maxp"][0] + 6])[0]
        hh = self.t["hhea"][0]
        nmet = struct.unpack(">H", d[hh + 34:hh + 36])[0]
        hm = self.t["hmtx"][0]
        adv = [struct.unpack(">H", d[hm + 4 * i:hm + 4 * i + 2])[0] for i in range(nmet)]
        self.adv = adv + [adv[-1]] * max(0, self.nglyphs - nmet)
        self._cmap()
        self.glyphs = {}
        self.cff = None
        if "CFF " in self.t:
            self._cff_init()
        cap = 0
        if "OS/2" in self.t:
            o = self.t["OS/2"][0]
            ver = struct.unpack(">H", d[o:o + 2])[0]
            if ver >= 2 and self.t["OS/2"][1] >= 90:
                cap = struct.unpack(">h", d[o + 88:o + 90])[0]
        if cap <= 0:
            pts = [seg[-1] for c in self.outline(self.gid("H")) for seg in c]
            cap = max((p[1] for p in pts), default=0) or 0.7 * self.upem
        self.cap = cap

    # -- character to glyph
    def _cmap(self):
        S, d = self.S, self.d
        c = self.t["cmap"][0]
        n = S.unpack(">H", d[c + 2:c + 4])[0]
        best = None
        for i in range(n):
            pid, eid, off = S.unpack(">HHI", d[c + 4 + 8 * i:c + 12 + 8 * i])
            fmt = S.unpack(">H", d[c + off:c + off + 2])[0]
            rank = {(3, 10): 0, (0, 4): 1, (3, 1): 2, (0, 3): 3, (0, 1): 4, (3, 0): 5}.get((pid, eid), 9)
            if fmt in (4, 12) and (best is None or rank < best[0]):
                best = (rank, c + off, fmt)
        self.map = {}
        if best is None:
            return
        _, o, fmt = best
        if fmt == 12:
            ng = S.unpack(">I", d[o + 12:o + 16])[0]
            for g in range(ng):
                a, b, gid = S.unpack(">III", d[o + 16 + 12 * g:o + 28 + 12 * g])
                if a > 0x2FFF:
                    continue
                for k in range(a, min(b, 0x2FFF) + 1):
                    self.map[k] = gid + k - a
            return
        seg = S.unpack(">H", d[o + 6:o + 8])[0] // 2
        ends = S.unpack(f">{seg}H", d[o + 14:o + 14 + 2 * seg])
        st = o + 16 + 2 * seg
        starts = S.unpack(f">{seg}H", d[st:st + 2 * seg])
        deltas = S.unpack(f">{seg}h", d[st + 2 * seg:st + 4 * seg])
        ro = st + 4 * seg
        ranges = S.unpack(f">{seg}H", d[ro:ro + 2 * seg])
        for i in range(seg):
            if starts[i] > 0x2FFF:
                continue
            for ch in range(starts[i], min(ends[i], 0x2FFF) + 1):
                if ranges[i] == 0:
                    g = (ch + deltas[i]) & 0xFFFF
                else:
                    a = ro + 2 * i + ranges[i] + 2 * (ch - starts[i])
                    g = S.unpack(">H", d[a:a + 2])[0]
                    g = (g + deltas[i]) & 0xFFFF if g else 0
                self.map[ch] = g

    def gid(self, ch):
        return self.map.get(ord(ch), 0)

    # -- outlines: contours of segments ("M", p) ("L", p) ("Q", c, p) ("C", c1, c2, p)
    def outline(self, g):
        if g not in self.glyphs:
            try:
                self.glyphs[g] = self._cff_glyph(g) if self.cff else self._tt_glyph(g, 0)
            except Exception:
                self.glyphs[g] = []
        return self.glyphs[g]

    def _tt_glyph(self, g, depth):
        S, d = self.S, self.d
        lo = self.t["loca"][0]
        if self.loca_long:
            a, b = S.unpack(">II", d[lo + 4 * g:lo + 4 * g + 8])
        else:
            a, b = (2 * v for v in S.unpack(">HH", d[lo + 2 * g:lo + 2 * g + 4]))
        if a == b:
            return []
        o = self.t["glyf"][0] + a
        nc = S.unpack(">h", d[o:o + 2])[0]
        if nc < 0:   # composite: its parts, moved by their offsets
            out, p = [], o + 10
            while depth < 6:
                fl, gi = S.unpack(">HH", d[p:p + 4])
                p += 4
                if fl & 1:
                    dx, dy = S.unpack(">hh", d[p:p + 4])
                    p += 4
                else:
                    dx, dy = S.unpack(">bb", d[p:p + 2])
                    p += 2
                sx = sy = 1.0
                if fl & 8:
                    sx = sy = S.unpack(">h", d[p:p + 2])[0] / 16384
                    p += 2
                elif fl & 0x40:
                    sx, sy = (v / 16384 for v in S.unpack(">hh", d[p:p + 4]))
                    p += 4
                elif fl & 0x80:
                    sx, _, _, sy = (v / 16384 for v in S.unpack(">hhhh", d[p:p + 8]))
                    p += 8
                if not fl & 2:
                    dx = dy = 0
                for c in self._tt_glyph(gi, depth + 1):
                    out.append([(seg[0],) + tuple((q[0] * sx + dx, q[1] * sy + dy) for q in seg[1:]) for seg in c])
                if not fl & 0x20:
                    break
            return out
        ends = S.unpack(f">{nc}H", d[o + 10:o + 10 + 2 * nc])
        p = o + 10 + 2 * nc
        il = S.unpack(">H", d[p:p + 2])[0]
        p += 2 + il
        npts = ends[-1] + 1 if nc else 0
        flags = []
        while len(flags) < npts:
            f = d[p]
            p += 1
            flags.append(f)
            if f & 8:
                r = d[p]
                p += 1
                flags += [f] * r
        xs, ys = [], []
        for arr, short, same in ((xs, 2, 16), (ys, 4, 32)):
            v = 0
            for f in flags[:npts]:
                if f & short:
                    dv = d[p]
                    p += 1
                    v += dv if f & same else -dv
                elif not f & same:
                    v += S.unpack(">h", d[p:p + 2])[0]
                    p += 2
                arr.append(v)
        out, s = [], 0
        for e in ends:
            pts = [((xs[i], ys[i]), bool(flags[i] & 1)) for i in range(s, e + 1)]
            s = e + 1
            if len(pts) < 2:
                continue
            k = next((i for i, (_, on) in enumerate(pts) if on), None)
            if k is None:   # all off-curve: start at the midpoint of the first two
                a0, a1 = pts[0][0], pts[1][0]
                pts = [(((a0[0] + a1[0]) / 2, (a0[1] + a1[1]) / 2), True)] + pts[1:] + pts[:1]
            else:
                pts = pts[k:] + pts[:k]
            c = [("M", pts[0][0])]
            ctrl = None
            for q, on in pts[1:] + pts[:1]:
                if on:
                    c.append(("Q", ctrl, q) if ctrl else ("L", q))
                    ctrl = None
                elif ctrl:
                    mid = ((ctrl[0] + q[0]) / 2, (ctrl[1] + q[1]) / 2)
                    c.append(("Q", ctrl, mid))
                    ctrl = q
                else:
                    ctrl = q
            out.append(c)
        return out

    # -- CFF (.otf): Type 2 charstrings
    def _index(self, o):
        S, d = self.S, self.d
        n = S.unpack(">H", d[o:o + 2])[0]
        if n == 0:
            return [], o + 2
        osz = d[o + 2]
        offs = [int.from_bytes(d[o + 3 + i * osz:o + 3 + (i + 1) * osz], "big") for i in range(n + 1)]
        base = o + 3 + (n + 1) * osz - 1
        return [d[base + offs[i]:base + offs[i + 1]] for i in range(n)], base + offs[-1]

    @staticmethod
    def _dict(b):
        out, ops, i = {}, [], 0
        while i < len(b):
            b0 = b[i]
            if b0 <= 21:
                op = b0
                i += 1
                if b0 == 12:
                    op = 1200 + b[i]
                    i += 1
                out[op] = ops
                ops = []
            elif b0 == 28:
                ops.append(int.from_bytes(b[i + 1:i + 3], "big", signed=True))
                i += 3
            elif b0 == 29:
                ops.append(int.from_bytes(b[i + 1:i + 5], "big", signed=True))
                i += 5
            elif b0 == 30:
                s, i, done = "", i + 1, False
                while not done:
                    byte = b[i]
                    i += 1
                    for nib in (byte >> 4, byte & 15):
                        if nib == 15:
                            done = True
                            break
                        s += {10: ".", 11: "E", 12: "E-", 14: "-"}.get(nib, str(nib) if nib < 10 else "")
                try:
                    ops.append(float(s))
                except ValueError:
                    ops.append(0.0)
            elif 32 <= b0 <= 246:
                ops.append(b0 - 139)
                i += 1
            elif 247 <= b0 <= 250:
                ops.append((b0 - 247) * 256 + b[i + 1] + 108)
                i += 2
            elif 251 <= b0 <= 254:
                ops.append(-(b0 - 251) * 256 - b[i + 1] - 108)
                i += 2
            else:
                i += 1
        return out

    def _cff_init(self):
        o = self.t["CFF "][0]
        d = self.d
        p = o + d[o + 2]
        _, p = self._index(p)              # names
        tops, p = self._index(p)           # top dicts
        _, p = self._index(p)              # strings
        self.gsubrs, p = self._index(p)
        top = self._dict(tops[0])
        self.charstrings, _ = self._index(o + int(top[17][0]))
        self.fd_subrs, self.fd_select = [], None

        def private(pr):
            size, off = (int(v) for v in pr)
            pd = self._dict(d[o + off:o + off + size])
            return self._index(o + off + int(pd[19][0]))[0] if 19 in pd else []
        if 1236 in top:   # CID-keyed: one private dict (and so subroutines) per font of the FDArray
            fds, _ = self._index(o + int(top[1236][0]))
            self.fd_subrs = [private(self._dict(f)[18]) if 18 in self._dict(f) else [] for f in fds]
            fs = o + int(top[1237][0])
            if d[fs] == 0:
                self.fd_select = list(d[fs + 1:fs + 1 + len(self.charstrings)])
            else:
                nr = int.from_bytes(d[fs + 1:fs + 3], "big")
                sel = [0] * len(self.charstrings)
                for r in range(nr):
                    a = int.from_bytes(d[fs + 3 + 3 * r:fs + 5 + 3 * r], "big")
                    fd = d[fs + 5 + 3 * r]
                    b = int.from_bytes(d[fs + 6 + 3 * r:fs + 8 + 3 * r], "big")
                    for gi in range(a, min(b, len(sel))):
                        sel[gi] = fd
                self.fd_select = sel
            self.subrs = []
        else:
            self.subrs = private(top[18]) if 18 in top else []
        self.cff = True

    def _cff_glyph(self, g):
        subrs = self.fd_subrs[self.fd_select[g]] if self.fd_select else self.subrs
        gs = self.gsubrs

        def bias(n):
            return 107 if n < 1240 else 1131 if n < 33900 else 32768
        st = {"x": 0.0, "y": 0.0, "stems": 0, "w": False, "done": False}
        stk = []
        cons = []

        def move(dx, dy):
            st["x"] += dx
            st["y"] += dy
            cons.append([("M", (st["x"], st["y"]))])

        def line(dx, dy):
            st["x"] += dx
            st["y"] += dy
            if cons:
                cons[-1].append(("L", (st["x"], st["y"])))

        def curve(a, b, c, e, f, g_):
            x0, y0 = st["x"], st["y"]
            c1 = (x0 + a, y0 + b)
            c2 = (c1[0] + c, c1[1] + e)
            st["x"], st["y"] = c2[0] + f, c2[1] + g_
            if cons:
                cons[-1].append(("C", c1, c2, (st["x"], st["y"])))

        def width(n_args):   # the first stack-clearing operator may carry the glyph width first
            if not st["w"]:
                st["w"] = True
                if len(stk) > n_args:
                    stk.pop(0)

        def run(code, depth=0):
            i = 0
            while i < len(code) and not st["done"]:
                b0 = code[i]
                if b0 == 28:
                    stk.append(int.from_bytes(code[i + 1:i + 3], "big", signed=True))
                    i += 3
                    continue
                if b0 >= 32:
                    if b0 <= 246:
                        stk.append(b0 - 139)
                        i += 1
                    elif b0 <= 250:
                        stk.append((b0 - 247) * 256 + code[i + 1] + 108)
                        i += 2
                    elif b0 <= 254:
                        stk.append(-(b0 - 251) * 256 - code[i + 1] - 108)
                        i += 2
                    else:
                        stk.append(int.from_bytes(code[i + 1:i + 5], "big", signed=True) / 65536)
                        i += 5
                    continue
                i += 1
                op = b0
                if op == 12:
                    op = 1200 + code[i]
                    i += 1
                if op in (1, 3, 18, 23, 19, 20):
                    if not st["w"] and len(stk) % 2:
                        stk.pop(0)
                    st["w"] = True
                    st["stems"] += len(stk) // 2
                    stk.clear()
                    if op in (19, 20):
                        i += (st["stems"] + 7) // 8
                elif op == 21:
                    width(2)
                    move(stk[-2], stk[-1])
                    stk.clear()
                elif op == 22:
                    width(1)
                    move(stk[-1], 0)
                    stk.clear()
                elif op == 4:
                    width(1)
                    move(0, stk[-1])
                    stk.clear()
                elif op == 5:
                    for k in range(0, len(stk) - 1, 2):
                        line(stk[k], stk[k + 1])
                    stk.clear()
                elif op in (6, 7):
                    hz = op == 6
                    for v in stk:
                        if hz:
                            line(v, 0)
                        else:
                            line(0, v)
                        hz = not hz
                    stk.clear()
                elif op == 8:
                    for k in range(0, len(stk) - 5, 6):
                        curve(*stk[k:k + 6])
                    stk.clear()
                elif op == 27:
                    k, dy1 = 0, 0
                    if len(stk) % 4:
                        dy1, k = stk[0], 1
                    while k + 3 < len(stk):
                        curve(stk[k], dy1, stk[k + 1], stk[k + 2], stk[k + 3], 0)
                        dy1, k = 0, k + 4
                    stk.clear()
                elif op == 26:
                    k, dx1 = 0, 0
                    if len(stk) % 4:
                        dx1, k = stk[0], 1
                    while k + 3 < len(stk):
                        curve(dx1, stk[k], stk[k + 1], stk[k + 2], 0, stk[k + 3])
                        dx1, k = 0, k + 4
                    stk.clear()
                elif op in (30, 31):
                    hz = op == 31
                    k = 0
                    while len(stk) - k >= 4:
                        last = len(stk) - k == 5
                        a = stk[k:k + 5] + [0]
                        if hz:
                            curve(a[0], 0, a[1], a[2], a[4] if last else 0, a[3])
                        else:
                            curve(0, a[0], a[1], a[2], a[3], a[4] if last else 0)
                        k += 5 if last else 4
                        hz = not hz
                    stk.clear()
                elif op == 24:
                    k = 0
                    while len(stk) - k >= 8:
                        curve(*stk[k:k + 6])
                        k += 6
                    line(stk[k], stk[k + 1])
                    stk.clear()
                elif op == 25:
                    k = 0
                    while len(stk) - k >= 8:
                        line(stk[k], stk[k + 1])
                        k += 2
                    curve(*stk[k:k + 6])
                    stk.clear()
                elif op in (10, 29):
                    sub = subrs if op == 10 else gs
                    idx = int(stk.pop()) + bias(len(sub))
                    if 0 <= idx < len(sub) and depth < 12:
                        run(sub[idx], depth + 1)
                elif op == 11:
                    return
                elif op == 14:
                    width(0)
                    st["done"] = True
                    stk.clear()
                elif op == 1235:   # flex
                    curve(*stk[0:6])
                    curve(*stk[6:12])
                    stk.clear()
                elif op == 1234:   # hflex
                    a = list(stk)
                    curve(a[0], 0, a[1], a[2], a[3], 0)
                    curve(a[4], 0, a[5], -a[2], a[6], 0)
                    stk.clear()
                elif op == 1236:   # hflex1
                    a = list(stk)
                    curve(a[0], a[1], a[2], a[3], a[4], 0)
                    curve(a[5], 0, a[6], a[7], a[8], -(a[1] + a[3] + a[7]))
                    stk.clear()
                elif op == 1237:   # flex1
                    a = list(stk)
                    dx = a[0] + a[2] + a[4] + a[6] + a[8]
                    dy = a[1] + a[3] + a[5] + a[7] + a[9]
                    if abs(dx) > abs(dy):
                        last = (a[10], -dy)
                    else:
                        last = (-dx, a[10])
                    curve(*a[0:6])
                    curve(a[6], a[7], a[8], a[9], *last)
                    stk.clear()
                else:
                    stk.clear()
        run(self.charstrings[g])
        return [c for c in cons if len(c) > 1]


def _flatten(cmds, sc, tol=0.02):
    """Path commands (font units or mm) to points, scaled by sc, curves cut finely enough for about tol mm."""
    pts = []
    cur = (0.0, 0.0)
    for seg in cmds:
        k = seg[0]
        if k in ("M", "L"):
            cur = seg[1]
            pts.append((cur[0] * sc, cur[1] * sc))
            continue
        ctrl = [cur] + list(seg[1:])
        L = sum(_d(ctrl[i], ctrl[i + 1]) for i in range(len(ctrl) - 1)) * sc
        n = max(2, min(48, int(math.sqrt(L / max(tol, 1e-3))) + 1))
        for j in range(1, n + 1):
            t = j / n
            u = 1 - t
            if k == "Q":
                a, b, c = ctrl
                x = u * u * a[0] + 2 * u * t * b[0] + t * t * c[0]
                y = u * u * a[1] + 2 * u * t * b[1] + t * t * c[1]
            else:
                a, b, c, e = ctrl
                x = u ** 3 * a[0] + 3 * u * u * t * b[0] + 3 * u * t * t * c[0] + t ** 3 * e[0]
                y = u ** 3 * a[1] + 3 * u * u * t * b[1] + 3 * u * t * t * c[1] + t ** 3 * e[1]
            pts.append((x * sc, y * sc))
        cur = seg[-1]
    return _dedupe(pts, 1e-6)


def get_font(path):
    if path not in _font_cache:
        try:
            _font_cache[path] = FontFile(path)
        except Exception:
            _font_cache[path] = None
    return _font_cache[path]


def load_logo(name):
    """A logo's fill paths in mm (y up), from logos/<file>.json - the same files APF+ uses."""
    if name not in _logo_cache:
        try:
            with open(os.path.join(LOGO_DIR, LOGOS[name] + ".json"), encoding="utf-8") as f:
                _logo_cache[name] = json.load(f)
        except (OSError, ValueError, KeyError):
            _logo_cache[name] = None
    return _logo_cache[name]


def _logo_cmds(sp):
    out = []
    for c in sp:
        if c[0] in ("M", "L"):
            out.append((c[0], (c[1], c[2])))
        elif c[0] == "C":
            out.append(("C", (c[1], c[2]), (c[3], c[4]), (c[5], c[6])))
    return out


_shape_cache = {}


def mark_shapes(m):
    """A mark (typed text or logo) as ([(points, closed)], width, height, problems), centred on (0, 0),
    before it is turned and moved."""
    key = json.dumps({k: m.get(k) for k in ("type", "text", "font", "bold", "size", "logo", "registered", "align")},
                     sort_keys=True)
    if key in _shape_cache:
        return _shape_cache[key]
    res = _mark_shapes(m)
    if len(_shape_cache) > 64:
        _shape_cache.clear()
    _shape_cache[key] = res
    return res


def _mark_shapes(m):
    problems = []
    try:
        size = max(0.1, float(str(m.get("size", 5)).replace(",", ".")))
    except ValueError:
        return [], 0, 0, ["A mark's size is not a number."]
    shapes = []
    if m.get("type") == "logo":
        logo = load_logo(m.get("logo", ""))
        if not logo:
            return [], 0, 0, [f"The logo file for \"{m.get('logo')}\" is missing from the logos folder."]
        fills = logo["mark"] + ((logo.get("registered") or []) if m.get("registered", True) else [])
        xs = [c[k] for f in fills for sp in f["subpaths"] for c in sp if c[0] != "Z" for k in range(1, len(c), 2)]
        sc = size / max(1e-9, max(xs) - min(xs))   # a logo's size is its width
        shapes = [(_flatten(_logo_cmds(sp), sc, 0.01), True) for f in fills for sp in f["subpaths"]]
        shapes = [(p, c) for p, c in shapes if len(p) >= 3]
    else:
        text = str(m.get("text", "")).replace("|", "\n")
        lines = [ln.rstrip() for ln in text.split("\n")]
        font = m.get("font", SINGLE_LINE)
        per_line = []
        if font == SINGLE_LINE:
            pitch = size * 10 / 6
            for li, ln in enumerate(lines):
                strokes, miss = text_strokes(ln, size, 0, -li * pitch, 0)
                problems += sorted(miss)
                per_line.append(([(s, False) for s in strokes], len(ln.rstrip()) * 5.5 * size / 6 - 1.5 * size / 6))
        else:
            faces = list_fonts().get(font)
            path = None
            if faces:
                path = faces.get("bold") if m.get("bold") and faces.get("bold") else faces.get("regular")
            ff = get_font(path) if path else None
            if ff is None:
                return [], 0, 0, [f"The font \"{font}\" could not be read; pick another one."]
            sc = size / ff.cap
            pitch = size * 1.5
            for li, ln in enumerate(lines):
                x, row_ = 0.0, []
                for ch in ln:
                    g = ff.gid(ch)
                    if g == 0 and not ch.isspace():
                        problems.append(ch)
                    for c in ff.outline(g):
                        moved = [(s[0],) + tuple((q[0] + x, q[1]) for q in s[1:]) for s in c]
                        pts = [(px, py - li * pitch) for px, py in _flatten(moved, sc, 0.01)]
                        if len(pts) >= 3:
                            row_.append((pts, True))
                    x += ff.adv[g]
                per_line.append((row_, x * sc))
        wmax = max((w for _, w in per_line), default=0)
        al = m.get("align", "Centre")
        for row_, w in per_line:
            dx = 0.0 if al == "Left" else (wmax - w) * (0.5 if al == "Centre" else 1.0)
            shapes += [([(px + dx, py) for px, py in s], c) for s, c in row_]
        if problems:
            problems = ["These characters are not in the font and are left as gaps: "
                        + " ".join(sorted(set(problems)))]
    pts = [p for s, _ in shapes for p in s]
    if not pts:
        return [], 0, 0, problems
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [([(px - cx, py - cy) for px, py in s], c) for s, c in shapes], x1 - x0, y1 - y0, problems


def mark_num(m, k, default=0.0):
    try:
        return float(str(m.get(k, default)).replace(",", "."))
    except ValueError:
        return default


def place_mark(shapes, m):
    """Shapes centred on (0, 0), turned by the mark's angle and moved to its X Y (the centre of its box)."""
    a = math.radians(mark_num(m, "angle"))
    ca, sa = math.cos(a), math.sin(a)
    x, y = mark_num(m, "x"), mark_num(m, "y")
    return [([(x + px * ca - py * sa, y + px * sa + py * ca) for px, py in s], c) for s, c in shapes]


def new_mark(kind, x=0.0, y=0.0):
    if kind == "logo":
        return {"type": "logo", "logo": "AGILITE scorpion", "registered": True, "size": 30.0, "x": x, "y": y,
                "angle": 0.0, "cut": "Outline", "depth": 0.2}
    return {"type": "text", "text": "AGILITE", "font": SINGLE_LINE, "bold": False, "size": 5.0, "x": x, "y": y,
            "angle": 0.0, "align": "Centre", "cut": "On the line", "depth": 0.2}


LAYER_OPS = ["Cut through", "Pocket", "Engrave on line", "Skip"]
SURF_AREAS = ["Drawing + margin", "Custom rectangle"]
SURF_PATTERNS = ["Back and forth", "One direction"]
MAIN = "Same as outlines"
MAIN_OLD = "(main tool)"   # (how older settings say it)
TOOL_KEYS = ("tool_name", "hole_tool", "edge_tool", "surf_tool", "pocket_tool", "engrave_tool", "chamfer_tool")
VBIT = "V-bit / chamfer"
SURF = "Surfacing bit"   # Agilite CNC: a wide flat insert cutter, for surfacing only (cuts like a flat end mill)
SPEED_KEYS = ("rpm", "feed", "ramp_feed", "plunge_feed", "step")
OLD_TC_CODES = ("M6 T{tool}; M0 (Check the new tool and Z, then Cycle Start)",)
TC_MODES = ["One file per tool", "One file, pause and measure"]
# Stepcraft homes with no back-off, so machine 0 sits on the switch: park 3 mm inside it
END_Z = ["Safe height", "Custom height", "Machine top (G53 Z-3)"]
END_XY = ["X0 Y0", "Custom X Y", "Stay where it ends", "Machine corner (G53 X3 Y3)"]
OP_NAMES = {"surface": "surfacing", "pocket": "pockets", "engrave": "engraving", "hole": "holes and slots",
            "part": "outlines", "edge": "edge break", "chamfer": "chamfer"}
TOOLS_FILE = os.path.join(HERE, "G-code Maker tools.json")
DEFAULT_TOOLS = [
    {"name": "4 mm ball nose 2F carbide", "tool_d": 4.0, "tool_type": "Ball nose", "flutes": 2, "tool_mat": "Carbide"},
    {"name": "3.175 mm flat 1F carbide (aluminium)", "tool_d": 3.175, "tool_type": "Flat end mill", "flutes": 1, "tool_mat": "Carbide"},
    {"name": "2 mm flat 2F carbide", "tool_d": 2.0, "tool_type": "Flat end mill", "flutes": 2, "tool_mat": "Carbide"},
    {"name": "3 mm flat 2F carbide", "tool_d": 3.0, "tool_type": "Flat end mill", "flutes": 2, "tool_mat": "Carbide"},
    {"name": "4 mm flat 2F carbide", "tool_d": 4.0, "tool_type": "Flat end mill", "flutes": 2, "tool_mat": "Carbide"},
    {"name": "6 mm flat 2F carbide", "tool_d": 6.0, "tool_type": "Flat end mill", "flutes": 2, "tool_mat": "Carbide"},
    {"name": "6 mm 90 deg V-bit carbide", "tool_d": 6.0, "tool_type": VBIT, "flutes": 2, "tool_mat": "Carbide",
     "tool_angle": 90.0},
]
TOOL_FIELDS = ("tool_d", "tool_type", "flutes", "tool_mat", "tool_angle")
# Starting values for a light machine like the M700, carbide tool, full-width cut:
#   vc   = cutting speed range, m/min
#   fz   = chip per tooth, per mm of tool diameter (4 mm tool in aluminium -> 0.016 mm)
#   fmin = smallest sensible chip per tooth, mm
#   step = depth per pass, per mm of tool diameter
MATERIALS = {
    "Aluminium": {"vc": (150, 250), "fz": 0.004, "fmin": 0.008, "step": 0.08},
    "Brass / bronze": {"vc": (100, 200), "fz": 0.004, "fmin": 0.008, "step": 0.08},
    "Plastic (POM, acrylic, PE)": {"vc": (200, 400), "fz": 0.010, "fmin": 0.02, "step": 0.25},
    "Wood / MDF / plywood": {"vc": (300, 600), "fz": 0.012, "fmin": 0.02, "step": 0.5},
    "Carbon fibre / G10": {"vc": (150, 250), "fz": 0.005, "fmin": 0.01, "step": 0.2},
    "Mild steel": {"vc": (50, 90), "fz": 0.0025, "fmin": 0.005, "step": 0.03},
}
TOOL_MATS = {"Carbide": 1.0, "HSS": 0.4}   # HSS runs at about 40 % of the carbide cutting speed
TOOL_TYPES = ["Ball nose", "Flat end mill", VBIT]
DIRECTIONS = ["Conventional", "Climb"]
ORIGINS = ["Lower-left of drawing", "Centre of drawing", "DXF origin (0,0)"]


# --------------------------------------------------------------------------- DXF

def _arc_steps(r, sweep):
    if r <= TOL:
        return 1
    step = 2 * math.acos(max(-1.0, 1 - TOL / r))
    return max(1, int(math.ceil(abs(sweep) / step)))


def _bulge(p0, p1, b):
    """Points after p0 up to and including p1 along a DXF bulge arc."""
    if abs(b) < 1e-9:
        return [p1]
    x0, y0 = p0
    x1, y1 = p1
    c = math.hypot(x1 - x0, y1 - y0)
    if c < 1e-12:
        return [p1]
    theta = 4 * math.atan(b)
    r = c / (2 * math.sin(abs(theta) / 2))
    d = math.sqrt(max(r * r - c * c / 4, 0.0))
    ux, uy = (x1 - x0) / c, (y1 - y0) / c
    side = 1 if (b > 0) == (abs(theta) < math.pi) else -1
    cx, cy = (x0 + x1) / 2 - uy * d * side, (y0 + y1) / 2 + ux * d * side
    a0 = math.atan2(y0 - cy, x0 - cx)
    n = _arc_steps(r, theta)
    pts = [(cx + r * math.cos(a0 + theta * i / n), cy + r * math.sin(a0 + theta * i / n)) for i in range(1, n)]
    return pts + [p1]


def _verts_to_pts(verts, closed):
    if not verts:
        return []
    pts = [(verts[0][0], verts[0][1])]
    n = len(verts)
    for i in range(n if closed else n - 1):
        a, b = verts[i], verts[(i + 1) % n]
        pts += _bulge((a[0], a[1]), (b[0], b[1]), a[2])
    if closed and len(pts) > 1:
        pts.pop()  # the last point repeats the first
    return pts


def _records(pairs):
    recs = []
    for c, v in pairs:
        if c == "0":
            recs.append({"type": v, "codes": []})
        elif recs:
            recs[-1]["codes"].append((c, v))
    return recs


def _get(codes, code, default=0.0):
    for c, v in codes:
        if c == code:
            try:
                return float(v)
            except ValueError:
                return default
    return default


def _layer(codes):
    for c, v in codes:
        if c == "8":
            return v
    return "0"


def _geom(rec):
    """(pts, closed) pieces of one entity, in its own coordinates."""
    t, codes = rec["type"], rec["codes"]
    if t == "LWPOLYLINE":
        verts = []
        for c, v in codes:
            if c == "10":
                verts.append([float(v), 0.0, 0.0])
            elif c == "20" and verts:
                verts[-1][1] = float(v)
            elif c == "42" and verts:
                verts[-1][2] = float(v)
        closed = int(_get(codes, "70", 0)) & 1
        return [(_verts_to_pts(verts, closed), bool(closed))]
    if t == "POLYLINE":
        verts = []
        for vr in rec.get("vertices", []):
            vc = vr["codes"]
            if int(_get(vc, "70", 0)) & 16:  # spline frame point, not on the curve
                continue
            verts.append([_get(vc, "10"), _get(vc, "20"), _get(vc, "42")])
        closed = int(_get(codes, "70", 0)) & 1
        return [(_verts_to_pts(verts, closed), bool(closed))]
    if t == "CIRCLE":
        cx, cy, r = _get(codes, "10"), _get(codes, "20"), _get(codes, "40")
        n = max(_arc_steps(r, 2 * math.pi), 12)
        return [([(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)], True)]
    if t == "ARC":
        cx, cy, r = _get(codes, "10"), _get(codes, "20"), _get(codes, "40")
        a0, a1 = math.radians(_get(codes, "50")), math.radians(_get(codes, "51"))
        sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
        n = _arc_steps(r, sweep)
        return [([(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)], False)]
    if t == "LINE":
        return [([(_get(codes, "10"), _get(codes, "20")), (_get(codes, "11"), _get(codes, "21"))], False)]
    return None


def read_dxf(path, skip_layers=()):
    """Every piece of the drawing: ([(points mm, closed, layer)], warnings)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read().splitlines()
    pairs = [(raw[i].strip(), raw[i + 1].strip()) for i in range(0, len(raw) - 1, 2)]
    warnings = []

    scale = 1.0
    for k, (c, v) in enumerate(pairs):
        if c == "9" and v == "$INSUNITS" and k + 1 < len(pairs):
            units = int(float(pairs[k + 1][1]))
            scale = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}.get(units, 1.0)
            if units == 1:
                warnings.append("The DXF is in inches; converted to mm.")
            break

    recs = _records(pairs)
    section, block = None, None
    blocks, top = {}, []
    i = 0
    while i < len(recs):
        r = recs[i]
        t = r["type"]
        if t == "SECTION":
            section = next((v for c, v in r["codes"] if c == "2"), None)
        elif t == "ENDSEC":
            section = None
        elif section == "BLOCKS" and t == "BLOCK":
            name = next((v for c, v in r["codes"] if c == "2"), "")
            block = {"base": (_get(r["codes"], "10"), _get(r["codes"], "20")), "ents": []}
            blocks[name] = block
        elif section == "BLOCKS" and t == "ENDBLK":
            block = None
        elif section in ("BLOCKS", "ENTITIES"):
            if t == "POLYLINE":
                r["vertices"] = []
                j = i + 1
                while j < len(recs) and recs[j]["type"] == "VERTEX":
                    r["vertices"].append(recs[j])
                    j += 1
                if j < len(recs) and recs[j]["type"] == "SEQEND":
                    j += 1
                i = j - 1
            if section == "BLOCKS" and block is not None:
                block["ents"].append(r)
            elif section == "ENTITIES":
                top.append(r)
        i += 1

    skip = {s.strip().lower() for s in skip_layers if s.strip()}
    shapes, unsupported = [], set()

    def walk(ents, xf, parent_layer, depth):
        for r in ents:
            layer = _layer(r["codes"])
            if layer == "0" and parent_layer:
                layer = parent_layer
            if layer.lower() in skip:
                continue
            if r["type"] == "INSERT":
                name = next((v for c, v in r["codes"] if c == "2"), "")
                b = blocks.get(name)
                if b is None or depth > 8:
                    continue
                px, py = _get(r["codes"], "10"), _get(r["codes"], "20")
                sx, sy = _get(r["codes"], "41", 1.0), _get(r["codes"], "42", 1.0)
                rot = math.radians(_get(r["codes"], "50"))
                bx, by = b["base"]
                cr, sr = math.cos(rot), math.sin(rot)

                def inner(p, xf=xf, px=px, py=py, sx=sx, sy=sy, bx=bx, by=by, cr=cr, sr=sr):
                    x, y = (p[0] - bx) * sx, (p[1] - by) * sy
                    return xf((px + x * cr - y * sr, py + x * sr + y * cr))
                walk(b["ents"], inner, layer, depth + 1)
                continue
            g = _geom(r)
            if g is None:
                if r["type"] not in ("VIEWPORT", "ATTDEF", "ATTRIB", "SEQEND", "VERTEX", "POINT", "DIMENSION"):
                    unsupported.add(r["type"])
                continue
            for pts, is_closed in g:
                pts = [xf(p) for p in pts]
                pts = [(x * scale, y * scale) for x, y in pts]
                if len(pts) < 2:
                    continue
                shapes.append((pts, is_closed, layer))

    walk(top, lambda p: p, None, 0)
    if unsupported:
        warnings.append("Not cut (unsupported in DXF): " + ", ".join(sorted(unsupported)) + ".")
    return shapes, warnings


def _clean_closed(cs):
    return [c for c in (_dedupe(c) for c in cs) if len(c) >= 3 and abs(area(c)) > 1e-6]


def load_dxf(path, skip_layers=()):
    """Closed contours (lists of points, mm) plus warnings, all layers together."""
    shapes, warnings = read_dxf(path, skip_layers)
    joined, left = _chain([p for p, c, _ in shapes if not c])
    if left:
        warnings.append(f"{len(left)} open line(s) do not close into a shape and are not cut.")
    return _clean_closed([p for p, c, _ in shapes if c] + joined), warnings


def default_layer_op(name):
    """A first guess from the layer's name; a number with mm in the name is the depth ("POCKET 1.5mm")."""
    n = name.lower()
    if "pocket" in n:
        op = "Pocket"
    elif any(w in n for w in ("engrav", "etch", "scribe", "mark", "text")):
        op = "Engrave on line"
    elif any(w in n for w in ("skip", "ignore", "construction", "dimension", "notes")):
        op = "Skip"
    else:
        op = "Cut through"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*mm", n)
    depth = m.group(1).replace(",", ".") if m else ("1.0" if op == "Pocket" else "0.2")
    return {"op": op, "depth": depth}


def split_layers(shapes, layer_ops):
    """Sorts the drawing by what each layer does.
    Returns cut contours [(pts, layer)], pockets {layer: [pts]}, engraving {layer: (loops, open paths)},
    layer stats {layer: [closed, open]} and warnings."""
    stats, by_layer = {}, {}
    for pts, closed, layer in shapes:
        stats.setdefault(layer, [0, 0])[0 if closed else 1] += 1
        by_layer.setdefault(layer, []).append((pts, closed))
    cut, pockets, engrave, warnings, loose = [], {}, {}, [], []
    for layer, items in by_layer.items():
        lo = layer_ops.get(layer) or default_layer_op(layer)
        op = lo.get("op", "Cut through")
        if op == "Skip":
            continue
        joined, left = _chain([p for p, c in items if not c])
        loops = _clean_closed([p for p, c in items if c] + joined)
        if op == "Engrave on line":
            engrave[layer] = (loops, [p for p in left if len(p) >= 2])
            continue
        if op == "Pocket":
            pockets[layer] = loops
        else:
            cut += [(c, layer) for c in loops]
        loose += [(p, layer, op) for p in left]
    if loose:   # a shape drawn in pieces on more than one layer: try joining across layers
        joined, left = _chain([p for p, _, _ in loose])
        for c in _clean_closed(joined):
            near = min(loose, key=lambda it: _d(it[0][0], c[0]))
            (pockets.setdefault(near[1], []).append(c) if near[2] == "Pocket" else cut.append((c, near[1])))
        if left:
            warnings.append(f"{len(left)} open line(s) do not close into a shape and are not cut "
                            f"(set their layer to Engrave on line to cut along them).")
    return cut, pockets, engrave, stats, warnings


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _chain(opens, tol=0.02):
    pieces = [list(p) for p in opens]
    closed, left = [], []
    while pieces:
        cur = pieces.pop()
        grown = True
        while grown:
            grown = False
            for k, p in enumerate(pieces):
                if _d(cur[-1], p[0]) < tol:
                    cur = cur + p[1:]
                elif _d(cur[-1], p[-1]) < tol:
                    cur = cur + p[::-1][1:]
                elif _d(cur[0], p[-1]) < tol:
                    cur = p + cur[1:]
                elif _d(cur[0], p[0]) < tol:
                    cur = p[::-1] + cur[1:]
                else:
                    continue
                pieces.pop(k)
                grown = True
                break
        if len(cur) > 3 and _d(cur[0], cur[-1]) < tol:
            closed.append(cur[:-1])
        else:
            left.append(cur)
    return closed, left


# ---------------------------------------------------------------------- geometry

def area(p):
    s = 0.0
    n = len(p)
    for i in range(n):
        x0, y0 = p[i]
        x1, y1 = p[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s / 2


def _dedupe(p, eps=1e-6):
    out = []
    for q in p:
        if not out or _d(q, out[-1]) > eps:
            out.append(q)
    while len(out) > 1 and _d(out[0], out[-1]) <= eps:
        out.pop()
    return out


def inside(pt, poly):
    x, y = pt
    res = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            if x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                res = not res
    return res


def _line_x(a, b, c, d):
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-12:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den
    return (a[0] + t * r[0], a[1] + t * r[1])


def _seg_x(a, b, c, d):
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-14:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * s[1] - qy * s[0]) / den
    u = (qx * r[1] - qy * r[0]) / den
    # a crossing exactly on a corner counts too (each segment owns its start, not its end),
    # or the offset keeps spikes that pass through a shared point
    e = 1e-9
    if -e <= t < 1 - e and -e <= u < 1 - e:
        return (a[0] + t * r[0], a[1] + t * r[1])
    return None


def _simplify(p, tol=0.003):
    """Drop points that sit within tol of the line between their neighbours (finely sampled curves)."""
    p = list(p)
    changed = True
    while changed and len(p) > 3:
        changed = False
        n = len(p)
        keep = [True] * n
        for k in range(n):
            if not keep[k - 1]:
                continue
            a, b, c = p[k - 1], p[k], p[(k + 1) % n]
            if _seg_dist(b, a, c) < tol and _d(a, c) < 1.0:
                keep[k] = False
                changed = True
        p = [q for q, kk in zip(p, keep) if kk]
    return p


def _raw_offset(p, d):
    """Offset of a counter-clockwise polygon: d > 0 outward, d < 0 inward (round corners)."""
    n = len(p)
    sh = []
    for i in range(n):
        x0, y0 = p[i]
        x1, y1 = p[(i + 1) % n]
        ln = math.hypot(x1 - x0, y1 - y0)
        nx, ny = (y1 - y0) / ln, -(x1 - x0) / ln
        sh.append(((x0 + d * nx, y0 + d * ny), (x1 + d * nx, y1 + d * ny), (x1 - x0, y1 - y0)))
    r = abs(d)
    out = []
    for k in range(n):
        a0, b0, e0 = sh[k - 1]
        a1, b1, e1 = sh[k]
        ang = math.atan2(e0[0] * e1[1] - e0[1] * e1[0], e0[0] * e1[0] + e0[1] * e1[1])
        if abs(ang) < 1e-9:
            out.append(b0)
            continue
        vx, vy = p[k]
        if ang * d > 0:  # the offset edges open a gap: fill it with an arc
            s0 = math.atan2(b0[1] - vy, b0[0] - vx)
            m = _arc_steps(r, ang)
            out.append(b0)
            for j in range(1, m):
                out.append((vx + r * math.cos(s0 + ang * j / m), vy + r * math.sin(s0 + ang * j / m)))
            out.append(a1)
        else:  # the offset edges overlap: meet at their crossing
            x = _line_x(a0, b0, a1, b1)
            if x is None or math.hypot(x[0] - vx, x[1] - vy) > 4 * r:
                out += [b0, a1]
            else:
                out.append(x)
    return _dedupe(out, 1e-5)


def _first_cross(q):
    n = len(q)
    boxes = []
    for i in range(n):
        a, b = q[i], q[(i + 1) % n]
        boxes.append((min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])))
    for i in range(n):
        bi = boxes[i]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            bj = boxes[j]
            if bj[0] > bi[1] + 1e-9 or bj[1] < bi[0] - 1e-9 or bj[2] > bi[3] + 1e-9 or bj[3] < bi[2] - 1e-9:
                continue
            x = _seg_x(q[i], q[(i + 1) % n], q[j], q[(j + 1) % n])
            if x:
                return i, j, x
    return None


def _seg_dist(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    wx, wy = p[0] - a[0], p[1] - a[1]
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / l2))
    return math.hypot(wx - t * vx, wy - t * vy)


def min_dist(q, poly):
    n = len(poly)
    best = float("inf")
    pts = list(q) + [((q[i][0] + q[(i + 1) % len(q)][0]) / 2, (q[i][1] + q[(i + 1) % len(q)][1]) / 2) for i in range(len(q))]
    for p in pts:
        for i in range(n):
            dd = _seg_dist(p, poly[i], poly[(i + 1) % n])
            if dd < best:
                best = dd
    return best


def project(loop, pt, closed=True):
    """Nearest point on a polyline: (segment index, point, distance along the line, distance to pt)."""
    n = len(loop)
    best, run = None, 0.0
    for i in range(n if closed else n - 1):
        a, b = loop[i], loop[(i + 1) % n]
        vx, vy = b[0] - a[0], b[1] - a[1]
        l2 = vx * vx + vy * vy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((pt[0] - a[0]) * vx + (pt[1] - a[1]) * vy) / l2))
        q = (a[0] + t * vx, a[1] + t * vy)
        dd = _d(q, pt)
        if best is None or dd < best[3]:
            best = (i, q, run + t * math.sqrt(l2), dd)
        run += math.sqrt(l2)
    return best


def shape_key(c):
    """Names a drawn shape so clicks (manual tabs, start points) find it again after a recompute."""
    mx = sum(p[0] for p in c) / len(c)
    my = sum(p[1] for p in c) / len(c)
    return f"{mx:.2f},{my:.2f},{abs(area(c)):.1f}"


def manual_key(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def offset(poly, d):
    """Clean tool-centre loops for a contour (counter-clockwise result)."""
    if area(poly) < 0:
        poly = poly[::-1]
    sp = _simplify(poly)
    # start the loop in the middle of the longest straight edge, where the offset is always clean
    k = max(range(len(sp)), key=lambda i: _d(sp[i], sp[(i + 1) % len(sp)]))
    a, b = sp[k], sp[(k + 1) % len(sp)]
    sp = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)] + sp[k + 1:] + sp[:k + 1]
    raw = _raw_offset(sp, d)
    done, stack = [], [raw]
    while stack:
        q = _dedupe(stack.pop(), 1e-7)
        if len(q) < 3:
            continue
        hit = _first_cross(q)
        if not hit:
            if area(q) > 1e-4:
                done.append(q)
            continue
        i, j, x = hit
        stack.append([x] + q[i + 1:j + 1])
        stack.append(q[:i + 1] + [x] + q[j + 1:])
    # a loop that comes closer to the line than the tool radius would cut into the part
    return [q for q in done if min_dist(q, poly) > abs(d) - 0.02]


def pocket_left(outer, inner, r):
    """Agilite CNC: the area (mm²) of a pocket an end mill of radius r cannot reach, beyond the small round left in
    every inside corner. inner = the tool centre's loops (the pocket offset in by r); what the tool sweeps is those
    grown back by r. A corner of interior angle t leaves r² (cot(t/2) - (pi - t)/2) anyway, so that is allowed."""
    reach = sum(abs(area(q)) for c in inner for q in offset(c, r))
    p = outer if area(outer) > 0 else outer[::-1]
    allow = 0.0
    n = len(p)
    for i in range(n):
        a, b, c = p[i - 1], p[i], p[(i + 1) % n]
        u = (b[0] - a[0], b[1] - a[1])
        v = (c[0] - b[0], c[1] - b[1])
        turn = math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])
        if turn > 1e-6:   # a left turn on a counter-clockwise loop: an inside corner of the pocket
            t = math.pi - turn
            allow += r * r * (1.0 / math.tan(t / 2) - (math.pi - t) / 2)
    return max(0.0, abs(area(outer)) - reach - allow)


def pocket_regions(cs):
    """Nesting inside one layer: each outer shape with the shapes directly inside it, which stay standing
    as islands (a shape inside an island is a pocket again)."""
    # Agilite CNC (Lev 2026-11-16): a shape is inside another only when all of it is. Shapes that overlap (the QD
    # ladderlock's lobes, each stretched into the round hole so the end mill enters from there) were taken for
    # islands of one another by their first point, and all but one lobe was left uncut
    ar_ = [abs(area(c)) for c in cs]
    bx_ = [(min(p[0] for p in c), min(p[1] for p in c), max(p[0] for p in c), max(p[1] for p in c)) for c in cs]

    def within(i, k):   # cs[i] wholly inside cs[k]
        a, b = bx_[i], bx_[k]
        if ar_[k] <= ar_[i] or a[0] < b[0] or a[1] < b[1] or a[2] > b[2] or a[3] > b[3]:
            return False
        return all(inside(p, cs[k]) for p in cs[i])
    lv = []
    for i, c in enumerate(cs):
        lv.append(sum(1 for k in range(len(cs)) if k != i and within(i, k)))
    out = []
    for i, c in enumerate(cs):
        if lv[i] % 2 == 0:
            out.append((c, [o for k, o in enumerate(cs) if k != i and lv[k] == lv[i] + 1 and within(k, i)]))
    return out


def rim_shapes(pockets):
    """Agilite CNC: the pockets' top rims, [(key, kind, loop)] in the order and with the keys the page and the
    chamfer picks use: "pocket:<layer>:<n>", a pocket's outer shape (kind "hole") then its islands ("part")."""
    out = []
    for layer, cs in pockets.items():
        n = 0
        for outer, islands in pocket_regions(cs):
            for kd, c in [("hole", outer)] + [("part", i_) for i_ in islands]:
                out.append((f"pocket:{layer}:{n}", kd, c))
                n += 1
    return out


def _in_region(pt, loops):
    return sum(1 for q in loops if inside(pt, q)) % 2 == 1


def pocket_rows(loops, so):
    """Rows across the tool-centre region, at most so apart: [[(y, x_from, x_to), ...] per row]."""
    ys = [p[1] for q in loops for p in q]
    y0, y1 = min(ys), max(ys)
    e = min(0.01, (y1 - y0) / 4)
    n = max(1, int(math.ceil((y1 - y0 - 2 * e) / so - 1e-9)))
    rows = []
    for i in range(n + 1):
        y = y0 + e + (y1 - y0 - 2 * e) * i / n + 1e-7
        xs = []
        for q in loops:
            m = len(q)
            for k in range(m):
                a, b = q[k], q[(k + 1) % m]
                if (a[1] > y) != (b[1] > y):
                    xs.append(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))
        xs.sort()
        rows.append([(y, xs[k], xs[k + 1]) for k in range(0, len(xs) - 1, 2) if xs[k + 1] - xs[k] > 1e-6])
    return rows


def pocket_chains(rows, loops, so):
    """Joins the row pieces into zigzags; a new zigzag starts wherever the link would leave the pocket."""
    segs = [(ri, a, b, y) for ri, row in enumerate(rows) for (y, a, b) in row]
    edges = []
    for q in loops:
        for k in range(len(q)):
            a, b = q[k], q[(k + 1) % len(q)]
            edges.append((a, b, min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])))

    def clear(p, q):
        dx, dy = q[0] - p[0], q[1] - p[1]
        p2, q2 = (p[0] + dx * 0.02, p[1] + dy * 0.02), (q[0] - dx * 0.02, q[1] - dy * 0.02)
        bx0, bx1, by0, by1 = min(p2[0], q2[0]), max(p2[0], q2[0]), min(p2[1], q2[1]), max(p2[1], q2[1])
        for a, b, ex0, ex1, ey0, ey1 in edges:
            if ex0 > bx1 or ex1 < bx0 or ey0 > by1 or ey1 < by0:
                continue
            if _seg_x(p2, q2, a, b):
                return False
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        return _in_region(mid, loops) or min(_seg_dist(mid, a, b) for a, b, *_ in edges) < 1e-3

    left = set(range(len(segs)))
    chains, pos = [], None
    while left:
        if pos is None:
            cur = min(left, key=lambda i: (segs[i][0], segs[i][1]))
        else:
            cur = min(left, key=lambda i: min(_d(pos, (segs[i][1], segs[i][3])), _d(pos, (segs[i][2], segs[i][3]))))
        chain = []
        while True:
            left.discard(cur)
            ri, a, b, y = segs[cur]
            if pos is not None and _d(pos, (b, y)) < _d(pos, (a, y)):
                a, b = b, a
            chain.append(((a, y), (b, y)))
            pos = (b, y)
            best = None
            for k in left:
                if abs(segs[k][0] - ri) != 1:
                    continue
                _, a2, b2, y2 = segs[k]
                e = min((a2, y2), (b2, y2), key=lambda p: _d(pos, p))
                dd = _d(pos, e)
                if dd <= 4 * so and (best is None or dd < best[0]) and clear(pos, e):
                    best = (dd, k)
            if best is None:
                break
            cur = best[1]
        chains.append(chain)
    return chains


def _walk(o, pts, closed, s0, s1, z0, z1):
    """Feeds along a polyline from distance s0 to s1 (past the end of a closed loop it goes round again),
    with Z going from z0 to z1 on the way."""
    P = list(pts) + ([pts[0]] if closed else [])
    C = [0.0]
    for i in range(len(P) - 1):
        C.append(C[-1] + _d(P[i], P[i + 1]))
    L = C[-1]
    if L < 1e-9:
        o.g1(P[0][0], P[0][1], z1)
        return

    def at(v):
        m = v % L if closed else max(0.0, min(v, L))
        if closed and m == 0 and v > 0:
            m = L
        k = min(bisect.bisect_right(C, m) - 1, len(P) - 2)
        seg = C[k + 1] - C[k]
        f = (m - C[k]) / seg if seg > 0 else 0.0
        return (P[k][0] + (P[k + 1][0] - P[k][0]) * f, P[k][1] + (P[k + 1][1] - P[k][1]) * f)

    bps = {s1}
    laps = range(int(math.floor(s0 / L)), int(math.floor(s1 / L)) + 1) if closed else (0,)
    for m in laps:
        for ck in C[1:]:
            v = ck + m * L
            if s0 < v < s1:
                bps.add(v)
    for v in sorted(bps):
        z = z0 + (z1 - z0) * (v - s0) / (s1 - s0) if s1 > s0 else z1
        x, y = at(v)
        o.g1(x, y, z)


def _ramp_line(o, a, b, z0, z1, ramp_tan):
    """From a at z0, ramps back and forth along a-b down to z1, ending at a."""
    L = _d(a, b)
    if L < 1e-6 or z0 - z1 < 1e-9:
        o.g1(a[0], a[1], z1)
        return
    remaining, at_a, z = (z0 - z1) / ramp_tan, True, z0
    while remaining > 1e-9:
        seg = min(remaining, L)
        f = seg / L
        p0, p1 = (a, b) if at_a else (b, a)
        z -= seg * ramp_tan
        x, y = p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f
        o.g1(x, y, max(z, z1))
        remaining -= seg
        at_a = not at_a if seg >= L - 1e-9 else at_a
        if seg < L - 1e-9:
            break
    o.g1(a[0], a[1], z1)


# ----------------------------------------------------------------------- toolpath

class Job:
    def __init__(self):
        self.files = []      # (file label, lines); one entry unless each tool gets its own file
        self.lines = []      # lines of the first file
        self.warnings = []
        self.preview = []    # (kind, loop, tab sample points, number, band width mm)
        self.contours = []
        self.time_min = 0.0
        self.summary = []
        self.open_paths = [] # open lines that are engraved
        self.mark_paths = [] # typed texts and logos, placed: (points, closed)
        self.pauses = []     # (seg index, message, file index): where the program stops for you
        self.stages = []     # (seg index, label): which tool does what from there on
        self.probe = None    # sensor spot (x, y, radius)
        self.run_steps = []  # what to do at the machine, in order
        self.seg_info = []   # per seg: (minutes, feed or None, file index, line index) - for playback
        self.slot_exact = 0  # slots exactly bit-wide, cut down the middle
        self.marks = []      # their boxes, for dragging in the preview: i, x, y, w, h, angle, label
        self.slugs = {}      # hole key -> [hole outline, shapes inside it]: falls out when cut
        self.layers = {}     # layer -> [closed shapes, open lines]
        self.loops = []      # cut-through loops that can be clicked: key, kind, loop, tab points
        self.origin = (0.0, 0.0)   # DXF point that became X0 Y0
        self.cut_shapes = []       # Agilite CNC: (shape key, "part"|"hole", drawn contour in job coordinates)
        self.segs = []       # every move: (x0, y0, z0, x1, y1, z1, rapid, tool) for the finished-part preview


def _fmt_time(minutes):
    s = int(round(minutes * 60))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _fmt(v):
    t = f"{v:.3f}"
    return "0.000" if t == "-0.000" else t


def _cmt(text):
    """Text that is safe inside a G-code ( ) comment."""
    return str(text).replace("(", "[").replace(")", "]")


def safe_line(line):
    """Agilite CNC (Lev 2026-11-15): a G-code line whose comment can't end early. A comment ends at the FIRST ')',
    so "(... 2 pass(es), 23 rows 2.70 mm apart)" let UCCNC read "rows 2.70" as S2.70: the spindle dropped to its
    10000 minimum right after M3 and stalled in aluminium. Brackets inside a comment become [ ]."""
    i = line.find("(")
    if i < 0:
        return line
    body = line[i + 1:]
    if body.endswith(")"):
        body = body[:-1]
    return line[:i] + "(" + body.replace("(", "[").replace(")", "]") + ")"


class _Out:
    """G-code writer that remembers where the tool is (an axis is unknown at the start and after G53 moves)."""

    def __init__(self):
        self.lines = []
        self.p = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.k = {"x": False, "y": False, "z": False}
        self.f = None
        self.t = None
        self.time = 0.0
        self.segs = []
        self.info = []   # per seg: (minutes it takes, feed or None for a rapid, index of its G-code line)
        self.pauses = []  # (index of the next seg, message) for every M0
        self.stages = []  # (index of the first seg, "T1 name: outlines")
        # Agilite CNC: where the preview shows the tool while the G-code's own position is unknown (after a G53
        # lift): [x, y, z, tool]. Moves recorded from it are for the preview only, never in the G-code or the time.
        self.ghost = None

    def show(self, x, y, z, t=None):
        """Agilite CNC: a preview-only rapid (the trip to the sensor spot: the lift to the machine top is G53, so
        the G-code's own moves can't be drawn). Starts from the ghost, or from the tool when its position is known."""
        t = t or self.t
        if self.ghost is not None:
            a = self.ghost[:3]
            t = t or self.ghost[3]
        elif all(self.k.values()):
            a = (self.p["x"], self.p["y"], self.p["z"])
        else:
            a = None
        if a is not None and t is not None and (a[0], a[1], a[2]) != (x, y, z):
            d = math.dist(a, (x, y, z))
            self.segs.append((a[0], a[1], a[2], x, y, z, True, t))
            self.info.append((d / RAPID, None, len(self.lines) - 1))
        self.ghost = [x, y, z, t]

    def _rec(self, old, rapid, dt=0.0, f=None):
        p = self.p
        if self.t is not None and (old["x"], old["y"], old["z"]) != (p["x"], p["y"], p["z"]):
            self.segs.append((old["x"], old["y"], old["z"], p["x"], p["y"], p["z"], rapid, self.t))
            self.info.append((dt, f, len(self.lines) - 1))

    def raw(self, line):
        self.lines.append(safe_line(line))

    def forget(self):
        self.k = {"x": False, "y": False, "z": False}
        self.f = None

    def g0(self, x=None, y=None, z=None):
        known = all(self.k.values())
        old = dict(self.p)
        parts, d2 = [], 0.0
        for a, v in (("x", x), ("y", y), ("z", z)):
            if v is None:
                continue
            if self.k[a]:
                if abs(v - self.p[a]) < 1e-6:
                    continue
                d2 += (v - self.p[a]) ** 2
            parts.append(a.upper() + _fmt(v))
            self.p[a], self.k[a] = v, True
        if parts:
            self.lines.append("G0 " + " ".join(parts))
            self.time += math.sqrt(d2) / RAPID
            if known:
                self._rec(old, True, math.sqrt(d2) / RAPID)
            elif self.ghost is not None:   # Agilite CNC: on the way back from the sensor spot, for the preview
                g = self.ghost
                self.show(*(self.p[a] if self.k[a] else g[i] for i, a in enumerate("xyz")))
                if all(self.k.values()):
                    self.ghost = None

    def machine(self, x=None, y=None, z=None):
        parts = [a.upper() + _fmt(v) for a, v in (("x", x), ("y", y), ("z", z)) if v is not None]
        self.lines.append("G53 G0 " + " ".join(parts))
        for a, v in (("x", x), ("y", y), ("z", z)):
            if v is not None:
                self.k[a] = False
        self.time += 0.1

    def g1(self, x, y, z):
        self.ghost = None
        p = self.p
        dxy = math.hypot(x - p["x"], y - p["y"])
        dz = z - p["z"]
        if dxy < 1e-5 and abs(dz) < 1e-5:
            return
        t = self.t
        if dxy < 1e-5:
            f = t["plunge_feed"]
        elif dz < -1e-6:
            f = t["ramp_feed"]
        else:
            f = t["feed"]
        parts = ["G1"]
        if abs(x - p["x"]) > 1e-6:
            parts.append("X" + _fmt(x))
        if abs(y - p["y"]) > 1e-6:
            parts.append("Y" + _fmt(y))
        if abs(dz) > 1e-6:
            parts.append("Z" + _fmt(z))
        if f != self.f:
            parts.append(f"F{f:g}")
            self.f = f
        self.lines.append(" ".join(parts))
        dt = math.sqrt(dxy * dxy + dz * dz) / max(f, 1e-6)
        self.time += dt
        old = dict(p)
        p.update(x=x, y=y, z=z)
        self._rec(old, False, dt, f)



SHOW_TOP = 40.0     # Agilite CNC: how high the preview draws the G53 lift to the machine top (work Z)
SHOW_SENSOR = 7.0   # ... and where it shows the end mill waiting over the sensor (the drawn puck is 4 mm tall)


def show_lift(o, t):
    """Agilite CNC: call before the G53 lift: the preview shows the end mill going straight up."""
    if all(o.k.values()):
        o.show(o.p["x"], o.p["y"], max(SHOW_TOP, o.p["z"]), o.t or t)


def show_sensor(o, x, y, t):
    """Agilite CNC: call once the G-code is over the sensor spot: the preview shows it there, over the sensor
    (still with the end mill that is coming out; the next one comes back from there)."""
    o.show(x, y, SHOW_TOP, o.t or t)
    o.show(x, y, SHOW_SENSOR, o.t or t)


def bed_change(o, s, t, n, pause):
    """Agilite CNC: a bit change on the fixed bed sensor. Lift, spindle off, go to the sensor in machine
    coordinates, a pause naming the new bit, then Stepcraft's M20201: it measures the bit still in the spindle,
    asks for the new one, measures that and shifts Z zero by the difference, so Z0 stays the stock top."""
    bx, by = s["bed_sensor"]
    name = _cmt(t["name"])
    o.raw(f"(---- Tool change: T{n} {name}, measured on the bed sensor at machine X{bx:.1f} Y{by:.1f} ----)")
    o.g0(z=s["safe_z"])
    o.machine(z=-3.0)
    o.raw("M5")
    o.machine(x=bx, y=by)
    pause(f"M0 (Next: T{n} {name}. Cycle Start, then OK the dialogs: UCCNC measures the old end mill, "
          f"asks for T{n}, measures it)")
    o.raw(s.get("bed_macro") or "M20201")
    o.pauses.append((len(o.segs), f"UCCNC asks for the new end mill: put in T{n} {name} and press OK"))
    o.forget()


def _park(o, s, final):
    """Lift, stop the spindle, move to the park spot (end of program and before a tool change)."""
    o.g0(z=s["safe_z"])
    if s["end_z_mode"].startswith("Custom"):
        o.g0(z=max(s["end_z"], s["safe_z"]))
    elif s["end_z_mode"].startswith("Machine"):
        o.machine(z=-3.0)
    o.raw("M5")
    xy = s["end_xy"]
    if xy.startswith("X0"):
        o.g0(x=0.0, y=0.0)
    elif xy.startswith("Custom"):
        o.g0(x=s["end_x"], y=s["end_y"])
    elif xy.startswith("Machine"):
        o.machine(x=3.0, y=3.0)
    if final:
        o.raw("M30")


def _tool_label(t):
    kind = {"Ball nose": "ball nose", "Flat end mill": "flat", VBIT: f"{t.get('tool_angle', 90):g} deg V-bit",
            SURF: "surfacing bit"}
    return f"{t['tool_d']:g} mm {kind.get(t['tool_type'], t['tool_type'])}"


def build(s, tools=None):
    """s: settings dict with numbers already parsed; tools: the tool list. Returns a Job."""
    job = Job()
    marks = [m for m in (s.get("marks") or []) if isinstance(m, dict) and not m.get("hidden")]
    has_text = bool(marks)
    # e.g. facing a spoilboard or engraving a label: no DXF needed
    surf_only = (s["surf"] and s["surf_area"].startswith("Custom")) or has_text
    layer_ops = s.get("layer_ops") or {}
    cut, pockets, engrave = [], {}, {}
    if not s["dxf"] or not os.path.isfile(s["dxf"]):
        if not surf_only:
            job.warnings.append("Choose a DXF file.")
            return job
        if s["dxf"]:
            job.warnings.append("The DXF file was not found; only the text and surfacing are cut.")
    else:
        shapes, w = read_dxf(s["dxf"], s["skip_layers"].split(","))
        cut, pockets, engrave, job.layers, w2 = split_layers(shapes, layer_ops)
        job.warnings += w + w2
        if not (cut or any(pockets.values()) or any(a or b for a, b in engrave.values())) and not surf_only:
            job.warnings.append("No closed shapes found in the DXF." if not shapes else
                                "Every layer is set to Skip (Layers tab), so there is nothing to cut.")
            return job

    allpts = ([p for c, _ in cut for p in c] + [p for cs in pockets.values() for c in cs for p in c]
              + [p for a, b in engrave.values() for c in a + b for p in c])
    xs = [p[0] for p in allpts] or [0.0]
    ys = [p[1] for p in allpts] or [0.0]
    if s["origin"].startswith("Lower"):
        ox, oy = min(xs), min(ys)
    elif s["origin"].startswith("Centre"):
        ox, oy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    else:
        ox, oy = 0.0, 0.0
    job.origin = (ox, oy)

    def sh(c):
        return [(x - ox, y - oy) for x, y in c]

    keys = [shape_key(c) for c, _ in cut]
    contours = [sh(c) for c, _ in cut]
    pockets = {k: [sh(c) for c in cs] for k, cs in pockets.items()}
    job.rim_shapes = rim_shapes(pockets)   # Agilite CNC: pocket rims the page lets the designer chamfer
    engrave = {k: ([sh(c) for c in a], [sh(c) for c in b]) for k, (a, b) in engrave.items()}
    job.contours = contours + [c for cs in pockets.values() for c in cs] + [c for a, _ in engrave.values() for c in a]
    job.open_paths = [c for _, b in engrave.values() for c in b]
    # tabs and start points placed by clicking in the preview (stored in drawing coordinates)
    man = (s.get("manual") or {}).get(manual_key(s["dxf"]), {}) if s["dxf"] else {}
    man_tabs = {k: [(x - ox, y - oy) for x, y in pts] for k, pts in man.get("tabs", {}).items()}
    man_starts = {k: (p[0] - ox, p[1] - oy) for k, p in man.get("starts", {}).items()}

    def layer_depth(layer):
        lo = layer_ops.get(layer) or default_layer_op(layer)
        try:
            return float(str(lo.get("depth", "")).replace(",", "."))
        except ValueError:
            job.warnings.append(f"The depth for layer \"{layer}\" is not a number; that layer is left out.")
            return None

    depth = s["thickness"] + s["extra"]
    z_tab = -s["thickness"] + s["tab_thick"]
    # surfacing takes the top down; the bottom (and so the cut-through depth and the tabs) stays put
    z_top = -s["surf_depth"] if s["surf"] and 0 < s["surf_depth"] < s["thickness"] else 0.0
    climb = s["direction"] == "Climb"

    if s["tool_d"] <= 0 or s["step"] <= 0 or depth <= 0:
        job.warnings.append("Tool diameter, depth per pass and depth must be above zero.")
        return job
    if s["extra"] < 0:
        job.warnings.append("Extra depth is negative: the cut will not go through the stock.")
    if s["tabs"] > 0 and s["tab_thick"] >= s["thickness"]:
        job.warnings.append("Tab thickness is not less than the stock, so the tabs do nothing.")

    # ---- tools for each operation
    if tools is None:
        tools = load_tools()
    # the outlines tool is a tool from the library, with the speeds and feeds saved with it
    lib_main = next((t for t in tools if t.get("name") == s["tool_name"]), None)
    if lib_main is not None:
        main = dict(lib_main)
        main["tool_d"] = float(main["tool_d"])
        main["flutes"] = int(main.get("flutes", 2))
        main["tool_angle"] = float(main.get("tool_angle", 90.0))
        main.update(tool_speeds(main, s))
        main["ramp_angle"] = float(s.get("ramp_set") or main.get("ramp_angle", s["ramp_angle"]))
    else:   # (settings from before the library, or a tool since deleted: the numbers in the settings)
        main = {"name": s["tool_name"] or "Main tool", "tool_d": s["tool_d"], "tool_type": s["tool_type"],
                "flutes": s["flutes"], "tool_mat": s["tool_mat"], "tool_angle": s["tool_angle"]}
        main.update({k: float(s[k]) for k in SPEED_KEYS})
        main["ramp_angle"] = float(s["ramp_angle"])
        if contours:
            job.warnings.append("The outlines tool is not in the tool library: pick one on the Cuts page.")

    def pick(name, what):
        if not name or name in (MAIN, MAIN_OLD) or name == s["tool_name"]:
            return main
        t = next((t for t in tools if t.get("name") == name), None)
        if t is None:
            job.warnings.append(f"The tool \"{name}\" for {what} is not in the tool list; the main tool is used.")
            return main
        t = dict(t)
        t["tool_d"] = float(t["tool_d"])
        t["tool_angle"] = float(t.get("tool_angle", 90.0))
        t.update(tool_speeds(t, s))
        if s.get("ramp_set"):   # Agilite CNC: the designer's ramp angle for every bit
            t["ramp_angle"] = float(s["ramp_set"])
        return t

    for t, what in ((main, "outlines"),):
        if t["tool_type"] == VBIT and contours:
            job.warnings.append("The main tool is a V-bit; it cannot cut parts out. Choose an end mill.")

    # ---- holes and outlines
    classified, slug_of = [], {}
    slot_mid, no_tabs = [], {}   # collected while cutting, told as one line each at the end
    for idx, c in enumerate(contours):
        a = abs(area(c))
        level = sum(1 for k, o in enumerate(contours) if k != idx and abs(area(o)) > a and inside(c[0], o))
        kind = "part" if level % 2 == 0 else "hole"
        bx = max(p[0] for p in c) - min(p[0] for p in c)
        by = max(p[1] for p in c) - min(p[1] for p in c)
        classified.append((kind, c, (bx, by), keys[idx]))
        job.cut_shapes.append((keys[idx], kind, c))   # Agilite CNC: the drawn shapes by key, for the page
        if kind == "hole":   # its slug falls out unless tabs hold it: the finished-part preview leaves it out
            slug_of[keys[idx]] = [c] + [o for o in contours if abs(area(o)) < a and inside(o[0], c)]

    def centreline(c, r):
        """A slot about as wide as the bit: the thinnest loop that still fits, i.e. down the middle.
        Returns (loops, drawn width) when the slot is at most slot_allow narrower than the bit."""
        lo, hi = max(0.0, r - s["slot_allow"] / 2 - 0.01), r
        if not offset(c, -lo):
            return None, 0.0
        for _ in range(14):
            mid = (lo + hi) / 2
            if offset(c, -mid):
                lo = mid
            else:
                hi = mid
        d = max(0.0, lo - 0.002)
        loops = offset(c, -d)
        return (loops, 2 * d) if loops else (None, 0.0)

    def loops_for(kinds, dist, tool, what, only=None):
        out = []
        for kind, c, size, key in classified:
            if kind not in kinds or (only is not None and key not in only):
                continue
            loops = offset(c, dist(kind))
            if not loops and kind == "hole" and what == "cutting" and s["slot_center"] and tool["tool_type"] != VBIT:
                loops, w = centreline(c, tool["tool_d"] / 2)
                if loops:   # told once, grouped, at the end of build()
                    slot_mid.append((size, w, tool))
            if not loops:
                job.warnings.append(f"A {size[0]:.1f} x {size[1]:.1f} mm {'hole' if kind == 'hole' else 'outline'} "
                                    f"is too small for {what} with the {_tool_label(tool)} and is skipped.")
                continue
            for q in loops:
                # q is counter-clockwise; conventional = outlines CCW, holes CW
                if (kind == "hole") != climb:
                    q = q[::-1]
                out.append((kind, q, size, key))
        return out

    def size_of(c):
        return (max(p[0] for p in c) - min(p[0] for p in c), max(p[1] for p in c) - min(p[1] for p in c))

    def pocket_rim_shapes():
        """Agilite CNC: the top rims of the pockets, [(key, kind, drawn loop, depth)]: a pocket's outer shape
        (kind "hole": the bit runs inside it) and its islands (kind "part": outside them). Keys are
        "pocket:<layer>:<n>"; the same numbering the page shows."""
        out = []
        for key, kd, c in rim_shapes(pockets):
            dp = layer_depth(key[len("pocket:"):].rsplit(":", 1)[0])
            if dp is not None and dp > 0:
                out.append((key, kd, c, dp))
        return out

    def rim_is_top(p, c):
        """Is the stock just past drawn loop c, beside tool-path point p, the uncut top? Inside the part (an
        outline, not a hole) and in no pocket of any layer (a pocket's open side runs out past the part, and a
        deeper pocket's rim sits on a shallower floor)."""
        best, foot = 9e9, None
        n_ = len(c)
        for j in range(n_):
            a_, b_ = c[j], c[(j + 1) % n_]
            dx, dy = b_[0] - a_[0], b_[1] - a_[1]
            ll = dx * dx + dy * dy
            f = 0.0 if ll <= 0 else max(0.0, min(1.0, ((p[0] - a_[0]) * dx + (p[1] - a_[1]) * dy) / ll))
            q_ = (a_[0] + dx * f, a_[1] + dy * f)
            d_ = _d(p, q_)
            if d_ < best:
                best, foot = d_, q_
        if foot is None or best <= 1e-9:
            return False
        probe = (foot[0] + (foot[0] - p[0]) / best * 0.3, foot[1] + (foot[1] - p[1]) / best * 0.3)
        if sum(1 for o in contours if inside(probe, o)) % 2 == 0:
            return False
        for cs in pockets.values():
            for outer, islands in pocket_regions(cs):
                if inside(probe, outer) and not any(inside(probe, i_) for i_ in islands):
                    return False
        return True

    def pocket_rims(clear, dc, only=None, picked=None):
        """Agilite CNC: chamfer items along the pockets' top rims (closed loops where the whole rim is at the top,
        open "crun" runs where only part of it is). only = the rim keys to do (None: all); picked = {key: set of
        drawn segment indexes} limits a rim to those segments. A pocket shallower than the chamfer is left out
        (the V-bit's tip would groove its floor)."""
        items = []
        for key, kd, c, dp in pocket_rim_shapes():
            if only is not None and key not in only:
                continue
            if dp < dc - 1e-6:
                job.warnings.append(f"A pocket {dp:.2f} mm deep is shallower than the chamfer ({dc:.2f} mm); its rim "
                                    f"is not chamfered.")
                continue
            for q in offset(c, clear if kd == "part" else -clear):
                if (kd == "hole") != climb:
                    q = q[::-1]
                chosen = (picked or {}).get(key)
                inn = [rim_is_top(p, c) and (chosen is None or any(
                    _seg_dist(p, c[j], c[(j + 1) % len(c)]) <= clear + 0.02 for j in chosen)) for p in q]
                if all(inn):
                    items.append((kd, q, size_of(c), key))
                    continue
                if not any(inn):
                    continue
                k0 = inn.index(False)
                qq, ii = q[k0:] + q[:k0], inn[k0:] + inn[:k0]
                run = []
                for p, ok in zip(qq + [qq[0]], ii + [ii[0]]):
                    if ok:
                        run.append(p)
                    elif run:
                        if len(run) > 1 and sum(_d(run[i], run[i + 1]) for i in range(len(run) - 1)) >= 0.5:
                            items.append(("crun", run, size_of(c), key))
                        run = []
        return items

    # ---- marks: typed texts and logos, placed in machine X Y (from X0 Y0) by the centre of their box
    placed_marks = []
    for mi, m in enumerate(s.get("marks") or []):
        if not isinstance(m, dict) or m.get("hidden"):
            continue
        shapes, w, h, problems = mark_shapes(m)
        label = (" ".join(str(m.get("text", "")).replace("|", " ").split())[:24] if m.get("type") != "logo"
                 else m.get("logo", "logo"))
        job.warnings += [f"Mark \"{label}\": {p_}" for p_ in problems]
        if not shapes:
            continue
        placed = place_mark(shapes, m)
        job.mark_paths += placed
        job.marks.append({"i": mi, "x": mark_num(m, "x"), "y": mark_num(m, "y"), "w": w, "h": h,
                          "angle": mark_num(m, "angle"), "label": label, "n_paths": len(placed)})
        placed_marks.append((m, placed, label, w, h))

    ops = []
    # ---- surfacing: flattens the top before anything else is cut
    if s["surf"]:
        ts = pick(s["surf_tool"], "surfacing")
        sd = s["surf_depth"]
        if ts["tool_type"] == VBIT:
            job.warnings.append("Surfacing needs a flat end mill, not a V-bit; it is left out.")
        elif sd <= 0:
            job.warnings.append("Surfacing depth must be above zero; it is left out.")
        elif sd >= s["thickness"]:
            job.warnings.append("Surfacing depth is not less than the stock thickness; it is left out.")
        elif not 5 <= s["surf_over"] <= 95:
            job.warnings.append("Surfacing stepover must be 5-95 % of the tool; it is left out.")
        else:
            if s["surf_area"].startswith("Custom") or not (job.contours or job.open_paths or job.mark_paths):
                ax0, ay0 = s["surf_x"], s["surf_y"]
                ax1, ay1 = ax0 + s["surf_w"], ay0 + s["surf_h"]
            else:
                m = s["surf_margin"]
                cx_ = [p[0] for c in job.contours + job.open_paths + [m_[0] for m_ in job.mark_paths] for p in c]
                cy_ = [p[1] for c in job.contours + job.open_paths + [m_[0] for m_ in job.mark_paths] for p in c]
                ax0, ay0, ax1, ay1 = min(cx_) - m, min(cy_) - m, max(cx_) + m, max(cy_) + m
            # Agilite CNC: "Part outline" surfaces only the part and the strip the outline bit cuts beside it (the
            # rest of the stock is cut away anyway): the tool centre stays inside the outlines grown by the outline
            # bit plus 0.5 mm, or by the margin less the surfacing bit's radius when that is wider
            region = None
            # the outline bit's groove beside the part must be faced: the surfacing bit's edge reaches its radius
            # past the tool centre, so the centre needs to go (outline bit + 0.5 mm - half that radius) out; the
            # other half of the radius covers the scallops where rows end on a slanted edge
            # (never inwards: a cutter wider than the strip covers it with its centre on the outline)
            need = max(0.0, float(main["tool_d"]) + 0.5 - ts["tool_d"] / 4)
            if not s["surf_area"].startswith(("Part", "Custom")):
                ax0, ay0, ax1, ay1 = (ax0 - max(0.0, need - s["surf_margin"]), ay0 - max(0.0, need - s["surf_margin"]),
                                      ax1 + max(0.0, need - s["surf_margin"]), ay1 + max(0.0, need - s["surf_margin"]))
            if s["surf_area"].startswith("Part") and contours:
                outs = [c for kd, c, size, key in classified if kd == "part"]
                # "Part outline": only what the part needs; "Part + margin": out to the margin as well
                grow = need if s["surf_area"] == "Part outline" else max(need, float(s["surf_margin"]) - ts["tool_d"] / 2)
                region = [q for c in outs for q in offset(c, grow)]
                if region:
                    ax0 = min(p[0] for q in region for p in q)
                    ay0 = min(p[1] for q in region for p in q)
                    ax1 = max(p[0] for q in region for p in q)
                    ay1 = max(p[1] for q in region for p in q)
                else:
                    region = None
            if ax1 - ax0 <= 0 or ay1 - ay0 <= 0:
                job.warnings.append("The surfacing area has no size; it is left out.")
            else:
                if ts["tool_type"] == "Ball nose":
                    job.warnings.append("Surfacing with a ball nose leaves ridges between rows; a flat end mill "
                                        "gives a flat finish.")
                ops.append({"kind": "surface", "tool": ts, "depth": sd, "tabs": False, "region": region,
                            "area": (ax0, ay0, ax1, ay1), "items": [("surface", None, (ax1 - ax0, ay1 - ay0))]})

    # ---- pockets: clear the inside of each closed shape on a Pocket layer to its depth
    if any(pockets.values()):
        tp = pick(s["pocket_tool"], "pockets")
        rp = tp["tool_d"] / 2
        so = tp["tool_d"] * s["pocket_over"] / 100
        if tp["tool_type"] == VBIT:
            job.warnings.append("Pockets need an end mill, not a V-bit; the pockets are left out.")
        elif not 5 <= s["pocket_over"] <= 95:
            job.warnings.append("Pocket stepover must be 5-95 % of the tool; the pockets are left out.")
        else:
            if tp["tool_type"] == "Ball nose":
                job.warnings.append("Pockets with a ball nose leave ridges on the floor and a round bottom edge; "
                                    "a flat end mill gives a flat floor.")
            for layer, cs in pockets.items():
                dp = layer_depth(layer)
                if dp is None or not cs:
                    continue
                if dp <= 0:
                    job.warnings.append(f"Layer \"{layer}\": pocket depth must be above zero; left out.")
                    continue
                if z_top - dp <= -s["thickness"] + 0.05:
                    job.warnings.append(f"Layer \"{layer}\": a {dp:.2f} mm pocket goes through the stock; "
                                        f"set the layer to Cut through instead. Left out.")
                    continue
                items = []
                for outer, islands in pocket_regions(cs):
                    size = size_of(outer)
                    loops = offset(outer, -rp)
                    if not loops:
                        job.warnings.append(f"A {size[0]:.1f} x {size[1]:.1f} mm pocket on \"{layer}\" is narrower "
                                            f"than the {_tool_label(tp)} and is skipped.")
                        continue
                    # Agilite CNC (Lev 2026-11-16): a pocket part of which is narrower than the end mill (the QD
                    # ladderlock's 3.6 mm lobes with a 4 mm end mill) was cut where it could reach and the rest was
                    # left without a word; now it says so, which also makes the end mill trials pick a smaller one
                    left = pocket_left(outer, loops, rp)
                    if left > max(0.5, 0.01 * abs(area(outer))):
                        job.warnings.append(f"Part of a {size[0]:.1f} x {size[1]:.1f} mm pocket on \"{layer}\" "
                                            f"is narrower than the {_tool_label(tp)} and is left out.")
                    for isl in islands:
                        loops += offset(isl, rp)
                    rows = pocket_rows(loops, so)
                    items.append(("pocket", {"loops": loops, "chains": pocket_chains(rows, loops, so),
                                             "rows": rows, "layer": layer}, size, None))
                if items:
                    ops.append({"kind": "pocket", "tool": tp, "depth": dp, "tabs": False, "items": items,
                                "layer": layer})

    # ---- engraving: the tool centre runs along the line (closed shapes and open lines)
    if engrave:
        tg = pick(s["engrave_tool"], "engraving")
        for layer, (loops, paths) in engrave.items():
            dg = layer_depth(layer)
            if dg is None or not (loops or paths):
                continue
            if dg <= 0:
                job.warnings.append(f"Layer \"{layer}\": engraving depth must be above zero; left out.")
                continue
            if z_top - dg <= -s["thickness"] + 0.05:
                job.warnings.append(f"Layer \"{layer}\": {dg:.2f} mm engraving goes through the stock; "
                                    f"left out.")
                continue
            items = [("engrave", c if (area(c) > 0) != climb else c[::-1], size_of(c), None) for c in loops]
            items += [("open", p, size_of(p), None) for p in paths]
            ops.append({"kind": "engrave", "tool": tg, "depth": dg, "tabs": False, "items": items, "layer": layer})
    for m, placed, label, w, h in placed_marks:
        tg = pick(s["engrave_tool"], "marks")
        dg = mark_num(m, "depth", 0.2)
        name = f"mark \"{label}\""
        if dg <= 0 or z_top - dg <= -s["thickness"] + 0.05:
            job.warnings.append(f"The depth of {name} must be above zero and less than the stock; it is left out.")
            continue
        cut = m.get("cut", "On the line")
        if any(not c for _, c in placed):
            cut = "On the line"
        elif cut not in CUT_STYLES_SHAPE:
            cut = "Outline"
        if cut == "Filled" and tg["tool_type"] == VBIT:
            job.warnings.append(f"Filled {name} needs an end mill as the engraving tool, not a V-bit; it is cut as "
                                f"an outline instead.")
            cut = "Outline"
        size = mark_num(m, "size", 5)
        if m.get("type") != "logo" and tg["tool_type"] != VBIT and size < 3 * tg["tool_d"]:
            job.warnings.append(f"The letters of {name} are {size:g} mm tall for a {_tool_label(tg)}: they will run "
                                f"together. Make them taller, or engrave with a V-bit or a smaller tool.")
        if cut == "Filled":
            rp = tg["tool_d"] / 2
            so = tg["tool_d"] * s["pocket_over"] / 100
            items, skipped = [], 0
            for outer, islands in pocket_regions([p_ for p_, _ in placed]):
                loops = offset(outer, -rp)
                if not loops:
                    skipped += 1
                    continue
                for isl in islands:
                    loops += offset(isl, rp)
                rows = pocket_rows(loops, so)
                items.append(("pocket", {"loops": loops, "chains": pocket_chains(rows, loops, so), "rows": rows,
                                         "layer": name}, size_of(outer), None))
            if skipped:
                job.warnings.append(f"{skipped} part(s) of {name} are thinner than the {_tool_label(tg)} and are not "
                                    f"cut. Use a smaller tool or make it bigger.")
            if items:
                ops.append({"kind": "pocket", "tool": tg, "depth": dg, "tabs": False, "items": items, "layer": name})
        elif cut == "Outline":
            ops.append({"kind": "engrave", "tool": tg, "depth": dg, "tabs": False, "layer": name,
                        "items": [("engrave", p_ if (area(p_) > 0) != climb else p_[::-1], size_of(p_), None)
                                  for p_, _ in placed]})
        else:
            ops.append({"kind": "engrave", "tool": tg, "depth": dg, "tabs": False, "layer": name,
                        "items": [("open", p_, size_of(p_), None) for p_, _ in placed]})

    th = pick(s["hole_tool"], "holes")
    finish_keys = set()

    def op_has_finish(kd, key):
        return key in finish_keys

    for kind, t in ((("hole", th), ("part", main)) if contours else ()):
        r = t["tool_d"] / 2
        if t["tool_type"] == "Ball nose" and s["extra"] < r - 0.05:
            job.warnings.append(f"Ball nose for {OP_NAMES[kind]}: extra depth {s['extra']:.2f} mm is less than the tool "
                                f"radius {r:.2f} mm, so the bottom edge will not be cut full width.")
        if t["step"] > t["tool_d"] / 2:
            job.warnings.append(f"Depth per pass for {OP_NAMES[kind]} is more than half the tool diameter; heavy for the M700.")
        if kind == "hole" and t is not main and t["tool_type"] == VBIT:
            job.warnings.append("The holes tool is a V-bit; it cannot cut through.")
        sign = -1 if kind == "hole" else 1
        fa = s["finish_allow"]
        if not (s["finish"] and 0 < fa <= r / 2 and t["tool_type"] != VBIT):
            ops.append({"kind": kind, "tool": t, "depth": depth, "tabs": True,
                        "items": loops_for((kind,), lambda k, r=r, sign=sign: sign * r, t, "cutting")})
            continue
        # finishing: the roughing passes stay fa off the wall, then one light pass takes the wall to size
        both = {key for kd, c, size, key in classified if kd == kind
                and offset(c, sign * (r + fa)) and offset(c, sign * r)}
        rest = {key for kd, c, size, key in classified if kd == kind} - both
        finish_keys.update(both)
        rough = loops_for((kind,), lambda k, r=r, sign=sign: sign * (r + fa), t, "cutting", only=both)
        rough += loops_for((kind,), lambda k, r=r, sign=sign: sign * r, t, "cutting", only=rest)
        ops.append({"kind": kind, "tool": t, "depth": depth, "tabs": True, "items": rough})
        ops.append({"kind": kind, "tool": t, "depth": depth, "tabs": True, "finish": True,
                    "items": loops_for((kind,), lambda k, r=r, sign=sign: sign * r, t, "cutting", only=both)})
    if s["finish"] and contours and not 0 < s["finish_allow"] <= min(th["tool_d"], main["tool_d"]) / 4:
        job.warnings.append("Finishing allowance must be above zero and at most a quarter of the tool diameter; "
                            "the finishing pass is left out where it is not.")

    # ---- edge break: a ball nose rounds the top edge after the cut-out
    if s["edge_break"]:
        te = pick(s["edge_tool"], "the edge break")
        R, e = te["tool_d"] / 2, s["edge_size"]
        if te["tool_type"] != "Ball nose":
            job.warnings.append("Edge break needs a ball nose tool; it is left out.")
        elif not 0 < e <= 0.7 * R:
            job.warnings.append(f"Edge break size must be between 0 and {0.7 * R:.2f} mm for this tool; it is left out.")
        else:
            # ball centre sits a outside the line and a above the top, so the ball passes through
            # the two points e from the corner: (-e, 0) on the top face and (0, -e) on the wall
            a = (-e + math.sqrt(2 * R * R - e * e)) / 2
            ops.append({"kind": "edge", "tool": te, "depth": R - a, "tabs": False,
                        "items": loops_for(("hole", "part"), lambda k: a if k == "part" else -a, te, "the edge break")})

    # Agilite CNC: chamfers chosen per shape and per edge, each with its own width.
    # s["chamfer_sel"] = {shape key: [(width mm, [(i0, i1), ...] point-index ranges of the drawn contour,
    # or None = the whole shape), ...]}. Whole shapes are cut as closed loops like the chamfer below; chosen
    # edges as open runs ("crun" items) along the same tool path, ramped in along the run (see emit_crun).
    sel = s.get("chamfer_sel")
    if s["chamfer"] and sel is not None:
        tc = pick(s["chamfer_tool"], "the chamfer")
        if tc["tool_type"] != VBIT:
            job.warnings.append("Chamfer needs a V-bit tool; it is left out.")
        else:
            half = math.radians(max(10.0, min(170.0, tc["tool_angle"])) / 2)
            clear = 0.2
            shape_of = {key: (kd, c) for kd, c, size, key in classified}
            by_w, wide, deepest = {}, set(), 0.0
            for key, entries in sel.items():
                if key not in shape_of and not key.startswith("pocket:"):
                    continue
                for wch, ranges in entries:
                    if wch <= 0:
                        continue
                    dc = (wch + clear) / math.tan(half)
                    if 2 * dc * math.tan(half) > tc["tool_d"] + 1e-6:
                        wide.add(wch)
                        continue
                    deepest = max(deepest, dc)
                    whole, runs = by_w.setdefault(wch, (set(), {}))
                    if ranges is None:
                        whole.add(key)
                    else:
                        runs.setdefault(key, []).extend(ranges)
            for wch in sorted(wide):
                job.warnings.append(f"A {wch:.2f} mm chamfer is too wide for the {_tool_label(tc)} (it cuts at most "
                                    f"{tc['tool_d'] / 2 - clear:.2f} mm); it is left out.")
            if by_w:
                if (s["tabs"] > 0 or man_tabs) and z_top - deepest < z_tab + 0.1:
                    job.warnings.append("The chamfer reaches down to the tabs; make the tabs thicker or the chamfer smaller.")
                lib = next((t for t in tools if t.get("name") == tc["name"]), None)
                if lib is not None and not all(k in lib for k in SPEED_KEYS):
                    tc.update(tool_speeds(lib, s, eff_d=max(1.0, 2 * deepest * math.tan(half))))

            def member(p, c, chosen, ends):
                """Is tool-path point p part of the chosen segments' run? Near a chosen segment and, where it is as
                near an unchosen one (round a corner), on the chosen side of the corner's bisector."""
                n_ = len(c)
                d_in = min((_seg_dist(p, c[j], c[(j + 1) % n_]) for j in chosen), default=9e9)
                if d_in > clear + 0.02:
                    return False
                d_out = min((_seg_dist(p, c[j], c[(j + 1) % n_]) for j in range(n_) if j not in chosen), default=9e9)
                if d_out > d_in + 1e-6:
                    return True
                K, a_, b_ = min(ends, key=lambda e: _d(p, e[0]))
                vx, vy = p[0] - K[0], p[1] - K[1]
                return vx * a_[0] + vy * a_[1] >= vx * b_[0] + vy * b_[1] - 1e-9

            def run_ends(c, chosen):
                """(corner, unit vector into the chosen side, unit vector into the unchosen side) at every end."""
                n_, out = len(c), []

                def unit(f, t_):
                    dx, dy = t_[0] - f[0], t_[1] - f[1]
                    ln = math.hypot(dx, dy) or 1.0
                    return dx / ln, dy / ln
                for j in chosen:
                    if (j - 1) % n_ not in chosen:   # run starts at point j
                        K = c[j]
                        out.append((K, unit(K, c[(j + 1) % n_]), unit(K, c[(j - 1) % n_])))
                    if (j + 1) % n_ not in chosen:   # run ends at point j + 1
                        K = c[(j + 1) % n_]
                        out.append((K, unit(K, c[j]), unit(K, c[(j + 2) % n_])))
                return out or [((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))]

            for wch in sorted(by_w):
                whole, runs = by_w[wch]
                dc = (wch + clear) / math.tan(half)
                items = loops_for(("hole", "part"), lambda k: clear if k == "part" else -clear, tc, "the chamfer",
                                  only={k for k in whole if not k.startswith("pocket:")})
                rims = {key: len(c) for key, kd, c in job.rim_shapes}
                rim_whole = {k for k in whole if k in rims}
                rim_runs = {}
                for key, ranges in runs.items():
                    if key in rims:
                        n_ = rims[key]
                        ch_ = rim_runs.setdefault(key, set())
                        for i0, i1 in ranges:
                            ch_.update((i0 + j) % n_ for j in range((i1 - i0) % n_ or n_))
                if rim_whole or rim_runs:
                    items += pocket_rims(clear, dc, only=rim_whole | set(rim_runs), picked=rim_runs)
                for key, ranges in runs.items():
                    if key in rims:
                        continue
                    kd, c = shape_of[key]
                    n_ = len(c)
                    chosen = set()
                    for i0, i1 in ranges:
                        chosen.update((i0 + j) % n_ for j in range((i1 - i0) % n_ or n_))
                    ends = run_ends(c, chosen)
                    for _, q, size, _ in loops_for((kd,), lambda k: clear if k == "part" else -clear, tc,
                                                   "the chamfer", only={key}):
                        inn = [member(p, c, chosen, ends) for p in q]
                        if all(inn):
                            items.append((kd, q, size, key))
                            continue
                        k0 = inn.index(False)
                        qq, ii = q[k0:] + q[:k0], inn[k0:] + inn[:k0]
                        run = []
                        for p, ok in zip(qq + [qq[0]], ii + [ii[0]]):
                            if ok:
                                run.append(p)
                            elif run:
                                if sum(_d(run[i], run[i + 1]) for i in range(len(run) - 1)) >= 0.5:
                                    items.append(("crun", run, size, key))
                                run = []
                ops.append({"kind": "chamfer", "tool": tc, "depth": dc, "tabs": False, "half": half, "items": items})

    # ---- chamfer: a V-bit cuts the top edge after the cut-out
    elif s["chamfer"]:
        tc = pick(s["chamfer_tool"], "the chamfer")
        wch = s["chamfer_w"]
        if tc["tool_type"] != VBIT:
            job.warnings.append("Chamfer needs a V-bit tool (set one on the Operations tab); it is left out.")
        elif wch <= 0:
            job.warnings.append("Chamfer width must be above zero; it is left out.")
        else:
            half = math.radians(max(10.0, min(170.0, tc["tool_angle"])) / 2)
            clear = 0.2  # the tip runs this far off the line so it never rubs the wall
            dc = (wch + clear) / math.tan(half)
            if 2 * dc * math.tan(half) > tc["tool_d"] + 1e-6:
                job.warnings.append(f"A {wch:.2f} mm chamfer is too wide for the {_tool_label(tc)}; it is left out.")
            else:
                if s["tabs"] > 0 and z_top - dc < z_tab + 0.1:
                    job.warnings.append("The chamfer reaches down to the tabs; make the tabs thicker or the chamfer smaller.")
                lib = next((t for t in tools if t.get("name") == tc["name"]), None)
                if lib is not None and not all(k in lib for k in SPEED_KEYS):
                    # no saved speeds: suggest them for the small diameter the V-bit actually cuts with
                    tc.update(tool_speeds(lib, s, eff_d=max(1.0, 2 * dc * math.tan(half))))
                ops.append({"kind": "chamfer", "tool": tc, "depth": dc, "tabs": False, "half": half,
                            "items": loops_for(("hole", "part"), lambda k: clear if k == "part" else -clear, tc,
                                               "the chamfer") + pocket_rims(clear, dc)})

    ops = [op for op in ops if op["items"]]
    if not ops:
        job.warnings.append("Nothing to cut.")
        return job

    # ---- run order: as few tool changes as possible. The surfacing tool first, then the tools that cut
    # holes and pockets, then the tool that cuts the outlines out, then the finishing tools (engraving,
    # chamfer, edge break) while the tabs still hold the parts. A tool's operations keep their order.
    phase = {"surface": 0, "pocket": 1, "hole": 1, "part": 2, "engrave": 3, "edge": 3, "chamfer": 3}
    first, rank = {}, {}
    for i, op in enumerate(ops):
        nm = op["tool"]["name"]
        first.setdefault(nm, i)
        kinds_ = rank.setdefault(nm, set())
        kinds_.add(op["kind"])
    tool_key = {nm: (0 if "surface" in kd else 2 if "part" in kd else min(phase[k] for k in kd), first[nm])
                for nm, kd in rank.items()}
    ops = sorted(ops, key=lambda op: (tool_key[op["tool"]["name"]], ops.index(op)))

    # ---- group operations that share a tool; number the tools
    groups = []
    for op in ops:
        if groups and groups[-1][0]["tool"]["name"] == op["tool"]["name"]:
            groups[-1].append(op)
        else:
            groups.append([op])
    tno = {}
    for g in groups:
        tno.setdefault(g[0]["tool"]["name"], len(tno) + 1)
    split = s["tc_mode"].startswith("One file per") and len(groups) > 1
    files = [[g] for g in groups] if split else [groups]

    counts = {"surface": 0, "pocket": 0, "engrave": 0, "hole": 0, "part": 0, "edge": 0, "chamfer": 0, "finish": 0}
    lift = 1.0   # short hops inside the drawing: just above the stock
    tab_pts = {}   # Agilite CNC: shape key -> tab centres (on its tool path) of the first pass that cut it

    def emit_pocket(o, op):
        """Zigzag rows at each depth, then one pass round the walls (and islands) at that depth."""
        t = op["tool"]
        o.t, o.f = t, None
        step = t["step"] if t["step"] > 0 else s["step"]
        z_end = z_top - op["depth"]
        passes = max(1, int(math.ceil((z_top - z_end) / step - 1e-9)))
        levels = [max(z_top - k * step, z_end) for k in range(1, passes + 1)]
        ramp_tan = math.tan(math.radians(max(1.0, min(float(t.get("ramp_angle", s["ramp_angle"])), 90.0))))
        items = list(op["items"])
        pos = (o.p["x"], o.p["y"])
        while items:
            k = min(range(len(items)), key=lambda i: min(_d(pos, p) for q in items[i][1]["loops"] for p in q))
            _, reg, size, _ = items.pop(k)
            counts["pocket"] += 1
            n = counts["pocket"]
            o.raw(f"(Pocket {n}: {size[0]:.2f} x {size[1]:.2f} mm, {op['depth']:.2f} mm deep, "
                  f"layer {_cmt(reg['layer'])})")
            zp = z_top
            for zt in levels:
                for chain in reg["chains"]:
                    a, b = chain[0]
                    o.g0(z=max(lift, o.p["z"]) if o.k["z"] else s["safe_z"])
                    o.g0(x=a[0], y=a[1])
                    o.g0(z=zp + 0.5)
                    o.g1(a[0], a[1], zp)
                    _ramp_line(o, a, b, zp, zt, ramp_tan)
                    for p, q in chain:
                        o.g1(p[0], p[1], zt)
                        o.g1(q[0], q[1], zt)
                for q in reg["loops"]:
                    j = min(range(len(q)), key=lambda i: _d((o.p["x"], o.p["y"]), q[i]))
                    q = q[j:] + q[:j]
                    o.g0(z=lift)
                    o.g0(x=q[0][0], y=q[0][1])
                    o.g0(z=zp + 0.5)
                    o.g1(q[0][0], q[0][1], zp)
                    rl = (zp - zt) / ramp_tan
                    L = sum(_d(q[i], q[(i + 1) % len(q)]) for i in range(len(q)))
                    _walk(o, q, True, 0.0, rl, zp, zt)
                    _walk(o, q, True, rl, rl + L, zt, zt)
                zp = zt
            o.g0(z=s["safe_z"])
            pos = (o.p["x"], o.p["y"])
            rows = [[(a, y), (b, y)] for row in reg["rows"] for (y, a, b) in row]
            for q in reg["loops"]:
                job.preview.append(("pocket", q, rows, n, t["tool_d"]))
                rows = []

    def emit_open(o, op, path, z_end, step, ramp_tan, n, size):
        """An open line, engraved: ramp in along the line, back and forth, then one clean pass at full depth."""
        o.raw(f"(Engraving {n}: open line {size[0]:.2f} x {size[1]:.2f} mm, {op['depth']:.2f} mm deep)")
        o.g0(z=s["safe_z"])
        o.g0(x=path[0][0], y=path[0][1])
        o.g0(z=z_top + 0.5)
        o.g1(path[0][0], path[0][1], z_top)
        L = sum(_d(path[i], path[i + 1]) for i in range(len(path) - 1))
        z, cur = z_top, path
        while z > z_end + 1e-9:
            zt = max(z - step, z_end)
            rl = min((z - zt) / ramp_tan, L) if L > 1e-9 else 0.0
            if rl > 1e-9:
                _walk(o, cur, False, 0.0, rl, z, zt)
            else:
                o.g1(cur[0][0], cur[0][1], zt)
            _walk(o, cur, False, rl, L, zt, zt)
            z, cur = zt, cur[::-1]
        _walk(o, cur, False, 0.0, L, z_end, z_end)   # clean-up: the ramp of the last pass at full depth
        o.g0(z=s["safe_z"])
    base = os.path.splitext(os.path.basename(s["dxf"]))[0] if s["dxf"] else "Surfacing"

    def emit_crun(o, op, path, z_end, step, ramp_tan, n):
        """Agilite CNC: a chamfer along an open run of chosen edges, always in the run's own direction (climb or
        conventional like the outline). Each depth pass ramps in back and forth along the start of the run (never
        a plunge onto the edge), then feeds the whole run; at the end it ramps out, rising back along the run."""
        L = sum(_d(path[i], path[i + 1]) for i in range(len(path) - 1))
        o.raw(f"(Chamfer {n}: chosen edges, {L:.2f} mm long, {op['depth']:.2f} mm deep)")
        z = z_top
        while z > z_end + 1e-9:
            zt = max(z - step, z_end)
            o.g0(z=s["safe_z"] if z >= z_top - 1e-9 else lift)
            o.g0(x=path[0][0], y=path[0][1])
            o.g0(z=z_top + 0.5)
            o.g1(path[0][0], path[0][1], z_top)
            # down from the top (through the groove of the passes before) by ramping along the first stretch of
            # the run, there and back, until zt; then back to the start
            span = min(L, max((z_top - zt) / ramp_tan / 2, 1.0))
            s_cur, zc = 0.0, z_top
            while zc > zt + 1e-9 and span > 1e-6:
                target = span if s_cur < span / 2 else 0.0
                leg = abs(target - s_cur)
                if zc - leg * ramp_tan <= zt:
                    leg = (zc - zt) / ramp_tan
                    target = s_cur + leg if target > s_cur else s_cur - leg
                    z1 = zt
                else:
                    z1 = zc - leg * ramp_tan
                if target > s_cur:
                    _walk(o, path, False, s_cur, target, zc, z1)
                else:
                    _walk(o, path[::-1], False, L - s_cur, L - target, zc, z1)
                s_cur, zc = target, z1
            if s_cur > 1e-9:
                _walk(o, path[::-1], False, L - s_cur, L, zt, zt)
            _walk(o, path, False, 0.0, L, zt, zt)
            # ramp out: rise back along the run, over the cut just made
            _walk(o, path[::-1], False, 0.0, min(L, (z_top + 0.2 - zt) / ramp_tan), zt, z_top + 0.2)
            z = zt
        o.g0(z=s["safe_z"])

    def emit_surface_region(o, op, rows, passes, step, sd, ramp_tan, so):
        """Agilite CNC: surfacing shaped to the part. Each row runs only across the region (from its first to its
        last crossing, so a row never lifts), back and forth, stepping to the next row's end on the way; each
        depth pass ramps in along the first row."""
        t = op["tool"]
        r = t["tool_d"] / 2
        spans = []
        for y in rows:
            xs = []
            for q in op["region"]:
                n_ = len(q)
                for j in range(n_):
                    (ax, ay), (bx, by) = q[j], q[(j + 1) % n_]
                    if (ay <= y < by) or (by <= y < ay):
                        xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
            if len(xs) >= 2:
                spans.append((y, min(xs), max(xs)))
        if not spans:
            return
        z = 0.0
        for k in range(1, passes + 1):
            zt = -min(k * step, sd)
            y, a0, b0 = spans[0]
            o.g0(z=s["safe_z"])
            o.g0(x=a0, y=y)
            o.g0(z=z + 0.5)
            o.g1(a0, y, z)
            L = b0 - a0
            if L < 1e-6:
                o.g1(a0, y, zt)
            else:
                remaining, cx, dirn, zc = (z - zt) / ramp_tan, a0, 1, z
                while remaining > 1e-9:
                    seg_ = min(remaining, L)
                    cx += dirn * seg_
                    zc -= seg_ * ramp_tan
                    o.g1(cx, y, max(zc, zt))
                    remaining -= seg_
                    dirn = -dirn
                if abs(cx - a0) > 1e-6:
                    o.g1(a0, y, zt)
            one_way = s["surf_pattern"].startswith("One")
            for i, (y, a, b) in enumerate(spans):
                if one_way:   # every row the same way: lift a little and go back to its start
                    if i > 0:
                        o.g0(z=zt + 1.0)
                        o.g0(x=a, y=y)
                        o.g1(a, y, zt)
                    o.g1(b, y, zt)
                    continue
                ea, eb = (a, b) if i % 2 == 0 else (b, a)
                if i > 0:
                    o.g1(ea, y, zt)
                o.g1(eb, y, zt)
            z = zt
        o.g0(z=s["safe_z"])
        loop = [(a - r, y) for y, a, b in spans] + [(b + r, y) for y, a, b in spans[::-1]]
        job.preview.append(("surface", loop, [[(a, y), (b, y)] for y, a, b in spans], counts["surface"], 0.0))

    def emit_surface_rings(o, op, passes, step, sd, ramp_tan, so):
        """Agilite CNC: surfacing in rings, from the edge of the area inwards (each ring the last one shrunk by the
        stepover). Every row of cut is the same direction, and the edge of the surfaced area is a clean line."""
        t = op["tool"]
        r = t["tool_d"] / 2
        x0, y0, x1, y1 = op["area"]
        base = op.get("region") or [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]
        base = [b if area(b) > 0 else b[::-1] for b in base if len(b) >= 3]
        rings, k = [list(b) for b in base], 1
        while k < 5000:
            lv = [q for b in base for q in offset(b, -k * so) if len(q) >= 3 and abs(area(q)) > 1e-4]
            if not lv:
                # the middle: one last ring a little inside the last one, so no ridge stays standing
                if r * 0.8 < so:
                    lv = [q for b in base for q in offset(b, -((k - 1) * so + r * 0.8)) if len(q) >= 3
                          and abs(area(q)) > 1e-4]
                    rings.extend(lv)
                break
            rings.extend(lv)
            k += 1

        def full_loop(loop, i0, pt, zb):
            """From pt (on the segment i0 -> i0+1) once round the loop at zb, back to pt."""
            n_ = len(loop)
            for j in range(1, n_ + 1):
                q = loop[(i0 + j) % n_]
                o.g1(q[0], q[1], zb)
            o.g1(pt[0], pt[1], zb)

        def ramp_round(loop, za, zb):
            """Ramp down along the loop from za to zb, then once round at zb."""
            n_ = len(loop)
            zc, i = za, 0
            if za - zb < 1e-9:
                full_loop(loop, n_ - 1, loop[0], zb)
                return
            while i < n_ * 200:
                a, b = loop[i % n_], loop[(i + 1) % n_]
                L = math.hypot(b[0] - a[0], b[1] - a[1])
                if L < 1e-9:
                    i += 1
                    continue
                dz = L * ramp_tan
                if zc - dz <= zb + 1e-9:
                    f = (zc - zb) / ramp_tan / L
                    pt = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                    o.g1(pt[0], pt[1], zb)
                    full_loop(loop, i % n_, pt, zb)
                    return
                zc -= dz
                o.g1(b[0], b[1], zc)
                i += 1

        z = 0.0
        for kp in range(1, passes + 1):
            zt = -min(kp * step, sd)
            prev = None
            for ring in rings:
                px, py = o.p["x"], o.p["y"]
                i0 = min(range(len(ring)), key=lambda j: (ring[j][0] - px) ** 2 + (ring[j][1] - py) ** 2)
                loop = ring[i0:] + ring[:i0]
                if prev is not None and inside(loop[0], prev):
                    o.g1(loop[0][0], loop[0][1], zt)   # one stepover in, at depth: inside what was just cut
                    full_loop(loop, len(loop) - 1, loop[0], zt)
                else:
                    o.g0(z=s["safe_z"])
                    o.g0(x=loop[0][0], y=loop[0][1])
                    o.g0(z=z + 0.5)
                    o.g1(loop[0][0], loop[0][1], z)
                    ramp_round(loop, z, zt)
                prev = ring
            z = zt
        o.g0(z=s["safe_z"])
        big = max(base, key=lambda b: abs(area(b)))
        edge = offset(big, r) or [big]
        job.preview.append(("surface", edge[0], [list(q) + [q[0]] for q in rings], counts["surface"], 0.0))

    def emit_surface(o, op):
        """Rows across the area, each depth pass ramped in along the first row."""
        t = op["tool"]
        o.t, o.f = t, None
        x0, y0, x1, y1 = op["area"]
        r = t["tool_d"] / 2
        # Agilite CNC: the designer's stepover, else the bit's own (a surfacing bit's), else the setting
        over = float(s.get("surf_over_set") or t.get("surf_over") or s["surf_over"])
        so = max(0.05, t["tool_d"] * over / 100)
        nrow = int(math.ceil((y1 - y0) / so - 1e-9)) + 1
        rows = [y0 + (y1 - y0) * i / (nrow - 1) for i in range(nrow)] if nrow > 1 else [y0]
        step = t["step"] if t["step"] > 0 else s["step"]
        sd = op["depth"]
        passes = max(1, int(math.ceil(sd / step - 1e-9)))
        ramp_tan = math.tan(math.radians(max(1.0, min(float(t.get("ramp_angle", s["ramp_angle"])), 90.0))))
        one_way = s["surf_pattern"].startswith("One")
        counts["surface"] += 1
        o.raw(f"(Surfacing: {x1 - x0:.1f} x {y1 - y0:.1f} mm, {sd:.2f} mm off the top in {passes} "
              f"{'pass' if passes == 1 else 'passes'}, "
              f"{len(rows)} rows {so:.2f} mm apart)")
        z = 0.0
        L = x1 - x0
        if s["surf_pattern"].startswith("Rings"):
            emit_surface_rings(o, op, passes, step, sd, ramp_tan, so)
            return
        if op.get("region"):
            emit_surface_region(o, op, rows, passes, step, sd, ramp_tan, so)
            return
        for k in range(1, passes + 1):
            zt = -min(k * step, sd)
            o.g0(z=s["safe_z"])
            o.g0(x=x0, y=rows[0])
            o.g0(z=z + 0.5)
            o.g1(x0, rows[0], z)
            if L < 1e-6:
                o.g1(x0, rows[0], zt)
                cx = x0
            else:   # ramp back and forth along the first row down to this pass
                remaining, cx, dirn, zc = (z - zt) / ramp_tan, x0, 1, z
                while remaining > 1e-9:
                    seg = min(remaining, L)
                    cx += dirn * seg
                    zc -= seg * ramp_tan
                    o.g1(cx, rows[0], max(zc, zt))
                    remaining -= seg
                    dirn = -dirn
                if abs(cx - x0) > 1e-6:
                    o.g1(x0, rows[0], zt)
            for i, y in enumerate(rows):
                if one_way:
                    if i > 0:
                        o.g0(z=zt + 1.0)
                        o.g0(x=x0, y=y)
                        o.g1(x0, y, zt)
                    o.g1(x1, y, zt)
                else:
                    a, b = (x0, x1) if i % 2 == 0 else (x1, x0)
                    if i > 0:
                        o.g1(a, y, zt)
                    o.g1(b, y, zt)
            z = zt
        o.g0(z=s["safe_z"])
        loop = [(x0 - r, y0 - r), (x1 + r, y0 - r), (x1 + r, y1 + r), (x0 - r, y1 + r)]
        job.preview.append(("surface", loop, [[(x0, y), (x1, y)] for y in rows], counts["surface"], 0.0))

    def emit(o, op):
        if op["kind"] == "surface":
            emit_surface(o, op)
            return
        if op["kind"] == "pocket":
            emit_pocket(o, op)
            return
        t = op["tool"]
        o.t, o.f = t, None
        dpt = op["depth"]
        # through cuts end at the fixed bottom; edge treatments are measured from the (surfaced) top
        z_end = -dpt if op["kind"] in ("hole", "part") else z_top - dpt
        step = t["step"] if t["step"] > 0 else s["step"]
        passes = max(1, int(math.ceil((z_top - z_end) / step - 1e-9)))
        ramp_tan = math.tan(math.radians(max(1.0, min(float(t.get("ramp_angle", s["ramp_angle"])), 90.0))))
        ordered, pos = [], (o.p["x"], o.p["y"])
        cutting = op["kind"] in ("hole", "part")
        for kinds in (("hole",), ("part",), ("engrave", "open", "crun")):   # Agilite CNC: + "crun"
            group = [it for it in op["items"] if it[0] in kinds]
            while group:
                def near(it):
                    if it[0] == "crun":   # Agilite CNC: a chamfer run keeps its direction
                        return _d(pos, it[1][0])
                    if it[0] == "open":
                        return min(_d(pos, it[1][0]), _d(pos, it[1][-1]))
                    if cutting and it[3] in man_starts:
                        return _d(pos, man_starts[it[3]])
                    return min(_d(pos, p) for p in it[1])
                kd, q, size, key = group.pop(min(range(len(group)), key=lambda k: near(group[k])))
                if kd == "crun":   # Agilite CNC
                    ordered.append((kd, q, size, key))
                    pos = q[-1]
                    continue
                if kd == "open":
                    if _d(pos, q[-1]) < _d(pos, q[0]):
                        q = q[::-1]
                    ordered.append((kd, q, size, key))
                    pos = q[-1]
                    continue
                if cutting and key in man_starts:   # start point clicked in the preview
                    i, pp, _, _ = project(q, man_starts[key])
                    q = _dedupe([pp] + q[i + 1:] + q[:i + 1], 1e-6)
                else:
                    start = min(range(len(q)), key=lambda k: _d(pos, q[k]))
                    q = q[start:] + q[:start]
                ordered.append((kd, q, size, key))
                pos = q[0]

        if op["kind"] == "engrave":
            vt = t["tool_type"]
            R = t["tool_d"] / 2
            if vt == VBIT:
                ebw = min(t["tool_d"], 2 * dpt * math.tan(math.radians(max(10.0, min(170.0, t["tool_angle"])) / 2)))
            elif vt == "Ball nose" and dpt < R:
                ebw = 2 * math.sqrt(R * R - (R - dpt) ** 2)
            else:
                ebw = t["tool_d"]

        fin = op.get("finish", False)
        if fin:   # one pass: ramp down along the wall (only the thin skin and the tabs are left), one lap
            passes, step = 1, z_top - z_end
        for kd, q, size, key in ordered:
            ck = "finish" if fin else op["kind"]
            counts[ck] += 1
            n = counts[ck]
            if kd == "crun":   # Agilite CNC: one chosen run of edges, chamfered
                emit_crun(o, op, q, z_end, step, ramp_tan, n)
                job.preview.append(("chamfer_open", q, [], n, 2 * dpt * math.tan(op["half"])))
                continue
            if kd == "open":
                emit_open(o, op, q, z_end, step, ramp_tan, n, size)
                job.preview.append(("engrave_open", q, [], n, ebw))
                continue
            P = q
            pts = P + [P[0]]
            C = [0.0]
            for i in range(len(P)):
                C.append(C[-1] + _d(pts[i], pts[i + 1]))
            L = C[-1]
            # Agilite CNC: internal windows have their own automatic tab count (hole_tabs, default: the outline's);
            # a window too small for a tab (see can_tab below) never takes one
            n_auto = s["tabs"] if kd == "part" else int(s.get("hole_tabs", s["tabs"]))
            # (a window takes a tab when its tool path is at least twice the tab and the bit together: a 10 mm
            # hole cut with a 4 mm bit can hold its slug with a 4 mm tab)
            can_tab = (op["tabs"] and (kd == "part" or (s["tabs_on_holes"] and L >= 2 * (s["tab_len"] + t["tool_d"])))
                       and z_tab > z_end + 1e-6)
            tabs_here = can_tab and n_auto > 0
            h = (s["tab_len"] + t["tool_d"]) / 2
            manual = cutting and key in man_tabs
            if manual:   # tabs clicked in the preview replace the automatic ones on this shape
                centers = sorted(project(P, p)[2] for p in man_tabs[key]) if can_tab else []
            elif cutting and key in tab_pts:
                # Agilite CNC: a later pass on the same shape (the finishing pass) used to place its automatic tabs
                # along its own, differently started loop, so it cut through the roughing tabs and left only
                # finish_allow-thin slivers. It now lifts where the first pass on this shape lifted.
                centers = sorted(project(P, p)[2] for p in tab_pts[key] if project(P, p)[3] < t["tool_d"])                     if can_tab else []
            else:
                if tabs_here and 2 * h * n_auto > 0.8 * L:
                    if not op.get("finish") and kd == "part":
                        no_tabs.setdefault(kd, []).append(size)
                    tabs_here = False
                centers = [L * (k + 0.5) / n_auto for k in range(n_auto)] if tabs_here else []

            def in_tab(v, centers=centers, L=L, h=h):
                m = v % L
                return any(((m - (c0 - h)) % L) < 2 * h for c0 in centers)

            def at(v, C=C, L=L, pts=pts, P=P):
                m = v % L
                k = min(bisect.bisect_right(C, m) - 1, len(P) - 1)
                seg = C[k + 1] - C[k]
                f = (m - C[k]) / seg if seg > 0 else 0.0
                return (pts[k][0] + (pts[k + 1][0] - pts[k][0]) * f, pts[k][1] + (pts[k + 1][1] - pts[k][1]) * f)

            if cutting and key not in tab_pts:   # Agilite CNC: where this shape's tabs are (see above)
                tab_pts[key] = [at(c0) for c0 in centers]

            def walk(s0, s1, z0, z1, C=C, L=L, centers=centers, h=h, at=at, in_tab=in_tab):
                bps = {s1}
                for m in range(int(math.floor(s0 / L)) - 1, int(math.floor(s1 / L)) + 2):
                    for ck in C[:-1]:
                        v = ck + m * L
                        if s0 < v < s1:
                            bps.add(v)
                    for c0 in centers:
                        for e in (c0 - h, c0 + h):
                            v = e + m * L
                            if s0 < v < s1:
                                bps.add(v)
                for v in sorted(bps):
                    zc = z0 + (z1 - z0) * (v - s0) / (s1 - s0) if s1 > s0 else z1
                    x, y = at(v)
                    zb = max(zc, z_tab) if in_tab(v - 1e-7) else zc
                    za = max(zc, z_tab) if in_tab(v + 1e-7) else zc
                    o.g1(x, y, zb)
                    if abs(za - zb) > 1e-6:
                        o.g1(x, y, za)

            what = "hole" if kd == "hole" else "outline"
            if fin:
                o.raw(f"(Finishing pass {n}: {what} {size[0]:.2f} x {size[1]:.2f} mm, takes the last "
                      f"{s['finish_allow']:.2f} mm off the wall)")
            elif op["kind"] in ("hole", "part"):
                o.raw(f"({what.capitalize()} {n}: {size[0]:.2f} x {size[1]:.2f} mm, tool runs "
                      f"{'inside' if kd == 'hole' else 'outside'} the line"
                      + (f", {s['finish_allow']:.2f} mm left for finishing)" if op_has_finish(kd, key) else ")"))
            else:
                o.raw(f"({OP_NAMES[op['kind']].capitalize()} {n}: {what} {size[0]:.2f} x {size[1]:.2f} mm, "
                      f"{dpt:.2f} mm deep)")
            x0, y0 = at(0.0)
            o.g0(z=s["safe_z"])
            o.g0(x=x0, y=y0)
            o.g0(z=z_top + 0.5)
            o.g1(x0, y0, z_top)
            pos_s, z = 0.0, z_top
            for k in range(1, passes + 1):
                zt = max(z_top - k * step, z_end)
                rl = (z - zt) / ramp_tan
                walk(pos_s, pos_s + rl, z, zt)
                pos_s += rl
                walk(pos_s, pos_s + L, zt, zt)
                pos_s += L
                z = zt
            o.g0(z=s["safe_z"])

            samples = [[at(c0 - h + 2 * h * i / 12) for i in range(13)] for c0 in centers]
            if op["kind"] == "chamfer":
                band = 2 * dpt * math.tan(op["half"])
            elif op["kind"] == "edge":
                band = 2 * math.sqrt(max(0.0, (t["tool_d"] / 2) ** 2 - (t["tool_d"] / 2 - dpt) ** 2))
            elif op["kind"] == "engrave":
                band = ebw
            else:
                band = t["tool_d"]
            if fin:
                job.preview.append(("finish", q, [], n, 0.0))
                continue
            job.preview.append((op["kind"] if op["kind"] in ("edge", "chamfer", "engrave") else kd, q, samples, n,
                                band))
            if op["kind"] == "hole" and not centers and key in slug_of:
                job.slugs[key] = slug_of[key]
            if cutting:
                job.loops.append({"key": key, "kind": kd, "loop": q, "tabs": [at(c0) for c0 in centers],
                                  "manual": manual, "can_tab": can_tab, "h": h})

    # ---- the sensor spot: every tool is measured there, on stock the job never cuts or surfaces
    bx_ = [p[0] for c in job.contours + job.open_paths + [m_[0] for m_ in job.mark_paths] for p in c]
    by_ = [p[1] for c in job.contours + job.open_paths + [m_[0] for m_ in job.mark_paths] for p in c]
    px_, py_ = (min(bx_) if bx_ else 0.0) + s["probe_dx"], (min(by_) if by_ else 0.0) + s["probe_dy"]
    job.probe = (px_, py_, s["probe_d"] / 2)
    measuring = s["probe_first"] or not s["tc_mode"].startswith("One file per")

    def pause(o, line):
        """An M0 with its message; kept for the run instructions and the playback."""
        o.raw(line)
        if line.upper().startswith("M0"):
            msg = line[2:].strip().strip("()").strip()
            o.pauses.append((len(o.segs), msg or "Paused: Cycle Start to carry on"))

    def measure(o, t, n, first):
        """Lift to the machine top, spindle off, go to the sensor spot, the change code (pause, M31, pause)."""
        if first and s.get("user_zero"):   # Agilite CNC: the operator zeroed T1 on the stock before starting
            return
        if not first and s.get("bed_sensor"):   # Agilite CNC: bit changes measured on the fixed bed sensor
            bed_change(o, s, t, n, lambda line: pause(o, line))
            return
        o.raw(f"(---- {'Start' if first else 'Tool change'}: T{n} {_cmt(t['name'])}, measured at the sensor spot "
              f"X{px_:.1f} Y{py_:.1f} ----)")
        if not first:
            o.g0(z=s["safe_z"])
        show_lift(o, t)
        o.machine(z=-3.0)
        o.raw("M5")
        o.g0(x=px_, y=py_)
        show_sensor(o, px_, py_, t)
        for line in s["tc_code"].split(";"):
            if first:   # the first tool is already in: only the sensor is wanted
                for w in ("end mill", "bit"):   # (older settings still say "bit")
                    line = line.replace(f"Put in T{{tool}} {{name}}, sensor under the {w}", "Sensor under T{tool} {name}")
            line = line.strip().replace("{tool}", str(n)).replace("{name}", _cmt(t["name"]))
            if line:
                pause(o, line)
        o.forget()

    total = 0.0
    nfile = len(files)
    run_groups = []
    for fi, fgroups in enumerate(files):
        o = _Out()
        label = f" - part {fi + 1} of {nfile}" if split else ""
        o.raw(f"(G-code Maker - {_cmt(base)}{_cmt(label)})")
        o.raw(f"(Source: {_cmt(s['dxf'])})")
        for g in fgroups:
            t = g[0]["tool"]
            st = t["step"] if t["step"] > 0 else s["step"]
            o.raw(f"(T{tno[t['name']]} {_cmt(t['name'])} = {_cmt(_tool_label(t))}: "
                  f"{', '.join(dict.fromkeys(OP_NAMES[op['kind']] for op in g))})")
            o.raw(f"(   {int(t['rpm'])} rpm, feed {t['feed']:g}, ramp {t['ramp_feed']:g}, plunge {t['plunge_feed']:g} mm/min, "
                  f"{st:.2f} mm per pass)")
        if contours:
            o.raw(f"(Material: {_cmt(s['material'])}; stock {s['thickness']:.2f} mm, cut depth {depth:.2f} mm)")
            o.raw(f"(Tabs: {s['tabs']} per outline, {s['tab_len']:.1f} mm long, {s['tab_thick']:.2f} mm of material left)")
        o.raw(f"(Milling: {s['direction'].lower()}; origin: {s['origin'].lower()}; Z0 = top of stock"
              + (f" before surfacing, new top Z{z_top:.2f}" if z_top < 0 else "") + ")")
        t1 = fgroups[0][0]["tool"]
        if measuring and not s.get("user_zero"):
            o.raw(f"(Before starting: home the machine, put T{tno[t1['name']]} in, zero X and Y on the stock. "
                  f"Z is measured by the program.)")
        else:
            o.raw(f"(Put T{tno[t1['name']]} in the spindle and zero Z on the stock before starting)")
        o.raw("TIME_PLACEHOLDER")
        o.raw("G21 G90 G17 G94")
        for gi, g in enumerate(fgroups):
            t = g[0]["tool"]
            n_ = tno[t["name"]]
            t_before, p_before = o.time, len(o.pauses)
            if gi > 0 or measuring:
                measure(o, t, n_, first=(gi == 0))
            run_groups.append({"n": n_, "tool": t, "file": fi, "first_in_file": gi == 0,
                               "ops": list(dict.fromkeys(OP_NAMES[op["kind"]] for op in g)),
                               "pauses": [m_ for _, m_ in o.pauses[p_before:]], "t0": t_before})
            # Agilite CNC (Lev 2026-11-15): Stepcraft's UCCNC ignores the S on an "M3 S24000" line (M3 runs its
            # own macro) and spins at its 10000 minimum; S on its own line first sets it
            o.raw(f"S{int(t['rpm'])}")
            o.raw("M3")
            for op in g:
                o.stages.append((len(o.segs), f"T{n_} {t['name']}: {OP_NAMES[op['kind']]}"
                                 + (" (finishing pass)" if op.get("finish") else "")))
                emit(o, op)
            run_groups[-1]["time"] = o.time - t_before
        _park(o, s, final=True)
        o.lines[o.lines.index("TIME_PLACEHOLDER")] = f"(Estimated time {_fmt_time(o.time)})"
        total += o.time
        job.files.append((label, o.lines))
        off_ = len(job.segs)
        job.pauses += [(off_ + i_, msg, fi) for i_, msg in o.pauses]
        job.stages += [(off_ + i_, msg) for i_, msg in o.stages]
        job.segs += o.segs
        job.seg_info += [(dt, f_, fi, li) for dt, f_, li in o.info]
    # safety net: a rapid that moves sideways below the stock top would crash into it
    bad = [sg for sg in job.segs if sg[6] and min(sg[2], sg[5]) < -1e-6 and math.hypot(sg[3] - sg[0], sg[4] - sg[1]) > 1e-3]
    if bad:
        job.warnings.append(f"{len(bad)} rapid move(s) go sideways below the top of the stock, e.g. at "
                            f"X{bad[0][3]:.2f} Y{bad[0][4]:.2f} Z{bad[0][5]:.2f}. Please report this; do not run it.")
    job.lines = job.files[0][1]
    job.time_min = total
    # the sensor must sit on stock the job never touches: Z0 is the ORIGINAL top
    # (Agilite CNC: the operator zeroes T1 before starting; the puck spot is only used for bit changes when there
    # is no bed sensor, and then it must sit on stock no bit cuts)
    puck_changes = measuring and not s.get("bed_sensor") and len(run_groups) > 1
    puck = puck_changes or (measuring and not s.get("user_zero"))
    if not puck:
        job.probe = None
    if puck:
        pr = s["probe_d"] / 2
        hit = None
        for sg in job.segs:
            if sg[6] or min(sg[2], sg[5]) > -1e-3:
                continue
            dd = _seg_dist((px_, py_), (sg[0], sg[1]), (sg[3], sg[4])) - sg[7]["tool_d"] / 2
            if dd < pr and (hit is None or dd < hit[0]):
                hit = (dd, sg[7]["name"])
        if hit:
            job.warnings.insert(0, f"The sensor spot (the {s['probe_d']:g} mm circle) lies on stock this job cuts or "
                                   f"surfaces, so the tools would be measured on a lower surface. Drag the sensor "
                                   f"onto untouched stock.")

    def sizes(lst):
        """'42.0 x 4.0 mm ×2, 32.0 x 4.0 mm' - the same size counted, not repeated."""
        cnt = {}
        for sz in lst:
            k_ = f"{sz[0]:.1f} x {sz[1]:.1f} mm"
            cnt[k_] = cnt.get(k_, 0) + 1
        return ", ".join(k_ + (f" ×{n_}" if n_ > 1 else "") for k_, n_ in cnt.items())
    # slots exactly as wide as the bit come out right: no warning, a line in the summary
    exact = [x for x in slot_mid if x[2]["tool_d"] - x[1] < 0.01]
    wider = [x for x in slot_mid if x[2]["tool_d"] - x[1] >= 0.01]
    if wider:
        worst = max(x[2]["tool_d"] - x[1] for x in wider)
        n_ = len(wider)
        job.warnings.append(
            f"{n_} slot{'s are' if n_ > 1 else ' is'} narrower than the {_tool_label(wider[0][2])} "
            f"({sizes([x[0] for x in wider])}): cut with one pass down the middle, so "
            f"{'they come' if n_ > 1 else 'it comes'} out as wide as the end mill, up to {worst:.2f} mm wider than drawn.")
    for kd, lst in no_tabs.items():
        n_ = len(lst)
        what = ("hole" if kd == "hole" else "outline") + ("s are" if n_ > 1 else " is")
        job.warnings.append(f"{n_} {what} too small for {s['tabs']} tab{'s' if s['tabs'] > 1 else ''} of "
                            f"{s['tab_len']:g} mm ({sizes(lst)}), so {'they are' if n_ > 1 else 'it is'} cut without "
                            f"tabs" + (": the loose pieces can fly when the cut finishes." if kd == "hole" else "."))
    job.slot_exact = len(exact)

    # ---- what to do at the machine, step by step (shown and copied when the G-code is saved)
    where0 = ("the lower-left corner of the drawing" if s["origin"].startswith("Lower") else
              "the centre of the drawing" if s["origin"].startswith("Centre") else "the DXF's 0,0 point")
    steps = []
    if contours or job.mark_paths:
        steps.append(f"Clamp the {s['thickness']:g} mm {s['material'].lower()} stock, clamps outside the cuts.")
    if s.get("user_zero"):
        if puck_changes:
            dx_, dy_ = s["probe_dx"], s["probe_dy"]
            steps.append(f"For the end mill changes, keep a {s['probe_d']:g} mm circle free for the sensor, centred "
                         f"{abs(dx_):g} mm {'left of' if dx_ < 0 else 'right of'} and {abs(dy_):g} mm "
                         f"{'below' if dy_ < 0 else 'above'} the drawing's lower-left corner.")
        steps.append("Home the machine" + (" (end mill changes go to the bed sensor in machine coordinates)."
                                            if s.get("bed_sensor") and len(run_groups) > 1 else "."))
    elif measuring:
        dx_, dy_ = s["probe_dx"], s["probe_dy"]
        steps.append(f"Keep the sensor spot free: a {s['probe_d']:g} mm circle centred {abs(dx_):g} mm "
                     f"{'left of' if dx_ < 0 else 'right of'} and {abs(dy_):g} mm {'below' if dy_ < 0 else 'above'} "
                     f"the drawing's lower-left corner, on stock that is not cut or surfaced.")
        steps.append("Home the machine (the program lifts to the machine top for every tool change).")
    first_g = run_groups[0] if run_groups else None
    if first_g:
        steps.append(f"Put T{first_g['n']} ({first_g['tool']['name']}) in the spindle.")
    steps.append(f"Jog to {where0} on the stock and zero X and Y. Do not zero X or Y again until the job is done.")
    if s.get("user_zero"):
        steps.append("Zero Z: set the tool sensor on the stock over the area that gets cut, measure with M31, then put the sensor back in its holder.")
    for gi, rg in enumerate(run_groups):
        if rg["first_in_file"]:
            fname = "the G-code file" if nfile == 1 else f"file {rg['file'] + 1} of {nfile}"
            steps.append(f"Load {fname} in UCCNC and press Cycle Start.")
            if not measuring:
                steps.append(f"(Before that: zero Z with T{rg['n']} on the top of the stock.)")
        if rg["pauses"]:
            steps.append("The machine lifts, stops the spindle and goes to the bed sensor." if s.get("bed_sensor")
                         and not rg["first_in_file"] else "The machine lifts, stops the spindle and goes to the sensor spot.")
        for m_ in rg["pauses"]:
            steps.append(f"Pause: {m_.rstrip('.')}.")
        mins = rg.get("time", 0)
        steps.append(f"T{rg['n']} cuts: {', '.join(rg['ops'])} (about {max(1, round(mins))} min).")
    xy_ = s["end_xy"]
    end_ = ("X0 Y0" if xy_.startswith("X0") else f"X{s['end_x']:g} Y{s['end_y']:g}" if xy_.startswith("Custom") else
            "the spot where it ends" if xy_.startswith("Stay") else "the machine corner")
    steps.append(f"Done: the spindle stops and the machine parks at {end_}. Break the parts out of their tabs.")
    job.run_steps = steps

    allx = [p[0] for c in job.contours + job.open_paths + [m_[0] for m_ in job.mark_paths] for p in c]
    ally = [p[1] for c in job.contours + job.open_paths + [m_[0] for m_ in job.mark_paths] for p in c]
    job.summary = []
    if allx:
        nman = sum(1 for lp in job.loops if lp["manual"])
        job.summary += [
            f"Drawing: {max(allx) - min(allx):.2f} x {max(ally) - min(ally):.2f} mm",
            f"Cuts: {counts['hole']} hole/slot (inside), {counts['part']} outline (outside)"
            + (f", {counts['pocket']} pocket" if counts["pocket"] else "")
            + (f", {counts['engrave']} engraving" if counts["engrave"] else "")
            + (f", {counts['finish']} finishing pass" if counts["finish"] else "")
            + (f", {counts['edge']} edge break" if counts["edge"] else "")
            + (f", {counts['chamfer']} chamfer" if counts["chamfer"] else ""),
        ]
        if job.slot_exact:
            job.summary.append(f"{job.slot_exact} slot{'s are' if job.slot_exact > 1 else ' is'} exactly as wide "
                               f"as the end mill: cut in one pass down the middle")
        if counts["finish"]:
            job.summary.append(f"Finishing: roughing leaves {s['finish_allow']:.2f} mm on the walls, then one "
                               f"full-depth pass per wall takes it to size")
        for op in ops:
            if op["kind"] in ("pocket", "engrave"):
                what = OP_NAMES[op['kind']][:-1] if op['kind'] == 'pocket' else 'engraving'
                if str(op["layer"]).startswith("mark "):
                    what = "filled" if op["kind"] == "pocket" else "engraved"
                    job.summary.append(f"{op['layer'][0].upper()}{op['layer'][1:]}: {what} {op['depth']:.2f} mm deep "
                                       f"with {_tool_label(op['tool'])}")
                else:
                    job.summary.append(f"Layer \"{op['layer']}\": {what} {op['depth']:.2f} mm deep with "
                                       f"{_tool_label(op['tool'])}")
        if contours:
            job.summary += [
                f"Depth: {depth:.2f} mm = stock {s['thickness']:.2f} + extra {s['extra']:.2f}",
                (f"Tabs: top at Z{z_tab:.2f}, leaving {s['tab_thick']:.2f} mm of material" if s["tabs"] > 0 or nman
                 else "Tabs: none") + (f"; {nman} shape(s) with tabs placed by hand" if nman else ""),
            ]
    sop = next((op for op in ops if op["kind"] == "surface"), None)
    if sop:
        a = sop["area"]
        job.summary.append(f"Surfacing first: {a[2] - a[0]:.1f} x {a[3] - a[1]:.1f} mm, {sop['depth']:.2f} mm off "
                           f"the top" + (", the other cuts start from the new top" if contours else ""))
    for g in groups:
        t = g[0]["tool"]
        job.summary.append(f"T{tno[t['name']]} {t['name']}: {', '.join(dict.fromkeys(OP_NAMES[op['kind']] for op in g))}"
                           f"  ({int(t['rpm'])} rpm, feed {t['feed']:g})")
    if split:
        job.summary.append(f"Saves {nfile} files, one per tool change; zero Z again after each change.")
    elif len(groups) > 1:
        job.summary.append(f"One file, {len(groups) - 1} tool change{'s' if len(groups) > 2 else ''}: the machine "
                           f"stops at the sensor spot and measures each tool")
    job.summary.append(f"Estimated time: {_fmt_time(total)}   ({sum(len(f[1]) for f in job.files)} lines)")
    return job


# ------------------------------------------------------------ feeds and speeds

def _round(v, step):
    return round(v / step) * step


def suggest(s):
    """Suggested spindle speed, feeds and depth per pass for the tool and material in s."""
    m = MATERIALS.get(s["material"], MATERIALS["Aluminium"])
    d = max(s["tool_d"], 0.1)
    z = max(1, int(s["flutes"]))
    k = TOOL_MATS.get(s["tool_mat"], 1.0)
    vlo, vhi = m["vc"][0] * k, m["vc"][1] * k
    fz = max(m["fz"] * d, m["fmin"])
    lo, hi = sorted((max(1, s["rpm_min"]), max(1, s["rpm_max"])))
    notes = []

    rpm = (vlo + vhi) / 2 * 1000 / (math.pi * d)
    if rpm > hi:
        rpm = hi
        notes.append(f"The spindle tops out at {hi} rpm, so the cutting speed stays below the ideal; that is fine.")
    elif rpm < lo:
        rpm = lo
        notes.append(f"The spindle cannot go below {lo} rpm; the feed is raised to match.")
    feed = rpm * z * fz
    if feed > s["feed_max"] > 0:
        feed = s["feed_max"]
        new_rpm = feed / (z * fz)
        rpm = max(lo, new_rpm)
        notes.append(f"Feed held at the machine limit {s['feed_max']:g} mm/min, so the spindle is slowed to keep the chip size.")
    rpm = int(_round(rpm, 100))
    feed = max(10.0, _round(feed, 10))
    step = max(0.05, _round(m["step"] * d, 0.05))
    if s["material"] == "Mild steel":
        notes.append("Steel is hard work for the M700: light passes, cutting fluid, and watch the tool.")
    if s["tool_type"] == "Ball nose":
        notes.append("Ball nose: the tip cuts slower than the sides, so keep passes shallow.")
    return {
        "rpm": rpm, "feed": feed,
        "ramp_feed": max(10.0, _round(feed / 2, 10)),
        "plunge_feed": max(10.0, _round(feed / 5, 10)),
        "step": round(step, 2),
        "fz": feed / (rpm * z), "fz_target": fz,
        "vc": math.pi * d * rpm / 1000, "vc_range": (vlo, vhi),
        "notes": notes,
    }


def check_speeds(s, sug):
    """(text, tag) lines judging the speeds now in the window."""
    z = max(1, int(s["flutes"]))
    out = []
    if s["rpm"] <= 0 or s["feed"] <= 0:
        return [("Spindle and feed must be above zero.", "warn")]
    fz = s["feed"] / (s["rpm"] * z)
    vc = math.pi * s["tool_d"] * s["rpm"] / 1000
    t = sug["fz_target"]
    if fz < 0.6 * t:
        out.append((f"Chip {fz:.3f} mm/tooth: thin, may rub and heat the tool. Lower the rpm or feed faster.", "amber"))
    elif fz > 1.6 * t:
        out.append((f"Chip {fz:.3f} mm/tooth: heavy, risk of chatter or a broken tool. Slow the feed.", "warn"))
    else:
        out.append((f"Chip {fz:.3f} mm/tooth: good (aim {t:.3f}).", "ok"))
    vlo, vhi = sug["vc_range"]
    if vc > vhi * 1.25:
        out.append((f"Cutting speed {vc:.0f} m/min: above the {vlo:.0f}-{vhi:.0f} range, the tool may overheat.", "warn"))
    elif vc < vlo * 0.5:
        out.append((f"Cutting speed {vc:.0f} m/min: well below the {vlo:.0f}-{vhi:.0f} range.", "amber"))
    else:
        out.append((f"Cutting speed {vc:.0f} m/min (range {vlo:.0f}-{vhi:.0f}).", "ok"))
    if s["step"] > sug["step"] * 1.5:
        out.append((f"Depth per pass {s['step']:.2f} mm is deep for this tool and material.", "amber"))
    return out


def tool_speeds(tool, s, eff_d=None):
    """Speeds for a library tool: the ones saved with it, else a suggestion for the material."""
    if all(k in tool for k in SPEED_KEYS):
        return _scaled({k: float(tool[k]) for k in SPEED_KEYS}, s)
    t = dict(s)
    t.update(tool_d=eff_d or float(tool["tool_d"]), flutes=int(tool.get("flutes", 2)),
             tool_mat=tool.get("tool_mat", "Carbide"), tool_type=tool.get("tool_type", "Flat end mill"))
    g = suggest(t)
    return _scaled({k: float(g[k]) for k in SPEED_KEYS}, s)


def _scaled(sp, s):
    """Agilite CNC: the designer's feed, depth-per-pass and spindle percentages (100 = the bit's own numbers),
    kept inside the machine's limits."""
    f, d, r = (float(s.get(k) or 100.0) / 100.0 for k in ("feed_pct", "step_pct", "rpm_pct"))
    if f != 1.0:
        fmax = float(s.get("feed_max") or 1e9)
        for k in ("feed", "ramp_feed", "plunge_feed"):
            sp[k] = round(min(fmax, max(1.0, sp[k] * f)), 1)
    if d != 1.0:
        sp["step"] = round(max(0.02, sp["step"] * d), 3)
    if r != 1.0:
        sp["rpm"] = round(min(float(s.get("rpm_max") or 1e9), max(float(s.get("rpm_min") or 0), sp["rpm"] * r)))
    return sp


def migrate_tools(s, tools):
    """Once: every library tool gets its own speeds and feeds saved. The tool the outlines used keeps the
    numbers that were in the window (they were its speeds); the others get the suggestion for the material."""
    if s.get("tools_v2"):
        return False
    try:
        p = parse(s)
    except ValueError:
        p = None
    for t in tools:
        if p is not None and t.get("name") == p["tool_name"]:
            for k in SPEED_KEYS:
                t[k] = float(p[k])
            t["ramp_angle"] = float(p["ramp_angle"])
        elif not all(k in t for k in SPEED_KEYS) and p is not None:
            t.update(tool_speeds(t, p))
            t.setdefault("ramp_angle", float(p["ramp_angle"]))
    save_tools(tools)
    s["tools_v2"] = True
    return True


def load_tools():
    try:
        with open(TOOLS_FILE, encoding="utf-8") as f:
            tools = [t for t in json.load(f) if isinstance(t, dict) and t.get("name")]
        if tools:
            if not any(t.get("tool_type") == VBIT for t in tools):  # lists from before chamfering existed
                tools.append(dict(DEFAULT_TOOLS[-1]))
                save_tools(tools)
            return tools
    except (OSError, ValueError):
        pass
    tools = [dict(t) for t in DEFAULT_TOOLS]
    save_tools(tools)
    return tools


def save_tools(tools):
    try:
        with open(TOOLS_FILE, "w", encoding="utf-8") as f:
            json.dump(tools, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------- settings

def load_settings():
    s = json.loads(json.dumps(DEFAULTS))   # a copy, so the dict settings are never shared with DEFAULTS
    raw = {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
            s.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    for k in DICT_KEYS:
        if not isinstance(s[k], type(DEFAULTS[k])):
            s[k] = type(DEFAULTS[k])()
    # the one typed text of the version before marks becomes the first mark (its X Y was the lower left)
    if not s["marks"] and isinstance(raw, dict) and raw.get("text_on") and str(raw.get("text", "")).strip():
        m = new_mark("text")
        m.update(text=str(raw["text"]).replace("|", "\n"), size=raw.get("text_h", 5), angle=raw.get("text_angle", 0),
                 depth=raw.get("text_depth", 0.2), align="Left")
        _, w, h, _ = mark_shapes(m)
        m["x"] = round(mark_num(raw, "text_x") + w / 2, 2)
        m["y"] = round(mark_num(raw, "text_y") + h / 2, 2)
        s["marks"] = [m]
    # settings saved by older versions
    for k in TOOL_KEYS:
        if s.get(k) == MAIN_OLD:
            s[k] = MAIN
    if not s["workflow_v2"]:   # 2026-11-13: Lev chose one file that pauses and measures at every tool change
        s["tc_mode"] = "One file, pause and measure"
        s["workflow_v2"] = True
    if s["tc_code"] in OLD_TC_CODES:
        s["tc_code"] = DEFAULTS["tc_code"]
    if s["tc_mode"] not in TC_MODES:
        s["tc_mode"] = TC_MODES[1]
    if s["end_z_mode"] not in END_Z:
        s["end_z_mode"] = next((m for m in END_Z if m[:6] == s["end_z_mode"][:6]), END_Z[0])
    if s["end_xy"] not in END_XY:
        s["end_xy"] = next((m for m in END_XY if m[:6] == s["end_xy"][:6]), END_XY[0])
    return s


JOBS_FILE = os.path.join(HERE, "G-code Maker jobs.json")


def load_jobs():
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def save_jobs(jobs):
    try:
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=1)
    except OSError:
        pass


def job_of(s):
    """The part of the settings that belongs to the drawing."""
    return {k: json.loads(json.dumps(s[k])) for k in JOB_KEYS if k in s}


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


def parse(raw):
    s = dict(raw)
    for k, v in DEFAULTS.items():
        if isinstance(v, (dict, list)):
            s[k] = json.loads(raw[k]) if isinstance(raw[k], str) else raw[k]
        elif isinstance(v, bool):
            s[k] = raw[k] if isinstance(raw[k], bool) else str(raw[k]).strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(v, int):
            s[k] = int(float(str(raw[k]).replace(",", ".")))
        elif isinstance(v, float):
            s[k] = float(str(raw[k]).replace(",", "."))
    return s


def default_name(dxf):
    return os.path.splitext(os.path.basename(dxf))[0] + ".ngc" if dxf else ""


def file_paths(job, path):
    """One path per file of the job: 'name.ngc', or 'name - part 1 of 2.ngc', ... when each tool has its own file."""
    if len(job.files) == 1:
        return [path]
    root, ext = os.path.splitext(path)
    return [root + label + ext for label, _ in job.files]


def write_job(lines, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write("\n".join(safe_line(ln) for ln in lines) + "\n")   # (lines that bypassed _Out.raw too)


# ------------------------------------------------------------ finished-part preview

def _thin(run, tol):
    """Fewer points along a run of joined moves at one height (arcs are sampled far finer than the preview grid)."""
    out = [run[0]]
    for i in range(1, len(run) - 1):
        a, b, c = out[-1], run[i], run[i + 1]
        if _seg_dist(b, a, c) > tol:
            out.append(b)
    out.append(run[-1])
    return out


def simulate(job, s, fit_w, fit_h, max_cells=320000, region=None):
    """Height of the material left after every move, on a grid: the stock starts flat at Z0.
    fit_w x fit_h is the picture size in pixels; region (x0, x1, y0, y1) mm = only that part, zoomed in.
    Returns None when there is nothing cut."""
    segs = [sg for sg in job.segs if min(sg[2], sg[5]) < 0]
    if not segs:
        return None
    rmax = max(sg[7]["tool_d"] for sg in segs) / 2
    xs = [v for sg in segs for v in (sg[0], sg[3])] + [p[0] for c in job.contours for p in c]
    ys = [v for sg in segs for v in (sg[1], sg[4])] + [p[1] for c in job.contours for p in c]
    m = rmax + 3.0
    full = (min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m)
    X0, X1, Y0, Y1 = region or full
    ppm = min(fit_w / (X1 - X0), fit_h / (Y1 - Y0))      # pixels per mm when it fills the view
    finest = max(1.0 / ppm, math.sqrt((X1 - X0) * (Y1 - Y0) / max_cells), 0.005)
    cell = math.ceil(finest * ppm - 1e-9) / ppm   # a whole number of pixels per cell, so the picture fills the view
    nx, ny = int((X1 - X0) / cell) + 1, int((Y1 - Y0) / cell) + 1
    grid = [[0.0] * nx for _ in range(ny)]   # row 0 = lowest Y

    # flat moves at one height: join neighbours into runs and thin them out
    pieces, run, key = [], None, None
    for sg in segs:
        x0, y0, z0, x1, y1, z1, rapid, t = sg
        flat = abs(z0 - z1) < 1e-9
        k = (id(t), z0) if flat else None
        if k is not None and run is not None and k == key and abs(run[-1][0] - x0) < 1e-9 and abs(run[-1][1] - y0) < 1e-9:
            run.append((x1, y1))
            continue
        if run is not None:
            pieces.append((run, key[1], key[1], t_run))
        if k is not None:
            run, key, t_run = [(x0, y0), (x1, y1)], k, t
        else:
            run, key = None, None
            pieces.append(([(x0, y0), (x1, y1)], z0, z1, t))
    if run is not None:
        pieces.append((run, key[1], key[1], t_run))
    # the same path cut again deeper (every pass of a loop): only the deepest one shapes the part
    moves, lowest = [], {}
    for pts, za, zb, t in pieces:
        if za != zb:
            moves.append((pts[0][0], pts[0][1], za, pts[1][0], pts[1][1], zb, t))
            continue
        pts = _thin(pts, cell * 0.25)
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            kk = (round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3), id(t))
            if kk not in lowest or za < lowest[kk][2]:
                lowest[kk] = (a[0], a[1], za, b[0], b[1], za, t)
    moves += lowest.values()

    for x0, y0, z0, x1, y1, z1, t in moves:
        r = t["tool_d"] / 2
        if (max(x0, x1) + r < X0 or min(x0, x1) - r > X1 or max(y0, y1) + r < Y0 or min(y0, y1) - r > Y1):
            continue   # (zoomed in: moves outside the view do nothing here)
        kind = t["tool_type"]
        tan_h = math.tan(math.radians(max(10.0, min(170.0, t.get("tool_angle", 90.0))) / 2))
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        L = math.sqrt(L2)
        simple = kind in ("Flat end mill", SURF) and abs(z0 - z1) < 1e-9
        j0 = max(0, int((min(y0, y1) - r - Y0) / cell))
        j1 = min(ny - 1, int((max(y0, y1) + r - Y0) / cell) + 1)
        for j in range(j0, j1 + 1):
            yc = Y0 + (j + 0.5) * cell
            lo, hi = math.inf, -math.inf
            for cx, cy in ((x0, y0), (x1, y1)):
                hh = r * r - (yc - cy) ** 2
                if hh >= 0:
                    w = math.sqrt(hh)
                    lo, hi = min(lo, cx - w), max(hi, cx + w)
            if L > 1e-9:   # the band between the two end circles
                a_lo, a_hi = -math.inf, math.inf
                # 0 <= ((x-x0)dx + (yc-y0)dy)/L <= L   and   |((x-x0)dy - (yc-y0)dx)/L| <= r
                for coef, const, lo_v, hi_v in ((dx / L, (yc - y0) * dy / L - x0 * dx / L, 0.0, L),
                                                (dy / L, -(yc - y0) * dx / L - x0 * dy / L, -r, r)):
                    if abs(coef) < 1e-12:
                        if not lo_v <= const <= hi_v:
                            a_lo, a_hi = 1.0, 0.0
                        continue
                    u, v_ = (lo_v - const) / coef, (hi_v - const) / coef
                    a_lo, a_hi = max(a_lo, min(u, v_)), min(a_hi, max(u, v_))
                if a_lo <= a_hi:
                    lo, hi = min(lo, a_lo), max(hi, a_hi)
            if lo > hi:
                continue
            i0 = max(0, int(math.ceil((lo - X0) / cell - 0.5)))
            i1 = min(nx - 1, int(math.floor((hi - X0) / cell - 0.5)))
            if i1 < i0:
                continue
            row = grid[j]
            if simple:
                z = z0
                row[i0:i1 + 1] = [z if z < v else v for v in row[i0:i1 + 1]]
                continue
            for i in range(i0, i1 + 1):
                xc = X0 + (i + 0.5) * cell
                tt = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, ((xc - x0) * dx + (yc - y0) * dy) / L2))
                px, py = x0 + tt * dx, y0 + tt * dy
                d2 = (xc - px) ** 2 + (yc - py) ** 2
                if d2 > r * r:
                    continue
                z = z0 + (z1 - z0) * tt
                if kind == "Ball nose":
                    z += r - math.sqrt(r * r - d2)
                elif kind == VBIT:
                    z += math.sqrt(d2) / tan_h
                if z < row[i]:
                    row[i] = z
    # slugs: inside a hole that was cut all round, the loose piece is gone (even-odd, so parts inside stay)
    th = s["thickness"]
    for loops in job.slugs.values():
        hy = [p[1] for p in loops[0]]
        for j in range(max(0, int((min(hy) - Y0) / cell)), min(ny, int((max(hy) - Y0) / cell) + 2)):
            yc = Y0 + (j + 0.5) * cell
            xs_ = []
            for q in loops:
                for k in range(len(q)):
                    a, b = q[k], q[(k + 1) % len(q)]
                    if (a[1] > yc) != (b[1] > yc):
                        xs_.append(a[0] + (yc - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))
            xs_.sort()
            row = grid[j]
            for k in range(0, len(xs_) - 1, 2):
                for i in range(max(0, int(math.ceil((xs_[k] - X0) / cell - 0.5))),
                               min(nx - 1, int(math.floor((xs_[k + 1] - X0) / cell - 0.5))) + 1):
                    if row[i] > -th:
                        row[i] = -th
    return {"grid": grid, "x0": X0, "y0": Y0, "cell": cell, "nx": nx, "ny": ny, "full": full, "ppm": ppm,
            "zoom": max(1, int(round(cell * ppm))), "thickness": s["thickness"]}


def render_tilted(sim, cols, W, H, yaw, pitch, zoom=1.0, step=1, bg="#ffffff"):
    """The finished part seen at an angle, true depth: a height-field render, near to far per screen column,
    each cell's top in its lit colour and the wall below it darker. cols = sim_rows(sim). Returns picture rows."""
    g, nx, ny, cell, X0, Y0 = sim["grid"], sim["nx"], sim["ny"], sim["cell"], sim["x0"], sim["y0"]
    th = sim["thickness"]
    a, p = math.radians(yaw), math.radians(pitch)
    ca, sa, cp, sp = math.cos(a), math.sin(a), math.cos(p), math.sin(p)
    mx, my = X0 + nx * cell / 2, Y0 + ny * cell / 2
    corners = [(x - mx, y - my) for x in (X0, X0 + nx * cell) for y in (Y0, Y0 + ny * cell)]
    us = [x * ca - y * sa for x, y in corners]
    ws = [x * sa + y * ca for x, y in corners]
    zlo = -th - 1.0
    span_u = max(us) - min(us)
    span_v = (max(ws) - min(ws)) * cp + (0 - zlo) * sp
    sc = min(W * 0.92 / max(span_u, 1e-6), H * 0.9 / max(span_v, 1e-6)) * zoom
    ox = W / 2 - (max(us) + min(us)) / 2 * sc
    oy = H / 2 + ((max(ws) + min(ws)) / 2 * cp + (zlo / 2) * sp) * sc
    rows = [[bg] * W for _ in range(H)]
    dark = {}

    def wall(c):
        if c not in dark:
            dark[c] = "#%02x%02x%02x" % tuple(int(int(c[k:k + 2], 16) * 0.72) for k in (1, 3, 5))
        return dark[c]
    dw = cell / max(cp, 0.2) * 0.9 * step
    wmin, wmax = min(ws), max(ws)
    nsteps = int((wmax - wmin) / dw) + 2
    flat_px = dw * cp * sc + 1.5   # the top face of one step; anything longer below it is a wall
    for X in range(0, W, step):
        u = (X + step / 2 - ox) / sc
        ybuf = H
        w = wmin
        prev_z = None
        for _ in range(nsteps):
            x = u * ca + w * sa + mx
            y = -u * sa + w * ca + my
            i = int((x - X0) / cell)
            j = int((y - Y0) / cell)
            if 0 <= i < nx and 0 <= j < ny:
                z = g[j][i]
                if z < -th:
                    z = -th
                yt = int(oy - (w * cp + z * sp) * sc)
                if prev_z is None:   # the stock's near edge: its wall goes down to the stock's bottom only
                    ybuf = min(ybuf, int(oy - (w * cp - th * sp) * sc) + 1)
                    is_wall = True
                else:
                    is_wall = z > prev_z + 0.02   # a face rising towards the viewer
                prev_z = z
                if yt < ybuf:
                    col = cols[ny - 1 - j][i]
                    wc = wall(col) if is_wall else col
                    top = max(yt, 0)
                    for yy in range(top, min(ybuf, H)):
                        r_ = rows[yy]
                        c_ = col if (yy - yt < 1 or not is_wall) else wc
                        for k in range(X, min(X + step, W)):
                            r_[k] = c_
                    ybuf = yt
                    if ybuf <= 0:
                        break
            else:
                prev_z = None
            w += dw
    return rows


def sim_rows(sim):
    """Picture rows (top row first) of '#rrggbb' colours: aluminium, darker and bluer with depth,
    lit from the upper left; where it is cut through you see the spoilboard."""
    g, nx, ny, cell, th = sim["grid"], sim["nx"], sim["ny"], sim["cell"], sim["thickness"]
    lx, ly, lz = -0.55, 0.55, 0.63
    out = []
    for j in range(ny - 1, -1, -1):
        row, up, dn = g[j], g[min(ny - 1, j + 1)], g[max(0, j - 1)]
        cols = []
        for i in range(nx):
            z = row[i]
            if z <= -th + 1e-3:
                cols.append("#e9dcbc" if z > -th - 0.05 else "#dccb9f")
                continue
            gx = (row[min(nx - 1, i + 1)] - row[max(0, i - 1)]) / (2 * cell)
            gy = (up[i] - dn[i]) / (2 * cell)
            nl = math.sqrt(gx * gx + gy * gy + 1)
            light = max(0.35, min(1.15, (-gx * lx - gy * ly + lz) / nl / lz))
            f = min(1.0, max(0.0, -z / th))
            base = (206 - 96 * f, 211 - 76 * f, 218 - 40 * f)
            cols.append("#%02x%02x%02x" % tuple(max(0, min(255, int(c * light))) for c in base))
        out.append(cols)
    return out


# --------------------------------------------------------------------------- icon

ICON_FILE = os.path.join(HERE, "G-code Maker.ico")
APP_ID = "Agilite.GcodeMaker"   # Windows groups the taskbar button by this, not by pythonw.exe


def _icon_pixels(n):
    """n x n RGBA rows of the app icon: an end mill cutting a plate, orange chips."""
    def hexc(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def rrect(x, y, x0, y0, x1, y1, r):
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r and x0 <= x <= x1 and y0 <= y <= y1

    def color(x, y):
        if not rrect(x, y, 0.02, 0.02, 0.98, 0.98, 0.2):
            return None
        g = y
        bg = tuple(int(a + (b - a) * g) for a, b in zip(hexc("#1e88e5"), hexc("#0d47a1")))
        # plate with a groove cut in it
        if 0.70 <= y <= 0.86 and 0.10 <= x <= 0.90:
            if 0.70 <= y <= 0.745 and 0.42 <= x <= 0.58:
                return hexc("#455a64")
            return hexc("#eceff1") if y < 0.745 else hexc("#b0bec5")
        # chips
        for cx, cy, r in ((0.27, 0.62, 0.045), (0.74, 0.58, 0.04), (0.20, 0.50, 0.03), (0.80, 0.47, 0.028)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return hexc("#ffa000")
        # end mill: shank, then fluted body down into the groove
        if 0.38 <= x <= 0.62 and 0.06 <= y <= 0.36:
            return hexc("#cfd8dc") if x < 0.5 else hexc("#b0bec5")
        if 0.36 <= y <= 0.40 and 0.40 <= x <= 0.60:
            return hexc("#90a4ae")
        if 0.40 <= y <= 0.73 and 0.42 <= x <= 0.58:
            return hexc("#607d8b") if ((y * 1.0 + (x - 0.42) * 0.9) * 7.0) % 1.0 < 0.38 else hexc("#f5f5f5")
        return bg

    rows = []
    ss = 3
    for j in range(n):
        row = []
        for i in range(n):
            acc, hit = [0, 0, 0], 0
            for a in range(ss):
                for b in range(ss):
                    c = color((i + (a + 0.5) / ss) / n, (j + (b + 0.5) / ss) / n)
                    if c:
                        hit += 1
                        for k in range(3):
                            acc[k] += c[k]
            if hit:
                row += [acc[0] // hit, acc[1] // hit, acc[2] // hit, 255 * hit // (ss * ss)]
            else:
                row += [0, 0, 0, 0]
        rows.append(bytes(row))
    return rows


def _png(n, rows):
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def make_icon(path=ICON_FILE):
    """Write the app icon (.ico holding PNGs) if it is not there yet."""
    if os.path.isfile(path):
        return path
    import struct
    sizes = (16, 24, 32, 48, 64, 256)
    pngs = [_png(n, _icon_pixels(n)) for n in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    dirs = b""
    for n, data in zip(sizes, pngs):
        dirs += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
    try:
        with open(path, "wb") as f:
            f.write(head + dirs + b"".join(pngs))
    except OSError:
        return None
    return path


# --------------------------------------------------------------------------- GUI

# APF+'s colours (apf/ui/app.css), so the two tools look like one family
C_BG, C_PANEL, C_INK, C_MUTED, C_LINE = "#f4f5f7", "#ffffff", "#1d2733", "#66727f", "#dfe3e8"
C_ACCENT, C_ACCENT_DK, C_OK, C_BAD, C_AUTO = "#1f6fd1", "#1a5fb4", "#1a7f37", "#d22f27", "#e8912d"
C_BAR, C_BARBTN, C_BARBTN_HI, C_BARLINE = "#18212c", "#243140", "#2e3d4f", "#3a4756"
C_BARTEXT, C_BARMUTED, C_STATUS, C_FIELD_LINE = "#e8edf3", "#aab6c3", "#eef1f4", "#c6ccd4"
FONT_UI = ("Segoe UI", 10)
FONT_HEAD = ("Segoe UI", 8, "bold")


def apf_style(root, ttk):
    """ttk drawn flat in APF+'s colours: white panels, thin grey lines, blue for what is on."""
    st = ttk.Style(root)
    st.theme_use("clam")
    root.option_add("*Font", FONT_UI)
    root.option_add("*TCombobox*Listbox.font", FONT_UI)
    root.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
    st.configure(".", background=C_PANEL, foreground=C_INK, font=FONT_UI, bordercolor=C_LINE,
                 lightcolor=C_PANEL, darkcolor=C_PANEL, troughcolor=C_BG, focuscolor=C_PANEL,
                 selectbackground=C_ACCENT, selectforeground="#ffffff")
    st.configure("TFrame", background=C_PANEL)
    st.configure("Bg.TFrame", background=C_BG)
    st.configure("TLabel", background=C_PANEL, foreground=C_INK)
    st.configure("Muted.TLabel", foreground=C_MUTED)
    st.configure("Head.TLabel", foreground=C_MUTED, font=FONT_HEAD)
    st.configure("Bg.TLabel", background=C_BG, foreground=C_MUTED)
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, fieldbackground="#ffffff", bordercolor=C_FIELD_LINE, lightcolor="#ffffff",
                     darkcolor="#ffffff", padding=(5, 3), arrowcolor=C_MUTED, insertcolor=C_INK)
        st.map(w, bordercolor=[("focus", C_ACCENT)], lightcolor=[("focus", C_ACCENT)],
               fieldbackground=[("readonly", "#ffffff"), ("disabled", C_BG)],
               foreground=[("disabled", "#a3acb6")], selectbackground=[("readonly", "#ffffff")],
               selectforeground=[("readonly", C_INK)])
    # side-panel buttons as APF+'s tool rows: light grey with a thin grey edge
    st.configure("TButton", background="#eceef1", bordercolor="#d3d7dd", lightcolor="#eceef1",
                 darkcolor="#eceef1", padding=(12, 3), relief="solid", borderwidth=1)
    st.map("TButton", background=[("pressed", "#dbe8fb"), ("active", "#e2e6eb")],
           lightcolor=[("pressed", "#dbe8fb"), ("active", "#e2e6eb")],
           darkcolor=[("pressed", "#dbe8fb"), ("active", "#e2e6eb")],
           bordercolor=[("pressed", "#8db3ea"), ("active", C_FIELD_LINE)])
    st.configure("TCombobox", background="#ffffff")
    st.map("TCombobox", background=[("active", "#eef2f7"), ("readonly", "#ffffff")],
           arrowcolor=[("active", C_INK)])
    st.configure("Primary.TButton", background=C_ACCENT, foreground="#ffffff", bordercolor=C_ACCENT)
    st.map("Primary.TButton", background=[("pressed", C_ACCENT_DK), ("active", C_ACCENT_DK)])
    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=C_PANEL, indicatorbackground="#ffffff", indicatorforeground=C_ACCENT,
                     indicatormargin=(0, 0, 6, 0), upperbordercolor=C_FIELD_LINE, lowerbordercolor=C_FIELD_LINE)
        st.map(w, background=[("active", C_PANEL)], indicatorbackground=[("selected", "#ffffff")])
    st.configure("TScrollbar", background="#d3d7dd", troughcolor=C_PANEL, bordercolor=C_PANEL,
                 arrowcolor=C_MUTED, lightcolor="#d3d7dd", darkcolor="#d3d7dd")
    st.configure("TPanedwindow", background=C_LINE)
    st.configure("Sash", sashthickness=5, gripcount=0, background=C_LINE, lightcolor=C_LINE, bordercolor=C_LINE)
    st.configure("Horizontal.TScale", background=C_ACCENT, troughcolor=C_LINE, bordercolor=C_LINE,
                 lightcolor=C_ACCENT, darkcolor=C_ACCENT)


def make_tabs_class(tk):
    class Tabs(tk.Frame):
        """APF+'s tabs: small capitals in grey, the open one dark with a line under it.
        Behaves enough like ttk.Notebook for this window: add, select, index("current"), <<NotebookTabChanged>>."""

        def __init__(self, parent, bg=C_PANEL, pad=0):
            super().__init__(parent, bg=bg)
            self.head = tk.Frame(self, bg=bg)
            self.head.pack(fill="x", padx=pad)
            tk.Frame(self, bg=C_LINE, height=1).pack(fill="x")
            self.pages, self.heads, self.cur = [], [], -1

        def add(self, page, text=""):
            i = len(self.pages)
            cell = tk.Frame(self.head, bg=self["bg"])
            cell.pack(side="left", padx=(0 if i == 0 else 18, 0))
            lab = tk.Label(cell, text=text.strip().upper(), font=FONT_HEAD, bg=self["bg"], fg=C_MUTED,
                           cursor="hand2", padx=1, pady=6)
            lab.pack()
            bar = tk.Frame(cell, height=2, bg=self["bg"])
            bar.pack(fill="x")
            for w in (lab, cell):
                w.bind("<Button-1>", lambda e, i=i: self.select(i))
            lab.bind("<Enter>", lambda e, i=i: self.heads[i][0].config(fg=C_INK))
            lab.bind("<Leave>", lambda e, i=i: self.heads[i][0].config(fg=C_INK if i == self.cur else C_MUTED))
            self.pages.append(page)
            self.heads.append((lab, bar))
            if self.cur < 0:
                self.select(0)

        def select(self, i):
            if i == self.cur:
                return
            if self.cur >= 0:
                self.pages[self.cur].pack_forget()
            self.cur = i
            self.pages[i].pack(fill="both", expand=True)
            for k, (lab, bar) in enumerate(self.heads):
                lab.config(fg=C_INK if k == i else C_MUTED)
                bar.config(bg=C_INK if k == i else self["bg"])
            self.event_generate("<<NotebookTabChanged>>")

        def index(self, what="current"):
            return self.cur
    return Tabs


def run_gui(open_dxf=None):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, filedialog, messagebox

    try:  # own taskbar button and icon instead of Python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    st = load_settings()
    # a DXF handed over on the command line (APF+'s Export > Send to G-code Maker): opened at start
    if open_dxf and os.path.isfile(open_dxf):
        st["dxf"] = os.path.normpath(open_dxf)
        st["out_name"] = default_name(open_dxf)
    root = tk.Tk()
    try:
        ico = make_icon()
        if ico:
            root.iconbitmap(default=ico)
    except tk.TclError:
        pass
    root.title(f"{APP} - Stepcraft M700 / UCCNC")
    root.geometry("1440x900")
    root.minsize(1100, 700)
    root.configure(bg=C_BG)
    apf_style(root, ttk)
    Tabs = make_tabs_class(tk)

    v = {}
    for k, d in DEFAULTS.items():
        if k in DICT_KEYS:   # kept in state, not in a field
            continue
        v[k] = tk.BooleanVar(value=bool(st[k])) if isinstance(d, bool) else tk.StringVar(value=str(st[k]))

    try:
        root.state("zoomed")   # the preview gets the room
    except tk.TclError:
        pass
    # ---- APF+'s dark title bar: icon, name, the open file, the main buttons
    bar = tk.Frame(root, bg=C_BAR, height=48)
    bar.pack(fill="x")
    bar.pack_propagate(False)
    try:
        import base64
        state_icon = tk.PhotoImage(data=base64.b64encode(_png(24, _icon_pixels(24))))
        tk.Label(bar, image=state_icon, bg=C_BAR).pack(side="left", padx=(14, 8))
        bar.icon = state_icon
    except Exception:
        pass
    tk.Label(bar, text=APP, bg=C_BAR, fg=C_BARTEXT, font=("Segoe UI", 11, "bold")).pack(side="left")
    tk.Label(bar, text="Stepcraft M700  ·  UCCNC", bg=C_BAR, fg=C_BARMUTED, font=("Segoe UI", 9)).pack(
        side="left", padx=(10, 0))
    bar_btns = tk.Frame(bar, bg=C_BAR)
    bar_btns.pack(side="right", padx=(0, 14))
    bar_file = tk.Label(bar, text="", bg=C_BAR, fg=C_BARMUTED, font=("Segoe UI", 9), anchor="e")
    bar_file.pack(side="right", fill="x", expand=True, padx=16)

    ICON_FONT = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in tkfont.families()), None)

    def bar_button(text, command, primary=False, icon=None):
        """A title-bar button: an icon and a word, dark like APF+'s, the main one blue."""
        bg, hi = (C_ACCENT, C_ACCENT_DK) if primary else (C_BARBTN, C_BARBTN_HI)
        fg = "#ffffff" if primary else C_BARTEXT
        b = tk.Frame(bar_btns, bg=bg, cursor="hand2", highlightthickness=1,
                     highlightbackground=bg if primary else C_BARLINE)
        b.pack(side="left", padx=(8, 0))
        parts = [b]
        if icon and ICON_FONT:
            ic = tk.Label(b, text=icon, bg=bg, fg=fg, font=(ICON_FONT, 11))
            ic.pack(side="left", padx=(12, 0), pady=6)
            parts.append(ic)
        lb = tk.Label(b, text=text, bg=bg, fg=fg, font=("Segoe UI Semibold", 10) if primary else FONT_UI)
        lb.pack(side="left", padx=(8 if icon and ICON_FONT else 14, 14), pady=6)
        parts.append(lb)
        for w_ in parts:
            w_.bind("<Button-1>", lambda e: command())
            w_.bind("<Enter>", lambda e: [p_.config(bg=hi) for p_ in parts])
            w_.bind("<Leave>", lambda e: [p_.config(bg=bg) for p_ in parts])
        return b

    # ---- APF+'s status line along the bottom
    sbar = tk.Frame(root, bg=C_STATUS, height=26)
    sbar.pack(side="bottom", fill="x")
    sbar.pack_propagate(False)
    tk.Frame(sbar, bg=C_LINE, height=1).pack(side="top", fill="x")
    sbar_left = tk.Label(sbar, text="", bg=C_STATUS, fg=C_MUTED, font=("Segoe UI", 9), anchor="w")
    sbar_left.pack(side="left", fill="x", expand=True, padx=12)
    sbar_right = tk.Label(sbar, text="", bg=C_STATUS, fg=C_MUTED, font=("Segoe UI", 9))
    sbar_right.pack(side="right", padx=12)

    body = tk.Frame(root, bg=C_BG)
    body.pack(fill="both", expand=True)
    # ---- the rail: one icon per step of the job, as a CAM program's browser
    rail = tk.Frame(body, bg=C_PANEL, width=84)
    rail.pack(side="left", fill="y")
    rail.pack_propagate(False)
    tk.Frame(body, bg=C_LINE, width=1).pack(side="left", fill="y")
    panes = ttk.PanedWindow(body, orient="horizontal")   # drag the divider to give the preview more room
    panes.pack(side="left", fill="both", expand=True)
    left = ttk.Frame(panes)
    right = ttk.Frame(panes, padding=(14, 12, 14, 12), style="Bg.TFrame")
    panes.add(left, weight=0)
    panes.add(right, weight=1)

    # the settings column scrolls on its own when a page is taller than the window
    lcan = tk.Canvas(left, bg=C_PANEL, highlightthickness=0, bd=0)
    lsb = ttk.Scrollbar(left, orient="vertical", command=lcan.yview)
    lcan.configure(yscrollcommand=lsb.set)
    lcan.pack(side="left", fill="both", expand=True)
    inner = ttk.Frame(lcan, padding=(22, 16, 20, 24))
    inner_id = lcan.create_window(0, 0, window=inner, anchor="nw")

    def panel_fit(e=None):
        lcan.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight()))
        lcan.itemconfigure(inner_id, width=max(lcan.winfo_width(), inner.winfo_reqwidth()))
        if inner.winfo_reqheight() > lcan.winfo_height() + 2:
            if not lsb.winfo_ismapped():
                lsb.pack(side="right", fill="y")
        elif lsb.winfo_ismapped():
            lsb.pack_forget()
            lcan.yview_moveto(0)
    inner.bind("<Configure>", panel_fit)
    lcan.bind("<Configure>", panel_fit)

    def panel_scroll(e):
        if inner.winfo_reqheight() > lcan.winfo_height() + 2:
            lcan.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"
    left.bind("<Enter>", lambda e: root.bind_all("<MouseWheel>", panel_scroll))
    left.bind("<Leave>", lambda e: root.unbind_all("<MouseWheel>"))
    root.bind_class("TCombobox", "<MouseWheel>", panel_scroll)   # a list never changes under a scrolling wheel

    class Rail:
        """The rail's pages: the same few calls as the Tabs (add, select, index, <<NotebookTabChanged>>)."""

        def __init__(self):
            self.pages, self.items, self.cur = [], [], -1

        def add(self, page, text, icon, title, about):
            i = len(self.pages)
            cell = tk.Frame(rail, bg=C_PANEL, cursor="hand2")
            cell.pack(fill="x", pady=(10 if i == 0 else 2, 0))
            bar_ = tk.Frame(cell, bg=C_PANEL, width=3)
            bar_.pack(side="left", fill="y")
            box = tk.Frame(cell, bg=C_PANEL)
            box.pack(side="left", fill="x", expand=True, pady=6)
            ic = tk.Label(box, text=icon if ICON_FONT else text[:1], bg=C_PANEL, fg=C_MUTED,
                          font=(ICON_FONT, 17) if ICON_FONT else ("Segoe UI", 15, "bold"))
            ic.pack()
            lb = tk.Label(box, text=text.upper(), bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 7, "bold"))
            lb.pack(pady=(2, 0))
            for w_ in (cell, box, ic, lb):
                w_.bind("<Button-1>", lambda e, i=i: self.select(i))
                w_.bind("<Enter>", lambda e, i=i: self._paint(i, hover=True))
                w_.bind("<Leave>", lambda e, i=i: self._paint(i))
            head = ttk.Frame(page)
            head.pack(fill="x")
            ttk.Label(head, text=title, font=("Segoe UI Semibold", 15)).pack(anchor="w")
            ttk.Label(head, text=about, foreground=C_MUTED, font=("Segoe UI", 9), wraplength=360,
                      justify="left").pack(anchor="w", pady=(2, 0))
            self.pages.append(page)
            self.items.append((cell, bar_, box, ic, lb))
            if self.cur < 0:
                self.select(0)

        def _paint(self, i, hover=False):
            cell, bar_, box, ic, lb = self.items[i]
            on = i == self.cur
            bg = "#eaf2fd" if on else ("#f3f5f8" if hover else C_PANEL)
            for w_ in (cell, box, ic, lb):
                w_.config(bg=bg)
            bar_.config(bg=C_ACCENT if on else bg)
            ic.config(fg=C_ACCENT if on else (C_INK if hover else C_MUTED))
            lb.config(fg=C_ACCENT if on else (C_INK if hover else C_MUTED))

        def select(self, i):
            if i == self.cur:
                return
            if self.cur >= 0:
                self.pages[self.cur].pack_forget()
            old, self.cur = self.cur, i
            self.pages[i].pack(fill="both", expand=True)
            for k in range(len(self.items)):
                self._paint(k)
            lcan.yview_moveto(0)
            root.after_idle(panel_fit)

        def index(self, what="current"):
            return self.cur

    nb = Rail()
    pg = {}
    for key_, text_, icon_, title_, about_ in (
            ("job", "Job", "", "Job", "The drawing, the stock and where X0 Y0 is."),
            ("tool", "Tool", "", "Tool", "The cutter in the spindle and how fast it runs."),
            ("cuts", "Cuts", "", "Cuts", "Holes, outlines, tabs and the finishing touches on the edges."),
            ("layers", "Layers", "", "Layers", "What each layer of the DXF does: cut through, pocket, "
                                                     "engrave or skip."),
            ("marks", "Marks", "", "Marks", "Text and AGILITE logos engraved into the part. Drag them on "
                                                  "the preview."),
            ("surf", "Surface", "", "Surfacing", "Face the top of the stock flat before anything is cut."),
            ("machine", "Machine", "", "Machine", "Tool changes and where the machine goes at the end.")):
        pg[key_] = ttk.Frame(inner)
        nb.add(pg[key_], text_, icon_, title_, about_)
    top = colA = colB = opsA = opsB = layA = layB = surf_tab = None   # (every section names its page)

    def section(title, parent=None):
        """APF+'s side-panel block: a small grey capital heading, then compact rows."""
        outer = ttk.Frame(parent)
        outer.pack(fill="x", pady=(18, 0))
        hd = ttk.Frame(outer)
        hd.pack(fill="x")
        ttk.Label(hd, text=title.upper(), style="Head.TLabel").pack(side="left")
        tk.Frame(hd, bg=C_LINE, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0), pady=(2, 0))
        f = ttk.Frame(outer)
        f.pack(fill="x", pady=(8, 0))
        f.columnconfigure(3, weight=1)
        return f

    NUM_STEP = {"probe_dx": 1, "probe_dy": 1, "probe_d": 1, "tool_d": 0.5, "rpm": 500, "feed": 25, "ramp_feed": 25, "plunge_feed": 10, "thickness": 0.1,
                "extra": 0.1, "step": 0.05, "ramp_angle": 1, "safe_z": 1, "tab_len": 0.5, "tab_thick": 0.1,
                "tool_angle": 5, "chamfer_w": 0.1, "edge_size": 0.1, "end_z": 1, "surf_depth": 0.05, "surf_over": 5,
                "surf_margin": 1, "pocket_over": 5, "finish_allow": 0.05, "slot_allow": 0.05, "rpm_min": 500,
                "rpm_max": 500, "feed_max": 100, "surf_w": 5, "surf_h": 5}
    NUM_MIN = {"tool_d": 0.1, "flutes": 1, "tabs": 0, "thickness": 0.1, "step": 0.01, "safe_z": 0.5,
               "surf_over": 5, "pocket_over": 5, "rpm": 0, "feed": 1, "ramp_feed": 1, "plunge_feed": 1}

    def spin(parent, var, step=1.0, width=8, lo=None, cmd=None):
        """A number box with up and down arrows (also the arrow keys, and the wheel while it has the cursor)."""
        sb_ = ttk.Spinbox(parent, textvariable=var, width=width, from_=-1e9, to=1e9, increment=step)
        dec = len(f"{step:g}".split(".")[1]) if "." in f"{step:g}" else 0

        def bump(sign):
            try:
                val = float(str(var.get()).replace(",", "."))
            except ValueError:
                val = 0.0
            val = round(val + sign * step, 6)
            if lo is not None:
                val = max(lo, val)
            cur_dec = len(str(var.get()).split(".")[1]) if "." in str(var.get()) else 0
            d_ = max(dec, min(cur_dec, 3))
            var.set(f"{val:.{d_}f}" if d_ else str(int(round(val))))
            (cmd or changed)()
            return "break"
        sb_.bind("<<Increment>>", lambda e: bump(+1))
        sb_.bind("<<Decrement>>", lambda e: bump(-1))
        sb_.bind("<MouseWheel>", lambda e: bump(1 if e.delta > 0 else -1) if root.focus_get() == sb_
                 else panel_scroll(e))
        sb_.bind("<KeyRelease>", lambda e: (cmd or changed)() if e.keysym not in ("Up", "Down") else None)
        return sb_

    def numbox(parent, key, width=7):
        d_ = DEFAULTS[key]
        return spin(parent, v[key], NUM_STEP.get(key, 1 if isinstance(d_, int) else 0.1), width, NUM_MIN.get(key))

    def row(f, label, key, unit="", values=None, width=10):
        r = f.grid_size()[1]
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=(0, 14), pady=3)
        d_ = DEFAULTS.get(key)
        if values:
            w = ttk.Combobox(f, textvariable=v[key], values=values, state="readonly", width=width + 10)
            w.bind("<<ComboboxSelected>>", lambda e: changed())
        elif isinstance(d_, (int, float)) and not isinstance(d_, bool):
            w = numbox(f, key, width=max(7, width - 1))
        else:
            w = ttk.Entry(f, textvariable=v[key], width=width)
            w.bind("<KeyRelease>", lambda e: changed())
        w.grid(row=r, column=1, sticky="w", pady=3)
        if unit:
            ttk.Label(f, text=unit, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=(8, 0))
        return w

    # File
    ff = section("File", pg["job"])
    ff.columnconfigure(1, weight=1)
    ff.columnconfigure(3, weight=0)
    ttk.Label(ff, text="DXF").grid(row=0, column=0, sticky="w", padx=(0, 10))
    ttk.Entry(ff, textvariable=v["dxf"], width=28).grid(row=0, column=1, sticky="we", pady=2)
    name_edited = {"v": bool(st.get("out_name"))}

    def pick_dxf():
        start = os.path.dirname(v["dxf"].get()) if v["dxf"].get() else os.path.expanduser("~")
        p = filedialog.askopenfilename(title="Choose a DXF", initialdir=start, filetypes=[("DXF", "*.dxf"), ("All files", "*.*")])
        if p:
            v["dxf"].set(os.path.normpath(p))
            v["out_name"].set(default_name(p))
            name_edited["v"] = False
            changed(now=True)

    ttk.Button(ff, text="Browse...", command=pick_dxf).grid(row=0, column=2, padx=(6, 0))
    ttk.Label(ff, text="Save to").grid(row=1, column=0, sticky="w", padx=(0, 10))
    ttk.Entry(ff, textvariable=v["out_dir"], width=28).grid(row=1, column=1, sticky="we", pady=2)

    def pick_dir():
        p = filedialog.askdirectory(title="Folder for G-code", initialdir=v["out_dir"].get() or DEFAULT_OUT)
        if p:
            v["out_dir"].set(os.path.normpath(p))

    ttk.Button(ff, text="Browse...", command=pick_dir).grid(row=1, column=2, padx=(6, 0))
    ttk.Label(ff, text="File name").grid(row=2, column=0, sticky="w", padx=(0, 10))
    name_entry = ttk.Entry(ff, textvariable=v["out_name"], width=28)
    name_entry.grid(row=2, column=1, sticky="we", pady=2)
    name_entry.bind("<KeyRelease>", lambda e: name_edited.update(v=True))
    ttk.Label(ff, text="Recent").grid(row=3, column=0, sticky="w", padx=(0, 10))
    recent_var = tk.StringVar(value="")
    recent_box = ttk.Combobox(ff, textvariable=recent_var, state="readonly", width=26, height=12)
    recent_box.grid(row=3, column=1, sticky="we", pady=2)

    def recent_label(p_):
        return f"{os.path.basename(p_)}   \u2014   {os.path.basename(os.path.dirname(p_))}"

    def refresh_recent():
        rec = [p_ for p_ in state["recent"] if os.path.isfile(p_)]
        recent_box.config(values=[recent_label(p_) for p_ in rec])
        recent_var.set("")
        state["recent_paths"] = rec

    def open_recent(e=None):
        i = recent_box.current()
        rec = state.get("recent_paths", [])
        if 0 <= i < len(rec):
            v["dxf"].set(rec[i])
            name_edited["v"] = False
            changed(now=True)
    recent_box.bind("<<ComboboxSelected>>", open_recent)

    # ---- Tool page: the tool library. Each tool keeps its own speeds and feeds; each cut picks a tool.
    tools = load_tools()
    if migrate_tools(st, tools):
        v["tools_v2"].set(True)
    lib = {"edit": None}
    tool_combos = []   # (combobox, include "Same as outlines", only this tool type, info label)
    EDIT_KEYS = TOOL_FIELDS + SPEED_KEYS + ("ramp_angle",)

    def find_tool(name):
        return next((t for t in tools if t["name"] == name), None)

    def tool_desc(t):
        return f"{_tool_label(t)}  ·  {t.get('flutes', 2)}F {str(t.get('tool_mat', '')).lower()}"

    def tool_speed_line(t):
        try:
            return (f"{int(float(t['rpm']))} rpm  ·  F{float(t['feed']):g}  ·  ramp {float(t['ramp_feed']):g}  "
                    f"·  plunge {float(t['plunge_feed']):g}  ·  {float(t['step']):g} mm/pass  ·  "
                    f"{float(t.get('ramp_angle', 0)):g}°")
        except (KeyError, ValueError):
            return "speeds not set yet"

    tl = section("Tool library", pg["tool"])
    lib_btns = ttk.Frame(tl)
    lib_btns.grid(row=0, column=0, columnspan=4, sticky="we")
    lib_list = tk.Frame(tl, bg=C_PANEL)
    lib_list.grid(row=1, column=0, columnspan=4, sticky="we", pady=(10, 0))
    tool_state = ttk.Label(tl, text="", foreground=C_MUTED, font=("Segoe UI", 9))
    tool_state.grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def small_btn(parent, text, cmd, primary=False):
        b_ = tk.Label(parent, text=text, bg=C_ACCENT if primary else "#eceef1", fg="#ffffff" if primary else C_INK,
                      padx=12, pady=4, cursor="hand2", font=("Segoe UI", 9, "bold" if primary else "normal"),
                      highlightthickness=1, highlightbackground=C_ACCENT if primary else "#d3d7dd")
        b_.bind("<Button-1>", lambda e: cmd())
        return b_

    def uses_of(name):
        names = {"tool_name": "outlines", "hole_tool": "holes", "pocket_tool": "pockets",
                 "engrave_tool": "engraving and marks", "surf_tool": "surfacing", "chamfer_tool": "chamfer",
                 "edge_tool": "edge break"}
        return [lab for k, lab in names.items() if v[k].get() == name]

    def lib_refresh():
        for w_ in lib_list.winfo_children():
            w_.destroy()
        for t in tools:
            on = t["name"] == lib["edit"]
            bg = "#eaf2fd" if on else C_PANEL
            r_ = tk.Frame(lib_list, bg=bg, cursor="hand2", highlightthickness=1,
                          highlightbackground="#8db3ea" if on else C_LINE)
            r_.pack(fill="x", pady=(0, 4))
            tx_ = tk.Frame(r_, bg=bg)
            tx_.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=5)
            l1 = tk.Label(tx_, text=t["name"], bg=bg, fg=C_INK, font=("Segoe UI Semibold", 10), anchor="w")
            l1.pack(fill="x")
            l2 = tk.Label(tx_, text=tool_speed_line(t), bg=bg, fg=C_MUTED, font=("Segoe UI", 8), anchor="w")
            l2.pack(fill="x")
            used = uses_of(t["name"])
            parts = [r_, tx_, l1, l2]
            if used:
                l3 = tk.Label(tx_, text="Used for " + ", ".join(used), bg=bg, fg=C_ACCENT, font=("Segoe UI", 8),
                              anchor="w")
                l3.pack(fill="x")
                parts.append(l3)
            for w_ in parts:
                w_.bind("<Button-1>", lambda e, n=t["name"]: edit_tool(n))
        root.after_idle(panel_fit)

    def edit_tool(name):
        """Show a library tool in the editor below (its numbers go into the fields)."""
        t = find_tool(name)
        if not t:
            return
        lib["edit"] = None   # (no writing back while the fields are filled)
        try:
            p_ = parse(raw_settings())
        except (ValueError, NameError):
            p_ = None
        if p_ is not None and not all(k in t for k in SPEED_KEYS):
            t.update(tool_speeds(t, p_))
        for k in EDIT_KEYS:
            if k in t:
                v[k].set(f"{t[k]:g}" if isinstance(t[k], float) else str(t[k]))
            elif k == "tool_angle":
                v[k].set("90")
        name_var.set(name)
        lib["edit"] = name
        lib_refresh()
        changed(now=True)

    def write_back():
        """The fields changed: the tool in the library changes with them (saved at once)."""
        t = find_tool(lib["edit"]) if lib["edit"] else None
        if not t:
            return
        try:
            new = {"tool_d": float(v["tool_d"].get().replace(",", ".")), "tool_type": v["tool_type"].get(),
                   "flutes": int(float(v["flutes"].get())), "tool_mat": v["tool_mat"].get(),
                   "tool_angle": float(v["tool_angle"].get().replace(",", "."))}
            for k in SPEED_KEYS + ("ramp_angle",):
                new[k] = float(v[k].get().replace(",", "."))
        except ValueError:
            return
        if any(t.get(k) != val for k, val in new.items()):
            t.update(new)
            save_tools(tools)
            tool_state.config(text=f"Saved in the library: {t['name']}")
            lib_refresh()

    def unique(name):
        base_, k = name, 2
        while find_tool(name):
            name = f"{base_} {k}"
            k += 1
        return name

    def new_tool():
        t = {"name": unique("New tool"), "tool_d": 3.0, "tool_type": "Flat end mill", "flutes": 2,
             "tool_mat": "Carbide"}
        try:
            t.update(tool_speeds(t, parse(raw_settings())))
        except ValueError:
            pass
        t["ramp_angle"] = 10.0
        tools.append(t)
        save_tools(tools)
        refresh_tools()
        edit_tool(t["name"])
        name_entry_.focus_set()
        name_entry_.select_range(0, "end")

    def dup_tool():
        t = find_tool(lib["edit"])
        if t:
            c = dict(t)
            c["name"] = unique(t["name"] + " copy")
            tools.append(c)
            save_tools(tools)
            refresh_tools()
            edit_tool(c["name"])

    def delete_tool():
        name = lib["edit"]
        if not find_tool(name):
            return
        used = uses_of(name)
        if not messagebox.askyesno(APP, f"Delete \"{name}\" from the tool library?"
                                   + (f"\n\nIt is used for {', '.join(used)}; those go back to the outlines tool."
                                      if used else "")):
            return
        tools[:] = [t for t in tools if t["name"] != name]
        save_tools(tools)
        for k in TOOL_KEYS:
            if v[k].get() == name:
                v[k].set(tools[0]["name"] if k == "tool_name" and tools else MAIN)
        refresh_tools()
        edit_tool(v["tool_name"].get() if find_tool(v["tool_name"].get()) else (tools[0]["name"] if tools else ""))

    def rename_tool(e=None):
        old, new = lib["edit"], name_var.get().strip()
        if not old or not new or new == old:
            name_var.set(old or "")
            return
        if find_tool(new):
            messagebox.showerror(APP, f"There is already a tool named \"{new}\".")
            name_var.set(old)
            return
        find_tool(old)["name"] = new
        save_tools(tools)
        for k in TOOL_KEYS:   # every cut that used it follows the new name
            if v[k].get() == old:
                v[k].set(new)
        lib["edit"] = new
        refresh_tools()
        lib_refresh()
        changed(now=True)

    small_btn(lib_btns, "+  New tool", new_tool, primary=True).pack(side="left")
    small_btn(lib_btns, "Duplicate", dup_tool).pack(side="left", padx=(6, 0))
    small_btn(lib_btns, "Delete", delete_tool).pack(side="left", padx=(6, 0))

    def refresh_tools():
        for cb, with_main, only, _ in tool_combos:
            vals = [t["name"] for t in tools if not only or t.get("tool_type") == only]
            cb.config(values=([MAIN] if with_main else []) + vals)

    tf = section("Selected tool", pg["tool"])
    name_var = tk.StringVar(value="")
    ttk.Label(tf, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 14), pady=3)
    name_entry_ = ttk.Entry(tf, textvariable=name_var, width=30)
    name_entry_.grid(row=0, column=1, columnspan=2, sticky="w", pady=3)
    name_entry_.bind("<Return>", rename_tool)
    name_entry_.bind("<FocusOut>", rename_tool)
    row(tf, "Diameter", "tool_d", "mm")
    row(tf, "Type", "tool_type", values=TOOL_TYPES)
    row(tf, "Flutes", "flutes")
    row(tf, "Tool material", "tool_mat", values=list(TOOL_MATS))
    row(tf, "V-bit angle", "tool_angle", "deg (V-bits only)")
    ttk.Label(tf, text="Everything here and in Speeds below is saved with this tool as you type. Pick which tool "
                       "does each cut on the Cuts, Layers, Marks and Surface pages.", foreground=C_MUTED,
              font=("Segoe UI", 9), wraplength=340, justify="left").grid(row=tf.grid_size()[1], column=0,
                                                                         columnspan=3, sticky="w", pady=(2, 4))

    sf = section("Stock and depth", pg["job"])
    row(sf, "Material", "material", values=list(MATERIALS), width=16)
    row(sf, "Material thickness", "thickness", "mm")
    row(sf, "Extra depth below stock", "extra", "mm")
    hint = ttk.Label(sf, text="", foreground=C_MUTED)
    hint.grid(row=sf.grid_size()[1], column=0, columnspan=3, sticky="w")
    row(sf, "Safe height", "safe_z", "mm")

    fd = section("Speeds, feeds and depth per pass", pg["tool"])
    row(fd, "Spindle speed", "rpm", "rpm")
    row(fd, "Cutting feed", "feed", "mm/min")
    row(fd, "Ramp feed", "ramp_feed", "mm/min")
    row(fd, "Plunge feed", "plunge_feed", "mm/min")
    row(fd, "Depth per pass", "step", "mm")
    row(fd, "Ramp angle", "ramp_angle", "deg")
    sug_box = tk.Text(fd, height=9, width=46, wrap="word", font=("Segoe UI", 9), relief="flat",
                      bg="#f7f9fb", fg=C_INK, padx=10, pady=6, highlightthickness=1,
                      highlightbackground=C_LINE)
    sug_box.grid(row=fd.grid_size()[1], column=0, columnspan=3, sticky="we", pady=(6, 2))
    sug_box.tag_configure("head", font=("Segoe UI", 9, "bold"))
    sug_box.tag_configure("warn", foreground=C_BAD)
    sug_box.tag_configure("amber", foreground="#b45309")
    sug_box.tag_configure("ok", foreground=C_OK)
    sug_box.tag_configure("note", foreground=C_MUTED)

    def use_suggestion():
        sug = state.get("sug")
        if not sug:
            return
        for k in ("rpm", "feed", "ramp_feed", "plunge_feed", "step"):
            v[k].set(f"{sug[k]:g}")
        changed(now=True)

    ttk.Button(fd, text="Use suggestion", command=use_suggestion).grid(
        row=fd.grid_size()[1], column=0, columnspan=3, sticky="we", pady=(2, 4))
    mr = fd.grid_size()[1]
    ttk.Label(fd, text="Spindle range").grid(row=mr, column=0, sticky="w", padx=(0, 6), pady=2)
    rng = ttk.Frame(fd)
    rng.grid(row=mr, column=1, sticky="w")
    for i, key in enumerate(("rpm_min", "rpm_max")):
        e = numbox(rng, key, width=7)
        e.pack(side="left")
        if i == 0:
            ttk.Label(rng, text=" to ").pack(side="left")
    ttk.Label(fd, text="rpm", foreground=C_MUTED).grid(row=mr, column=2, sticky="w", padx=(4, 0))
    row(fd, "Machine max feed", "feed_max", "mm/min")

    tb = section("Tabs (on outlines)", pg["cuts"])
    row(tb, "Tabs per outline", "tabs", "0 = none")
    row(tb, "Tab length", "tab_len", "mm")
    row(tb, "Tab thickness left", "tab_thick", "mm")
    c = ttk.Checkbutton(tb, text="Tabs on holes too", variable=v["tabs_on_holes"], command=lambda: changed())
    c.grid(row=tb.grid_size()[1], column=0, columnspan=3, sticky="w")

    su = section("Setup", pg["job"])
    row(su, "Milling direction", "direction", values=DIRECTIONS)
    row(su, "X0 Y0 at", "origin", values=ORIGINS)

    # ---- second tab: operations, tool changes, end of program
    def tool_row(f, label, key, with_main, only=None):
        cb = row(f, label, key, values=[MAIN], width=22)
        info_ = ttk.Label(f, text="", foreground=C_MUTED, font=("Segoe UI", 8))
        info_.grid(row=f.grid_size()[1], column=1, columnspan=3, sticky="w", pady=(0, 4))
        tool_combos.append((cb, with_main, only, info_))
        cb.bind("<<ComboboxSelected>>", lambda e: (changed(), refresh_tool_info()), add="+")
        return cb

    def refresh_tool_info():
        """Under each tool picker: the speeds that tool will cut with."""
        for cb, with_main, only, info_ in tool_combos:
            nm = cb.get()
            t_ = find_tool(v["tool_name"].get() if nm in (MAIN, MAIN_OLD) else nm)
            info_.config(text=tool_speed_line(t_) if t_ else ("pick a tool" if not nm else "not in the tool library"))
        lib_refresh()

    def note(f, text, wrap=340):
        ttk.Label(f, text=text, foreground=C_MUTED, wraplength=wrap, justify="left", font=("Segoe UI", 9)).grid(
            row=f.grid_size()[1], column=0, columnspan=3, sticky="w", pady=(0, 4))

    op = section("Cutting", pg["cuts"])
    tool_row(op, "Outlines tool", "tool_name", False)
    tool_row(op, "Holes and slots tool", "hole_tool", True)
    note(op, "Pick from the tool library (Tool page); each tool cuts with its own speeds. Holes can use a "
             "smaller tool than the outlines.")
    tb.master.pack_forget()   # (the tabs read best after the cutting tools)
    tb.master.pack(fill="x", pady=(18, 0), after=op.master)
    ttk.Checkbutton(op, text="Cut slots as wide as the end mill down the middle", variable=v["slot_center"],
                    command=lambda: changed()).grid(row=op.grid_size()[1], column=0, columnspan=3, sticky="w")
    row(op, "...even if narrower by", "slot_allow", "mm")
    note(op, "Such a slot comes out as wide as the end mill. Without this it is skipped, because the end mill cannot "
             "go around inside it.")
    ttk.Checkbutton(op, text="Finishing pass on walls", variable=v["finish"],
                    command=lambda: changed()).grid(row=op.grid_size()[1], column=0, columnspan=3, sticky="w",
                                                    pady=(6, 0))
    row(op, "Leave for finishing", "finish_allow", "mm")
    note(op, "The roughing passes stay this far off the wall; at the end one full-depth pass per outline and "
             "hole takes it off. Cleaner walls, and holes come out on size.")

    # ---- third tab: surfacing
    sfc = section("Surfacing (facing the top flat)", pg["surf"])
    ttk.Checkbutton(sfc, text="Surface the top first", variable=v["surf"], command=lambda: changed()).grid(
        row=0, column=0, columnspan=3, sticky="w")
    tool_row(sfc, "Tool", "surf_tool", True)
    row(sfc, "Depth to take off", "surf_depth", "mm")
    row(sfc, "Stepover", "surf_over", "% of the tool width")
    row(sfc, "Pattern", "surf_pattern", values=SURF_PATTERNS, width=12)
    row(sfc, "Area", "surf_area", values=SURF_AREAS, width=12)
    row(sfc, "Margin around drawing", "surf_margin", "mm")
    ar = sfc.grid_size()[1]
    ttk.Label(sfc, text="Custom X / Y").grid(row=ar, column=0, sticky="w", padx=(0, 6), pady=2)
    af = ttk.Frame(sfc)
    af.grid(row=ar, column=1, sticky="w")
    for key in ("surf_x", "surf_y"):
        ee = numbox(af, key, width=6)
        ee.pack(side="left", padx=(0, 4))
    ttk.Label(sfc, text="mm, lower-left corner", foreground=C_MUTED).grid(row=ar, column=2, sticky="w")
    ar = sfc.grid_size()[1]
    ttk.Label(sfc, text="Custom width / height").grid(row=ar, column=0, sticky="w", padx=(0, 6), pady=2)
    af = ttk.Frame(sfc)
    af.grid(row=ar, column=1, sticky="w")
    for key in ("surf_w", "surf_h"):
        ee = numbox(af, key, width=6)
        ee.pack(side="left", padx=(0, 4))
    ttk.Label(sfc, text="mm", foreground=C_MUTED).grid(row=ar, column=2, sticky="w")
    note(sfc, "Runs before everything else, with the depth per pass and feeds of its tool. The end mill's centre "
              "runs to the area's edges, so it overhangs them by its radius: keep clamps outside the yellow box "
              "in the preview.\n\n"
              "The bottom of the stock does not move, so cut-through depth and tabs stay the same; "
              "chamfers and edge breaks are measured from the new top.\n\n"
              "Custom rectangle works without a DXF (clear the DXF box), for facing a spoilboard or a sheet. "
              "Use the widest flat end mill you have; One direction gives a more even finish in aluminium.")

    # ---- layers tab: what each DXF layer does
    lay = section("What each layer of the DXF does", pg["layers"])
    lay_rows = ttk.Frame(lay)
    lay_rows.grid(row=0, column=0, columnspan=3, sticky="we")
    note(lay, "Cut through: outlines and holes, as before. Pocket: clears the inside of each closed shape to the "
              "depth (a shape inside a pocket stays standing as an island). Engrave on line: the tool centre runs "
              "along the line, closed or open, to the depth. Skip: not cut.\n"
              "Depths are from the top of the stock (after surfacing). A layer named like \"POCKET 1.5mm\" or "
              "\"ENGRAVE\" is set up from its name the first time.")
    row(lay, "Also skip layers", "skip_layers", "comma list", width=20)
    pk = section("Pockets and engraving", pg["layers"])
    tool_row(pk, "Pocket tool", "pocket_tool", True)
    row(pk, "Pocket stepover", "pocket_over", "% of tool")
    tool_row(pk, "Engraving tool", "engrave_tool", True)
    note(pk, "Pockets run first, then engraving, then holes and outlines. Each pocket depth is cleared in "
             "zigzag rows, ramping in, then the walls get a clean pass. A V-bit makes a fine engraving line.")

    # ---- marks page: typed texts and logos, a list and an editor for the one picked
    mk = section("Texts and logos", pg["marks"])
    mk_btns = ttk.Frame(mk)
    mk_btns.grid(row=0, column=0, columnspan=4, sticky="we")
    mk_list = tk.Frame(mk, bg=C_PANEL)
    mk_list.grid(row=1, column=0, columnspan=4, sticky="we", pady=(10, 0))
    mk_edit = ttk.Frame(pg["marks"])
    mk_edit.pack(fill="x")
    mt = section("Engraving tool", pg["marks"])
    tool_row(mt, "Tool", "engrave_tool", True)
    note(mt, "Every mark is cut with this tool (the same as layer engraving). A V-bit gives the cleanest lines; "
             "Filled needs an end mill, and its stepover is the pocket stepover on the Layers page.")
    FONT_NAMES = [SINGLE_LINE] + list(list_fonts())

    def sel_mark():
        i = state.get("sel")
        return state["marks"][i] if i is not None and 0 <= i < len(state["marks"]) else None

    def add_mark(kind):
        job = state["job"]
        cx_ = cy_ = 0.0
        pts_ = [p for c in (job.contours if job else []) for p in c]
        if pts_:
            cx_ = (min(p[0] for p in pts_) + max(p[0] for p in pts_)) / 2
            cy_ = (min(p[1] for p in pts_) + max(p[1] for p in pts_)) / 2
        state["marks"].append(new_mark(kind, round(cx_, 1), round(cy_, 1)))
        select_mark(len(state["marks"]) - 1)
        changed(now=True)

    def dup_mark():
        m = sel_mark()
        if m:
            c = json.loads(json.dumps(m))
            c["x"] = round(mark_num(c, "x") + 5, 2)
            c["y"] = round(mark_num(c, "y") - 5, 2)
            state["marks"].append(c)
            select_mark(len(state["marks"]) - 1)
            changed(now=True)

    def remove_mark():
        i = state.get("sel")
        if i is None:
            return
        state["marks"].pop(i)
        state["sel"] = None
        marks_refresh()
        changed(now=True)

    def pill(parent, text, cmd, primary=False):
        b = tk.Label(parent, text=text, bg=C_ACCENT if primary else "#eceef1", fg="#ffffff" if primary else C_INK,
                     padx=12, pady=4, cursor="hand2", font=("Segoe UI", 9, "bold" if primary else "normal"),
                     highlightthickness=1, highlightbackground=C_ACCENT if primary else "#d3d7dd")
        b.bind("<Button-1>", lambda e: cmd())
        hi = C_ACCENT_DK if primary else "#e2e6eb"
        lo_ = C_ACCENT if primary else "#eceef1"
        b.bind("<Enter>", lambda e: b.config(bg=hi))
        b.bind("<Leave>", lambda e: b.config(bg=lo_))
        return b
    pill(mk_btns, "+  Text", lambda: add_mark("text"), primary=True).pack(side="left")
    pill(mk_btns, "+  Logo", lambda: add_mark("logo"), primary=True).pack(side="left", padx=(6, 0))

    def mark_title(m):
        if m.get("type") == "logo":
            return m.get("logo", "Logo"), f"{mark_num(m, 'size'):g} mm wide  ·  {m.get('cut', 'Outline')}"
        txt = " ".join(str(m.get("text", "")).replace("|", " ").split()) or "(empty text)"
        f_ = m.get("font", SINGLE_LINE)
        return txt[:30], (f"{'Single line' if f_ == SINGLE_LINE else f_}  ·  {mark_num(m, 'size'):g} mm  ·  "
                          f"{m.get('cut', 'On the line')}")

    def marks_refresh():
        marks_list()
        mark_editor()
        root.after_idle(panel_fit)

    def marks_list():
        """The list's rows alone (the editor under the cursor is left as it is)."""
        for w_ in mk_list.winfo_children():
            w_.destroy()
        if not state["marks"]:
            tk.Label(mk_list, text="No marks yet. Add a text or an AGILITE logo, then drag it into place on the "
                                   "preview.", bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 9), wraplength=340,
                     justify="left").pack(anchor="w")
        for i, m in enumerate(state["marks"]):
            on = i == state.get("sel")
            bg = "#eaf2fd" if on else C_PANEL
            r_ = tk.Frame(mk_list, bg=bg, cursor="hand2", highlightthickness=1,
                          highlightbackground="#8db3ea" if on else C_LINE)
            r_.pack(fill="x", pady=(0, 4))
            if ICON_FONT:
                ic = tk.Label(r_, text="" if m.get("type") != "logo" else "", bg=bg,
                              fg=C_ACCENT if on else C_MUTED, font=(ICON_FONT, 13), width=3)
            else:
                ic = tk.Label(r_, text="T" if m.get("type") != "logo" else "*", bg=bg,
                              fg=C_ACCENT if on else C_MUTED, font=("Segoe UI", 11, "bold"), width=3)
            ic.pack(side="left", padx=(4, 2), pady=6)
            tx_ = tk.Frame(r_, bg=bg)
            tx_.pack(side="left", fill="x", expand=True)
            a_, b_ = mark_title(m)
            l1 = tk.Label(tx_, text=a_, bg=bg, fg=C_INK if not m.get("hidden") else "#a3acb6",
                          font=("Segoe UI Semibold", 10), anchor="w")
            l1.pack(fill="x")
            l2 = tk.Label(tx_, text=b_, bg=bg, fg=C_MUTED, font=("Segoe UI", 8), anchor="w")
            l2.pack(fill="x")
            if ICON_FONT:
                eye = tk.Label(r_, text="" if not m.get("hidden") else "", bg=bg,
                               fg=C_MUTED if not m.get("hidden") else "#b9c0ca", font=(ICON_FONT, 11),
                               cursor="hand2", padx=10)
            else:
                eye = tk.Label(r_, text="on" if not m.get("hidden") else "off", bg=bg, fg=C_MUTED,
                               font=("Segoe UI", 8), cursor="hand2", padx=10)
            eye.pack(side="right")

            def toggle(e, m=m):
                m["hidden"] = not m.get("hidden")
                marks_refresh()
                changed(now=True)
                return "break"
            eye.bind("<Button-1>", toggle)
            for w_ in (r_, ic, tx_, l1, l2):
                w_.bind("<Button-1>", lambda e, i=i: select_mark(i))

    def select_mark(i, show_page=False):
        state["sel"] = i
        marks_refresh()
        if show_page and nb.index() != 4:
            nb.select(4)
        draw_handles()

    def mark_editor():
        for w_ in mk_edit.winfo_children():
            w_.destroy()
        m = sel_mark()
        state["mark_vars"] = {}
        if not m:
            return
        f = section("Text" if m.get("type") != "logo" else "Logo", mk_edit)
        vars_ = state["mark_vars"]

        def setter(k):
            def go(*_):
                m[k] = vars_[k].get()
                changed()
            return go

        def mrow(label, k, step, unit, lo=None):
            r_ = f.grid_size()[1]
            ttk.Label(f, text=label).grid(row=r_, column=0, sticky="w", padx=(0, 14), pady=3)
            vars_[k] = tk.StringVar(value=f"{mark_num(m, k):g}")
            sb_ = spin(f, vars_[k], step, 8, lo, cmd=setter(k))
            sb_.grid(row=r_, column=1, sticky="w", pady=3)
            if unit:
                ttk.Label(f, text=unit, style="Muted.TLabel").grid(row=r_, column=2, sticky="w", padx=(8, 0))
            return sb_

        def mcombo(label, k, values, on_change=None):
            r_ = f.grid_size()[1]
            ttk.Label(f, text=label).grid(row=r_, column=0, sticky="w", padx=(0, 14), pady=3)
            vars_[k] = tk.StringVar(value=m.get(k, values[0]))
            cb_ = ttk.Combobox(f, textvariable=vars_[k], values=values, state="readonly", width=24, height=20)
            cb_.grid(row=r_, column=1, columnspan=2, sticky="w", pady=3)

            def go(e=None):
                m[k] = vars_[k].get()
                if on_change:
                    on_change()
                changed(now=True)
                marks_refresh()
            cb_.bind("<<ComboboxSelected>>", go)
            return cb_

        if m.get("type") != "logo":
            tb_ = tk.Text(f, height=3, width=30, wrap="none", font=("Segoe UI", 11), relief="flat", bg="#ffffff",
                          fg=C_INK, insertbackground=C_INK, padx=8, pady=6, highlightthickness=1,
                          highlightbackground=C_FIELD_LINE, highlightcolor=C_ACCENT, undo=True)
            tb_.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, 8))
            tb_.insert("1.0", str(m.get("text", "")).replace("|", "\n"))

            def text_changed(e=None):
                m["text"] = tb_.get("1.0", "end-1c")
                changed()
            tb_.bind("<KeyRelease>", text_changed)
            tb_.bind("<FocusOut>", lambda e: marks_list())

            def font_changed():
                fam = m.get("font", SINGLE_LINE)
                if fam == SINGLE_LINE:
                    m["cut"] = "On the line"
                elif m.get("cut") not in CUT_STYLES_SHAPE:
                    m["cut"] = "Outline"
            mcombo("Font", "font", FONT_NAMES, font_changed)
            if m.get("font", SINGLE_LINE) != SINGLE_LINE and "bold" in list_fonts().get(m.get("font"), {}):
                bv = tk.BooleanVar(value=bool(m.get("bold")))
                ttk.Checkbutton(f, text="Bold", variable=bv,
                                command=lambda: (m.__setitem__("bold", bv.get()), changed(now=True))).grid(
                    row=f.grid_size()[1], column=1, sticky="w")
            mrow("Letter height", "size", 0.5, "mm", 0.5)
        else:
            mcombo("Logo", "logo", list(LOGOS))
            rv = tk.BooleanVar(value=bool(m.get("registered", True)))
            ttk.Checkbutton(f, text="With the ® sign", variable=rv,
                            command=lambda: (m.__setitem__("registered", rv.get()), changed(now=True))).grid(
                row=f.grid_size()[1], column=1, columnspan=2, sticky="w")
            mrow("Width", "size", 1, "mm", 2)
        r_ = f.grid_size()[1]
        ttk.Label(f, text="Centre X / Y").grid(row=r_, column=0, sticky="w", padx=(0, 14), pady=3)
        xy_ = ttk.Frame(f)
        xy_.grid(row=r_, column=1, columnspan=2, sticky="w")
        for k in ("x", "y"):
            vars_[k] = tk.StringVar(value=f"{mark_num(m, k):g}")
            spin(xy_, vars_[k], 0.5, 7, None, cmd=setter(k)).pack(side="left", padx=(0, 6))
        ttk.Label(xy_, text="mm", style="Muted.TLabel").pack(side="left", padx=(2, 0))
        mrow("Angle", "angle", 5, "deg")
        if m.get("type") != "logo" and "\n" in str(m.get("text", "")).replace("|", "\n").strip():
            mcombo("Line align", "align", ALIGNS)
        single = m.get("type") != "logo" and m.get("font", SINGLE_LINE) == SINGLE_LINE
        mcombo("Cut", "cut", CUT_STYLES_LINE if single else CUT_STYLES_SHAPE)
        mrow("Depth", "depth", 0.05, "mm", 0.01)
        bt = ttk.Frame(f)
        bt.grid(row=f.grid_size()[1], column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(bt, text="Duplicate", command=dup_mark).pack(side="left")
        ttk.Button(bt, text="Remove", command=remove_mark).pack(side="left", padx=(6, 0))
        note(f, ("On the line: the end mill's centre follows the single-line letters. " if single else
                 "Outline: the end mill runs round each letter or plate. Filled: it clears them, like a pocket. ")
             + "On the preview, drag the mark to move it, a corner to size it, the round handle to turn it "
               "(hold Shift for 15° steps). Arrow keys nudge it 0.5 mm, Shift + arrow 5 mm.", wrap=340)

    def update_mark_fields():
        """After a drag on the preview: the editor's numbers follow."""
        m = sel_mark()
        vars_ = state.get("mark_vars") or {}
        if not m:
            return
        for k in ("x", "y", "size", "angle"):
            if k in vars_:
                vars_[k].set(f"{round(mark_num(m, k), 2):g}")

    def show_layers(stats):
        for w_ in lay_rows.winfo_children():
            w_.destroy()
        if not stats:
            ttk.Label(lay_rows, text="Choose a DXF to see its layers.", foreground=C_MUTED).grid(row=0, column=0)
            return
        for c_, head in enumerate(("Layer", "Shapes", "Does", "Depth mm")):
            ttk.Label(lay_rows, text=head.upper(), style="Head.TLabel").grid(row=0, column=c_, sticky="w", padx=(0, 10))
        for r_, name in enumerate(sorted(stats, key=str.lower), start=1):
            nc, no = stats[name]
            cur = state["layer_ops"].get(name) or default_layer_op(name)
            ttk.Label(lay_rows, text=name if len(name) <= 28 else name[:26] + "...").grid(
                row=r_, column=0, sticky="w", padx=(0, 8), pady=1)
            ttk.Label(lay_rows, text=f"{nc} closed" + (f", {no} open" if no else ""), foreground=C_MUTED).grid(
                row=r_, column=1, sticky="w", padx=(0, 8))
            opv, dv = tk.StringVar(value=cur["op"]), tk.StringVar(value=str(cur["depth"]))
            cb = ttk.Combobox(lay_rows, textvariable=opv, values=LAYER_OPS, state="readonly", width=15)
            cb.grid(row=r_, column=2, sticky="w", padx=(0, 8))
            de = ttk.Entry(lay_rows, textvariable=dv, width=6)
            de.grid(row=r_, column=3, sticky="w")

            def store(e=None, name=name, opv=opv, dv=dv, de=de):
                state["layer_ops"][name] = {"op": opv.get(), "depth": dv.get().strip()}
                de.config(state="normal" if opv.get() in ("Pocket", "Engrave on line") else "disabled")
                changed()
            cb.bind("<<ComboboxSelected>>", store)
            de.bind("<KeyRelease>", store)
            de.config(state="normal" if cur["op"] in ("Pocket", "Engrave on line") else "disabled")

    ch = section("Chamfer (V-bit)", pg["cuts"])
    ttk.Checkbutton(ch, text="Chamfer the top edges", variable=v["chamfer"], command=lambda: changed()).grid(
        row=0, column=0, columnspan=3, sticky="w")
    tool_row(ch, "V-bit", "chamfer_tool", False, VBIT)
    row(ch, "Chamfer width", "chamfer_w", "mm")
    note(ch, "Runs after the cut-out, on outlines and holes, and cleans off the top burr. "
             "Add your V-bits in the tool list with type V-bit and their angle.")

    eb = section("Edge break (ball nose)", pg["cuts"])
    ttk.Checkbutton(eb, text="Round the top edges", variable=v["edge_break"], command=lambda: changed()).grid(
        row=0, column=0, columnspan=3, sticky="w")
    tool_row(eb, "Ball nose", "edge_tool", True, "Ball nose")
    row(eb, "Edge size", "edge_size", "mm")
    note(eb, "Runs after the cut-out. The ball takes this much off the top face and the wall. "
             "With the main tool as the ball nose there is no tool change.")

    tcs = section("Tool changes and measuring", pg["machine"])
    row(tcs, "How", "tc_mode", values=TC_MODES, width=16)
    ttk.Checkbutton(tcs, text="Measure the first tool too (the job starts at the sensor)", variable=v["probe_first"],
                    command=lambda: changed()).grid(row=tcs.grid_size()[1], column=0, columnspan=3, sticky="w")
    pr_ = tcs.grid_size()[1]
    ttk.Label(tcs, text="Sensor spot").grid(row=pr_, column=0, sticky="w", padx=(0, 14), pady=3)
    prf = ttk.Frame(tcs)
    prf.grid(row=pr_, column=1, columnspan=2, sticky="w")
    for key in ("probe_dx", "probe_dy"):
        numbox(prf, key, width=6).pack(side="left", padx=(0, 6))
    ttk.Label(prf, text="mm from the drawing's lower-left", style="Muted.TLabel").pack(side="left")
    row(tcs, "Sensor base", "probe_d", "mm across")
    note(tcs, "Every tool is measured on the same spot of untouched stock, so Z0 is always the original top, even "
              "after surfacing. The spot is the dashed circle on the top view: drag it where your stock has room. "
              "At each change the machine lifts to the top, stops the spindle and goes there; it pauses for the end mill "
              "and the sensor, measures with Stepcraft's M31, and pauses again for you to take the sensor away.")
    ttk.Label(tcs, text="Change code").grid(row=tcs.grid_size()[1], column=0, sticky="w", padx=(0, 14), pady=3)
    e = ttk.Entry(tcs, textvariable=v["tc_code"], width=30)
    e.grid(row=tcs.grid_size()[1] - 1, column=1, columnspan=2, sticky="we", pady=3)
    e.bind("<KeyRelease>", lambda e: changed())
    note(tcs, "Advanced: what runs at the sensor spot. M0 = pause (its text shows in UCCNC), M31 = measure. "
              "{tool} = tool number, {name} = tool name, ; = new line. Not M6: your UCCNC ignores M6.")

    en = section("End of program", pg["machine"])
    row(en, "Lift to", "end_z_mode", values=END_Z, width=16)
    row(en, "Height", "end_z", "mm above stock")
    row(en, "Then move to", "end_xy", values=END_XY, width=16)
    xyr = en.grid_size()[1]
    ttk.Label(en, text="X / Y").grid(row=xyr, column=0, sticky="w", padx=(0, 6), pady=2)
    xyf = ttk.Frame(en)
    xyf.grid(row=xyr, column=1, sticky="w")
    for key in ("end_x", "end_y"):
        ee = numbox(xyf, key, width=6)
        ee.pack(side="left", padx=(0, 4))
    ttk.Label(en, text="mm (Custom X Y)", foreground=C_MUTED).grid(row=xyr, column=2, sticky="w")
    note(en, "Order: lift, spindle off, then move, so the end mill never drags over the part. "
             "Machine top / corner (G53) need a homed machine; they stop 3 mm short of the home switches.")
    refresh_tools()
    # the side panel as wide as its widest tab, so switching tabs never cuts a column off
    root.update_idletasks()
    lcan.configure(width=max(p_.winfo_reqwidth() for p_ in nb.pages) + 44)
    if not v["chamfer_tool"].get():
        v["chamfer_tool"].set(next((t["name"] for t in tools if t.get("tool_type") == VBIT), ""))

    # ---- right side: the check card, the views on a white card, the warnings and summary below
    status_card = tk.Frame(right, bg="#e7f5ec")
    status_card.pack(fill="x", pady=(0, 12))
    status_icon = tk.Label(status_card, text="", bg="#e7f5ec", fg=C_OK, font=(ICON_FONT or "Segoe UI", 14),
                           padx=12, pady=8)
    status_icon.pack(side="left")
    status = tk.Label(status_card, text="", anchor="w", font=("Segoe UI Semibold", 11), bg="#e7f5ec", fg=C_OK)
    status.pack(side="left", fill="x", expand=True)
    status_time = tk.Label(status_card, text="", bg="#e7f5ec", fg=C_OK, font=("Segoe UI Semibold", 11), padx=14)
    status_time.pack(side="right")
    card = tk.Frame(right, bg=C_PANEL, highlightthickness=1, highlightbackground=C_LINE)
    card.pack(fill="both", expand=True)
    views = Tabs(card, pad=12)
    views.pack(fill="both", expand=True)
    top_f = ttk.Frame(views)
    views.add(top_f, text="Top view")
    v3_f = ttk.Frame(views)
    views.add(v3_f, text="3D toolpath")
    sim_f = ttk.Frame(views)
    views.add(sim_f, text="Finished part")
    tbar = ttk.Frame(top_f, padding=(12, 8, 12, 8))
    tbar.pack(fill="x")
    mode = tk.StringVar(value="view")
    # a segmented switch for what the mouse does, as a CAD program's selection modes
    seg = tk.Frame(tbar, bg="#d3d7dd", padx=1, pady=1)
    seg.pack(side="left")
    seg_btns = {}

    def set_mode(val):
        mode.set(val)
        for k_, (b_, ic_) in seg_btns.items():
            on = k_ == val
            for w_ in (b_, ic_):
                w_.config(bg=C_ACCENT if on else "#ffffff", fg="#ffffff" if on else C_INK)
        canvas.config(cursor="arrow" if val == "view" else "crosshair")
        hint_status({"view": None, "tabs": "Click a red or blue cut line to add a tab, or click a tab to take it away.",
                     "start": "Click a red or blue cut line where that cut should start."}[val])
    for val, icon, text in (("view", "", "Select and move"), ("tabs", "", "Tabs"),
                            ("start", "", "Start points")):
        cellf = tk.Frame(seg, bg="#ffffff", cursor="hand2")
        cellf.pack(side="left", padx=(0 if val == "view" else 1, 0))
        ic_ = tk.Label(cellf, text=icon if ICON_FONT else "", bg="#ffffff", fg=C_INK,
                       font=(ICON_FONT, 10) if ICON_FONT else ("Segoe UI", 1), padx=(8 if ICON_FONT else 0))
        ic_.pack(side="left")
        b_ = tk.Label(cellf, text=text, bg="#ffffff", fg=C_INK, font=("Segoe UI", 9), padx=10, pady=4)
        b_.pack(side="left")
        for w_ in (cellf, ic_, b_):
            w_.bind("<Button-1>", lambda e, val=val: set_mode(val))
        seg_btns[val] = (b_, ic_)
    ttk.Button(tbar, text="Automatic tabs again", command=lambda: reset_manual()).pack(side="right")
    ttk.Button(tbar, text="Fit", command=lambda: fit()).pack(side="right", padx=(0, 6))
    tk.Frame(top_f, bg=C_LINE, height=1).pack(fill="x")
    canvas = tk.Canvas(top_f, bg="white", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    SPEEDS_LIST = ["1x real time", "5x", "10x", "25x", "50x", "100x", "250x"]
    DEPTH_LIST = ["True scale", "2x deeper", "5x deeper", "10x deeper"]
    TOP_HELP = ("Drag a mark to move it, drag empty space to pan, wheel to zoom, double-click to fit. "
                "Right-drag always pans.")
    V3_HELP = ("Press play to watch the cut; drag the bar to jump.  Drag to turn, wheel to zoom, double-click to "
               "reset.  Light blue = near the top, dark = deep.")
    SIM_HELP = ("Wheel to zoom, drag to move, double-click to fit.  Grey = stock left (darker = deeper), beige = cut "
                "through.  Point at the part to read the depth.")
    TILT_HELP = "Drag to turn it, wheel to zoom, double-click to reset.  True depth: nothing is stretched."
    sbar3 = ttk.Frame(sim_f, padding=(12, 8, 12, 8))
    sbar3.pack(fill="x")
    sseg = tk.Frame(sbar3, bg="#d3d7dd", padx=1, pady=1)
    sseg.pack(side="left")
    sim_seg = {}
    for val, text in (("top", "From above"), ("tilted", "Tilted 3D")):
        b_ = tk.Label(sseg, text=text, bg="#ffffff", fg=C_INK, font=("Segoe UI", 9), padx=14, pady=4,
                      cursor="hand2")
        b_.pack(side="left", padx=(0 if val == "top" else 1, 0))
        b_.bind("<Button-1>", lambda e, val=val: sim_mode(val))
        sim_seg[val] = b_
    sim_seg["top"].config(bg=C_ACCENT, fg="#ffffff")
    ttk.Button(sbar3, text="Fit", command=lambda: sim_fit()).pack(side="right")
    tk.Frame(sim_f, bg=C_LINE, height=1).pack(fill="x")
    cs = tk.Canvas(sim_f, bg="#ffffff", highlightthickness=0)
    cs.pack(fill="both", expand=True)
    c3 = tk.Canvas(v3_f, bg="#ffffff", highlightthickness=0)
    c3.pack(fill="both", expand=True)
    tk.Frame(v3_f, bg=C_LINE, height=1).pack(fill="x")
    bar3 = ttk.Frame(v3_f, padding=(12, 8))
    bar3.pack(fill="x")
    # playback: play / pause, back to the start, a scrub bar, the time, how fast
    def round_btn(parent, text, cmd, primary=False):
        b_ = tk.Label(parent, text=text, bg=C_ACCENT if primary else "#eceef1", fg="#ffffff" if primary else C_INK,
                      font=(ICON_FONT, 12) if ICON_FONT else ("Segoe UI", 9, "bold"), width=3, pady=3,
                      cursor="hand2")
        b_.bind("<Button-1>", lambda e: cmd())
        return b_
    restart_btn = round_btn(bar3, "" if ICON_FONT else "|<", lambda: play_restart())
    restart_btn.pack(side="left")
    play_btn = round_btn(bar3, "" if ICON_FONT else "Play", lambda: play_toggle(), primary=True)
    play_btn.pack(side="left", padx=(6, 10))
    play_pos = tk.DoubleVar(value=0)
    ttk.Scale(bar3, from_=0, to=1000, variable=play_pos, command=lambda val: play_scrub(val)).pack(
        side="left", fill="x", expand=True)
    play_label = ttk.Label(bar3, text="", width=19, anchor="center", font=("Segoe UI Semibold", 9))
    play_label.pack(side="left", padx=(10, 10))
    ttk.Label(bar3, text="SPEED", style="Head.TLabel").pack(side="left", padx=(6, 6))
    speed_var = tk.StringVar(value="25x")
    sp_ = ttk.Combobox(bar3, textvariable=speed_var, values=list(SPEEDS_LIST), state="readonly", width=12)
    sp_.pack(side="left")
    ttk.Label(bar3, text="DEPTH", style="Head.TLabel").pack(side="left", padx=(16, 6))
    depth_var = tk.StringVar(value="True scale")
    dp_ = ttk.Combobox(bar3, textvariable=depth_var, values=list(DEPTH_LIST), state="readonly", width=12)
    dp_.pack(side="left")
    dp_.bind("<<ComboboxSelected>>", lambda e: draw3d())

    info_card = tk.Frame(right, bg=C_PANEL, highlightthickness=1, highlightbackground=C_LINE)
    info_card.pack(fill="x", pady=(10, 0))
    info = tk.Text(info_card, height=11, wrap="word", font=("Segoe UI", 10), relief="flat", bg=C_PANEL,
                   fg=C_INK, padx=12, pady=8, highlightthickness=0, spacing1=1, spacing3=1)
    sb = ttk.Scrollbar(info_card, orient="vertical", command=info.yview)
    info.config(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    info.pack(side="left", fill="x", expand=True)
    info.tag_configure("warn", foreground=C_BAD, lmargin1=2, lmargin2=14)
    info.tag_configure("whead", foreground=C_BAD, font=FONT_HEAD, spacing1=2, spacing3=4)
    info.tag_configure("head", foreground=C_MUTED, font=FONT_HEAD, spacing1=2, spacing3=4)
    info.tag_configure("line", lmargin1=2, lmargin2=2)
    info.tag_configure("ok", foreground=C_OK)

    def hint_status(text=None, kind="muted"):
        sbar_left.config(text=TOP_HELP if text is None else text,
                         fg={"muted": C_MUTED, "ok": C_OK, "bad": C_BAD}[kind])

    state = {"job": None, "after": None, "zoom": 1.0, "pan": (0.0, 0.0), "yaw": -35.0, "pitch": 55.0,
             "zoom3": 1.0, "moves": None, "layer_ops": st["layer_ops"], "manual": st["manual"],
             "layers_shown": None, "sim": None, "sim_img": None, "marks": st["marks"], "sel": None,
             "mark_vars": {}, "recent": st["recent"], "cur_dxf": st["dxf"], "hist": [], "hist_i": -1,
             "jobs": load_jobs()}
    marks_refresh()
    set_mode("view")
    root._rail, root._state = nb, state   # (for the test scripts)

    def space(e):
        if views.index("current") != 1:
            return None
        if isinstance(root.focus_get(), (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox, ttk.Combobox)):
            return None
        play_toggle()
        return "break"
    root.bind("<space>", space)
    root._v = v
    root._hooks = {"undo": lambda: undo(), "redo": lambda: redo(), "changed": lambda: changed(now=True),
                   "play": lambda: play_toggle(), "seek": lambda t_: play_seek(t_), "restart": lambda: play_restart()}

    def set_status(job, bad_numbers=False):
        """APF+'s check card: green when ready, amber with warnings, red when nothing can be saved."""
        dxf = v["dxf"].get().strip()
        bar_file.config(text=os.path.basename(dxf) if dxf else "No DXF open")
        if job and job.lines:
            nl = sum(len(f[1]) for f in job.files)
            sbar_right.config(text=f"{_fmt_time(job.time_min)}  ·  {nl:,} lines  ·  "
                                   f"{len(job.files)} file{'s' if len(job.files) > 1 else ''}")
        else:
            sbar_right.config(text="")
        def paint(text, bg, fg, icon, tm=""):
            for w_ in (status_card, status_icon, status, status_time):
                w_.config(bg=bg, fg=fg) if w_ is not status_card else w_.config(bg=bg)
            status.config(text=text)
            status_icon.config(text=icon if ICON_FONT else "")
            status_time.config(text=tm)
        if bad_numbers:
            paint("A number field has something that is not a number", "#fdecea", C_BAD, "")
        elif not job or not job.lines:
            if job and job.warnings:
                paint(job.warnings[0], "#fdecea", C_BAD, "")
            else:
                paint("Open a DXF to start, or add a mark or surfacing to cut without one", "#eef1f4", C_MUTED,
                      "")
        elif job.warnings:
            n = len(job.warnings)
            paint(f"Ready to save, with {n} warning{'s' if n > 1 else ''} to read below", "#fff4e5", "#b45309",
                  "", _fmt_time(job.time_min))
        else:
            paint("Ready to save, no problems found", "#e7f5ec", C_OK, "", _fmt_time(job.time_min))

    def update_hint():
        try:
            d = float(v["tool_d"].get().replace(",", "."))
        except ValueError:
            hint.config(text="")
            return
        if v["tool_type"].get() == "Ball nose":
            hint.config(text=f"Ball nose: radius + 0.2 = {d / 2 + 0.2:.2f} mm to cut full width through")
        else:
            hint.config(text="Flat end mill: 0.2 mm is enough to cut through")

    def raw_settings():
        r = {k: (var.get() if not isinstance(var, tk.BooleanVar) else bool(var.get())) for k, var in v.items()}
        r["layer_ops"], r["manual"], r["marks"] = state["layer_ops"], state["manual"], state["marks"]
        r["recent"] = state["recent"]
        return r

    # ---- one job per drawing: switching drawings puts this one's job away and brings the other's back
    def store_job(dxf, raw=None):
        if dxf:
            state["jobs"][manual_key(dxf)] = job_of(raw or raw_settings())
            save_jobs(state["jobs"])

    def set_var(k, val):
        if k not in v:
            return
        if isinstance(v[k], tk.BooleanVar):
            v[k].set(bool(val))
        else:
            v[k].set(str(val))

    def switch_drawing(new):
        old = state.get("cur_dxf")
        if old and os.path.isfile(old):
            raw_ = raw_settings()   # (the fields still hold the old drawing's job; only the file name moved on)
            raw_["out_name"] = (state.get("last_raw") or raw_).get("out_name", raw_["out_name"])
            store_job(old, raw_)
        job_ = state["jobs"].get(manual_key(new))
        if job_:
            for k, val in job_.items():
                if k == "marks":
                    state["marks"][:] = json.loads(json.dumps(val))
                elif k != "out_name" or not name_edited["v"]:
                    set_var(k, val)
            name_edited["v"] = bool(job_.get("out_name"))
            tell(f"Opened {os.path.basename(new)} with its own settings, as you left them.")
        else:   # a new drawing: today's settings, no marks from the last one
            state["marks"][:] = []
            tell("New drawing: the settings you had, no marks yet.")
        state["sel"] = None
        state["cur_dxf"] = new
        rec = [new] + [r_ for r_ in state["recent"] if os.path.normcase(r_) != os.path.normcase(new)]
        state["recent"][:] = rec[:12]
        refresh_recent()
        marks_refresh()
        state["hist"], state["hist_i"] = [], -1   # undo stays within one drawing
        state["zoom"], state["pan"] = 1.0, (0.0, 0.0)

    # ---- undo / redo: every settled change is a step (typing is grouped by the half-second pause)
    def snapshot():
        r_ = raw_settings()
        r_ = {k: val for k, val in r_.items() if k not in ("dxf", "recent")}
        return json.dumps({"s": r_, "tools": tools}, sort_keys=True)

    def remember():
        snap_ = snapshot()
        h, i = state["hist"], state["hist_i"]
        if i >= 0 and h[i] == snap_:
            return
        del h[i + 1:]
        h.append(snap_)
        if len(h) > 150:
            del h[0]
        state["hist_i"] = len(h) - 1

    def restore(snap_):
        d = json.loads(snap_)
        for k, val in d["s"].items():
            if k == "marks":
                state["marks"][:] = val
            elif k in ("manual", "layer_ops"):
                state[k].clear()
                state[k].update(val)
            else:
                set_var(k, val)
        tools[:] = d["tools"]
        save_tools(tools)
        refresh_tools()
        state["layers_shown"] = None
        if state.get("sel") is not None and state["sel"] >= len(state["marks"]):
            state["sel"] = None
        marks_refresh()
        ed = lib["edit"] if find_tool(lib["edit"] or "") else v["tool_name"].get()
        lib["edit"] = None
        edit_tool(ed)
        compute()

    def undo(e=None):
        if isinstance(root.focus_get(), tk.Text):   # (the mark's text box has its own undo)
            return None
        if state["hist_i"] > 0:
            state["hist_i"] -= 1
            restore(state["hist"][state["hist_i"]])
            n_ = state["hist_i"]
            tell(f"Undone.  {n_} more step{'s' if n_ != 1 else ''} back.  Ctrl+Y to redo.")
        else:
            tell("Nothing more to undo.", True)
        return "break"

    def redo(e=None):
        if isinstance(root.focus_get(), tk.Text):
            return None
        if state["hist_i"] < len(state["hist"]) - 1:
            state["hist_i"] += 1
            restore(state["hist"][state["hist_i"]])
            tell("Redone.")
        else:
            tell("Nothing to redo.", True)
        return "break"

    def compute():
        state["after"] = None
        new_dxf = v["dxf"].get().strip()
        if new_dxf != (state.get("cur_dxf") or "") and os.path.isfile(new_dxf):
            switch_drawing(new_dxf)
        write_back()
        update_hint()
        info.delete("1.0", "end")
        try:
            s = parse(raw_settings())
        except ValueError:
            info.insert("end", "A number field has something that is not a number.\n", "warn")
            state["job"] = None
            set_status(None, bad_numbers=True)
            draw(None)
            return
        if not name_edited["v"] and s["dxf"]:
            v["out_name"].set(default_name(s["dxf"]))
        show_suggestion(s)
        try:
            job = build(s, tools)
        except Exception as ex:  # keep the window alive on a bad file
            info.insert("end", f"Could not read this DXF: {ex}\n", "warn")
            state["job"] = None
            set_status(None)
            draw(None)
            return
        if state["job"] is None or state["job"].contours != job.contours:
            state["zoom"], state["pan"] = 1.0, (0.0, 0.0)   # new drawing: fit it
        state["job"] = job
        state["moves"] = None
        play_stop()
        state["play_t"] = None
        state["sim"] = None
        state["sim_key"] = state["tilt_key"] = None
        shown = tuple(sorted(job.layers))
        if shown != state["layers_shown"]:
            state["layers_shown"] = shown
            show_layers(job.layers)
        set_status(job)
        refresh_tool_info()
        if job.warnings:
            info.insert("end", f"WARNINGS  ({len(job.warnings)})\n", "whead")
            for w in job.warnings:
                info.insert("end", "•  " + w + "\n", "warn")
            info.insert("end", "\n")
        info.insert("end", "SUMMARY\n", "head")
        for line in job.summary:
            info.insert("end", line + "\n", "line")
        draw(job)
        if views.index("current") == 1:
            draw3d()
        elif views.index("current") == 2:
            draw_sim()
        state["last_raw"] = raw_settings()
        remember()

    def show_suggestion(s):
        sug_box.config(state="normal")
        sug_box.delete("1.0", "end")
        if s["tool_d"] <= 0 or s["flutes"] < 1:
            sug_box.insert("end", "Set a tool diameter and at least 1 flute for a suggestion.", "warn")
            state["sug"] = None
            sug_box.config(state="disabled")
            return
        sug = suggest(s)
        state["sug"] = sug
        sug_box.insert("end", f"Suggested for {s['tool_d']:g} mm, {s['flutes']} flute, {s['material'].lower()}:\n", "head")
        sug_box.insert("end", f"{sug['rpm']} rpm, feed {sug['feed']:g}, ramp {sug['ramp_feed']:g}, "
                              f"plunge {sug['plunge_feed']:g} mm/min, {sug['step']:.2f} mm per pass\n")
        sug_box.insert("end", "Your settings now:\n", "head")
        for text, tag in check_speeds(s, sug):
            sug_box.insert("end", text + "\n", tag)
        for n in sug["notes"]:
            sug_box.insert("end", n + "\n", "note")
        sug_box.insert("end", "Starting values: trust the chips and the sound and adjust.", "note")
        sug_box.config(state="disabled")

    def changed(now=False):
        if state["after"]:
            root.after_cancel(state["after"])
        state["after"] = root.after(10 if now else 500, compute)

    def draw(job):
        canvas.delete("all")
        W, H = canvas.winfo_width(), canvas.winfo_height()
        if not job or not (job.contours or job.preview or job.mark_paths):
            # empty state: a big friendly target to click
            if ICON_FONT:
                canvas.create_text(W / 2, H / 2 - 40, text="", fill="#c6ccd4", font=(ICON_FONT, 44),
                                   tags="open")
            canvas.create_text(W / 2, H / 2 + 14, text="Open a DXF to start", fill=C_INK,
                               font=("Segoe UI Semibold", 14), tags="open")
            canvas.create_text(W / 2, H / 2 + 40, text="or add a text or logo on the Marks page, or surface a "
                                                       "custom rectangle", fill=C_MUTED, font=("Segoe UI", 10))
            canvas.tag_bind("open", "<Button-1>", lambda e: pick_dxf())
            return
        # the view fits the drawing; marks and cuts only when there is no drawing (moving a mark never re-zooms)
        base = [p for c in job.contours + job.open_paths for p in c]
        pts = (base or ([p for m_ in job.mark_paths for p in m_[0]] + [p for _, q, _, _, _ in job.preview for p in q]))             + [(0, 0)]
        if job.probe and job.pauses:   # the sensor spot is always in view
            pts += [(job.probe[0] - job.probe[2], job.probe[1] - job.probe[2]),
                    (job.probe[0] + job.probe[2], job.probe[1] + job.probe[2])]
        x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
        y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
        try:
            td = float(v["tool_d"].get().replace(",", "."))
        except ValueError:
            td = 0
        pad = td + 4
        sc = min((W - 40) / max(x1 - x0 + 2 * pad, 1e-6), (H - 60) / max(y1 - y0 + 2 * pad, 1e-6))
        sc *= state["zoom"]
        cx = (W - (x1 - x0) * sc) / 2 - x0 * sc + state["pan"][0]
        cy = (H + (y1 - y0) * sc) / 2 + y0 * sc + state["pan"][1]
        state["T"] = (cx, cy, sc)

        def T(p):
            return (cx + p[0] * sc, cy - p[1] * sc)

        # a quiet millimetre grid: 10 mm lines, 50 mm a little darker, as on a CAD sheet
        step_mm = 10 if sc * 10 >= 12 else 50 if sc * 50 >= 12 else 100
        gx0, gx1 = (0 - cx) / sc, (W - cx) / sc
        gy0, gy1 = (cy - H) / sc, cy / sc
        k_ = math.floor(gx0 / step_mm)
        while k_ * step_mm <= gx1:
            X = cx + k_ * step_mm * sc
            canvas.create_line(X, 0, X, H, fill="#e9edf2" if (k_ * step_mm) % 50 == 0 else "#f3f5f8")
            k_ += 1
        k_ = math.floor(gy0 / step_mm)
        while k_ * step_mm <= gy1:
            Y = cy - k_ * step_mm * sc
            canvas.create_line(0, Y, W, Y, fill="#e9edf2" if (k_ * step_mm) % 50 == 0 else "#f3f5f8")
            k_ += 1

        for kd, q, rows, n, bw in job.preview:   # surfacing area under everything else
            if kd == "surface":
                xs_, ys_ = zip(*[T(p) for p in q])
                canvas.create_rectangle(min(xs_), min(ys_), max(xs_), max(ys_), fill="#fff8d6", outline="#e0b100",
                                        width=2)
                for a, b in rows:
                    canvas.create_line(*T(a), *T(b), fill="#f0d36b")
        cuts = [pv for pv in job.preview if pv[0] not in ("surface", "pocket")]

        def flat(loop, close=True):
            out = []
            for p in (loop + [loop[0]] if close else loop):
                out += T(p)
            return out

        for kd, q, rows, n, bw in job.preview:   # pockets: the cleared area, its rows and the wall pass
            if kd == "pocket":
                canvas.create_line(*flat(q), fill="#c8e6c9", width=max(1, bw * sc), capstyle="round",
                                   joinstyle="round")
                for a, b in rows:
                    canvas.create_line(*T(a), *T(b), fill="#a5d6a7", width=max(1, min(bw * sc, 3)))
        for kd, q, rows, n, bw in job.preview:
            if kd == "pocket":
                canvas.create_line(*flat(q), fill=C_OK, width=1, dash=(4, 3))

        band = {"part": "#f9d3d0", "hole": "#d2e3f8", "chamfer": "#e1bee7", "edge": "#b2dfdb",
                "engrave": "#ffe0b2", "engrave_open": "#ffe0b2"}
        line = {"part": C_BAD, "hole": C_ACCENT, "chamfer": "#7b1fa2", "edge": "#00796b",
                "engrave": "#e65100", "engrave_open": "#e65100", "finish": "#1d2733"}
        letter = {"part": "O", "hole": "H", "chamfer": "C", "edge": "E"}
        for kd, q, tabs, n, bw in cuts:
            if kd in ("part", "hole"):
                canvas.create_line(*flat(q), fill=band[kd], width=max(1, bw * sc), capstyle="round", joinstyle="round")
        for kd, q, tabs, n, bw in cuts:
            if kd not in ("part", "hole", "finish"):
                canvas.create_line(*flat(q, kd != "engrave_open"), fill=band[kd], width=max(1, bw * sc),
                                   capstyle="round", joinstyle="round")
        for c in job.contours:
            canvas.create_line(*flat(c), fill=C_INK, width=1.5)
        for c in job.open_paths:
            canvas.create_line(*flat(c, False), fill=C_INK, width=1.5)
        for pts_, closed_ in job.mark_paths:   # marks: the letters and logos themselves
            if len(pts_) >= 2:
                canvas.create_line(*flat(pts_, closed_), fill="#5b6b7c", width=1)
        for kd, q, tabs, n, bw in cuts:
            canvas.create_line(*flat(q, kd != "engrave_open"), fill=line[kd], width=1,
                               dash=(1, 3) if kd == "finish" else (4, 3))
            for smp in tabs:
                canvas.create_line(*flat(smp, close=False), fill=C_AUTO, width=max(3, bw * sc * 0.6), capstyle="butt")
            if kd in ("part", "hole"):
                sx, sy = T(q[0])
                canvas.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, fill=C_OK, outline="")
                canvas.create_text(sx + 8, sy - 8, text=letter[kd] + str(n), fill=C_OK,
                                   font=("Segoe UI", 9, "bold"), anchor="sw")
        ox, oy = T((0, 0))
        canvas.create_line(ox - 12, oy, ox + 12, oy, fill=C_OK, width=2)
        canvas.create_line(ox, oy - 12, ox, oy + 12, fill=C_OK, width=2)
        canvas.create_text(ox + 6, oy + 6, text="X0 Y0", anchor="nw", fill=C_OK, font=("Segoe UI", 9))
        # legend: a dot for each colour that is in this job, wrapping onto more rows when the view is narrow
        kinds = {pv[0] for pv in job.preview}
        has_tabs = any(pv[2] for pv in job.preview if pv[0] in ("part", "hole"))
        items = []
        lx, ly = 14, 0
        for col, text, shown in ((C_INK, "drawing", bool(job.contours or job.open_paths)),
                                 (C_BAD, "outline cut", "part" in kinds), (C_ACCENT, "holes", "hole" in kinds),
                                 ("#43a047", "pocket", "pocket" in kinds),
                                 ("#ef6c00", "engraving", bool(kinds & {"engrave", "engrave_open"})),
                                 ("#8e24aa", "chamfer", "chamfer" in kinds), ("#00897b", "edge break", "edge" in kinds),
                                 ("#e0b100", "surfacing", "surface" in kinds), (C_AUTO, "tabs", has_tabs),
                                 (C_OK, "start point, H / O = order", bool(kinds & {"part", "hole"}))):
            if not shown:
                continue
            tid = canvas.create_text(0, 0, anchor="w", text=text, fill=C_MUTED, font=("Segoe UI", 9))
            tw = canvas.bbox(tid)[2] - canvas.bbox(tid)[0]
            if lx + 14 + tw > W - 10 and lx > 14:
                lx, ly = 14, ly + 1
            items.append((tid, col, lx, ly))
            lx += 14 + tw + 18
        rows_ = ly + 1
        band_top = H - 24 * rows_ - 4
        canvas.create_rectangle(0, band_top, W, H, fill="#ffffff", outline="")
        canvas.create_line(0, band_top, W, band_top, fill=C_LINE)
        for tid, col, x, r_ in items:
            y = band_top + 15 + 24 * r_
            canvas.create_oval(x, y - 4, x + 8, y + 4, fill=col, outline="")
            canvas.coords(tid, x + 14, y)
            canvas.tag_raise(tid)
        draw_probe()
        draw_handles()

    def zoom_top(e, f):
        cx, cy, sc = state.get("T", (0, 0, 1))
        # keep the point under the mouse where it is
        px, py = state["pan"]
        state["zoom"] = max(0.2, min(200.0, state["zoom"] * f))
        state["pan"] = (e.x - (e.x - px - (canvas.winfo_width() / 2)) * f - canvas.winfo_width() / 2,
                        e.y - (e.y - py - (canvas.winfo_height() / 2)) * f - canvas.winfo_height() / 2)
        draw(state["job"])

    def drag_start(e):
        state["drag"] = (e.x, e.y, state["pan"])

    def drag_move(e):
        x0_, y0_, (px, py) = state.get("drag", (e.x, e.y, state["pan"]))
        state["pan"] = (px + e.x - x0_, py + e.y - y0_)
        draw(state["job"])

    def fit(e=None):
        state["zoom"], state["pan"] = 1.0, (0.0, 0.0)
        draw(state["job"])

    def tell(text, bad=False):
        hint_status(text, "bad" if bad else "ok")

    def manual_here():
        dxf = v["dxf"].get().strip()
        return state["manual"].setdefault(manual_key(dxf), {"tabs": {}, "starts": {}}) if dxf else None

    def click_edit(e):
        job = state["job"]
        if not job or not job.loops or "T" not in state:
            tell("Nothing to click on yet.", True)
            return
        cx, cy, sc = state["T"]
        p = ((e.x - cx) / sc, (cy - e.y) / sc)
        best = min(job.loops, key=lambda lp: project(lp["loop"], p)[3])
        hit = project(best["loop"], p)
        if hit[3] * sc > 15:
            tell("Click closer to a dashed red or blue cut line.", True)
            return
        man = manual_here()
        if man is None:
            return
        ox, oy = job.origin
        what = "outline" if best["kind"] == "part" else "hole"
        if mode.get() == "start":
            man["starts"][best["key"]] = [hit[1][0] + ox, hit[1][1] + oy]
            tell(f"This {what} now starts where you clicked.")
        else:
            if not best["can_tab"]:
                tell("Holes get tabs only with \"Tabs on holes too\" ticked (Tool and cut tab)." if best["kind"] == "hole"
                     else "This cut cannot have tabs (the tab is as deep as the cut).", True)
                return
            tabs = man["tabs"].get(best["key"])
            if tabs is None:   # first click on this shape: start from the automatic tabs
                tabs = [[x + ox, y + oy] for x, y in best["tabs"]]
            near = [i for i, (x, y) in enumerate(tabs) if _d((x - ox, y - oy), hit[1]) <= max(best["h"], 12 / sc)]
            if near:
                tabs.pop(min(near, key=lambda i: _d((tabs[i][0] - ox, tabs[i][1] - oy), hit[1])))
            else:
                tabs.append([hit[1][0] + ox, hit[1][1] + oy])
            man["tabs"][best["key"]] = tabs
            tell(f"{'Removed a tab' if near else 'Added a tab'}: this {what} has {len(tabs)} tab(s), placed by hand.")
        changed(now=True)

    def reset_manual():
        dxf = v["dxf"].get().strip()
        if dxf and state["manual"].pop(manual_key(dxf), None):
            tell("Tabs and start points are automatic again.")
            changed(now=True)

    # ---- the sensor spot on the top view: a circle the size of the sensor's base, dragged like a mark
    def draw_probe(at=None):
        canvas.delete("probe")
        job = state["job"]
        if not job or not job.probe or not job.pauses or "T" not in state:
            return
        cx, cy, sc = state["T"]
        px, py, r = at or job.probe
        X, Y, R = cx + px * sc, cy - py * sc, r * sc
        bad = any(w_.startswith("The sensor spot") for w_ in job.warnings) and at is None
        col = C_BAD if bad else "#7c3aed"
        canvas.create_oval(X - R, Y - R, X + R, Y + R, outline=col, width=2, dash=(6, 4), tags="probe")
        canvas.create_line(X - 7, Y, X + 7, Y, fill=col, width=1.5, tags="probe")
        canvas.create_line(X, Y - 7, X, Y + 7, fill=col, width=1.5, tags="probe")
        canvas.create_text(X, Y + R + 10, text="Z sensor" + ("  (on a cut!)" if bad else ""), fill=col,
                           font=("Segoe UI Semibold", 9), tags="probe")

    def probe_hit(px_, py_):
        job = state["job"]
        if not job or not job.probe or not job.pauses or "T" not in state:
            return False
        cx, cy, sc = state["T"]
        x, y, r = job.probe
        return math.hypot((px_ - cx) / sc - x, (cy - py_) / sc - y) <= r

    # ---- marks on the preview: pick one, drag it, size it from a corner, turn it with the round handle
    def mark_box(m, w, h):
        """The mark's box corners (drawing mm), a little bigger than its letters, and its up direction."""
        cx_, cy_ = mark_num(m, "x"), mark_num(m, "y")
        a = math.radians(mark_num(m, "angle"))
        ca, sa = math.cos(a), math.sin(a)
        sc_ = state.get("T", (0, 0, 1))[2]
        pad = 5 / max(sc_, 1e-6)
        hw, hh = w / 2 + pad, h / 2 + pad
        pts_ = [(cx_ + x_ * ca - y_ * sa, cy_ + x_ * sa + y_ * ca) for x_, y_ in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
        return pts_, (-sa, ca), hh

    def mark_dims(i):
        job = state["job"]
        for mm_ in (job.marks if job else []):
            if mm_["i"] == i:
                return mm_["w"], mm_["h"]
        return None

    def draw_handles():
        canvas.delete("handles")
        i = state.get("sel")
        job = state["job"]
        if i is None or not job or "T" not in state or i >= len(state["marks"]):
            return
        dims = mark_dims(i)
        if not dims or state["marks"][i].get("hidden"):
            return
        m = state["marks"][i]
        cx, cy, sc = state["T"]

        def T(p):
            return (cx + p[0] * sc, cy - p[1] * sc)
        box, up, hh = mark_box(m, *dims)
        sp = [T(p) for p in box]
        canvas.create_polygon(*[c for p in sp for c in p], fill="", outline=C_ACCENT, dash=(5, 3), width=1.5,
                              tags="handles")
        top_mid = ((sp[2][0] + sp[3][0]) / 2, (sp[2][1] + sp[3][1]) / 2)
        ux, uy = up[0], -up[1]
        rot = (top_mid[0] + ux * 26, top_mid[1] + uy * 26)
        canvas.create_line(*top_mid, *rot, fill=C_ACCENT, width=1.5, tags="handles")
        canvas.create_oval(rot[0] - 7, rot[1] - 7, rot[0] + 7, rot[1] + 7, fill="#ffffff", outline=C_ACCENT,
                           width=2, tags=("handles", "h_rot"))
        for k_, p in enumerate(sp):
            canvas.create_rectangle(p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5, fill="#ffffff", outline=C_ACCENT,
                                    width=2, tags=("handles", f"h_c{k_}"))
        state["handle_px"] = {"rot": rot, "corners": sp}

    def mark_hit(px, py):
        """The topmost shown mark whose box holds this screen point."""
        job = state["job"]
        if not job or "T" not in state:
            return None
        cx, cy, sc = state["T"]
        x, y = (px - cx) / sc, (cy - py) / sc
        for mm_ in reversed(job.marks):
            m = state["marks"][mm_["i"]] if mm_["i"] < len(state["marks"]) else None
            if not m:
                continue
            a = math.radians(mark_num(m, "angle"))
            dx, dy = x - mark_num(m, "x"), y - mark_num(m, "y")
            lx, ly = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
            pad = 5 / sc
            if abs(lx) <= mm_["w"] / 2 + pad and abs(ly) <= mm_["h"] / 2 + pad:
                return mm_["i"]
        return None

    def handle_hit(px, py):
        hp = state.get("handle_px")
        if not hp or state.get("sel") is None:
            return None
        if math.hypot(px - hp["rot"][0], py - hp["rot"][1]) <= 10:
            return "rotate"
        for p in hp["corners"]:
            if math.hypot(px - p[0], py - p[1]) <= 9:
                return "scale"
        return None

    def ghost(m, shapes, factor=1.0):
        canvas.delete("ghost")
        cx, cy, sc = state["T"]
        placed = place_mark([([(x_ * factor, y_ * factor) for x_, y_ in s], c) for s, c in shapes], m)
        for pts_, closed_ in placed:
            if len(pts_) < 2:
                continue
            fl = []
            for p in (pts_ + [pts_[0]] if closed_ else pts_):
                fl += (cx + p[0] * sc, cy - p[1] * sc)
            canvas.create_line(*fl, fill=C_ACCENT, width=1.5, tags="ghost")

    def mark_press(e):
        canvas.focus_set()
        h_ = handle_hit(e.x, e.y)
        if not h_ and mark_hit(e.x, e.y) is None and probe_hit(e.x, e.y):
            cx, cy, sc = state["T"]
            state["mdrag"] = {"kind": "probe", "p0": ((e.x - cx) / sc, (cy - e.y) / sc), "moved": False,
                              "orig": (float(v["probe_dx"].get() or 0), float(v["probe_dy"].get() or 0)),
                              "at": state["job"].probe}
            return
        i = state.get("sel") if h_ else mark_hit(e.x, e.y)
        if i is None:
            if state.get("sel") is not None:
                state["sel"] = None
                marks_refresh()
                draw_handles()
            drag_start(e)
            state["mdrag"] = None
            return
        if i != state.get("sel"):
            select_mark(i, show_page=True)
        m = state["marks"][i]
        cx, cy, sc = state["T"]
        shapes, w, h, _ = mark_shapes(m)
        state["mdrag"] = {"kind": h_ or "move", "i": i, "p0": ((e.x - cx) / sc, (cy - e.y) / sc),
                          "orig": dict(m), "shapes": shapes, "moved": False}

    def mark_motion(e):
        d_ = state.get("mdrag")
        if not d_:
            if mode.get() == "view":
                drag_move(e)
            return
        if d_["kind"] == "probe":
            cx, cy, sc = state["T"]
            ddx, ddy = (e.x - cx) / sc - d_["p0"][0], (cy - e.y) / sc - d_["p0"][1]
            nx_, ny_ = round(d_["orig"][0] + ddx), round(d_["orig"][1] + ddy)   # whole millimetres
            v["probe_dx"].set(str(nx_))
            v["probe_dy"].set(str(ny_))
            x0_, y0_, r_ = d_["at"]
            draw_probe((x0_ + nx_ - d_["orig"][0], y0_ + ny_ - d_["orig"][1], r_))
            d_["moved"] = True
            hint_status(f"Sensor spot {nx_:g} mm, {ny_:g} mm from the drawing's lower-left corner", "ok")
            return
        m = state["marks"][d_["i"]]
        o = d_["orig"]
        cx, cy, sc = state["T"]
        p = ((e.x - cx) / sc, (cy - e.y) / sc)
        c0 = (mark_num(o, "x"), mark_num(o, "y"))
        factor = 1.0
        if d_["kind"] == "move":
            m["x"] = round(c0[0] + p[0] - d_["p0"][0], 2)
            m["y"] = round(c0[1] + p[1] - d_["p0"][1], 2)
            hint_status(f"X {m['x']:.1f}   Y {m['y']:.1f}  (the centre of the mark)", "ok")
        elif d_["kind"] == "scale":
            r0 = max(_d(c0, d_["p0"]), 1e-6)
            factor = max(0.05, _d(c0, p) / r0)
            m["size"] = round(max(0.5, mark_num(o, "size") * factor), 2)
            factor = m["size"] / max(mark_num(o, "size"), 1e-6)
            hint_status(f"{'Width' if m.get('type') == 'logo' else 'Letter height'} {m['size']:g} mm", "ok")
        else:
            a0 = math.atan2(d_["p0"][1] - c0[1], d_["p0"][0] - c0[0])
            a1 = math.atan2(p[1] - c0[1], p[0] - c0[0])
            ang = mark_num(o, "angle") + math.degrees(a1 - a0)
            if e.state & 0x0001:   # Shift: 15 degree steps
                ang = round(ang / 15) * 15
            ang = (ang + 180) % 360 - 180
            m["angle"] = round(ang, 1)
            hint_status(f"Angle {m['angle']:g}°   (hold Shift for 15° steps)", "ok")
        d_["moved"] = True
        ghost(m, d_["shapes"], factor)
        tmp = dict(m)
        dims = mark_dims(d_["i"]) or (0, 0)
        canvas.delete("handles")
        box, up, hh = mark_box(tmp, dims[0] * factor, dims[1] * factor)
        canvas.create_polygon(*[c for q in box for c in (cx + q[0] * sc, cy - q[1] * sc)], fill="",
                              outline=C_ACCENT, dash=(5, 3), width=1.5, tags="handles")

    def mark_release(e):
        d_ = state.get("mdrag")
        state["mdrag"] = None
        if d_ and d_["moved"]:
            update_mark_fields()
            marks_list()
            changed(now=True)
            root.after(400, lambda: hint_status())

    def nudge(dx, dy, e):
        m = sel_mark()
        if not m or nb.index() is None:
            return
        stp = 5.0 if e.state & 0x0001 else 0.5
        m["x"] = round(mark_num(m, "x") + dx * stp, 2)
        m["y"] = round(mark_num(m, "y") + dy * stp, 2)
        update_mark_fields()
        changed(now=True)
        return "break"

    def hover(e):
        if mode.get() != "view" or state.get("mdrag"):
            return
        if handle_hit(e.x, e.y) is None and mark_hit(e.x, e.y) is None and probe_hit(e.x, e.y):
            canvas.config(cursor="fleur")
            return
        h_ = handle_hit(e.x, e.y)
        canvas.config(cursor="exchange" if h_ == "rotate" else "size_nw_se" if h_ == "scale" else
                      "fleur" if mark_hit(e.x, e.y) is not None else "arrow")

    def press1(e):
        if mode.get() == "view":
            mark_press(e)
        else:
            click_edit(e)

    canvas.bind("<MouseWheel>", lambda e: zoom_top(e, 1.25 if e.delta > 0 else 0.8))
    canvas.bind("<ButtonPress-1>", press1)
    canvas.bind("<B1-Motion>", lambda e: mark_motion(e) if mode.get() == "view" else None)
    canvas.bind("<ButtonRelease-1>", mark_release)
    canvas.bind("<Motion>", hover)
    canvas.bind("<ButtonPress-3>", drag_start)
    canvas.bind("<B3-Motion>", drag_move)
    # (a quick second click on a mark is a double-click to Tk: it still picks the mark up)
    canvas.bind("<Double-Button-1>", lambda e: (fit() if mark_hit(e.x, e.y) is None and handle_hit(e.x, e.y) is None
                                                else mark_press(e)) if mode.get() == "view" else press1(e))
    for key_, dxy in (("Left", (-1, 0)), ("Right", (1, 0)), ("Up", (0, 1)), ("Down", (0, -1))):
        canvas.bind(f"<{key_}>", lambda e, dxy=dxy: nudge(dxy[0], dxy[1], e))

    # ---- finished part: the stock after every move. Top: zoom (wheel) and move (drag) - it is cut again,
    # sharp, for what is in view. Tilted: turn it (drag) and zoom (wheel), true depth.
    def simv():
        return state.setdefault("simv", {"mode": "top", "region": None, "yaw": -30.0, "pitch": 55.0, "zoom": 1.0})

    def sim_settings():
        try:
            return parse(raw_settings())
        except ValueError:
            return None

    def sim_message(text):
        cs.delete("all")
        W, H = cs.winfo_width(), cs.winfo_height()
        cs.create_text(W / 2, H / 2, text=text, fill="#a3acb6", font=("Segoe UI", 12))

    def put_image(rows):
        img = tk.PhotoImage(width=len(rows[0]), height=len(rows))
        img.put(" ".join("{" + " ".join(r_) + "}" for r_ in rows))
        return img

    def draw_sim(*_, quick=False):
        W, H = cs.winfo_width(), cs.winfo_height()
        job = state["job"]
        if not job or not job.segs or W < 40:
            sim_message("Nothing to show yet")
            return
        sv = simv()
        s = sim_settings()
        if s is None:
            return
        if sv["mode"] == "top":
            key = ("top", W, H, sv["region"], id(job))
            if state.get("sim_key") != key:
                if not quick:
                    cs.create_text(W / 2, 30, text="Cutting the part in the computer...", fill=C_MUTED,
                                   font=("Segoe UI", 10), tags="busy")
                    cs.update_idletasks()
                sim = simulate(job, s, W - 20, H - 20, max_cells=900000, region=sv["region"])
                if sim is None:
                    sim_message("Nothing cuts into the stock")
                    return
                if sv["region"] is None:
                    state["sim_full"] = sim["full"]
                img = put_image(sim_rows(sim))
                if sim["zoom"] > 1:
                    img = img.zoom(sim["zoom"])
                state["sim"], state["sim_img"], state["sim_key"] = (sim, (W, H)), img, key
            cs.delete("all")
            img = state["sim_img"]
            state["sim_at"] = ((W - img.width()) / 2, (H - img.height()) / 2)
            cs.create_image(*state["sim_at"], image=img, anchor="nw", tags="img")
        else:
            if state.get("tilt_key") != id(job):   # the part, cut once at a fine grid, for every angle
                cs.create_text(W / 2, 30, text="Cutting the part in the computer...", fill=C_MUTED,
                               font=("Segoe UI", 10), tags="busy")
                cs.update_idletasks()
                sim = simulate(job, s, 6000, 6000, max_cells=450000)
                if sim is None:
                    sim_message("Nothing cuts into the stock")
                    return
                state["tilt_sim"], state["tilt_cols"], state["tilt_key"] = sim, sim_rows(sim), id(job)
            step = 3 if quick else 1
            rows = render_tilted(state["tilt_sim"], state["tilt_cols"], W, H, sv["yaw"], sv["pitch"], sv["zoom"],
                                 step=step)
            img = put_image(rows)
            cs.delete("all")
            state["sim_img"] = img
            cs.create_image(0, 0, image=img, anchor="nw", tags="img")
        hint_status(SIM_HELP if sv["mode"] == "top" else TILT_HELP)

    def sim_mm_at(px, py):
        """Drawing mm under a point of the top picture."""
        sim = state.get("sim", (None,))[0]
        if not sim:
            return None
        k = sim["zoom"]
        ax, ay = state["sim_at"]
        return sim["x0"] + (px - ax) / k * sim["cell"], sim["y0"] + (sim["ny"] - (py - ay) / k) * sim["cell"]

    def sim_region():
        sv = simv()
        return sv["region"] or state.get("sim_full") or (state.get("sim", (None,))[0] or {}).get("full")

    def sim_later(quick=False):
        """Redraw soon; a quick rough one while the mouse moves, then the sharp one when it rests."""
        for k_ in ("sim_after", "sim_full_after"):
            if state.get(k_):
                root.after_cancel(state[k_])
                state[k_] = None
        if quick:
            state["sim_after"] = root.after(30, lambda: (state.__setitem__("sim_after", None), draw_sim(quick=True)))
            state["sim_full_after"] = root.after(450, lambda: (state.__setitem__("sim_full_after", None), draw_sim()))
        else:
            state["sim_after"] = root.after(220, lambda: (state.__setitem__("sim_after", None), draw_sim()))

    def sim_wheel(e):
        sv = simv()
        f = 0.8 if e.delta > 0 else 1.25
        if sv["mode"] == "tilted":
            sv["zoom"] = max(0.3, min(20.0, sv["zoom"] / f))
            sim_later(quick=True)
            return
        reg, at = sim_region(), sim_mm_at(e.x, e.y)
        if not reg or not at:
            return
        x0, x1, y0, y1 = reg
        W, H = cs.winfo_width(), cs.winfo_height()
        # keep the picture's shape; zoom about the point under the mouse
        w_, h_ = (x1 - x0) * f, (y1 - y0) * f
        if w_ / max(h_, 1e-9) < W / max(H, 1):
            w_ = h_ * W / H
        else:
            h_ = w_ * H / W
        fx, fy = (at[0] - x0) / (x1 - x0), (at[1] - y0) / (y1 - y0)
        full = state.get("sim_full")
        if full and w_ >= (full[1] - full[0]) and h_ >= (full[3] - full[2]):
            sv["region"] = None
        else:
            sv["region"] = (at[0] - fx * w_, at[0] + (1 - fx) * w_, at[1] - fy * h_, at[1] + (1 - fy) * h_)
        # instant feedback: the picture scaled on screen, then cut again sharply
        cs.scale("img", e.x, e.y, 1 / f, 1 / f)
        sim_later()

    def sim_press(e):
        state["sim_drag"] = (e.x, e.y, dict(simv()))

    def sim_motion(e):
        d_ = state.get("sim_drag")
        if not d_:
            return
        sv = simv()
        dx, dy = e.x - d_[0], e.y - d_[1]
        if sv["mode"] == "tilted":
            sv["yaw"] = d_[2]["yaw"] + dx * 0.4
            sv["pitch"] = max(5.0, min(85.0, d_[2]["pitch"] - dy * 0.3))
            sim_later(quick=True)
        else:
            cs.delete("img_moving")
            cs.coords("img", state["sim_at"][0] + dx, state["sim_at"][1] + dy)

    def sim_release(e):
        d_ = state.pop("sim_drag", None)
        if not d_:
            return
        sv = simv()
        dx, dy = e.x - d_[0], e.y - d_[1]
        if sv["mode"] == "tilted":
            sim_later()
            return
        if abs(dx) + abs(dy) < 3:
            return
        sim = state.get("sim", (None,))[0]
        reg = sim_region()
        if not sim or not reg:
            return
        mmpp = sim["cell"] / sim["zoom"]
        x0, x1, y0, y1 = reg
        sv["region"] = (x0 - dx * mmpp, x1 - dx * mmpp, y0 + dy * mmpp, y1 + dy * mmpp)
        sim_later()

    def sim_fit(e=None):
        sv = simv()
        if sv["mode"] == "tilted":
            sv.update(yaw=-30.0, pitch=55.0, zoom=1.0)
        else:
            sv["region"] = None
        draw_sim()

    def sim_mode(val):
        simv()["mode"] = val
        for k_, b_ in sim_seg.items():
            on = k_ == val
            b_.config(bg=C_ACCENT if on else "#ffffff", fg="#ffffff" if on else C_INK)
        draw_sim()

    def sim_hover(e):
        if simv()["mode"] != "top" or not state.get("sim") or state.get("sim_drag"):
            return
        sim = state["sim"][0]
        k = sim["zoom"]
        ax, ay = state["sim_at"]
        i, j = int((e.x - ax) / k), sim["ny"] - 1 - int((e.y - ay) / k)
        if not (0 <= i < sim["nx"] and 0 <= j < sim["ny"]):
            return
        z = sim["grid"][j][i]
        x, y = sim["x0"] + (i + 0.5) * sim["cell"], sim["y0"] + (j + 0.5) * sim["cell"]
        th = sim["thickness"]
        left_ = th + z
        hint_status(f"X{x:.1f}  Y{y:.1f}:  " + ("not cut, full stock" if z > -1e-3 else
                    "cut through" if left_ <= 1e-3 else f"cut to Z{z:.2f}, {left_:.2f} mm of material left"))

    cs.bind("<Configure>", lambda e: sim_later() if views.index("current") == 2 else None)
    cs.bind("<Motion>", sim_hover)
    cs.bind("<MouseWheel>", sim_wheel)
    cs.bind("<ButtonPress-1>", sim_press)
    cs.bind("<B1-Motion>", sim_motion)
    cs.bind("<ButtonRelease-1>", sim_release)
    cs.bind("<Double-Button-1>", sim_fit)

    # ---- 3D toolpath view
    def moves():
        """Every move with its timing: (x0, y0, z0, x1, y1, z1, rapid, tool, end time min, feed, file, line)."""
        if state["moves"] is not None:
            return state["moves"]
        job, out = state["job"], []
        if job and job.segs and len(job.seg_info) == len(job.segs):
            tot = sum(i_[0] for i_ in job.seg_info)
            k = job.time_min / tot if tot > 0 and job.time_min > 0 else 1.0   # matches the estimated time
            t_ = 0.0
            for sg, (dt, f_, fi, li) in zip(job.segs, job.seg_info):
                t_ += dt * k
                out.append(sg + (t_, f_, fi, li))
        state["moves"] = out
        state["move_t"] = [m_[8] for m_ in out]
        # the program's pauses (M0) at their times, and which tool does what from when
        mt = state["move_t"]
        state["pauses3"] = [((mt[i - 1] if 0 < i <= len(mt) else 0.0) if i > 0 else 0.0, msg)
                            for i, msg, _ in (job.pauses if job else [])]
        state["stages3"] = [(i, lab) for i, lab in (job.stages if job else [])]
        state["next_pause"] = 0
        state["at_pause"] = False
        return out

    DEPTH_SCALES = {"True scale": 1.0, "2x deeper": 2.0, "5x deeper": 5.0, "10x deeper": 10.0}
    SPEEDS = {"1x real time": 1, "5x": 5, "10x": 10, "25x": 25, "50x": 50, "100x": 100, "250x": 250}

    def view3d_setup(segs, job, W, H):
        """The projection for the 3D view (kept in state["v3"] for the playback to draw into)."""
        ez = DEPTH_SCALES.get(depth_var.get(), 1.0)
        ya, pa = math.radians(state["yaw"]), math.radians(state["pitch"])
        cyw, syw, cp, sp = math.cos(ya), math.sin(ya), math.cos(pa), math.sin(pa)
        cut = [s_ for s_ in segs if not s_[6]] or segs
        xs_ = [s_[k] for s_ in cut for k in (0, 3)] + [p[0] for c in job.contours for p in c]
        ys_ = [s_[k] for s_ in cut for k in (1, 4)] + [p[1] for c in job.contours for p in c]
        bx0, bx1, by0, by1 = min(xs_), max(xs_), min(ys_), max(ys_)
        mx, my = (bx0 + bx1) / 2, (by0 + by1) / 2
        zlo = min(min(s_[2], s_[5]) for s_ in segs)

        def P(x, y, z):
            x, y = x - mx, y - my
            x1 = x * cyw - y * syw
            y1 = x * syw + y * cyw
            return x1, -(y1 * cp + z * ez * sp)
        # frame the stock (not the tall rapid moves above it), so the part fills the view
        corners = [P(x, y, z) for x in (bx0, bx1) for y in (by0, by1) for z in (zlo, 2.0)]
        span_w = max(max(a for a, _ in corners) - min(a for a, _ in corners), 1e-6)
        span_h = max(max(b for _, b in corners) - min(b for _, b in corners), 1e-6)
        sc = min((W - 220) * 0.9 / span_w, (H - 40) * 0.85 / span_h) * state["zoom3"]   # (room on the left: ruler)
        ccx = W / 2 - (max(a for a, _ in corners) + min(a for a, _ in corners)) / 2 * sc
        ccy = (H - 30) / 2 - (max(b for _, b in corners) + min(b for _, b in corners)) / 2 * sc

        def S(x, y, z):
            a, b = P(x, y, z)
            return ccx + a * sc, ccy + b * sc
        return {"S": S, "box": (bx0, bx1, by0, by1), "ez": ez, "sc": sc}

    def depth_colour(z, ztop, lo):
        f = max(0.0, min(1.0, (ztop - z) / max(ztop - lo, 1e-6)))
        f = round(f * 12) / 12   # 12 steps, so neighbouring moves join into one line
        a, b = (144, 202, 249), (13, 71, 161)
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))

    def draw3d(*_):
        c3.delete("all")
        W, H = c3.winfo_width(), c3.winfo_height()
        segs = moves()
        job = state["job"]
        if not segs or not job or W < 30:
            c3.create_text(W / 2, H / 2, text="Nothing to show yet", fill="#a3acb6", font=("Segoe UI", 12))
            return
        v3 = view3d_setup(segs, job, W, H)
        state["v3"] = v3
        S = v3["S"]
        try:
            th = float(v["thickness"].get().replace(",", "."))
        except ValueError:
            th = 0.0
        bx0, bx1, by0, by1 = v3["box"]
        # the stock as a box, its true thickness: top face, bottom edges, upright corners
        top_ = [S(bx0, by0, 0), S(bx1, by0, 0), S(bx1, by1, 0), S(bx0, by1, 0)]
        bot_ = [S(bx0, by0, -th), S(bx1, by0, -th), S(bx1, by1, -th), S(bx0, by1, -th)]
        c3.create_polygon(*[c for p in top_ for c in p], fill="#f3f5f8", outline="#c6ccd4")
        for a_, b_ in zip(top_, bot_):
            c3.create_line(*a_, *b_, fill="#c6ccd4")
        c3.create_line(*[c for p in bot_ + bot_[:1] for c in p], fill="#c6ccd4", dash=(4, 3))
        for cnt in job.contours:
            c3.create_line(*[v_ for p in cnt + [cnt[0]] for v_ in S(p[0], p[1], 0.0)], fill="#b9c0ca")
        zcut = [min(s_[2], s_[5]) for s_ in segs if not s_[6]]
        ztop = max(zcut) if zcut else 0.0
        lo = min(zcut) if zcut else min(min(s_[2], s_[5]) for s_ in segs)
        v3["ztop"], v3["lo"] = ztop, lo
        # a ruler on the front left corner in true millimetres, whatever the depth scale
        rx, ry = bx0, by0
        c3.create_line(*S(rx, ry, 0), *S(rx, ry, lo), fill=C_INK, width=1)
        ticks = sorted({0.0, round(-th, 3), round(lo, 3)})
        stepz = 1.0 if abs(lo) <= 6 else 2.0
        k_ = 0
        while -k_ * stepz > lo - 1e-9:
            ticks.append(round(-k_ * stepz, 3))
            k_ += 1
        for z_ in sorted(set(ticks)):
            X, Y = S(rx, ry, z_)
            main_ = z_ in (0.0, round(-th, 3), round(lo, 3))
            c3.create_line(X - (8 if main_ else 4), Y, X, Y, fill=C_INK)
        # the labels: stacked at least a line apart, each with a thin leader to its tick
        labs = []
        for z_, lab in ((0.0, "Z0  top of stock"), (round(-th, 3), f"Z{-th:.2f}  bottom of stock"),
                        (round(lo, 3), f"Z{lo:.2f}  cut depth")):
            if any(abs(z_ - z2) < 1e-6 for z2, _ in labs):
                continue
            labs.append((z_, lab))
        ys_l, last = [], -1e9
        for z_, lab in sorted(labs, key=lambda a: -a[0]):   # top first, going down
            X, Y = S(rx, ry, z_)
            ly = max(Y, last + 16)
            last = ly
            ys_l.append((X, Y, ly, lab))
        for X, Y, ly, lab in ys_l:
            c3.create_line(X - 8, Y, X - 30, ly, fill="#a3acb6")
            c3.create_text(X - 34, ly, text=lab, anchor="e", fill=C_INK, font=("Segoe UI", 9))
        if job.probe and job.pauses:   # the sensor's base on the stock, where every tool is measured
            px_, py_, pr_ = job.probe
            ring = [(px_ + pr_ * math.cos(a_), py_ + pr_ * math.sin(a_)) for a_ in (2 * math.pi * k / 40 for k in range(41))]
            c3.create_line(*[c for q in ring for c in S(q[0], q[1], 0.0)], fill="#7c3aed", dash=(5, 3), width=1.5)
            c3.create_text(*S(px_, py_ - pr_ - 4, 0.0), text="Z sensor", fill="#7c3aed", font=("Segoe UI Semibold", 8))
        n_now = len(segs) if state.get("play_t") is None else bisect.bisect_left(state["move_t"], state["play_t"])
        # the path: still to come faint, already cut coloured by depth
        run, run_col, run_rapid = [], None, None

        def flush():
            if len(run) >= 4:
                if run_rapid:
                    c3.create_line(*run, fill="#d5dae0", dash=(3, 3))
                else:
                    c3.create_line(*run, fill=run_col)
        stride = max(1, len(segs) // 60000)
        for i in range(0, len(segs), stride):
            s_ = segs[i]
            rp = s_[6]
            done = i < n_now
            col = None if rp else (depth_colour(min(s_[2], s_[5]), ztop, lo) if done else "#e3e8ee")
            if rp and not done:
                col = "skip"
            if rp != run_rapid or col != run_col or not run:
                flush()
                run = [*S(s_[0], s_[1], s_[2])]
                run_col, run_rapid = col, rp
            if col == "skip":
                run = []
                continue
            run += S(s_[3], s_[4], s_[5])
        flush()
        state["drawn_to"] = n_now
        scale_txt = "true scale" if v3["ez"] == 1 else f"depth drawn {v3['ez']:g}x deeper than it is"
        c3.create_text(14, H - 12, anchor="sw", fill=C_MUTED, font=("Segoe UI", 9),
                       text=f"Cuts from Z{ztop:.2f} (light blue) down to Z{lo:.2f} (dark blue), {scale_txt}.")
        if state.get("play_t") is not None:
            draw_tool()
        draw_pause_box()

    def draw_tool():
        """The bit where it is now, its true diameter, and what the machine is doing."""
        c3.delete("tool")
        segs = state["moves"]
        v3 = state.get("v3")
        if not segs or not v3 or state.get("play_t") is None:
            return
        S = v3["S"]
        t_ = state["play_t"]
        i = min(bisect.bisect_left(state["move_t"], t_), len(segs) - 1)
        s_ = segs[i]
        t0 = segs[i - 1][8] if i > 0 else 0.0
        f = 1.0 if s_[8] <= t0 else max(0.0, min(1.0, (t_ - t0) / (s_[8] - t0)))
        x, y, z = (s_[0] + (s_[3] - s_[0]) * f, s_[1] + (s_[4] - s_[1]) * f, s_[2] + (s_[5] - s_[2]) * f)
        tool = s_[7]
        job_ = state["job"]
        if state.get("at_pause") and job_ and job_.probe:   # the program waits at the sensor spot
            x, y, z = job_.probe[0], job_.probe[1], 12.0
            ps_, k_ = state.get("pauses3", []), state.get("next_pause", 0)
            nxt = [sg for sg in segs[i:i + 400] if not sg[6]]
            if nxt and k_ < len(ps_) and "T" in ps_[k_][1][:12] and "Take" not in ps_[k_][1]:
                tool = nxt[0][7]   # (the tool being put in)
        r = tool["tool_d"] / 2
        ring = [(x + r * math.cos(a), y + r * math.sin(a)) for a in (2 * math.pi * k / 20 for k in range(21))]
        hgt = max(5.0, 2.5 * tool["tool_d"]) / max(v3["ez"], 1)
        for zz in (z, z + hgt):
            c3.create_line(*[c for p in ring for c in S(p[0], p[1], zz)], fill="#e65100", width=2, tags="tool")
        for a in (0, math.pi):
            p0 = (x + r * math.cos(a + math.radians(state["yaw"])), y + r * math.sin(a + math.radians(state["yaw"])))
            c3.create_line(*S(p0[0], p0[1], z), *S(p0[0], p0[1], z + hgt), fill="#e65100", width=2, tags="tool")
        X, Y = S(x, y, z)
        c3.create_oval(X - 3, Y - 3, X + 3, Y + 3, fill="#e65100", outline="", tags="tool")
        # readout, top left: time, where, how fast, the line of G-code
        job = state["job"]
        line = ""
        try:
            line = job.files[s_[10]][1][s_[11]]
        except (IndexError, TypeError, AttributeError):
            pass
        what = "rapid move" if s_[6] else f"F{s_[9]:g} mm/min"
        stage = ""
        for k_, lab in state.get("stages3", []):
            if k_ <= i:
                stage = lab
        c3.create_rectangle(10, 10, 480, 88, fill="#ffffff", outline=C_LINE, tags="tool")
        c3.create_text(22, 18, anchor="nw", tags="tool", fill=C_ACCENT, font=("Segoe UI Semibold", 10),
                       text=stage or " ")
        c3.create_text(22, 40, anchor="nw", tags="tool", fill=C_INK, font=("Segoe UI Semibold", 10),
                       text=f"X{x:8.2f}   Y{y:8.2f}   Z{z:7.2f}      {what}")
        c3.create_text(22, 64, anchor="nw", tags="tool", fill=C_MUTED, font=("Consolas", 9),
                       text=f"{'file ' + str(s_[10] + 1) + '  ' if len(job.files) > 1 else ''}"
                            f"line {s_[11] + 1}:  {line[:52]}")
        play_label.config(text=f"{_fmt_time(t_)} / {_fmt_time(state['move_t'][-1])}")

    def play_seek(t_):
        segs = moves()
        if not segs:
            return
        t_ = max(0.0, min(t_, state["move_t"][-1]))
        back = state.get("play_t") is None or t_ < state["play_t"]
        state["play_t"] = t_
        n_now = bisect.bisect_left(state["move_t"], t_)
        if back or "v3" not in state or abs(n_now - state.get("drawn_to", 0)) > 4000:
            draw3d()
        else:   # forward: only the moves cut since the last frame are added
            S = state["v3"]["S"]
            ztop, lo = state["v3"]["ztop"], state["v3"]["lo"]
            for s_ in segs[state.get("drawn_to", 0):n_now]:
                if not s_[6]:
                    c3.create_line(*S(s_[0], s_[1], s_[2]), *S(s_[3], s_[4], s_[5]),
                                   fill=depth_colour(min(s_[2], s_[5]), ztop, lo))
            state["drawn_to"] = n_now
            draw_tool()
        state["seeking"] = True
        play_pos.set(t_ / max(state["move_t"][-1], 1e-9) * 1000)
        state["seeking"] = False

    def play_tick():
        state["play_after"] = None
        if not state.get("playing"):
            return
        segs = moves()
        if not segs:
            play_stop()
            return
        t_ = (state.get("play_t") or 0.0) + 0.04 / 60 * SPEEDS.get(speed_var.get(), 25)
        ps = state.get("pauses3", [])
        k_ = state.get("next_pause", 0)
        if k_ < len(ps) and ps[k_][0] <= t_ + 1e-12:   # the program stops here: so does the preview
            play_seek(ps[k_][0])
            at_pause(k_)
            return
        if t_ >= state["move_t"][-1]:
            play_seek(state["move_t"][-1])
            play_stop()
            return
        play_seek(t_)
        state["play_after"] = root.after(40, play_tick)

    def play_toggle():
        if state.get("playing"):
            play_stop()
            return
        if not moves():
            return
        if views.index("current") != 1:
            views.select(1)
        if state.get("play_t") is None or state["play_t"] >= state["move_t"][-1] - 1e-9:
            state["play_t"] = 0.0
            state["next_pause"] = 0
            state["at_pause"] = False
            draw3d()
        elif state.get("at_pause"):   # Cycle Start: past this pause
            state["at_pause"] = False
            state["next_pause"] = state.get("next_pause", 0) + 1
            c3.delete("pausebox")
        state["playing"] = True
        play_btn.config(text="" if ICON_FONT else "Pause")
        play_tick()

    def play_stop():
        state["playing"] = False
        if state.get("play_after"):
            root.after_cancel(state["play_after"])
            state["play_after"] = None
        play_btn.config(text="" if ICON_FONT else "Play")

    def at_pause(k_):
        """Stopped at the program's pause k_: its message, as UCCNC would show it, until Space."""
        play_stop()
        state["at_pause"] = True
        state["next_pause"] = k_
        draw_pause_box()

    def draw_pause_box():
        c3.delete("pausebox")
        if not state.get("at_pause"):
            return
        ps = state.get("pauses3", [])
        k_ = state.get("next_pause", 0)
        if k_ >= len(ps):
            return
        W = c3.winfo_width()
        x0, x1 = W / 2 - 300, W / 2 + 300
        c3.create_rectangle(x0, 14, x1, 120, fill="#fff8e1", outline="#f0b400", width=2, tags="pausebox")
        c3.create_text(x0 + 18, 26, anchor="nw", tags="pausebox", fill="#8a5a00", font=("Segoe UI Semibold", 9),
                       text=f"PAUSE {k_ + 1} OF {len(ps)}   M0 - the machine waits here for you")
        c3.create_text(x0 + 18, 48, anchor="nw", tags="pausebox", fill=C_INK, font=("Segoe UI Semibold", 12),
                       width=564, text=ps[k_][1])
        c3.create_text(x0 + 18, 98, anchor="nw", tags="pausebox", fill=C_MUTED, font=("Segoe UI", 9),
                       text="Press Space (Cycle Start) to carry on")

    def play_restart():
        play_stop()
        state["next_pause"] = 0
        state["at_pause"] = False
        state["play_t"] = None
        play_pos.set(0)
        play_label.config(text="")
        draw3d()

    def play_scrub(val):
        if state.get("seeking"):
            return
        segs = moves()
        if segs:
            t_ = float(val) / 1000 * state["move_t"][-1]
            state["at_pause"] = False
            c3.delete("pausebox")
            state["next_pause"] = next((k_ for k_, (pt, _) in enumerate(state.get("pauses3", []))
                                        if pt > t_ + 1e-9 or (t_ <= 1e-9 and pt <= 1e-9)), len(state.get("pauses3", [])))
            play_seek(t_)

    def drag3_start(e):
        state["drag3"] = (e.x, e.y, state["yaw"], state["pitch"])

    def drag3_move(e):
        x0_, y0_, yw, pt = state.get("drag3", (e.x, e.y, state["yaw"], state["pitch"]))
        state["yaw"] = yw + (e.x - x0_) * 0.4
        state["pitch"] = max(0.0, min(90.0, pt - (e.y - y0_) * 0.4))
        draw3d()

    def reset3(e=None):
        state["yaw"], state["pitch"], state["zoom3"] = -35.0, 55.0, 1.0
        draw3d()

    def zoom3(e):
        state["zoom3"] = max(0.2, min(50.0, state["zoom3"] * (1.25 if e.delta > 0 else 0.8)))
        draw3d()

    c3.bind("<ButtonPress-1>", drag3_start)
    c3.bind("<B1-Motion>", drag3_move)
    c3.bind("<Double-Button-1>", reset3)
    c3.bind("<MouseWheel>", zoom3)
    c3.bind("<Configure>", draw3d)
    def view_changed(e=None):
        i = views.index("current")
        hint_status([None, V3_HELP, SIM_HELP][i])
        if i == 1:
            draw3d()
        elif i == 2:
            draw_sim()
    views.bind("<<NotebookTabChanged>>", view_changed)

    def out_path():
        name = v["out_name"].get().strip() or default_name(v["dxf"].get())
        if not name.lower().endswith((".ngc", ".nc", ".tap", ".txt")):
            name += ".ngc"
        return os.path.join(v["out_dir"].get().strip() or DEFAULT_OUT, name)

    def save():
        compute()
        job = state["job"]
        if not job or not job.lines:
            messagebox.showerror(APP, "Nothing to save yet. Check the messages under the preview.")
            return
        paths = file_paths(job, out_path())
        there = [os.path.basename(p) for p in paths if os.path.exists(p)]
        if there and not messagebox.askyesno(APP, "Already exists:\n" + "\n".join(there) + "\n\nReplace?"):
            return
        try:
            for (label, lines), p in zip(job.files, paths):
                write_job(lines, p)
        except OSError as ex:
            messagebox.showerror(APP, f"Could not save:\n{ex}")
            return
        save_settings(raw_settings())
        store_job(state.get("cur_dxf"))
        for p in paths:
            info.insert("end", f"Saved: {p}\n", "ok")
        info.see("end")
        show_run_sheet(job, paths)

    def run_sheet_text(job, paths):
        names = [os.path.basename(p) for p in paths]
        tools_ = list(dict.fromkeys(ln.split(":")[0] for ln in job.summary if re.match(r"T\d+ ", ln)))
        mins = max(1, round(job.time_min))
        head = [f"G-code Maker run sheet - {os.path.splitext(names[0])[0]}",
                f"File{'s' if len(names) > 1 else ''}: {', '.join(names)}",
                f"Time: about {mins // 60} h {mins % 60} min" if mins >= 60 else f"Time: about {mins} min",
                "Tools: " + "; ".join(tools_), ""]
        return "\n".join(head + [f"{i}. {s_}" for i, s_ in enumerate(job.run_steps, 1)])

    def show_run_sheet(job, paths):
        """After saving: what to do at the machine, in order, ready to copy (to send to yourself)."""
        txt = run_sheet_text(job, paths)
        w_ = tk.Toplevel(root)
        w_.title("Run at the machine")
        w_.configure(bg=C_PANEL)
        w_.transient(root)
        hd = tk.Frame(w_, bg=C_PANEL)
        hd.pack(fill="x", padx=22, pady=(18, 6))
        if ICON_FONT:
            tk.Label(hd, text="", font=(ICON_FONT, 18), fg=C_OK, bg=C_PANEL).pack(side="left", padx=(0, 10))
        tk.Label(hd, text="Saved. Here is how to run it", font=("Segoe UI Semibold", 14), fg=C_INK,
                 bg=C_PANEL).pack(side="left")
        tk.Label(w_, text="Copy it and send it to yourself, or keep this window open beside UCCNC.",
                 font=("Segoe UI", 9), fg=C_MUTED, bg=C_PANEL).pack(anchor="w", padx=22)
        box = tk.Text(w_, width=78, height=min(26, txt.count("\n") + 3), wrap="word", font=("Segoe UI", 10),
                      relief="flat", bg="#f7f9fb", fg=C_INK, padx=14, pady=10, highlightthickness=1,
                      highlightbackground=C_LINE, spacing1=2, spacing3=2)
        box.pack(fill="both", expand=True, padx=22, pady=(10, 10))
        box.insert("1.0", txt)
        box.tag_add("head", "1.0", "1.end")
        box.tag_configure("head", font=("Segoe UI Semibold", 11))
        bt = tk.Frame(w_, bg=C_PANEL)
        bt.pack(fill="x", padx=22, pady=(0, 18))
        done = tk.Label(bt, text="", fg=C_OK, bg=C_PANEL, font=("Segoe UI", 9))

        def copy():
            root.clipboard_clear()
            root.clipboard_append(box.get("1.0", "end-1c"))
            done.config(text="Copied - paste it into WhatsApp")
        cb = tk.Label(bt, text="Copy instructions", bg=C_ACCENT, fg="#ffffff", font=("Segoe UI Semibold", 10),
                      padx=16, pady=6, cursor="hand2")
        cb.pack(side="left")
        cb.bind("<Button-1>", lambda e: copy())
        done.pack(side="left", padx=12)
        cl = tk.Label(bt, text="Close", bg="#eceef1", fg=C_INK, font=("Segoe UI", 10), padx=16, pady=6,
                      cursor="hand2", highlightthickness=1, highlightbackground="#d3d7dd")
        cl.pack(side="right")
        cl.bind("<Button-1>", lambda e: w_.destroy())
        w_.bind("<Escape>", lambda e: w_.destroy())
        root._run_sheet = (w_, box)   # (for the test scripts)

    def open_folder():
        d = v["out_dir"].get().strip() or DEFAULT_OUT
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def defaults():
        if messagebox.askyesno(APP, "Put every setting back to its default?\n\n(The DXF, folder and file name stay.)"):
            for k in DEFAULTS:
                if k not in ("dxf", "out_dir", "out_name") + DICT_KEYS:
                    v[k].set(DEFAULTS[k])
            changed(now=True)

    bar_button("Undo", undo, icon="\uE7A7")
    bar_button("Redo", redo, icon="\uE7A6")
    root.bind("<Control-z>", undo)
    root.bind("<Control-y>", redo)
    root.bind("<Control-Z>", redo)
    bar_button("Open DXF", pick_dxf, icon="")
    bar_button("Folder", open_folder, icon="")
    bar_button("Defaults", defaults, icon="")
    bar_button("Save G-code", save, primary=True, icon="")
    root.bind("<Control-s>", lambda e: save())
    root.bind("<Control-o>", lambda e: pick_dxf())

    canvas.bind("<Configure>", lambda e: draw(state["job"]))
    root.protocol("WM_DELETE_WINDOW", lambda: (store_job(state.get("cur_dxf")), save_settings(raw_settings()),
                                                root.destroy()))
    edit_tool(v["tool_name"].get() if find_tool(v["tool_name"].get()) else (tools[0]["name"] if tools else ""))
    if state["cur_dxf"] and os.path.isfile(state["cur_dxf"]):
        if manual_key(state["cur_dxf"]) not in state["jobs"]:
            store_job(state["cur_dxf"])
        if not state["recent"]:
            state["recent"].append(state["cur_dxf"])
    refresh_recent()
    root.after(100, compute)
    root.mainloop()


def run_cli(args):
    s = load_settings()
    stored = load_jobs().get(manual_key(os.path.abspath(args[0])))
    if stored:   # the drawing's own job, as the window would open it
        s.update({k: v for k, v in stored.items() if k in JOB_KEYS})
    if not s.get("tools_v2"):
        migrate_tools(s, load_tools())
    s["dxf"], out = args[0], args[1]
    for a in args[2:]:
        k, _, val = a.partition("=")
        s[k] = val
    p = parse(s)
    job = build(p)
    print("\n".join(job.summary))
    sug = suggest(p)
    print(f"Suggested: {sug['rpm']} rpm, feed {sug['feed']:g}, ramp {sug['ramp_feed']:g}, plunge {sug['plunge_feed']:g}, "
          f"{sug['step']:.2f} mm/pass")
    for text, _ in check_speeds(p, sug):
        print("  " + text)
    for n in sug["notes"]:
        print("  " + n)
    for w in job.warnings:
        print("! " + w)
    if job.lines:
        for (label, lines), path in zip(job.files, file_paths(job, out)):
            write_job(lines, path)
            print("Saved:", path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        run_cli(sys.argv[2:])
    else:
        run_gui(sys.argv[1] if len(sys.argv) > 1 else None)
