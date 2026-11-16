"""
Agilite CNC: the local server behind the window. The window is a web page (ui/) shown by WebView2 through
pywebview, like APF+. The page talks to this server with small JSON calls; big arrays (height grids, toolpaths)
come as raw float32 binary.

    pythonw "Agilite CNC.pyw" [part]      the app in its own window
    py -3.12 server.py --serve 8231        the page served for a browser (development)
"""
import json
import math
import os
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gcm_engine as gcm   # noqa: E402
import presets as P        # noqa: E402
import plan2d              # noqa: E402
import brand               # noqa: E402

APP = "Agilite CNC"
APP_ID = "Agilite.CNC"
VERSION = "0.2.0"
UI = os.path.join(HERE, "ui")
UPLOADS = os.path.join(tempfile.gettempdir(), "agilite-cnc-uploads")
LOG = os.path.join(HERE, "Agilite CNC log.txt")
GCODE_MAKER = os.path.join(os.path.expanduser("~"), "Desktop", "G-code", "G-code Maker.pyw")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json"}


def log(text):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def engine3d():
    import engine3d as e
    return e


def _tool_public(t):
    keys = ("name", "tool_d", "tool_type", "flutes", "tool_angle", "rpm", "feed", "ramp_feed", "plunge_feed", "step",
            "flute_len", "reach", "shank_d", "owned", "tool_mat", "sug", "own", "mat_family")
    return {k: t[k] for k in keys if k in t}


def usb_drives():
    """Removable drives (USB sticks) on Windows: [{"drive": "E:\\", "label": "KINGSTON", "free": bytes}]."""
    out = []
    if os.name != "nt":
        return out
    import ctypes
    import shutil
    import string
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = letter + ":\\"
        if k32.GetDriveTypeW(root) != 2:   # DRIVE_REMOVABLE
            continue
        name = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0):
            continue   # a card reader with no card in it
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        out.append({"drive": root, "label": name.value or "USB drive", "free": free})
    return out


def _label(t):
    try:
        return gcm._tool_label(t)
    except Exception:
        return t.get("name", "bit")


NEEDS_BED = ("This job changes end mills, and end mill changes are measured on the bed sensor, whose position isn't entered "
             "yet (Advanced → Tabs & holding → Bed sensor X/Y). Enter it, or choose Fewest end mill changes under End mills.")


