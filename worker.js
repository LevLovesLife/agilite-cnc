// Agilite CNC web app: the engine's worker. Loads Python (Pyodide) with numpy, the app's own Python files
// (py/), and the STEP reader (occt-import-js), then answers the page's /api, /bin and /dl requests (web/shim.js).
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const OCCT = "https://cdn.jsdelivr.net/npm/occt-import-js@0.0.22/dist/";
importScripts(PYODIDE + "pyodide.js");

let py = null, handle = null, occt = null;
const say = (f, t) => postMessage({ progress: [f, t] });
self.post_progress = (f, t) => say(f, t);

// a STEP file's faces as a binary STL (millimetres), like stepconv.py does with OpenCascade on the PC
self.step_to_stl = (bytes) => {
  const r = occt.ReadStepFile(new Uint8Array(bytes),
    { linearUnit: "millimeter", linearDeflectionType: "absolute_value", linearDeflection: 0.02, angularDeflection: 0.2 });
  if (!r || !r.success) throw new Error("The STEP file could not be read.");
  let n = 0;
  for (const m of r.meshes) n += m.index.array.length / 3;
  const out = new ArrayBuffer(84 + 50 * n), dv = new DataView(out);
  dv.setUint32(80, n, true);
  let o = 84;
  for (const m of r.meshes) {
    const p = m.attributes.position.array, ix = m.index.array;
    for (let i = 0; i < ix.length; i += 3) {
      o += 12;   // the normal: the engine works it out from the corners
      for (let k = 0; k < 3; k++) {
        const v = ix[i + k] * 3;
        dv.setFloat32(o, p[v], true); dv.setFloat32(o + 4, p[v + 1], true); dv.setFloat32(o + 8, p[v + 2], true);
        o += 12;
      }
      o += 2;
    }
  }
  return new Uint8Array(out);
};

const syncfs = (populate) => new Promise((res) => py.FS.syncfs(populate, () => res()));
let saving = null;
const save = () => { clearTimeout(saving); saving = setTimeout(() => syncfs(false), 300); };

const ready = (async () => {
  say(0.05, "Loading the engine");
  const occtLoad = (async () => { importScripts(OCCT + "occt-import-js.js"); occt = await occtimportjs({ locateFile: (f) => OCCT + f }); })();
  py = await loadPyodide({ indexURL: PYODIDE, stdout: (t) => postMessage({ log: t }), stderr: (t) => postMessage({ log: t }) });
  say(0.4, "Loading numpy");
  await py.loadPackage("numpy");
  say(0.7, "Loading Agilite CNC");
  const files = await (await fetch("py/files.json", { cache: "no-cache" })).json();
  await Promise.all(files.map(async (f) => {
    const b = new Uint8Array(await (await fetch("py/" + f, { cache: "no-cache" })).arrayBuffer());
    const path = "/app/" + f;
    py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    py.FS.writeFile(path, b);
  }));
  py.FS.mkdirTree("/data");
  py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");   // each designer's profile, kept in this browser
  await syncfs(true);
  py.runPython(new TextDecoder().decode(py.FS.readFile("/app/boot.py")), { filename: "boot.py" });
  handle = py.globals.get("handle");
  await occtLoad;
  postMessage({ ready: true });
})().catch((e) => { postMessage({ failed: String(e && e.message || e) }); throw e; });

onmessage = async (e) => {
  const { id, method, url, user, body } = e.data;
  if (e.data.debug) {   // window.__acncDebug("python") from the console (F12): for finding faults
    await ready;
    let out;
    try { out = String(py.runPython(e.data.debug)); } catch (err) { out = String(err); }
    postMessage({ id, status: 200, ctype: "text/plain", data: new TextEncoder().encode(out) });
    return;
  }
  try {
    await ready;
    const r = handle(method, url, user, body || null);
    const [status, ctype, view] = r;
    const data = new Uint8Array(view).slice();
    r.destroy?.();
    postMessage({ id, status, ctype, data }, [data.buffer]);
    if (!url.startsWith("/api/poll") && !url.startsWith("/bin/")) save();
  } catch (err) {
    const data = new TextEncoder().encode(JSON.stringify({ error: String(err && err.message || err) }));
    postMessage({ id, status: 500, ctype: "application/json", data }, [data.buffer]);
  }
};
