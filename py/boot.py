"""
Agilite CNC web app: the very same Python engine and session code as on Lev's PC, run in the browser by Pyodide
(web/worker.js). This file adapts the few things a browser can't do: no threads (work runs in turn, the worker
keeps the page responsive), no OpenCascade (STEP files are meshed by occt-import-js in the worker), and files
live in the browser (each designer's profile in /data, kept in the browser's storage).
"""
import json
import os
import sys
import threading
import traceback
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, "/app")


class _InTurn(threading.Thread):
    """No threads in the browser: a 'background' job runs straight away."""

    def start(self):
        self.run()


threading.Thread = _InTurn

from js import post_progress, step_to_stl   # noqa: E402  (worker.js functions)
from pyodide.ffi import to_js               # noqa: E402

import numpy as np   # noqa: E402

# the browser's numpy is 32-bit: counts the engine works out as int64 must become its own index size
_repeat = np.repeat


def _repeat32(a, repeats, axis=None):
    if isinstance(repeats, np.ndarray) and repeats.dtype != np.intp:
        repeats = repeats.astype(np.intp)
    return _repeat(a, repeats, axis=axis)


np.repeat = _repeat32

import presets as P   # noqa: E402

P.PREFS_FILE = "/data/settings.json"
P.USERS_DIR = "/data/users"


def _no_migrate():
    os.makedirs(P.USERS_DIR, exist_ok=True)


P._migrate = _no_migrate   # nobody's old settings file in a browser

import engine3d   # noqa: E402


def _step_to_stl(path):
    out = path + ".stl"
    if os.path.isfile(out) and os.path.getsize(out) > 84 and os.path.getmtime(out) >= os.path.getmtime(path):
        return out
    with open(path, "rb") as f:
        data = f.read()
    stl = step_to_stl(to_js(data)).to_bytes()
    if len(stl) <= 84:
        raise ValueError("The STEP file holds no solid shape.")
    with open(out, "wb") as f:
        f.write(stl)
    return out


engine3d._step_to_stl = _step_to_stl

import server   # noqa: E402

server.UPLOADS = "/tmp/agilite-cnc-uploads"
server.log = lambda text: print("[agilite-cnc] " + text)   # to the browser console (F12)
_start = server.Session._start


def _start_with_progress(self, fn, text):
    post_progress(0.0, text)

    def fn2(progress, cancelled):
        def p(frac, txt):
            progress(frac, txt)
            post_progress(float(frac), str(txt))
        fn(p, cancelled)
    return _start(self, fn2, text)


server.Session._start = _start_with_progress
_api_rack = server.Session.api_rack


def _rack_read_only(self, req):
    # the rack is the one at the machine, published with the site: a browser never changes it
    return _api_rack(self, {})


server.Session.api_rack = _rack_read_only


def _reply(status, ctype, data):
    return to_js([status, ctype, memoryview(data)])


def handle(method, url, user, body):
    """One request from the page: (status, content type, bytes), like server.Handler."""
    body = body.to_bytes() if body is not None else b""
    S = server.session_for(user or "", False)
    u = urlparse(url)
    q = parse_qs(u.query)
    try:
        if u.path.startswith("/dl/"):
            bits = u.path[4:].split("/")
            f = os.path.join(server._out_dir(unquote(bits[0])), os.path.basename(unquote(bits[-1])))
            if not os.path.isfile(f):
                return _reply(404, "text/plain", b"not found")
            with open(f, "rb") as fh:
                return _reply(200, "application/octet-stream", fh.read())
        if u.path.startswith("/bin/"):
            what = u.path[5:]
            if what == "grid":
                hdr, data = S.bin_grid(q.get("kind", ["prepare"])[0])
            elif what == "paths":
                hdr, data = S.bin_paths()
            elif what == "mesh":
                hdr, data = S.bin_mesh()
            else:
                return _reply(404, "application/json", b"{}")
            h = json.dumps(hdr or {}).encode()
            h += b" " * ((-(4 + len(h))) % 4)
            return _reply(200, "application/octet-stream", len(h).to_bytes(4, "little") + h + data)
        name = u.path.rsplit("/", 1)[-1]
        if name == "upload":
            res = S.api_upload(None, body, q)
        else:
            fn = getattr(S, "api_" + name, None)
            if fn is None:
                return _reply(404, "application/json", b"{}")
            res = fn(json.loads(body or b"{}"))
        return _reply(200, "application/json", json.dumps(res, default=server._jsonable).encode())
    except Exception as ex:
        traceback.print_exc()
        return _reply(500, "application/json", json.dumps({"error": f"{type(ex).__name__}: {ex}"}).encode())