def sensor_spot(p, opts, machine=None):
    """Where the tool sensor sits on a 3D job: always on the stock's top, which is Z0 for every bit.
    A dragged spot wins; on a block it is the flat, uncut top nearest the lower-left corner; on any other
    part the stock gets a margin wide enough to hold the sensor, and the sensor sits in that margin.
    Returns (machine settings, stock margin)."""
    machine = dict(machine or P.MACHINE)
    r = machine["probe_d"] / 2
    an = p.get("an") or {}
    sen = (opts or {}).get("sensor") or {}
    block = bool(an.get("is_block"))
    # the designer's "Margin round the part" (Surfacing), 5 mm unless set
    surf = next((a for a in (opts or {}).get("addons") or [] if a.get("kind") == "surface" and not a.get("hidden")), {})
    try:
        user_m = max(0.0, float(surf.get("margin", 5.0)))
    except (TypeError, ValueError):
        user_m = 5.0
    # with the bed sensor no puck ever sits on the stock (Lev zeroes on it by hand before the start), so the stock
    # only needs the designer's margin; the puck-on-stock mode needs room for the puck beside the part
    puck_on_stock = machine.get("sensor_mode") != "bed"
    margin = user_m if block or not puck_on_stock else max(user_m, r * 2 + 6.0)
    if "dx" in sen and "dy" in sen:
        machine["probe_dx"], machine["probe_dy"] = float(sen["dx"]), float(sen["dy"])
        return machine, margin
    if not block:
        machine["probe_dx"] = machine["probe_dy"] = r + 3.0
        return machine, margin
    z = np.asarray(an["grid"], float)
    cell = float(an["cell"])
    x0, y0 = an["origin"]
    top = float(an.get("top", np.nanmax(z)))
    # coarse 1 mm map of everything that is not the flat top, grown by the sensor radius + a bit's width
    k = max(1, int(round(1.0 / cell)))
    ny, nx = (z.shape[0] // k) * k, (z.shape[1] // k) * k
    low = ~(np.isfinite(z[:ny, :nx]) & (z[:ny, :nx] >= top - 0.02))
    low = low.reshape(ny // k, k, nx // k, k).any(axis=(1, 3))
    R = int(math.ceil(r + 6.0))
    ii = np.pad(low.astype(np.int32), R).cumsum(0).cumsum(1)
    ii = np.pad(ii, ((1, 0), (1, 0)))
    H, W = low.shape
    s = (ii[2 * R + 1:2 * R + 1 + H, 2 * R + 1:2 * R + 1 + W] - ii[:H, 2 * R + 1:2 * R + 1 + W]
         - ii[2 * R + 1:2 * R + 1 + H, :W] + ii[:H, :W])
    ok = s == 0
    ok[:R, :] = ok[-R:, :] = False
    ok[:, :R] = ok[:, -R:] = False
    if ok.any():
        jj, ii_ = np.nonzero(ok)
        best = np.argmin(jj ** 2 + ii_ ** 2)
        machine["probe_dx"] = round(float(x0 + (ii_[best] + 0.5) * k * cell), 1)
        machine["probe_dy"] = round(float(y0 + (jj[best] + 0.5) * k * cell), 1)
    return machine, margin


class Session:
    """One open part and its plan. Planning runs on a worker thread; the page polls."""

    def __init__(self, user=None):
        self.user = user          # the designer's profile; None = the old single settings file (tests)
        self.lock = threading.Lock()
        self.part = None          # {"path", "name", "kind", "obj", "an"}: the first part on the sheet
        self.extra = []           # the other parts on the sheet (same dicts)
        self.project = None       # {"path", "name", "opts"?, "newer"?} once saved or opened as a project
        self.job = None
        self.meta = None          # {"used", "stock", "settings"}
        self.sim = None
        self.gen = 0              # bumps with every new plan, so the page knows when to fetch
        self.work = {"busy": False, "progress": 0.0, "text": "", "error": None, "token": 0}
        self.prefs = P.load_user(user) if user else P.load_prefs()
        self._rack = None if user else P.load_rack()
        self.window = None

    def _save_prefs(self):
        if self.user:
            P.save_user(self.user, self.prefs)
        else:
            P.save_prefs(self.prefs)

    @property
    def rack(self):
        """The rack every plan uses: the shared one with this designer's own speeds over it."""
        if not self.user:
            return self._rack
        return P.rack_with_speeds(shared_rack(), self.prefs.get("speeds"))

    @rack.setter
    def rack(self, value):
        self._rack = value

    # ---------------------------------------------------------------- background work
    def _start(self, fn, text):
        with self.lock:
            self.work["token"] += 1
            token = self.work["token"]
            self.work.update(busy=True, progress=0.0, text=text, error=None)

        def progress(frac, txt):
            if self.work["token"] == token:
                self.work.update(progress=float(frac), text=txt)

        def go():
            try:
                fn(progress, lambda: self.work["token"] != token)
            except Exception as ex:
                log(traceback.format_exc())
                if self.work["token"] == token:
                    self.work["error"] = f"{type(ex).__name__}: {ex}"
            finally:
                if self.work["token"] == token:
                    self.work["busy"] = False
        threading.Thread(target=go, daemon=True).start()
        return token

    # ---------------------------------------------------------------- api
    def api_state(self, req):
        mats = {k: {kk: vv for kk, vv in v.items()} for k, v in P.MATERIALS.items()}
        return {
            "app": APP, "version": VERSION,
            "machine": P.MACHINE, "materials": mats, "finishes": P.FINISHES,
            "rack": self.rack, "fonts": [gcm.SINGLE_LINE] + list(gcm.list_fonts()), "logos": list(gcm.LOGOS),
            "prefs": self.prefs, "part": self._part_public(), "folder": P.machine_folder(self.prefs),
            "has_gcm": os.path.isfile(GCODE_MAKER),
            "user": self.user, "users": P.list_users(), "in_window": self.window is not None,
        }

    def api_users(self, req):
        return {"users": P.list_users(), "user": self.user}

    def api_prefs(self, req):
        self.prefs.update(req or {})
        self._save_prefs()
        return {"ok": True}

    @staticmethod
    def _one_public(p):
        out = {"name": p["name"], "kind": p["kind"], "path": p["path"]}
        if p.get("size"):
            out["size"] = p["size"]
        if p.get("obj") is not None:
            out["flat"] = bool(p.get("flat"))
            out["edge_break"] = float(p.get("edge_break") or 0.0)
        elif p["kind"] == "3d":
            out["loading"] = True
        return out

    def _part_public(self):
        p = self.part
        if not p:
            return None
        out = self._one_public(p)
        # the other parts on the sheet (the first part is this one)
        out["others"] = [self._one_public(x) for x in self.extra]
        return out

    def _load(self, path, up="auto"):
        """A part dict for a file; a 3D model is read in the background (its dict fills in when done)."""
        kind = "2d" if path.lower().endswith(".dxf") else "3d"
        name = os.path.splitext(os.path.basename(path))[0]
        d = {"path": path, "name": name, "kind": kind, "obj": None, "an": None, "up": up}
        if kind == "3d":
            def fn(progress, cancelled):
                e = engine3d()
                progress(0.1, f"Reading {name}")
                part = e.place(e.load_part(path), up=up)
                progress(0.6, "Measuring the part")
                an = e.analyze(part)
                b = np.asarray(part.tris).reshape(-1, 3)
                size = [float(v) for v in (b.max(0) - b.min(0))]
                brk = float(e.edge_break(part))   # the model's own chamfers/fillets on the top edges, mm
                flat = brk > 0 or bool(e.is_flat(part))
                d.update(obj=part, an=an, size=size, flat=flat, edge_break=brk)
                self.gen += 1
            # not through _start: adding a part must not cancel the reading of another one
            threading.Thread(target=lambda: self._guard(fn), daemon=True).start()
        else:
            try:   # a DXF's size from its drawing, for the parts list
                shapes, _ = gcm.read_dxf(path)
                xs = [q[0] for pts, _, _ in shapes for q in pts] or [0.0]
                ys = [q[1] for pts, _, _ in shapes for q in pts] or [0.0]
                d["size"] = [max(xs) - min(xs), max(ys) - min(ys), 0.0]
            except Exception:
                pass
        return d

    def _guard(self, fn):
        try:
            fn(lambda *a: None, lambda: False)
        except Exception as ex:
            log(traceback.format_exc())
            self.work["error"] = f"{type(ex).__name__}: {ex}"
            self.gen += 1

    def open_path(self, path, up="auto", add=False):
        if path.lower().endswith(".acnc"):
            return self.open_project(path)
        d = self._load(path, up)
        with self.lock:
            if add and self.part:
                self.extra.append(d)
            else:
                self.part, self.extra, self.project = d, [], None
            self.job = self.meta = self.sim = None   # the sheet changed: the page plans again
            self.gen += 1
        return {"part": self._part_public()}

    def api_remove(self, req):
        """Takes a part off the sheet (1 = the first added one; the first part itself is replaced by Open)."""
        i = int(req.get("i", 0)) - 1
        with self.lock:
            if 0 <= i < len(self.extra):
                self.extra.pop(i)
                self.job = self.meta = self.sim = None
                self.gen += 1
        return {"part": self._part_public()}

    def api_upload(self, req, body, query):
        name = os.path.basename(unquote(query.get("name", ["part.dxf"])[0]))
        os.makedirs(UPLOADS, exist_ok=True)
        path = os.path.join(UPLOADS, name)
        with open(path, "wb") as f:
            f.write(body)
        return self.open_path(path, add=query.get("add", ["0"])[0] == "1")

    def api_open(self, req):
        return self.open_path(req["path"], req.get("up", "auto"), add=bool(req.get("add")))

    def api_pick(self, req):
        """The Windows open dialog (only in the app window; the page uses a file input in a browser)."""
        if not self.window:
            return {"path": None}
        import webview
        r = self.window.create_file_dialog(webview.OPEN_DIALOG, directory=self.prefs.get("last_dir", ""),
                                           file_types=("Parts and projects (*.dxf;*.step;*.stp;*.stl;*.acnc)",
                                                       "All files (*.*)"))
        if not r:
            return {"path": None}
        path = r[0] if isinstance(r, (list, tuple)) else r
        self.prefs["last_dir"] = os.path.dirname(path)
        self._save_prefs()
        return self.open_path(path, add=bool(req.get("add")))

    # ---------------------------------------------------------------- projects
    # A project (.acnc) is one JSON file: every part on the sheet with its file inside it (so it opens on any PC,
    # even when the drawings move), how it faces up, and every option of the job (the page's opts).
    def _project_data(self, opts):
        import base64
        items = []
        for p in [self.part] + self.extra:
            with open(p["path"], "rb") as f:
                raw = f.read()
            items.append({"name": p["name"], "file": os.path.basename(p["path"]), "path": p["path"],
                          "up": p.get("up", "auto"), "data": base64.b64encode(raw).decode("ascii"),
                          "mtime": os.path.getmtime(p["path"])})
        return {"app": APP, "kind": "agilite-cnc project", "version": 1, "saved": time.strftime("%Y-%m-%d %H:%M"),
                "parts": items, "opts": opts or {}}

    def api_save_project(self, req):
        """Saves the job. In the app window: to the project's own file, or through a Save dialog (as=True or
        a new project). In a browser: the file's text comes back for the page to download."""
        if not self.part:
            return {"ok": False, "error": "Open a part first."}
        data = json.dumps(self._project_data(req.get("opts")), indent=0)
        name = (self.project or {}).get("name") or self.part["name"]
        if not self.window:
            return {"ok": True, "download": data, "name": name + ".acnc"}
        path = (self.project or {}).get("path")
        if req.get("as") or not path:
            import webview
            folder = os.path.dirname(path) if path else self.prefs.get("project_dir") or self.prefs.get("last_dir", "")
            r = self.window.create_file_dialog(webview.SAVE_DIALOG, directory=folder, save_filename=name + ".acnc",
                                               file_types=("AGILITE CNC project (*.acnc)",))
            if not r:
                return {"ok": False, "cancelled": True}
            path = r[0] if isinstance(r, (list, tuple)) else r
            if not path.lower().endswith(".acnc"):
                path += ".acnc"
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
        self.project = {"path": path, "name": os.path.splitext(os.path.basename(path))[0]}
        self.prefs["project_dir"] = os.path.dirname(path)
        self._save_prefs()
        self.gen += 1
        return {"ok": True, "path": path}

    def open_project(self, path):
        import base64
        with open(path, encoding="utf-8") as f:
            pj = json.load(f)
        if pj.get("kind") != "agilite-cnc project" or not pj.get("parts"):
            raise ValueError("This is not an AGILITE CNC project.")
        pname = os.path.splitext(os.path.basename(path))[0]
        folder = os.path.join(UPLOADS, "projects", "".join(ch for ch in pname if ch not in '\\/:*?"<>|'))
        os.makedirs(folder, exist_ok=True)
        parts, newer = [], []
        for it in pj["parts"]:
            fp = os.path.join(folder, os.path.basename(it["file"]))
            with open(fp, "wb") as f:
                f.write(base64.b64decode(it["data"]))
            parts.append(self._load(fp, it.get("up", "auto")))
            src = it.get("path")
            try:
                if src and os.path.isfile(src) and os.path.getmtime(src) > float(it.get("mtime", 0)) + 1:
                    newer.append(it["name"])
            except (OSError, TypeError, ValueError):
                pass
        with self.lock:
            self.part, self.extra = parts[0], parts[1:]
            self.project = {"path": path, "name": pname, "opts": pj.get("opts") or {}, "newer": newer,
                            "opened": time.time()}   # the page applies the options once per opening
            self.job = self.meta = self.sim = None
            self.gen += 1
        return {"part": self._part_public()}

    def api_up(self, req):
        p = self.part
        if not p or p["kind"] != "3d":
            return {"ok": False}
        with self.lock:   # the first part again, facing another way; the rest of the sheet stays
            self.part = self._load(p["path"], req.get("up", "auto"))
            self.job = self.meta = self.sim = None
            self.gen += 1
        return {"part": self._part_public()}

    def _flat_source(self, p):
        """(DXF, layer ops, thickness or None) the 2D engine cuts a part from: a DXF itself, a plate's outlines."""
        if p["kind"] == "2d":
            return p["path"], {}, None
        if not p.get("sheet"):
            os.makedirs(UPLOADS, exist_ok=True)
            dxf = os.path.join(UPLOADS, f"{p['name']} - outlines ({p.get('up', 'auto')}).dxf")
            lops, T = engine3d().sheet_dxf(p["obj"], dxf)
            p["sheet"] = (dxf, lops, T)
        return p["sheet"]

    def api_plan(self, req):
        p = self.part
        if not p:
            return {"ok": False, "error": "Open a part first."}
        opts = req or {}
        material = opts.get("material") or "Aluminium sheet"
        finish = opts.get("finish") or "Standard"
        rack = self.rack
        items = [p] + list(self.extra)
        sh = opts.get("sheet") or {}
        copies = [max(0, int(c)) for c in (sh.get("copies") or [])][:len(items)]
        copies += [1] * (len(items) - len(copies))
        if len(items) > 1 or copies[0] > 1:
            return self._plan_sheet(items, copies, float(sh.get("gap") or 8.0), opts, material, finish, rack)

        if p["kind"] == "2d":
            thickness = float(opts.get("thickness") or P.MATERIALS[material]["thickness"])

            def fn(progress, cancelled):
                job, s, used = plan2d.plan(p["path"], material, thickness, finish, rack,
                                           machine=P.machine_with(self.prefs), progress=progress, options=opts)
                if cancelled():
                    return
                stock = plan2d.stock_of(job, s)
                job.stock = stock
                self._finish_plan(job, used, stock, s, progress, cancelled)
        else:
            def fn(progress, cancelled):
                for _ in range(600):           # the model may still be loading
                    if p.get("obj") is not None or cancelled():
                        break
                    time.sleep(0.05)
                if p.get("obj") is None:
                    return
                e = engine3d()
                if p.get("flat"):
                    # a plate (from sheet or from a block): the very same tools as a DXF (thickness, cut past the
                    # bottom, tabs, chamfers, text, logos, surfacing), from the part's own outlines. One end mill
                    # cuts it out; the model's broken top edges are the Chamfer operation's job (a V-bit), never
                    # 3D finishing with ball noses
                    if not p.get("sheet"):
                        os.makedirs(UPLOADS, exist_ok=True)
                        dxf = os.path.join(UPLOADS, f"{p['name']} - outlines ({p.get('up', 'auto')}).dxf")
                        lops, T = e.sheet_dxf(p["obj"], dxf)
                        p["sheet"] = (dxf, lops, T)
                    dxf, lops, T = p["sheet"]
                    thickness = float(opts.get("thickness") or T)
                    # the finished thickness: the model's, or less when the designer skims the top (Surfacing)
                    pt = float(opts["part_t"]) if opts.get("part_t") is not None else T
                    pt = min(pt, T) if thickness > T + 0.004 else pt
                    job, s, used = plan2d.plan(dxf, material, thickness, finish, rack,
                                               machine=P.machine_with(self.prefs), progress=progress,
                                               options=dict(opts, part_t=pt), layer_ops=lops)
                    if cancelled():
                        return
                    if thickness < T - 0.01:
                        job.warnings.insert(0, f"The sheet ({thickness:g} mm) is thinner than the part ({T:g} mm).")
                    elif pt < T - 0.004:
                        job.warnings.insert(0, f"Surfacing takes the part {T - pt:.2f} mm thinner than the model "
                                               f"({pt:g} instead of {T:g} mm).")
                    job.route2d, job.part_t = True, T
                    stock = plan2d.stock_of(job, s)
                    job.stock = stock
                    self._finish_plan(job, used, stock, s, progress, cancelled)
                    return
                machine, margin = sensor_spot(p, opts, P.machine_with(self.prefs))
                job = e.build3d(p["obj"], material, finish, rack=rack, machine=machine, progress=progress,
                                margin=margin, options=opts)
                if cancelled():
                    return
                self._finish_plan(job, getattr(job, "tools_used", []), getattr(job, "stock", None), None,
                                  progress, cancelled)
        self._start(fn, "Making the toolpaths")
        return {"ok": True}

    def _plan_sheet(self, items, copies, gap, opts, material, finish, rack):
        """Several parts (or copies) on one sheet: their outlines laid out and cut as one drawing by the 2D engine."""
        import sheet
        if not sum(copies):
            return {"ok": False, "error": "Every part is set to 0 copies: there is nothing on the sheet."}

        def fn(progress, cancelled):
            for _ in range(1200):          # models may still be loading
                if all(x["kind"] == "2d" or x.get("obj") is not None for x in items) or cancelled():
                    break
                time.sleep(0.05)
            if cancelled():
                return
            bad = [x["name"] for x, n in zip(items, copies) if n and x["kind"] == "3d" and not x.get("flat")]
            if bad:
                raise ValueError(f"Only flat parts can share a sheet; {', '.join(bad)} is a 3D part. Take it off "
                                 f"the sheet (✕) or open it on its own.")
            progress(0.02, "Laying the parts out on the sheet")
            srcs = [self._flat_source(x) if n else (None, {}, None) for x, n in zip(items, copies)]
            Ts = [T for (_, _, T), n in zip(srcs, copies) if n and T]
            T_model = max(Ts) if Ts else None
            thickness = float(opts.get("thickness") or T_model or P.MATERIALS[material]["thickness"])
            dxf, lops, placed, size = sheet.compose(
                [{"dxf": d, "layer_ops": lo, "copies": n if d else 0, "name": x["name"]}
                 for x, (d, lo, _), n in zip(items, srcs, copies)], gap, os.path.join(UPLOADS, "sheets"))
            o = dict(opts)
            if T_model is not None:   # plates from a thicker stock: surfaced down to the model, as for one part
                pt = float(opts["part_t"]) if opts.get("part_t") is not None else T_model
                o["part_t"] = min(pt, T_model) if thickness > T_model + 0.004 else pt
            job, s, used = plan2d.plan(dxf, material, thickness, finish, rack, machine=P.machine_with(self.prefs),
                                       progress=progress, options=o, layer_ops=lops)
            if cancelled():
                return
            if len(set(round(T, 2) for T in Ts)) > 1:
                job.warnings.insert(0, "The parts are not all the same thickness (" + ", ".join(f"{T:g}" for T in sorted(set(Ts)))
                                    + f" mm); all are cut from the {thickness:g} mm stock.")
            n = sum(copies)
            job.summary.insert(0, f"{n} part{'s' if n != 1 else ''} on one sheet, {size[0]:.0f} x {size[1]:.0f} mm, "
                                  f"{gap:g} mm apart.")
            job.route2d, job.part_t = True, T_model
            job.placed = placed
            stock = plan2d.stock_of(job, s)
            job.stock = stock
            self._finish_plan(job, used, stock, s, progress, cancelled)
        self._start(fn, "Making the toolpaths")
        return {"ok": True}

    def _finish_plan(self, job, used, stock, s, progress, cancelled):
        with self.lock:
            self.job, self.meta, self.sim = job, {"used": used, "stock": stock, "settings": s}, None
            self.gen += 1
        progress(0.9, "Simulating the cut")
        try:
            sim = engine3d().simulate3d(job)
        except Exception:
            log(traceback.format_exc())
            sim = None
        if not cancelled():
            self.sim = sim
            self.gen += 1

    def api_poll(self, req):
        w = dict(self.work)
        out = {"busy": w["busy"], "progress": w["progress"], "text": w["text"], "error": w["error"], "gen": self.gen,
               "part": self._part_public(), "has_sim": self.sim is not None, "project": self.project}
        if self.job is not None and req.get("gen") != self.gen:
            out["plan"] = self._plan_public()
        return out

    def _plan_public(self):
        job, meta = self.job, self.meta or {}
        used = meta.get("used") or []
        stages = []
        segs = job.segs
        info = job.seg_info
        cum = np.cumsum([x[0] for x in info]) if info else np.zeros(0)
        bounds = list(job.stages) + [(len(segs), "")]
        for (i0, label), (i1, _) in zip(bounds[:-1], bounds[1:]):
            t0 = float(cum[i0 - 1]) if i0 > 0 and len(cum) else 0.0
            t1 = float(cum[i1 - 1]) if i1 > 0 and len(cum) else 0.0
            stages.append({"from": i0, "to": i1, "label": label, "t0": t0, "t1": t1})
        tools = []
        for item in used:
            t, why = item[0], item[1]
            what = item[2] if len(item) > 2 else []
            tools.append({"label": _label(t), "why": why, "what": what, "tool": _tool_public(t)})
        marks = [dict(m) for m in (job.marks or [])] if hasattr(job, "marks") else []
        s = meta.get("settings") or {}
        cut = ({"extra": s.get("extra"), "tab_len": s.get("tab_len"), "tab_thick": s.get("tab_thick"),
                "tabs": s.get("tabs")} if s else None)
        return {
            "shapes": self._shapes_public(job, s), "cut": cut,
            "time_min": job.time_min, "warnings": list(job.warnings), "summary": list(job.summary),
            "run_steps": list(job.run_steps), "tools": tools, "stages": stages,
            "stock": meta.get("stock"), "probe": list(job.probe) if job.probe else None,
            "pauses": [[int(a), m] for a, m, *_ in job.pauses], "sensor": self._sensor_public(job),
            "flat": bool(getattr(job, "route2d", False)), "part_t": getattr(job, "part_t", None),
            "thickness": (s or {}).get("thickness"), "nsegs": len(segs), "marks": marks,
            "ops": self._ops_public(job, cum), "strategies": getattr(job, "strategies", None),
            "strategy": getattr(job, "strategy", None),
            "params": plan2d.params_used(s) if s else getattr(job, "params_used", None),
            "chip": getattr(job, "chip", None),
            "placed": getattr(job, "placed", None),
            "outlines": [[list(p) for p in c] for c in (job.contours or [])][:400],
            "mark_paths": [[[list(p) for p in pts], bool(closed)] for pts, closed in (getattr(job, "mark_paths", None) or [])][:3000],
        }

    @staticmethod
    def _ops_public(job, cum):
        """The operations in run order, each with its bit: [{key, label, bit, bit_label, choices, minutes, ...}].
        Sheet parts (plan2d) carry them with the bits they could use; a 3D block's come from engine3d, read-only."""
        cards = getattr(job, "op_cards", None)
        if cards is not None:
            return cards
        out = []
        for op in getattr(job, "ops", None) or []:
            t = op.get("tool") or {}
            a, b = op.get("seg_range") or (0, 0)
            t0 = float(cum[a - 1]) if a > 0 and len(cum) else 0.0
            t1 = float(cum[b - 1]) if b > 0 and len(cum) else 0.0
            out.append({"key": op.get("kind", ""), "label": str(op.get("name", "")).capitalize(),
                        "bit": t.get("name", ""), "bit_label": _label(t) if t else "", "choices": [],
                        "owned": t.get("owned", True) is not False, "minutes": round(t1 - t0, 3), "readonly": True})
        return out

    def _sensor_public(self, job):
        """Which tool sensor the program uses: the bed sensor for bit changes, or the puck for every bit."""
        m = P.machine_with(self.prefs)
        bed = P.bed_sensor(m)
        changes = sum(1 for _, msg, *_ in job.pauses if msg.startswith("UCCNC asks for the new end mill"))
        # bed mode with no position yet: a job with a bit change could only measure it with the puck on the stock,
        # which Lev does not want; such a job is not sent until the position is entered
        waiting = m["sensor_mode"] == "bed" and not bed and job.probe is not None
        return {"mode": m["sensor_mode"], "bed": list(bed) if bed else None, "bed_changes": changes,
                "macro": m["bed_macro"], "needs_bed": waiting}

    @staticmethod
    def _shapes_public(job, s):
        """Every closed shape the job cuts, in job coordinates: its drawn points, its edges (point index ranges)
        and its tabs, taken from the very loop and centres the G-code lifts over, projected onto the drawn line."""
        out = []
        # edges from the very DXF contour the chamfer options are read against, so the numbering always agrees
        drawn = plan2d.drawing(s)[0] if s.get("dxf") else {}
        for key, kind, c in getattr(job, "cut_shapes", None) or []:
            loops = [lp for lp in job.loops if lp["key"] == key]
            tabs = []
            for lp in loops:
                tabs += [list(gcm.project(c, p)[1]) for p in lp["tabs"]]
            out.append({"key": key, "kind": kind, "points": [[float(x), float(y)] for x, y in c],
                        "edges": plan2d.contour_edges(drawn.get(key, c)), "tabs": tabs,
                        "can_tab": any(lp["can_tab"] for lp in loops), "tab_len": float(s.get("tab_len", 0.0) or 0.0),
                        "manual": any(lp["manual"] for lp in loops)})
        for key, kind, c in getattr(job, "rim_shapes", None) or []:   # pocket rims: chamfer picks only
            out.append({"key": key, "kind": "rim", "points": [[float(x), float(y)] for x, y in c],
                        "edges": plan2d.contour_edges(drawn.get(key, c)), "tabs": [], "can_tab": False,
                        "tab_len": 0.0, "manual": False})
        return out

    def api_send(self, req):
        job = self.job
        if not job or not job.files:
            return {"ok": False, "error": "There is nothing to send yet."}
        bad = [w for w in job.warnings if "do not run it" in w]
        if bad:
            return {"ok": False, "error": bad[0]}
        if self._sensor_public(job)["needs_bed"]:
            return {"ok": False, "error": NEEDS_BED}
        folder = P.machine_folder(self.prefs)
        nm = (self.project or {}).get("name") or (self.part["name"] + (" and more" if self.extra else ""))
        base = f"{nm} - {req.get('material', '')}".strip(" -")
        base = "".join(ch for ch in base if ch not in '\\/:*?"<>|')
        target = os.path.join(folder, base + ".ngc")
        downloads = None
        if not self.window:
            # in a browser: the program comes down as a download (the browser saves it where it saves files);
            # a copy stays in this designer's own folder for Copy to USB
            folder = _out_dir(self.user)
            for f in os.listdir(folder):
                try:
                    os.remove(os.path.join(folder, f))
                except OSError:
                    pass
            target = os.path.join(folder, base + ".ngc")
            paths = gcm.file_paths(job, target)
            for (_, lines), path in zip(job.files, paths):
                gcm.write_job(lines, path)
            from urllib.parse import quote
            downloads = [{"name": os.path.basename(p),
                          "url": f"/dl/{quote(self.user or '-')}/{quote(os.path.basename(p))}"} for p in paths]
            return {"ok": True, "paths": paths, "folder": folder, "steps": list(job.run_steps), "usb": usb_drives(),
                    "downloads": downloads}
        if self.window:
            # Lev 2026-11-15: choose where it goes; the dialog opens in the folder used last time
            import webview
            r = self.window.create_file_dialog(webview.SAVE_DIALOG, directory=folder, save_filename=base + ".ngc",
                                               # (pywebview takes only letters, digits and spaces before the brackets)
                                               file_types=("UCCNC program (*.ngc)", "All files (*.*)"))
            if not r:
                return {"ok": False, "cancelled": True}
            target = r[0] if isinstance(r, (list, tuple)) else r
            if not os.path.splitext(target)[1]:
                target += ".ngc"
            folder = os.path.dirname(target)
            self.prefs["machine_folder"] = folder
            self._save_prefs()
        os.makedirs(folder, exist_ok=True)
        paths = gcm.file_paths(job, target)
        for (_, lines), path in zip(job.files, paths):
            gcm.write_job(lines, path)
        return {"ok": True, "paths": paths, "folder": folder, "steps": list(job.run_steps), "usb": usb_drives()}

    def api_usb(self, req):
        """The USB sticks plugged in now: [{drive, label, free}]."""
        return {"drives": usb_drives()}

    def api_copy_usb(self, req):
        """Lev 2026-11-15: copy the saved G-code to a USB stick (its top folder) for the machine PC."""
        import shutil
        drive = req.get("drive") or ""
        if drive not in [d["drive"] for d in usb_drives()]:
            return {"ok": False, "error": "That USB stick isn't plugged in any more."}
        out = []
        try:
            for p in req.get("paths") or []:
                if not os.path.isfile(p):
                    return {"ok": False, "error": f"{os.path.basename(p)} isn't there any more: send again."}
                dst = os.path.join(drive, os.path.basename(p))
                shutil.copyfile(p, dst)
                out.append(dst)
        except OSError as e:
            return {"ok": False, "error": f"Couldn't write to the USB stick: {e.strerror or e}"}
        return {"ok": True, "paths": out}

    def api_folder(self, req):
        f = req.get("path") or P.machine_folder(self.prefs)
        try:
            os.startfile(f)
        except OSError:
            pass
        return {"ok": True}

    def api_rack(self, req):
        if "rack" in req:
            if self.user:   # shared: an edit is for everyone, but each designer's own speeds stay theirs
                with _RACK_LOCK:
                    new = P.rack_for_saving(req["rack"], shared_rack())
                    P.save_rack(new)
                    _RACK["rack"], _RACK["mtime"] = new, _rack_mtime()
            else:
                self.rack = req["rack"]
                P.save_rack(self.rack)
        return {"rack": self.rack}

    def api_speeds(self, req):
        """Lev's own speeds for one end mill in one material (req: name, material, speeds {rpm, feed, ramp_feed,
        plunge_feed, step}; speeds null = back to the program's numbers). Kept in the rack, so every job uses them."""
        mat = req.get("material")
        if mat not in P.MATERIALS:
            return {"ok": False, "error": "Unknown material"}
        if self.user:   # Lev 2026-11-16: speeds are each designer's own, over the shared rack's
            if not P.set_user_speeds(self.prefs, shared_rack(), req.get("name"), mat, req.get("speeds")):
                return {"ok": False, "error": "That end mill isn't in the rack"}
            self._save_prefs()
            return {"ok": True, "rack": self.rack}
        bit = P.set_own_speeds(self.rack, req.get("name"), mat, req.get("speeds"))
        if bit is None:
            return {"ok": False, "error": "That end mill isn't in the rack"}
        P.save_rack(self.rack)
        return {"ok": True, "rack": self.rack}

    def api_gcm(self, req):
        if not self.part or self.part["kind"] != "2d":
            return {"ok": False}
        import subprocess
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        subprocess.Popen([exe if os.path.isfile(exe) else sys.executable, GCODE_MAKER, self.part["path"]])
        return {"ok": True}

    # ---------------------------------------------------------------- binary
    def bin_grid(self, kind):
        """[u32 header length][header json][float32 heights (ny*nx), NaN = nothing]"""
        if kind == "preview" and self.sim is not None:
            g = self.sim
            z, (x0, y0), cell = np.asarray(g["grid"], np.float32), g["origin"], g["cell"]
            lo = None
            if self.part and self.part["kind"] == "3d":
                try:
                    lo = engine3d().leftover(self.job)
                except Exception:
                    lo = None
            hdr = {"nx": int(z.shape[1]), "ny": int(z.shape[0]), "x0": float(x0), "y0": float(y0),
                   "cell": float(cell), "kind": "preview", "left": lo is not None}
            data = z.tobytes()
            if lo is not None:
                lg = np.asarray(lo["grid"] if isinstance(lo, dict) else lo, np.float32)
                if lg.shape == z.shape:
                    data += lg.tobytes()
                else:
                    hdr["left"] = False
            return hdr, data
        p = self.part
        route2d = bool(getattr(self.job, "route2d", False))
        if p and p["kind"] == "3d" and p.get("an") is not None and not route2d:
            an = p["an"]
            z = np.asarray(an["grid"], np.float32)
            off = getattr(self.job, "part_offset", None) if self.job is not None else None
            ox, oy = (float(off[0]), float(off[1])) if off is not None else (0.0, 0.0)
            hdr = {"nx": int(z.shape[1]), "ny": int(z.shape[0]), "x0": float(an["origin"][0]) + ox,
                   "y0": float(an["origin"][1]) + oy, "cell": float(an["cell"]), "kind": "part"}
            und = an.get("undercut")
            data = z.tobytes()
            if und is not None:
                hdr["undercut"] = True
                data += np.asarray(und, np.float32).tobytes()
            return hdr, data
        if p and (p["kind"] == "2d" or route2d) and self.job is not None and self.meta and self.meta.get("stock"):
            # the sheet with the part drawn on it: 1 inside the part, 0 outside (the page colours it)
            import raster
            st = self.meta["stock"]
            cell = max(0.08, math.sqrt(st["w"] * st["h"] / 600000.0))
            nx, ny = int(st["w"] / cell) + 1, int(st["h"] / cell) + 1
            m = raster.fill_polys(self.job.contours, st["x0"], st["y0"], cell, nx, ny, "evenodd").astype(np.float32)
            hdr = {"nx": nx, "ny": ny, "x0": st["x0"], "y0": st["y0"], "cell": cell, "kind": "sheet"}
            return hdr, m.tobytes()
        return None, b""

    def bin_mesh(self):
        """The part's own triangles (9 float32 each) in the program frame, for a crisp view of 3D parts."""
        p = self.part
        if not p or p.get("obj") is None:
            return {"n": 0}, b""
        tris = np.asarray(p["obj"].tris, np.float32).copy()
        off = getattr(self.job, "part_offset", None) if self.job is not None else None
        if off is not None:
            tris[:, :, 0] += float(off[0])
            tris[:, :, 1] += float(off[1])
        return {"n": int(tris.shape[0])}, tris.tobytes()

    def bin_paths(self):
        """Every move as 8 float32: x0 y0 z0 x1 y1 z1 stage rapid; then the cumulative minutes per move."""
        job = self.job
        if not job or not job.segs:
            return {"n": 0}, b""
        segs = job.segs
        arr = np.array([s[:6] for s in segs], np.float32)
        rapid = np.array([1.0 if s[6] else 0.0 for s in segs], np.float32)
        stage = np.zeros(len(segs), np.float32)
        for i, (i0, _) in enumerate(job.stages):
            stage[i0:] = i
        cum = np.cumsum([x[0] for x in job.seg_info]).astype(np.float32) if job.seg_info else np.zeros(len(segs), np.float32)
        out = np.column_stack([arr, stage, rapid]).astype(np.float32)
        return {"n": len(segs)}, out.tobytes() + cum.tobytes()


S = Session()   # the old single session: tests, and requests that name no designer
_LOGOS = {}
WINDOW = None   # the app's own window, when it runs as one

# Lev 2026-11-16: one session per designer (their part, plan and preferences), and one shared rack.
_SESSIONS = {}
_SESS_LOCK = threading.Lock()
_RACK = {"rack": None, "mtime": None}
_RACK_LOCK = threading.Lock()
LAST_SEEN = [time.time()]


def _rack_mtime():
    try:
        return os.path.getmtime(P.RACK_FILE)
    except OSError:
        return None


def shared_rack():
    """The shared rack, read again whenever its file changed (another designer saved it)."""
    with _RACK_LOCK:
        m = _rack_mtime()
        if _RACK["rack"] is None or m != _RACK["mtime"]:
            _RACK["rack"], _RACK["mtime"] = P.load_rack(), m
        return _RACK["rack"]


def _out_dir(user):
    d = os.path.join(UPLOADS, "out", P.clean_user(user) or "-")
    os.makedirs(d, exist_ok=True)
    return d


def session_for(user, app=False):
    """The designer's session; the app window gets its own (it has the Windows dialogs)."""
    user = P.clean_user(user)
    if not user:
        return S
    key = (user.lower(), bool(app))
    with _SESS_LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            s = _SESSIONS[key] = Session(user)
            if user not in P.list_users():
                P.save_user(user, s.prefs)   # a new designer: their profile now exists
        s.window = WINDOW if app else None
        return s


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session(self):
        LAST_SEEN[0] = time.time()
        return session_for(unquote(self.headers.get("X-ACNC-User") or ""), self.headers.get("X-ACNC-App") == "1")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/dl/"):   # a G-code download: /dl/<designer>/<file>
            bits = u.path[4:].split("/")
            if len(bits) != 2:
                return self._send(404, b"")
            f = os.path.join(_out_dir(unquote(bits[0])), os.path.basename(unquote(bits[1])))
            if not os.path.isfile(f):
                return self._send(404, b"not found", "text/plain")
            with open(f, "rb") as fh:
                data = fh.read()
            from urllib.parse import quote
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(os.path.basename(f))}"})
        if u.path.startswith("/bin/"):
            S = self._session()
            what = u.path[5:]
            try:
                if what == "grid":
                    hdr, data = S.bin_grid(q.get("kind", ["prepare"])[0])
                elif what == "paths":
                    hdr, data = S.bin_paths()
                elif what == "mesh":
                    hdr, data = S.bin_mesh()
                else:
                    return self._send(404, b"{}")
            except Exception:
                log(traceback.format_exc())
                return self._send(500, b"{}")
            h = json.dumps(hdr or {}).encode()
            pad = (-(4 + len(h))) % 4
            h += b" " * pad
            body = len(h).to_bytes(4, "little") + h + data
            return self._send(200, body, "application/octet-stream")
        path = u.path if u.path != "/" else "/index.html"
        if path in ("/logo.png", "/logo-dark.png"):
            data = _LOGOS.get(path)
            if data is None:
                data = _LOGOS[path] = brand.logo_png(96 if path == "/logo.png" else 192,
                                                     (255, 255, 255) if path == "/logo.png" else (29, 39, 51))
            return self._send(200, data, "image/png")
        if path == "/icon.ico":
            f = brand.make_icon()
        else:
            f = os.path.normpath(os.path.join(UI, path.lstrip("/")))
            if not f.startswith(UI):
                return self._send(403, b"")
        if not os.path.isfile(f):
            return self._send(404, b"not found", "text/plain")
        with open(f, "rb") as fh:
            data = fh.read()
        self._send(200, data, MIME.get(os.path.splitext(f)[1].lower(), "application/octet-stream"))

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        name = u.path.rsplit("/", 1)[-1]
        S = self._session()
        try:
            if name == "upload":
                res = S.api_upload(None, body, parse_qs(u.query))
            else:
                req = json.loads(body or b"{}")
                fn = getattr(S, "api_" + name, None)
                if fn is None:
                    return self._send(404, b"{}")
                res = fn(req)
            self._send(200, json.dumps(res, default=_jsonable).encode())
        except Exception as ex:
            log(traceback.format_exc())
            self._send(500, json.dumps({"error": f"{type(ex).__name__}: {ex}"}).encode())


def _jsonable(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, tuple):
        return list(o)
    return str(o)


BROWSER_PORT = 8232
IDLE_EXIT_S = 15 * 60


def _already_running(url):
    import urllib.request
    try:
        with urllib.request.urlopen(url + "logo.png", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def main(argv=None):
    global WINDOW
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 0
    serve = browser = False
    if argv and argv[0] == "--serve":
        serve = True
        port = int(argv[1]) if len(argv) > 1 else 8231
        argv = argv[2:]
    elif argv and argv[0] == "--browser":
        # Lev 2026-11-16: the app in the browser (Chrome/Edge) on this PC; a second start only opens a new tab
        browser = True
        port = BROWSER_PORT
        argv = argv[1:]
        url = f"http://127.0.0.1:{port}/"
        if _already_running(url):
            import webbrowser
            webbrowser.open(url)
            return 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if argv and os.path.isfile(argv[0]):
        S.open_path(argv[0])
    if serve:
        print(f"Agilite CNC serving at {url}", flush=True)
        server.serve_forever()
        return 0
    if browser:
        import webbrowser

        def idle():   # no window to close: the server stops once no page has asked anything for a while
            while time.time() - LAST_SEEN[0] < IDLE_EXIT_S:
                time.sleep(30)
            server.shutdown()
        threading.Thread(target=idle, daemon=True).start()
        webbrowser.open(url)
        server.serve_forever()
        return 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    import webview
    S.window = WINDOW = webview.create_window(APP, url + "?app=1", width=1500, height=920, min_size=(1000, 640), maximized=True,
                                     background_color="#f4f5f7")
    webview.start(icon=brand.make_icon())
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
