// Agilite CNC - the page. Prepare (material, finish, add-ons) and Preview (what happens, playback),
// a three.js view on the graphics card, and a small JSON/binary API to the Python side (server.py).
import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { StockSim } from "./stocksim.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const IN_APP = new URLSearchParams(location.search).has("app");
const WEB = !!window.ACNC_WEB;   // the public web app: the engine runs in this browser (web/shim.js), no PC behind it

const OP_COLOURS = [["rough", "#7F77DD"], ["finish", "#1D9E75"], ["waterline", "#1D9E75"], ["parallel", "#1D9E75"],
  ["hole", "#D85A30"], ["slot", "#D85A30"], ["pocket", "#B0477A"], ["detail", "#BA7517"], ["rest", "#BA7517"],
  ["pencil", "#BA7517"], ["outline", "#1f6fd1"], ["cut", "#1f6fd1"], ["engrav", "#3C3489"], ["chamfer", "#5F5E5A"],
  ["edge", "#5F5E5A"], ["surfac", "#888780"]];
const MAT_RGB = { "Aluminium": [0.78, 0.80, 0.84], "Wood / MDF / plywood": [0.84, 0.76, 0.62] };
const SPEEDS = [[1, "1×"], [10, "10×"], [50, "50×"], [200, "200×"], [0, "Max"]];
const ADD_NAMES = { text: "Text", logo: "Logo", chamfer: "Chamfer", edge: "Round edges", surface: "Surfacing" };

const st = {
  info: null, part: null, plan: null, gen: -1, mode: "prepare", busy: false,
  opts: { material: "Aluminium sheet", finish: "Standard", thickness: 3, tabs: { count: 3, len: 4, thick: 1, windows: 0 }, extra: 0.5, sensor: null, addons: [], strategy: "recommended", bits: {}, params: {}, sheet: { copies: [1], gap: 8 } },
  paths: null, playing: false, speed: 50, tNow: 1e9, expanded: -1, hiddenStages: new Set(),
};

// ------------------------------------------------------------------ api
// Lev 2026-11-16: each designer works in their own profile (own part, presets, speeds, preferences); the rack is shared
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } } };
let USER = store.get("acnc_user") || "";
const who = () => ({ "X-ACNC-User": encodeURIComponent(USER), ...(IN_APP ? { "X-ACNC-App": "1" } : {}) });
async function api(name, body = {}) {
  const r = await fetch("/api/" + name, { method: "POST", body: JSON.stringify(body), headers: who() });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function partMesh(rgb) {
  try {
    const { hdr, data } = await bin("/bin/mesh");
    if (!hdr.n) return null;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(data), 3));
    g.computeVertexNormals();   // separate triangles: flat faces stay flat, curves show their real facets
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: new THREE.Color(...rgb), roughness: 0.55, metalness: 0.15, side: THREE.DoubleSide }));
    const grp = new THREE.Group(); grp.add(m);
    grp.add(new THREE.LineSegments(new THREE.EdgesGeometry(g, 25), new THREE.LineBasicMaterial({ color: 0x5b6470, transparent: true, opacity: 0.55 })));
    return grp;
  } catch (e) { console.warn("no part mesh", e); return null; }
}
async function bin(path) {
  const r = await fetch(path, { cache: "no-store", headers: who() });
  const buf = await r.arrayBuffer();
  const n = new DataView(buf).getUint32(0, true);
  const hdr = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
  return { hdr, data: new Float32Array(buf, 4 + n) };
}
function toast(text, ms = 2600) {
  const t = $("#toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), ms);
}
const fmtMin = (m) => { const s = Math.round(m * 60); const h = Math.floor(s / 3600);
  return h ? `${h}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtLong = (m) => m < 1 ? `${Math.max(1, Math.round(m * 60))} s` : m < 60 ? `${Math.round(m)} min` : `${Math.floor(m / 60)} h ${String(Math.round(m % 60)).padStart(2, "0")} min`;
const opColour = (label) => { const l = (label || "").toLowerCase(); for (const [k, c] of OP_COLOURS) if (l.includes(k)) return c; return "#534AB7"; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------------------ three.js scene
const canvas = $("#gl");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 1, 0.5, 20000);
camera.up.set(0, 0, 1);
camera.position.set(-200, -300, 260);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true; controls.dampingFactor = 0.12; controls.screenSpacePanning = true;
controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.PAN };
scene.add(new THREE.HemisphereLight(0xffffff, 0x8a9099, 1.35));
const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(-0.6, -0.9, 1.4); scene.add(sun);
const fill = new THREE.DirectionalLight(0xffffff, 0.45); fill.position.set(0.8, 0.5, 0.6); scene.add(fill);
const world = new THREE.Group(); scene.add(world);
const layers = { bed: new THREE.Group(), body: new THREE.Group(), paths: new THREE.Group(), tool: new THREE.Group(), sensor: new THREE.Group(), marks: new THREE.Group(), tabs: new THREE.Group(), edges: new THREE.Group(), ghost: new THREE.Group() };
Object.values(layers).forEach((g) => world.add(g));

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h; camera.updateProjectionMatrix();
  needs = true;   // setSize clears the canvas: draw again, or it stays blank until the next change
}
new ResizeObserver(resize).observe(canvas);
let needs = true;
controls.addEventListener("change", () => (needs = true));
function loop(t) {
  requestAnimationFrame(loop);
  controls.update();
  if (st.playing) tickPlay(t);
  if (needs) { renderer.render(scene, camera); needs = false; }
}
requestAnimationFrame(loop);
const redraw = () => (needs = true);

function clear(g) { while (g.children.length) { const c = g.children.pop(); c.traverse?.((o) => { o.geometry?.dispose(); o.material?.dispose?.(); }); } }

function bedGrid(stock) {
  clear(layers.bed);
  const w = Math.max(stock.w * 1.6, 120), h = Math.max(stock.h * 1.6, 120);
  const step = w > 600 ? 50 : w > 250 ? 20 : 10;
  const cx = stock.x0 + stock.w / 2, cy = stock.y0 + stock.h / 2;
  const pts = [];
  const x0 = Math.floor((cx - w / 2) / step) * step, x1 = Math.ceil((cx + w / 2) / step) * step;
  const y0 = Math.floor((cy - h / 2) / step) * step, y1 = Math.ceil((cy + h / 2) / step) * step;
  const z = -stock.t - 0.05;
  for (let x = x0; x <= x1; x += step) pts.push(x, y0, z, x, y1, z);
  for (let y = y0; y <= y1; y += step) pts.push(x0, y, z, x1, y, z);
  const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  layers.bed.add(new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0xb8c0ca, transparent: true, opacity: 0.55 })));
  // X/Y arrows at the job's zero
  const ax = new THREE.Group();
  const mk = (dx, dy, col) => { const gg = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0.1), new THREE.Vector3(dx, dy, 0.1)]);
    ax.add(new THREE.Line(gg, new THREE.LineBasicMaterial({ color: col }))); };
  const L = Math.max(8, Math.min(stock.w, stock.h) * 0.12);
  mk(L, 0, 0xd85a30); mk(0, L, 0x1d9e75);
  layers.bed.add(ax);
}

// a height grid as a mesh: top surface plus walls down to the bottom where it ends (NaN = nothing)
function gridMesh(hdr, z, colourOf, bottom, maxSide = 900) {
  const { nx, ny, x0, y0, cell } = hdr;
  const s = Math.max(1, Math.ceil(Math.max(nx, ny) / maxSide));
  const mx = Math.ceil(nx / s), my = Math.ceil(ny / s);
  const zz = new Float32Array(mx * my);
  for (let j = 0; j < my; j++) for (let i = 0; i < mx; i++) zz[j * mx + i] = z[Math.min(ny - 1, j * s) * nx + Math.min(nx - 1, i * s)];
  const cs = cell * s;
  const pos = [], col = [], idx = [];
  const vid = new Int32Array(mx * my).fill(-1);
  const c = new THREE.Color();
  for (let j = 0; j < my; j++) for (let i = 0; i < mx; i++) {
    const v = zz[j * mx + i];
    if (!Number.isFinite(v)) continue;
    vid[j * mx + i] = pos.length / 3;
    pos.push(x0 + (i + 0.5) * cs, y0 + (j + 0.5) * cs, v);
    colourOf(c, Math.min(ny - 1, j * s) * nx + Math.min(nx - 1, i * s), v);
    col.push(c.r, c.g, c.b);
  }
  for (let j = 0; j < my - 1; j++) for (let i = 0; i < mx - 1; i++) {
    const a = vid[j * mx + i], b = vid[j * mx + i + 1], d = vid[(j + 1) * mx + i], e = vid[(j + 1) * mx + i + 1];
    if (a >= 0 && b >= 0 && d >= 0 && e >= 0) idx.push(a, b, e, a, e, d);
    else if (a >= 0 && b >= 0 && e >= 0) idx.push(a, b, e);
    else if (a >= 0 && e >= 0 && d >= 0) idx.push(a, e, d);
    else if (b >= 0 && e >= 0 && d >= 0) idx.push(b, e, d);
    else if (a >= 0 && b >= 0 && d >= 0) idx.push(a, b, d);
  }
  // walls: along every edge between a cell that is there and one that is not (or the grid's border)
  const wallCol = (k) => [col[k * 3] * 0.72, col[k * 3 + 1] * 0.72, col[k * 3 + 2] * 0.74];
  const wall = (k1, k2) => {
    const p1 = pos.slice(k1 * 3, k1 * 3 + 3), p2 = pos.slice(k2 * 3, k2 * 3 + 3);
    const n = pos.length / 3;
    pos.push(...p1, ...p2, p2[0], p2[1], bottom, p1[0], p1[1], bottom);
    const w1 = wallCol(k1), w2 = wallCol(k2);
    col.push(...w1, ...w2, ...w2, ...w1);
    idx.push(n, n + 1, n + 2, n, n + 2, n + 3);
  };
  const at = (i, j) => (i < 0 || j < 0 || i >= mx || j >= my) ? -1 : vid[j * mx + i];
  for (let j = 0; j < my; j++) for (let i = 0; i < mx; i++) {
    const k = at(i, j); if (k < 0) continue;
    if (at(i + 1, j) >= 0 && (at(i, j - 1) < 0 || at(i + 1, j - 1) < 0)) wall(k, at(i + 1, j));
    if (at(i + 1, j) >= 0 && (at(i, j + 1) < 0 || at(i + 1, j + 1) < 0)) wall(k, at(i + 1, j));
    if (at(i, j + 1) >= 0 && (at(i - 1, j) < 0 || at(i - 1, j + 1) < 0)) wall(k, at(i, j + 1));
    if (at(i, j + 1) >= 0 && (at(i + 1, j) < 0 || at(i + 1, j + 1) < 0)) wall(k, at(i, j + 1));
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return new THREE.Mesh(g, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.62, metalness: 0.08, side: THREE.DoubleSide }));
}

function matRGB() {
  const m = st.info?.materials[st.opts.material];
  return MAT_RGB[m?.gcm] || [0.8, 0.82, 0.85];
}

function simTools() { return (st.plan?.stages || []).map((_, i) => toolForStage(i) || {}); }
// the holes whose middle falls out once cut free (no tabs on them): the islands inside a hole stay, and the slug
// drops when points just inside its outline are all cut through
function slugDrops() {
  const sh = (st.plan?.shapes || []).filter((s) => s.kind !== "rim" && s.points?.length > 2);
  const areaOf = (p) => { let a = 0; for (let i = 0; i < p.length; i++) { const [x0, y0] = p[i], [x1, y1] = p[(i + 1) % p.length]; a += x0 * y1 - x1 * y0; } return a / 2; };
  const inside = ([x, y], poly) => { let r = false; for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) { const [xi, yi] = poly[i], [xj, yj] = poly[j]; if ((yi > y) !== (yj > y) && x < xi + ((y - yi) * (xj - xi)) / (yj - yi)) r = !r; } return r; };
  const out = [];
  for (const h of sh) {
    if (h.kind !== "hole" || (h.tabs || []).length) continue;
    const A = areaOf(h.points);
    const inner = sh.filter((s) => s !== h && Math.abs(areaOf(s.points)) < Math.abs(A) && inside(s.points[0], h.points))
      .filter((s, _, arr) => !arr.some((o) => o !== s && Math.abs(areaOf(o.points)) > Math.abs(areaOf(s.points)) && inside(s.points[0], o.points)))
      .map((s) => s.points);
    // 32 points along the outline, 0.25 mm towards the inside of the hole
    const p = h.points, n = p.length, L = [0];
    for (let i = 0; i < n; i++) L.push(L[i] + Math.hypot(p[(i + 1) % n][0] - p[i][0], p[(i + 1) % n][1] - p[i][1]));
    const sgn = A > 0 ? 1 : -1, samples = [];
    for (let k = 0; k < 32; k++) {
      const s = (L[n] * (k + 0.5)) / 32;
      let i = 0; while (i < n - 1 && L[i + 1] < s) i++;
      const [x0, y0] = p[i], [x1, y1] = p[(i + 1) % n], d = Math.hypot(x1 - x0, y1 - y0) || 1, f = (s - L[i]) / d;
      samples.push([x0 + (x1 - x0) * f - sgn * (y1 - y0) / d * 0.25, y0 + (y1 - y0) * f + sgn * (x1 - x0) / d * 0.25]);
    }
    out.push({ outer: p, inner, samples });
  }
  return out;
}
async function loadBody() {
  if (st.sim) { st.sim.dispose(); st.sim = null; }
  clear(layers.body);
  // Preview: the stock on the graphics card, cut move by move as the playback runs
  if (st.mode === "preview" && st.plan?.stock && !(st.part?.kind === "3d" && $("#showleft").checked)) {
    try {
      st.sim = new StockSim(renderer, { stock: st.plan.stock, colour: matRGB() });
      layers.body.add(st.sim.object3d);
      st.sim.setDrops(slugDrops());
      if (st.paths) { st.sim.setMoves(st.paths.segs, st.paths.cum, simTools()); st.sim.setTime(Math.min(st.tNow, st.paths.total)); }
      bedGrid(st.plan.stock);
      if (!loadBody.fitted || loadBody.fitted !== st.part?.path) { fit(); loadBody.fitted = st.part?.path; }
      redraw();
      return;
    } catch (e) { console.warn("StockSim not available, using the server's simulation", e); st.sim = null; }
  }
  const kind = st.mode === "preview" && st.hasSim ? "preview" : "prepare";
  const { hdr, data } = await bin("/bin/grid?kind=" + kind);
  if (!hdr.nx) { redraw(); return; }
  const n = hdr.nx * hdr.ny;
  const z = data.subarray(0, n);
  const base = matRGB();
  const stock = st.plan?.stock;
  if (hdr.kind === "sheet") {
    // the sheet as a box, the part drawn on its top in blue
    const t = stock.t;
    const box = new THREE.Mesh(new THREE.BoxGeometry(stock.w, stock.h, t),
      new THREE.MeshStandardMaterial({ color: new THREE.Color(...base), roughness: 0.6, metalness: 0.1 }));
    box.position.set(stock.x0 + stock.w / 2, stock.y0 + stock.h / 2, -t / 2);
    layers.body.add(box);
    const tex = new Uint8Array(n * 4);
    for (let k = 0; k < n; k++) {
      const p = z[k] > 0.5;
      tex[k * 4] = p ? 70 : 0; tex[k * 4 + 1] = p ? 125 : 0; tex[k * 4 + 2] = p ? 210 : 0; tex[k * 4 + 3] = p ? 150 : 0;
    }
    const dt = new THREE.DataTexture(tex, hdr.nx, hdr.ny, THREE.RGBAFormat);
    dt.needsUpdate = true; dt.magFilter = THREE.LinearFilter; dt.minFilter = THREE.LinearFilter;
    const top = new THREE.Mesh(new THREE.PlaneGeometry(hdr.nx * hdr.cell, hdr.ny * hdr.cell),
      new THREE.MeshBasicMaterial({ map: dt, transparent: true, depthWrite: false }));
    top.position.set(hdr.x0 + hdr.nx * hdr.cell / 2, hdr.y0 + hdr.ny * hdr.cell / 2, 0.02);
    layers.body.add(top);
    // outlines, crisp
    for (const c of st.plan?.outlines || []) {
      const pts = c.map((p) => new THREE.Vector3(p[0], p[1], 0.05)); pts.push(pts[0].clone());
      layers.body.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0x1a4f9c })));
    }
  } else {
    let bottom = stock ? -stock.t : Infinity;
    for (let k = 0; k < n; k++) if (Number.isFinite(z[k]) && z[k] < bottom) bottom = z[k];
    if (hdr.kind === "preview") {
      for (let k = 0; k < n; k++) if (z[k] <= bottom + 1e-3) z[k] = NaN;   // cut through: nothing there
    }
    const left = hdr.left && $("#showleft").checked ? data.subarray(n, 2 * n) : null;
    const und = hdr.undercut ? data.subarray(n, 2 * n) : null;
    const colourOf = (c, k, v) => {
      let [r, g, b] = base;
      if (hdr.kind === "preview" && v < -0.005) { r = r * 0.9 + 0.08; g = g * 0.9 + 0.08; b = b * 0.9 + 0.09; }
      if (left && Number.isFinite(left[k])) {
        const L = left[k];
        if (L < -0.02) [r, g, b] = [0.89, 0.29, 0.29];
        else if (L <= 0.05) [r, g, b] = [0.39, 0.6, 0.13];
        else if (L <= 0.3) [r, g, b] = [0.94, 0.62, 0.15];
        else [r, g, b] = [0.85, 0.35, 0.19];
      }
      if (und && und[k] > 0.5) [r, g, b] = [0.89, 0.29, 0.29];
      c.setRGB(r, g, b);
    };
    // Prepare: the part's own surfaces, crisp at any zoom (the height grid only feeds the cut simulation)
    const model = hdr.kind === "part" ? await partMesh(base) : null;
    layers.body.add(model || gridMesh(hdr, z, colourOf, bottom));
    if (stock && hdr.kind === "part") {   // the stock around the part, as glass
      const box = new THREE.Mesh(new THREE.BoxGeometry(stock.w, stock.h, stock.t),
        new THREE.MeshBasicMaterial({ color: 0x8aa0b8, transparent: true, opacity: 0.08, depthWrite: false }));
      box.position.set(stock.x0 + stock.w / 2, stock.y0 + stock.h / 2, -stock.t / 2);
      layers.body.add(box);
      layers.body.add(new THREE.LineSegments(new THREE.EdgesGeometry(box.geometry), new THREE.LineBasicMaterial({ color: 0x8a96a3 })).translateX(box.position.x).translateY(box.position.y).translateZ(box.position.z));
    }
  }
  if (stock) bedGrid(stock);
  if (!loadBody.fitted || loadBody.fitted !== st.part?.path) { fit(); loadBody.fitted = st.part?.path; }
  redraw();
}

// ------------------------------------------------------------------ camera
function bounds() {
  const s = st.plan?.stock;
  if (s) return { x0: s.x0, y0: s.y0, x1: s.x0 + s.w, y1: s.y0 + s.h, z0: -s.t, z1: 0 };
  const b = new THREE.Box3().setFromObject(layers.body);
  if (b.isEmpty()) return { x0: -50, y0: -50, x1: 50, y1: 50, z0: -5, z1: 0 };
  return { x0: b.min.x, y0: b.min.y, x1: b.max.x, y1: b.max.y, z0: b.min.z, z1: b.max.z };
}
function fit(dir = "iso") {
  const b = bounds();
  const c = new THREE.Vector3((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, (b.z0 + b.z1) / 2);
  const r = Math.hypot(b.x1 - b.x0, b.y1 - b.y0, b.z1 - b.z0) / 2;
  const d = r / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05;
  const v = dir === "top" ? new THREE.Vector3(0, -0.001, 1) : dir === "front" ? new THREE.Vector3(0, -1, 0.18) : new THREE.Vector3(-0.55, -0.95, 0.95);
  v.normalize();
  camera.position.copy(c).addScaledVector(v, d);
  controls.target.copy(c);
  camera.near = d / 100; camera.far = d * 20; camera.updateProjectionMatrix();
  controls.update(); redraw();
}
$$("[data-cam]").forEach((b) => b.onclick = () => fit(b.dataset.cam === "fit" ? "iso" : b.dataset.cam));

// ------------------------------------------------------------------ toolpaths and playback
async function loadPaths() {
  clear(layers.paths); clear(layers.tool);
  st.paths = null;
  if (!st.plan || !st.plan.nsegs) { redraw(); return; }
  const { hdr, data } = await bin("/bin/paths");
  const n = hdr.n;
  const segs = data.subarray(0, n * 8), cum = data.subarray(n * 8, n * 9);
  const stages = st.plan.stages;
  const cols = stages.map((s) => new THREE.Color(opColour(s.label)));
  const cut = [], cutCol = [], cutIdx = [], air = [], airIdx = [];
  for (let k = 0; k < n; k++) {
    const o = k * 8, si = segs[o + 6] | 0, rapid = segs[o + 7] > 0.5;
    if (rapid) { air.push(segs[o], segs[o + 1], segs[o + 2], segs[o + 3], segs[o + 4], segs[o + 5]); airIdx.push(k); continue; }
    cut.push(segs[o], segs[o + 1], segs[o + 2], segs[o + 3], segs[o + 4], segs[o + 5]);
    const c = cols[si] || cols[0];
    cutCol.push(c.r, c.g, c.b, c.r, c.g, c.b);
    cutIdx.push(k);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(cut, 3));
  g.setAttribute("color", new THREE.Float32BufferAttribute(cutCol, 3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.55, depthTest: true }));
  lines.renderOrder = 2;
  const ga = new THREE.BufferGeometry();
  ga.setAttribute("position", new THREE.Float32BufferAttribute(air, 3));
  const airLines = new THREE.LineSegments(ga, new THREE.LineDashedMaterial({ color: 0x9aa4ae, dashSize: 2, gapSize: 2, transparent: true, opacity: 0.7 }));
  airLines.computeLineDistances();
  layers.paths.add(lines, airLines);
  st.paths = { segs, cum, n, lines, airLines, cutIdx: Int32Array.from(cutIdx), airIdx: Int32Array.from(airIdx), total: n ? cum[n - 1] : 0 };
  if (st.sim) { st.sim.setDrops(slugDrops()); st.sim.setMoves(segs, cum, simTools()); }
  // the bit
  const tool = new THREE.Group();
  const shank = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 24), new THREE.MeshStandardMaterial({ color: 0xc8ccd2, metalness: 0.7, roughness: 0.3, transparent: true, opacity: 0.85 }));
  shank.rotation.x = Math.PI / 2; tool.add(shank); tool.userData.shank = shank;
  const dot = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), new THREE.MeshBasicMaterial({ color: 0xe24b4a }));
  tool.add(dot); tool.userData.dot = dot;
  layers.tool.add(tool); st.tool = tool;
  layers.tool.visible = false;
  updateTicks();
  applyVisibility();
  setTime(st.tNow);
}

function applyVisibility() {
  const show = $("#showpaths").checked && st.mode === "preview";
  layers.paths.visible = show;
  if (st.paths) st.paths.airLines.visible = $("#showrapids").checked;
  redraw();
}
$("#showpaths").onchange = applyVisibility;
$("#showrapids").onchange = applyVisibility;
$("#showleft").onchange = () => loadBody();

function countBefore(arr, k) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < k) lo = m + 1; else hi = m; } return lo; }

function setTime(t) {
  const p = st.paths;
  if (!p) return;
  st.tNow = Math.max(0, Math.min(t, p.total));
  if (st.sim) st.sim.setTime(st.tNow);
  let k = countBefore(p.cum, st.tNow);           // moves finished by now
  if (st.tNow >= p.total) k = p.n;
  p.lines.geometry.setDrawRange(0, countBefore(p.cutIdx, k) * 2);
  p.airLines.geometry.setDrawRange(0, countBefore(p.airIdx, k) * 2);
  const done = st.tNow >= p.total - 1e-9;
  layers.tool.visible = !done && st.mode === "preview";
  if (!done) {
    const kk = Math.min(k, p.n - 1), o = kk * 8;
    const t0 = kk > 0 ? p.cum[kk - 1] : 0, t1 = p.cum[kk];
    const f = t1 > t0 ? (st.tNow - t0) / (t1 - t0) : 1;
    const x = p.segs[o] + (p.segs[o + 3] - p.segs[o]) * f, y = p.segs[o + 1] + (p.segs[o + 4] - p.segs[o + 1]) * f, z = p.segs[o + 2] + (p.segs[o + 5] - p.segs[o + 2]) * f;
    const si = p.segs[o + 6] | 0;
    const d = st.plan.tools.length ? toolForStage(si)?.tool_d || 4 : 4;
    const L = Math.max(20, d * 6);
    st.tool.userData.shank.scale.set(d / 2, L, d / 2);
    st.tool.userData.shank.position.set(0, 0, L / 2);
    st.tool.userData.dot.scale.setScalar(Math.max(0.6, d * 0.18));
    st.tool.position.set(x, y, z);
  }
  $("#scrub").value = String(Math.round(1000 * (p.total ? st.tNow / p.total : 1)));
  $("#time").textContent = `${fmtMin(st.tNow)} / ${fmtMin(p.total)}`;
  // the step now running
  const si = done ? -1 : (p.segs[Math.min(k, p.n - 1) * 8 + 6] | 0);
  $$("#steps li[data-stage]").forEach((li) => li.classList.toggle("now", +li.dataset.stage === si));
  const cap = $("#caption");
  if (!done && st.playing && st.plan.stages[si]) { cap.hidden = false; cap.textContent = explainStage(st.plan.stages[si], si); }
  else cap.hidden = true;
  redraw();
}
function toolForStage(si) {
  const lab = st.plan.stages[si]?.label || "";
  const m = lab.match(/^T(\d+)\s+(.*?):/);
  if (!m) return null;
  const hit = st.plan.tools.find((t) => t.tool.name === m[2]);
  return hit ? hit.tool : null;
}
// the T number the G-code (and so the machine and its pauses) gives an end mill: from the stage labels "T2 name: ..."
function tNum(name) {
  for (const s of st.plan?.stages || []) {
    const m = s.label.match(/^T(\d+)\s+(.*?):/);
    if (m && m[2] === name) return +m[1];
  }
  return null;
}
function toolsInOrder() {   // the plan's end mills in the order they go in the spindle (T1, T2, ...)
  return [...(st.plan?.tools || [])].sort((a, b) => (tNum(a.tool.name) ?? 99) - (tNum(b.tool.name) ?? 99));
}
let lastT = null;
function tickPlay(now) {
  const p = st.paths;
  if (!p) { st.playing = false; return; }
  if (lastT === null) lastT = now;
  const dt = (now - lastT) / 1000; lastT = now;
  const rate = st.speed > 0 ? st.speed / 60 : p.total / 20;   // minutes of machine time per second
  const to = st.tNow + dt * rate;
  // the machine stops at every bit change (and every other pause) until the operator carries on: so does the
  // playback, on the spot, with what the machine asks for; Play goes on past it
  const stop = (st.plan?.pauses || []).map(([k, m]) => [k > 0 && k <= p.n ? p.cum[k - 1] : -1, m])
    .find(([tp]) => tp > st.tNow + 1e-9 && tp <= to);
  if (stop) {
    setTime(stop[0]); stopPlay();
    const cap = $("#caption"); cap.hidden = false;
    cap.textContent = `Paused: ${stop[1]} Press Play to go on.`;
    return;
  }
  setTime(to);
  if (st.tNow >= p.total) stopPlay();
}
function play() { if (!st.paths) return; if (st.tNow >= st.paths.total) setTime(0); st.playing = true; lastT = null; $("#play").classList.add("on"); }
function stopPlay() { st.playing = false; $("#play").classList.remove("on"); $("#caption").hidden = true; redraw(); }
$("#play").onclick = () => (st.playing ? stopPlay() : play());
$("#scrub").oninput = (e) => { if (st.paths) setTime(st.paths.total * e.target.value / 1000); };
$("#speeds").innerHTML = SPEEDS.map(([v, l]) => `<button data-v="${v}" class="${v === st.speed ? "on" : ""}">${l}</button>`).join("");
$$("#speeds button").forEach((b) => b.onclick = () => { st.speed = +b.dataset.v; $$("#speeds button").forEach((x) => x.classList.toggle("on", x === b)); });
function updateTicks() {
  const p = st.paths, el = $("#ticks");
  if (!p || !st.plan) { el.innerHTML = ""; return; }
  el.innerHTML = st.plan.pauses.filter((q) => q[0] > 0).map(([k]) => `<i style="left:${(100 * p.cum[Math.min(k, p.n) - 1] / p.total).toFixed(2)}%"></i>`).join("");
}
window.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); saveProject(e.shiftKey); return; }   // also while typing
  if (e.target.matches?.("input, select, textarea")) return;
  if (e.code === "Space" && st.mode === "preview") { e.preventDefault(); st.playing ? stopPlay() : play(); }
  if (e.key === "o" && e.ctrlKey) { e.preventDefault(); openPart(false); }
  const k = e.key.toLowerCase();
  if (e.ctrlKey && k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
  if (e.ctrlKey && (k === "y" || (k === "z" && e.shiftKey))) { e.preventDefault(); redo(); }
});

// ------------------------------------------------------------------ undo and redo
// Every change the designer makes to the job (settings, tabs, text and logos, chamfer edges, add-ons, material) ends
// in replan(), which records the options; the first plan of a part records where it started. Undo puts the options
// back and plans again. Typing a number in a box keeps the box's own browser undo (the keys are left to it there).
const hist = { stack: [], i: -1, part: null, busy: false, at: 0 };
function record() {
  if (hist.busy || !st.part) return;
  const s = JSON.stringify(st.opts), now = performance.now();
  if (hist.part !== st.part.path) { Object.assign(hist, { stack: [s], i: 0, part: st.part.path }); undoButtons(); return; }
  if (hist.stack[hist.i] === s) return;
  hist.stack = hist.stack.slice(0, hist.i + 1);
  // a run of quick changes to the same thing (holding a ▴ arrow, dragging) is one step
  if (now - hist.at < 600 && hist.i > 0 && sameShape(hist.stack[hist.i], s)) hist.stack[hist.i] = s;
  else hist.stack.push(s);
  if (hist.stack.length > 200) hist.stack.shift();
  hist.i = hist.stack.length - 1; hist.at = now;
  undoButtons();
}
function sameShape(a, b) {   // the same keys changed as in the step before: only numbers differ
  return a.replace(/-?\d+(\.\d+)?(e-?\d+)?/g, "#") === b.replace(/-?\d+(\.\d+)?(e-?\d+)?/g, "#");
}
function goto(i) {
  if (i < 0 || i >= hist.stack.length || i === hist.i) return;
  const was = st.opts.material;
  hist.i = i; hist.busy = true;
  try {
    st.opts = JSON.parse(hist.stack[i]);
    st.expanded = -1;
    renderPresets(); drawEdges();
    if (st.opts.material !== was) loadBody();
    savePrefs(); replan();
  } finally { hist.busy = false; }
  undoButtons();
}
function undo() { const i = hist.i; goto(i - 1); if (hist.i !== i) toast("Undone"); }
function redo() { goto(hist.i + 1); }
function undoButtons() {
  $("#undobtn").disabled = hist.i <= 0;
  $("#redobtn").disabled = hist.i >= hist.stack.length - 1;
}
$("#undobtn").onclick = undo;
$("#redobtn").onclick = redo;

// ------------------------------------------------------------------ the sensor and the marks, dragged in the view
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2(), plane0 = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
function onPlane(e) {
  const r = canvas.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  const p = new THREE.Vector3();
  return ray.ray.intersectPlane(plane0, p) ? p : null;
}
function drawSensor() {
  clear(layers.sensor);
  const pr = st.plan?.probe;
  // with the bed sensor the operator zeroes on the stock before starting: no puck spot in the plan, ever
  if (!pr || st.plan?.sensor?.mode === "bed") return;
  const [x, y, r] = pr;
  const disc = new THREE.Mesh(new THREE.CircleGeometry(r, 48), new THREE.MeshBasicMaterial({ color: 0x1d9e75, transparent: true, opacity: 0.22, depthWrite: false }));
  disc.position.set(x, y, 0.08);
  const ring = new THREE.Mesh(new THREE.RingGeometry(r - 0.6, r, 64), new THREE.MeshBasicMaterial({ color: 0x0f6e56 }));
  ring.position.set(x, y, 0.1);
  const puck = new THREE.Mesh(new THREE.CylinderGeometry(r * 0.35, r * 0.35, 4, 32), new THREE.MeshStandardMaterial({ color: 0x2b2f35, metalness: 0.6, roughness: 0.35 }));
  puck.rotation.x = Math.PI / 2; puck.position.set(x, y, 2);
  layers.sensor.add(disc, ring, puck);
  layers.sensor.userData = { x, y, r };
  redraw();
}
// Each text or logo is a box you drag to move (its letters ride along), with a round handle above it you drag to
// turn it (5 degree steps; hold Shift for any angle). The letters draw over the part, never sunk into it.
function drawMarks() {
  clear(layers.marks);
  const paths = st.plan?.mark_paths || [];
  let next = 0;
  for (const m of st.plan?.marks || []) {
    const a = THREE.MathUtils.degToRad(m.angle || 0), ca = Math.cos(a), sa = Math.sin(a);
    const g = new THREE.PlaneGeometry(m.w + 2, m.h + 2);
    const box = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0x3c3489, transparent: true, opacity: 0.06, depthWrite: false, depthTest: false }));
    box.position.set(m.x, m.y, 0.15); box.rotation.z = a; box.renderOrder = 6;
    box.userData = { mark: m };
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineDashedMaterial({ color: 0x3c3489, dashSize: 1.2, gapSize: 1.2, transparent: true, opacity: 0.55, depthTest: false }));
    edges.computeLineDistances(); edges.renderOrder = 7; box.add(edges); box.userData.edges = edges;
    // the letters, in the box's own frame so they move and turn with it
    const lines = [];
    const loc = ([px, py]) => { const dx = px - m.x, dy = py - m.y; return [dx * ca + dy * sa, -dx * sa + dy * ca]; };
    for (const [pts, closed] of paths.slice(next, next + (m.n_paths ?? paths.length))) {
      const q = pts.map(loc);
      for (let k = 0; k + 1 < q.length; k++) lines.push(q[k][0], q[k][1], -0.09, q[k + 1][0], q[k + 1][1], -0.09);
      if (closed && q.length > 2) lines.push(q.at(-1)[0], q.at(-1)[1], -0.09, q[0][0], q[0][1], -0.09);
    }
    next += m.n_paths ?? paths.length;
    if (lines.length) {
      const lg = new THREE.BufferGeometry(); lg.setAttribute("position", new THREE.Float32BufferAttribute(lines, 3));
      const l = new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color: 0x3c3489, depthTest: false }));
      l.renderOrder = 8; l.raycast = () => {}; box.add(l);
    }
    // the turn handle: a stalk up from the box's top edge and a round knob
    const up = m.h / 2 + 1 + 4;
    const stalk = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, m.h / 2 + 1, 0), new THREE.Vector3(0, up, 0)]),
      new THREE.LineBasicMaterial({ color: 0x1f6fd1, depthTest: false }));
    stalk.renderOrder = 7; stalk.raycast = () => {}; box.add(stalk);
    const knob = new THREE.Mesh(new THREE.CircleGeometry(1.3, 24), new THREE.MeshBasicMaterial({ color: 0x1f6fd1, depthTest: false }));
    knob.position.set(0, up, 0.01); knob.renderOrder = 9; knob.userData = { turn: true, mark: m, box };
    box.add(knob);
    layers.marks.add(box);
  }
  redraw();
}
function hitTurn() {
  ray.setFromCamera(ndc, camera);
  const knobs = layers.marks.children.flatMap((b) => b.children.filter((c) => c.userData?.turn));
  const h = ray.intersectObjects(knobs, false)[0];
  return h ? h.object : null;
}
const markAddon = (m) => st.opts.addons.filter((a) => (a.kind === "text" || a.kind === "logo") && !a.hidden)[m.i];
// ---- geometry helpers for picking on the drawing (job coordinates, mm)
function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L)) : 0;
  return [Math.hypot(ax + t * dx - px, ay + t * dy - py), t];
}
function edgePoints(shape, e) {
  const [i0, i1] = shape.edges[e], P = shape.points, n = P.length, out = [];
  if (i0 === i1) return [...P.slice(i0), ...P.slice(0, i0 + 1)];   // one edge is the whole loop (a circle)
  for (let i = i0; ; i = (i + 1) % n) { out.push(P[i]); if (i === i1 || out.length > n) break; }
  return out;
}
function project(shape, x, y) {   // closest point on a closed shape: [x, y, dist, index, tangent angle]
  const P = shape.points; let best = null;
  for (let i = 0; i < P.length; i++) {
    const a = P[i], b = P[(i + 1) % P.length];
    const [d, t] = segDist(x, y, a[0], a[1], b[0], b[1]);
    if (!best || d < best[2]) best = [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), d, i, Math.atan2(b[1] - a[1], b[0] - a[0])];
  }
  return best;
}
function pickTol(px = 9) {   // px pixels in mm at the view's centre
  const d = camera.position.distanceTo(controls.target);
  return px * 2 * d * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) / Math.max(1, canvas.clientHeight);
}
function nearestEdge(x, y) {
  let best = null;
  for (const s of st.plan?.shapes || []) s.edges.forEach((_, e) => {
    const pts = edgePoints(s, e);
    for (let k = 0; k + 1 < pts.length; k++) {
      const [d] = segDist(x, y, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1]);
      if (!best || d < best.d) best = { key: s.key, e, d };
    }
  });
  return best && best.d <= pickTol() ? { key: best.key, e: best.e } : null;
}
function ribbon(pts, w, z, colour, closed = false) {
  const pos = [];
  const P = closed ? [...pts, pts[0]] : pts;
  for (let k = 0; k + 1 < P.length; k++) {
    const [ax, ay] = P[k], [bx, by] = P[k + 1];
    const L = Math.hypot(bx - ax, by - ay) || 1, nx = -(by - ay) / L * w / 2, ny = (bx - ax) / L * w / 2;
    pos.push(ax + nx, ay + ny, z, bx + nx, by + ny, z, bx - nx, by - ny, z, ax + nx, ay + ny, z, bx - nx, by - ny, z, ax - nx, ay - ny, z);
  }
  const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  return new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: colour, side: THREE.DoubleSide, transparent: true, opacity: 0.9, depthTest: false }));
}
// Chamfer edges: every edge is chamfered by default. The first chamfer keeps a list of the edges taken OFF
// (a.off); each further width ("+ Another width") lists the edges it takes (a.edges), which leave the first one.
// While "Choose edges" is on, a click on an edge in the view takes it off or puts it back, whole.
const activeChamfer = () => { const a = st.opts.addons[st.expanded]; return a && a.kind === "chamfer" && !a.hidden && st.tab === "ops" ? a : null; };
const chamferList = () => st.opts.addons.filter((a) => a.kind === "chamfer" && !a.hidden);
const allEdges = () => (st.plan?.shapes || []).flatMap((s) => s.edges.map((_, e) => [s.key, e]));
const ek = (k, e) => k + "#" + e;
function chamferEdges(a) {   // the [key, edge] pairs this chamfer cuts
  const list = chamferList();
  if (a !== list[0]) return a.edges || [];
  const taken = new Set(list.slice(1).flatMap((b) => (b.edges || []).map(([k, e]) => ek(k, e))));
  const off = new Set((a.off || []).map(([k, e]) => ek(k, e)));
  return allEdges().filter(([k, e]) => !off.has(ek(k, e)) && !taken.has(ek(k, e)));
}
function toggleChamferEdge(a, key, e) {
  const list = chamferList(), has = (l) => (l || []).findIndex(([k, x]) => k === key && x === e);
  if (a === list[0]) {
    const other = list.slice(1).find((b) => has(b.edges) >= 0);
    if (other) { other.edges.splice(has(other.edges), 1); a.off = (a.off || []).filter(([k, x]) => !(k === key && x === e)); return true; }
    a.off = a.off || [];
    const i = has(a.off);
    if (i >= 0) { a.off.splice(i, 1); return true; }
    a.off.push([key, e]); return false;
  }
  a.edges = a.edges || [];
  const i = has(a.edges);
  if (i >= 0) { a.edges.splice(i, 1); return false; }
  a.edges.push([key, e]); return true;
}

// ---- tabs (G-code Maker's way): hover an outline or window edge to see where a tab would go, click to drop it,
// click a tab to take it off; a tab can still be dragged along its line
function drawTabs() {
  clear(layers.tabs);
  if (!isFlat()) return;
  for (const s of st.plan?.shapes || []) {
    if (!s.can_tab) continue;
    s.tabs.forEach((t, ti) => {
      const pr = project(s, t[0], t[1]);
      const len = Math.max(2, s.tab_len || 4);
      const m = tabMesh(len, 0xe8912d);
      m.position.set(pr[0], pr[1], 0.4); m.rotation.z = pr[4];
      m.userData = { tab: true, key: s.key, ti };
      layers.tabs.add(m);
    });
  }
  redraw();
}
function tabMesh(len, colour, opacity = 1) {
  return new THREE.Mesh(new THREE.BoxGeometry(len, 2.2, 0.8), new THREE.MeshStandardMaterial({ color: colour, roughness: 0.5, transparent: opacity < 1, opacity, depthTest: opacity >= 1 }));
}
let ghostAt = null;   // {key, x, y}: where a click would drop a tab
function tabSpot(x, y) {   // the nearest point on a shape that can take a tab, within a few pixels
  if (!isFlat() || st.mode !== "prepare" || activeChamfer()) return null;
  let best = null;
  for (const s of st.plan?.shapes || []) if (s.can_tab) { const pr = project(s, x, y); if (!best || pr[2] < best.pr[2]) best = { s, pr }; }
  return best && best.pr[2] <= pickTol(12) ? best : null;
}
function drawGhost(b) {
  clear(layers.ghost);
  ghostAt = b ? { key: b.s.key, x: b.pr[0], y: b.pr[1] } : null;
  if (b) {
    const e = b.s.edges.findIndex(([i0, i1]) => i0 === i1 || (i0 <= i1 ? b.pr[3] >= i0 && b.pr[3] < i1 : b.pr[3] >= i0 || b.pr[3] < i1));
    if (e >= 0) layers.ghost.add(ribbon(edgePoints(b.s, e), 0.9, 0.12, 0xe8912d));
    const m = tabMesh(Math.max(2, b.s.tab_len || 4), 0xe8912d, 0.45);
    m.position.set(b.pr[0], b.pr[1], 0.4); m.rotation.z = b.pr[4];
    layers.ghost.add(m);
  }
  redraw();
}
function setTabs(key, list) {
  st.opts.tabs.manual = st.opts.tabs.manual || {};
  st.opts.tabs.manual[key] = list.map(([x, y]) => [+x.toFixed(2), +y.toFixed(2)]);
  renderHold();   // shows "Put the tabs back"
  replan();
}

// ---- chamfered edges
function drawEdges(hover = null) {
  clear(layers.edges);
  if (!isFlat() || !st.plan?.shapes) return;
  const act = activeChamfer();
  const shp = (key) => st.plan.shapes.find((x) => x.key === key);
  const cut = new Set();
  chamferList().forEach((a) => {
    const col = a === act ? 0x6c4fd6 : 0x8f80c9;
    for (const [key, e] of chamferEdges(a)) {
      const s = shp(key);
      if (s && s.edges[e]) { layers.edges.add(ribbon(edgePoints(s, e), 0.9, 0.12, col)); cut.add(ek(key, e)); }
    }
  });
  if (act) {   // while choosing: the edges without a chamfer show thin and grey, ready to be clicked back
    for (const [key, e] of allEdges()) if (!cut.has(ek(key, e))) {
      const s = shp(key);
      if (s && s.edges[e]) layers.edges.add(ribbon(edgePoints(s, e), 0.35, 0.12, 0x9a9a9a));
    }
  }
  if (hover && act) {
    const s = st.plan.shapes.find((x) => x.key === hover.key);
    if (s) layers.edges.add(ribbon(edgePoints(s, hover.e), 1.4, 0.14, 0x1f6fd1));
  }
  redraw();
}

// ---- the tool sensor must sit on stock no bit touches
function sensorClear(x, y, r) {
  // a block is the stock: the sensor must sit on it. A flat part's sheet is only the least it needs, worked out
  // around the sensor itself, so it grows to hold the sensor wherever it goes (Lev 2026-11-15: the box shrank
  // with every drag and the sensor couldn't go further out)
  const k = st.plan?.stock;
  if (k && !isFlat() && (x - r < k.x0 || y - r < k.y0 || x + r > k.x0 + k.w || y + r > k.y0 + k.h)) return false;
  if (!puckAfterCuts()) return true;   // the puck zeroes the first bit only, before anything is cut
  const p = st.paths; if (!p) return true;
  for (let k = 0; k < p.n; k++) {
    const o = k * 8; if (p.segs[o + 7] > 0.5 || Math.min(p.segs[o + 2], p.segs[o + 5]) > -0.001) continue;
    const tr = (toolForStage(p.segs[o + 6] | 0)?.tool_d || 4) / 2;
    const [d] = segDist(x, y, p.segs[o], p.segs[o + 1], p.segs[o + 3], p.segs[o + 4]);
    if (d < r + tr + 0.5) return false;
  }
  return true;
}
function puckAfterCuts() { const s = st.plan?.sensor; return !(s && s.bed_changes > 0); }
function tintSensor(ok) {
  layers.sensor.children.forEach((c) => {
    if (c.geometry.type === "RingGeometry") c.material.color.set(ok ? 0x0f6e56 : 0xd22f27);
    if (c.geometry.type === "CircleGeometry") c.material.color.set(ok ? 0x1d9e75 : 0xe24b4a);
  });
}

let drag = null, hoverEdge = null;
function hitTab() { ray.setFromCamera(ndc, camera); const h = ray.intersectObjects(layers.tabs.children, false)[0]; return h ? h.object : null; }
function hitMark() { ray.setFromCamera(ndc, camera); const h = ray.intersectObjects(layers.marks.children.filter((o) => !o.userData.art), false)[0]; return h ? h.object : null; }
canvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !st.plan || st.mode !== "prepare") return;
  const p = onPlane(e); if (!p) return;
  const s = layers.sensor.userData;
  const tab = hitTab();
  const knob = hitTurn();
  if (knob) {
    drag = { kind: "turn", obj: knob.userData.box, mark: knob.userData.mark };
  } else if (s && s.r && Math.hypot(p.x - s.x, p.y - s.y) <= s.r) {
    drag = { kind: "sensor", dx: p.x - s.x, dy: p.y - s.y, ok: true };
  } else if (tab) {
    drag = { kind: "tab", obj: tab, key: tab.userData.key, ti: tab.userData.ti, x0: e.clientX, y0: e.clientY };
  } else if (activeChamfer() && hoverEdge) {
    const on = toggleChamferEdge(activeChamfer(), hoverEdge.key, hoverEdge.e);
    toast(on ? "Chamfer put back on this edge" : "Chamfer taken off this edge", 1600);
    renderAddons(); drawEdges(hoverEdge); replan();
    return;
  } else {
    const m = hitMark();
    if (m) drag = { kind: "mark", mark: m.userData.mark, obj: m, dx: p.x - m.position.x, dy: p.y - m.position.y };
    else {
      const b = tabSpot(p.x, p.y);
      if (b) { drawGhost(null); setTabs(b.s.key, [...b.s.tabs, [b.pr[0], b.pr[1]]]); toast("Tab added"); return; }
    }
  }
  if (drag) { controls.enabled = false; canvas.setPointerCapture(e.pointerId); canvas.style.cursor = "grabbing"; }
});
canvas.addEventListener("pointermove", (e) => {
  const p = onPlane(e);
  if (!drag) {
    if (!p || !st.plan || st.mode !== "prepare") return;
    const s = layers.sensor.userData;
    const over = s && s.r && Math.hypot(p.x - s.x, p.y - s.y) <= s.r;
    const tab = hitTab();
    const boxes = layers.marks.children.filter((o) => !o.userData.art);
    const m = hitMark(), knob = hitTurn();
    boxes.forEach((b) => (b.userData.edges.material.opacity = (m && b === m) || (knob && knob.userData.box === b) ? 1 : 0.55));
    if (knob) { canvas.style.cursor = "alias"; if (ghostAt) drawGhost(null); redraw(); return; }
    const he = activeChamfer() ? nearestEdge(p.x, p.y) : null;
    if (JSON.stringify(he) !== JSON.stringify(hoverEdge)) { hoverEdge = he; drawEdges(he); }
    const ts = over || tab || m ? null : tabSpot(p.x, p.y);
    if (ts || ghostAt) drawGhost(ts);
    canvas.style.cursor = tab ? "pointer" : over || m ? "grab" : he || ts ? "pointer" : activeChamfer() ? "crosshair" : "";
    redraw();
    return;
  }
  if (!p) return;
  if (drag.kind === "sensor") {
    const x = p.x - drag.dx, y = p.y - drag.dy;
    layers.sensor.children.forEach((c) => c.position.set(x, y, c.position.z));
    drag.ok = sensorClear(x, y, layers.sensor.userData.r); tintSensor(drag.ok);
  } else if (drag.kind === "tab") {
    const sh = st.plan.shapes.find((x) => x.key === drag.key);
    const pr = project(sh, p.x, p.y);
    drag.obj.position.set(pr[0], pr[1], 0.4); drag.obj.rotation.z = pr[4]; drag.to = [pr[0], pr[1]];
  } else if (drag.kind === "turn") {
    let deg = THREE.MathUtils.radToDeg(Math.atan2(p.y - drag.obj.position.y, p.x - drag.obj.position.x)) - 90;
    deg = e.shiftKey ? +deg.toFixed(1) : Math.round(deg / 5) * 5;
    deg = ((deg % 360) + 540) % 360 - 180;   // -180 .. 180
    drag.obj.rotation.z = THREE.MathUtils.degToRad(deg); drag.deg = deg;
    toast(`Turned ${deg}°`, 900);
  } else drag.obj.position.set(p.x - drag.dx, p.y - drag.dy, 0.15);
  redraw();
});
canvas.addEventListener("pointerup", (e) => {
  if (!drag) return;
  const p = onPlane(e);
  controls.enabled = true; canvas.style.cursor = "";
  if (drag.kind === "sensor") {
    if (!drag.ok || !p) {
      const s = layers.sensor.userData; layers.sensor.children.forEach((c) => c.position.set(s.x, s.y, c.position.z)); tintSensor(true);
      toast(puckAfterCuts() ? "The tool sensor has to sit on stock no end mill cuts, so every end mill is measured from the same top."
        : "The puck has to sit on the stock: it zeroes the first end mill on the stock's top.", 4200);
    } else {
      st.opts.sensor = { dx: +(p.x - drag.dx).toFixed(1), dy: +(p.y - drag.dy).toFixed(1) };
      replan("The tool sensor moved");
    }
  } else if (drag.kind === "tab") {
    const sh = st.plan.shapes.find((x) => x.key === drag.key);
    if (!drag.to || Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 4) {   // a click on a tab takes it off
      setTabs(sh.key, sh.tabs.filter((_, i) => i !== drag.ti)); toast("Tab removed");
    } else {
      const list = sh.tabs.map((t) => [...t]); list[drag.ti] = drag.to;
      setTabs(drag.key, list);
    }
  } else if (drag.kind === "turn") {
    const a = markAddon(drag.mark);
    if (a && drag.deg != null) { a.angle = drag.deg; renderAddons(); replan(); }
  } else if (p && drag.kind === "mark") {
    const a = markAddon(drag.mark);
    if (a) { a.x = +(p.x - drag.dx).toFixed(1); a.y = +(p.y - drag.dy).toFixed(1); renderAddons(); replan(); }
  }
  drag = null;
});
canvas.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  if (st.mode !== "prepare" || !st.plan?.shapes) return;
  onPlane(e);
  const tab = hitTab(); if (!tab) return;
  const sh = st.plan.shapes.find((x) => x.key === tab.userData.key);
  setTabs(sh.key, sh.tabs.filter((_, i) => i !== tab.userData.ti));
  toast("Tab removed");
});
canvas.addEventListener("dblclick", (e) => {
  const p = onPlane(e);
  if (st.mode === "prepare" && p && st.plan?.shapes && !activeChamfer()) {
    let best = null;
    for (const s of st.plan.shapes) if (s.can_tab) { const pr = project(s, p.x, p.y); if (!best || pr[2] < best.pr[2]) best = { s, pr }; }
    if (best && best.pr[2] <= pickTol(60)) return;   // (a click already put a tab there)   // a near miss on the outline: don't throw the view away
  }
  fit();
});

// ------------------------------------------------------------------ the left panel
// Laid out like Bambu Studio's Prepare sidebar: Machine, Material, Part, then Process: the finish preset, an
// Advanced switch, and three tabs: Operations (in the order they run) | Tabs & holding | Bits.
const DEF = { extra: 0.5, count: 3, len: 4, thick: 1, windows: 0 };   // presets: a changed value gets a mark and a reset arrow
const OP_ROWS = [
  { key: "surface", label: "Surfacing", opt: true },
  { key: "holes", label: "Holes and slots" },
  { key: "pockets", label: "Pockets" },
  { key: "outline", label: "Outline cut" },
  { key: "chamfer", label: "Chamfer", opt: true },
  { key: "edge", label: "Round the edges", opt: true },
  { key: "text", label: "Text", opt: true, card: "engrave" },
  { key: "logo", label: "Logo", opt: true, card: "engrave" },
];
// a 3D block: the engine's operations in run order, plus the optional ones (Surfacing, Chamfer, Text, Logo)
const ROWS3 = {
  surface: [{ key: "surface", label: "Surfacing", opt: true }], holes: [{ key: "holes", label: "Holes", need: true }],
  rough: [{ key: "rough", label: "Roughing", need: true }], finish_flat: [{ key: "finish_flat", label: "Finishing: flat floors", need: true }],
  finish_curved: [{ key: "finish_curved", label: "Finishing: curved", need: true }], finish_steep: [{ key: "finish_steep", label: "Finishing: steep walls", need: true }],
  rest: [{ key: "rest", label: "Rest: tight corners", need: true }], chamfer: [{ key: "chamfer", label: "Chamfer", opt: true }],
  engrave: [{ key: "text", label: "Text", opt: true, card: "engrave" }, { key: "logo", label: "Logo", opt: true, card: "engrave" }],
  cutout: [{ key: "cutout", label: "Cut-out", need: true }],
};
const DEF_ORDER3 = ["surface", "holes", "rough", "finish_flat", "finish_curved", "finish_steep", "rest", "chamfer", "engrave", "cutout"];
const FIN3 = ["finish_flat", "finish_curved", "finish_steep", "rest"], MOVE3 = ["holes", "chamfer", "engrave"];
function opOrder3() {   // every op key in run order: the plan's own order, the ones it doesn't have where they would go
  const have = (st.plan?.ops || []).map((c) => c.key);
  const want = [...(st.opts.order || have)].filter((k) => DEF_ORDER3.includes(k));
  for (const k of DEF_ORDER3) {
    if (want.includes(k)) continue;
    const di = DEF_ORDER3.indexOf(k), j = want.findIndex((x) => DEF_ORDER3.indexOf(x) > di);
    want.splice(j < 0 ? want.length : j, 0, k);
  }
  return [...new Set(want)];
}
function rows3d() { return opOrder3().flatMap((k) => ROWS3[k] || []); }
function units3(order) {   // the finishing ops move as one block
  const u = [];
  for (const k of order) { if (FIN3.includes(k) && u.length && u[u.length - 1] === "FIN") continue; u.push(FIN3.includes(k) ? "FIN" : k); }
  return u;
}
function canMove3(order, k, dir) {
  const u = units3(order), i = u.indexOf(k), j = i + dir;
  if (i < 0 || j < 0 || j >= u.length) return false;
  const n = u[j];
  return k === "holes" ? n === "rough" : n === "FIN" || n === "chamfer" || n === "engrave";
}
function moveOp3(order, k, dir) {
  if (!canMove3(order, k, dir)) return order;
  const u = units3(order), i = u.indexOf(k);
  [u[i], u[i + dir]] = [u[i + dir], u[i]];
  return u.flatMap((x) => x === "FIN" ? FIN3 : [x]);
}
const STRAT_NOTE = {
  fewest: "One flat end mill does every cut it can, surfacing included: slower, but the job never stops for an end mill change.",
  recommended: "An end mill change only where it saves more time than the change costs.",
  fastest: "The quickest end mill for every cut, however many end mill changes that takes. Best with an automatic tool changer.",
};
const ex = () => $("#expert").checked;
st.tab = "ops";

function isFlat() {
  return st.part?.kind === "2d" || !!st.part?.flat;   // a plate is cut the 2D way from sheet and from a block alike
}
const isStep = () => st.part?.kind === "3d";
function modelT() { return isStep() && st.part?.size ? +st.part.size[2].toFixed(3) : null; }
// Surfacing is ONE value, set in the Operations tab: how much comes off the top ("Take off") and so what is left
// ("Leaves" = stock - take off). A STEP part on thicker stock is always surfaced down to the model's height.
function surf() {
  const T = +st.opts.thickness, M = modelT();
  const add = st.opts.addons.find((a) => a.kind === "surface" && !a.hidden) || null;
  if (M != null && T > M + 0.004) return { on: true, locked: true, take: +(T - M).toFixed(3), leaves: M, add };
  if (!add) return { on: false, locked: false, take: 0, leaves: M != null ? Math.min(M, T) : T, add };
  const take = Math.max(0.01, Math.min(T - 0.1, st.opts.take ?? 0.2));
  return { on: true, locked: false, take: +take.toFixed(3), leaves: +(T - take).toFixed(3), add };
}
function partT() { return surf().leaves; }   // the finished part's thickness

// ---- number boxes (with ▴▾ arrows), as a labelled cell (numBox) or a label-left row (numRow)
function nbHTML(k, v, step, unit, min = null) {
  return `<span class="nb" data-step="${step}"><input data-k="${k}" data-n ${min !== null ? `data-min="${min}"` : ""} value="${v ?? ""}" inputmode="decimal"><em>${unit}</em>`
    + `<span class="arr"><button data-dir="1" tabindex="-1" aria-label="More">▴</button><button data-dir="-1" tabindex="-1" aria-label="Less">▾</button></span></span>`;
}
function numBox(label, k, v, step, unit, min = null) {
  return `<label class="nbl"><span>${label}</span>${nbHTML(k, v, step, unit, min)}</label>`;
}
function numRow(label, k, v, step, unit, min = null, def = null) {
  const chg = def !== null && Math.abs(+v - def) > 1e-9;
  return `<div class="kv${chg ? " chg" : ""}"><span>${label}</span>`
    + (chg ? `<button class="rst" data-rst="${k}" title="Back to ${def}${unit ? " " + unit : ""}">↺</button>` : "")
    + `${nbHTML(k, v, step, unit, min)}</div>`;
}
function wireNbs(root, set) {   // every number box under root: arrows, typing, the wheel and the arrow keys
  root.querySelectorAll(".nb").forEach((nb) => {
    const inp = nb.querySelector("input");
    const bump = (dir) => { inp.value = String(+((parseFloat(String(inp.value).replace(",", ".")) || 0) + dir * +nb.dataset.step).toFixed(3)); set(inp.dataset.k, clampNum(inp), inp); };
    nb.querySelectorAll("[data-dir]").forEach((b) => b.onclick = (e) => { e.preventDefault(); if (!inp.disabled) bump(+b.dataset.dir); });
    inp.onchange = () => set(inp.dataset.k, clampNum(inp), inp);
    inp.addEventListener("wheel", (e) => { if (document.activeElement !== inp) return; e.preventDefault(); bump(e.deltaY < 0 ? 1 : -1); }, { passive: false });
    inp.addEventListener("keydown", (e) => { if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); bump(e.key === "ArrowUp" ? 1 : -1); } });
  });
}
function clampNum(inp) {
  let v = parseFloat(String(inp.value).replace(",", ".")) || 0;
  if (inp.dataset.min !== undefined) v = Math.max(+inp.dataset.min, v);
  inp.value = String(+v.toFixed(3));
  return v;
}

// ---- Machine, Material, Part
function renderPresets() {
  const info = st.info;
  $("#material").innerHTML = Object.keys(info.materials).map((k) => `<option ${k === st.opts.material ? "selected" : ""}>${esc(k)}</option>`).join("");
  $("#matsw").style.background = `rgb(${matRGB().map((v) => Math.round(v * 255)).join(",")})`;
  $("#matnote").textContent = info.materials[st.opts.material]?.about || "";
  $("#finishes").innerHTML = Object.keys(info.finishes).map((k) => `<button data-f="${k}" class="${k === st.opts.finish ? "on" : ""}">${k}</button>`).join("");
  $$("#finishes button").forEach((b) => b.onclick = () => { st.opts.finish = b.dataset.f; renderPresets(); savePrefs(); replan(); });
  $("#finishnote").textContent = info.finishes[st.opts.finish]?.about || "";
  renderThick();
  renderPanel();
}
$("#material").onchange = () => {
  st.opts.material = $("#material").value;
  const m = st.info.materials[st.opts.material];
  if (st.part?.kind === "2d" && m.kind === "sheet") st.opts.thickness = m.thickness;
  renderPresets(); savePrefs(); replan(); loadBody();
};
function renderThick() {
  $("#thickrow").hidden = !st.part;
  $("#thickbox").innerHTML = nbHTML("thickness", st.opts.thickness, 0.1, "mm", 0.1);
  wireNbs($("#thickrow"), (k, v) => { if (v > 0) { st.opts.thickness = v; renderThick(); renderOps(); savePrefs(); replan(); } });
  const M = modelT(), pi = $("#partinfo");
  pi.hidden = M == null;
  if (M != null) pi.textContent = `Part ${M} mm thick, from the model.`
    + (st.opts.thickness < M - 0.004 ? ` The stock is thinner, so the part comes out ${+(M - st.opts.thickness).toFixed(3)} mm thinner.` : "");
}
$("#up").onchange = async () => { await api("up", { up: $("#up").value }); st.gen = -1; poll(); };

function savePrefs() { api("prefs", { material: st.opts.material, finish: st.opts.finish, thickness: st.opts.thickness, extra: st.opts.extra, tabs: { count: st.opts.tabs.count, len: st.opts.tabs.len, thick: st.opts.tabs.thick, windows: st.opts.tabs.windows || 0 }, strategy: st.opts.strategy, params: st.opts.params, expert: $("#expert").checked }).catch(() => {}); }

// ---- Process: the tabs
$$("#ptabs button").forEach((b) => b.onclick = () => { st.tab = b.dataset.tab; renderPanel(); });
function renderPanel() {
  $$("#ptabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === st.tab));
  for (const t of ["ops", "hold", "bits", "set"]) $("#pane-" + t).hidden = t !== st.tab;
  renderOps(); renderHold(); renderBits(); renderSettings(); renderPresetRow();
}
function refreshPanel() {   // a new plan: redraw the panel, unless the designer is typing in it (then the chips only)
  const a = document.activeElement;
  if (a && a.closest?.("#left") && a.matches("input, textarea")) { updateChips(); renderBits(); }
  else renderPanel();
}
const cardMap = () => Object.fromEntries((st.plan?.ops || []).map((c) => [c.key, c]));
function chip(key) {
  const c = cardMap()[key];
  if (!c) return `<span class="chip none" data-chip="${key}"></span>`;
  return `<span class="chip${c.owned === false ? " buy" : ""}${c.picked ? " mine" : ""}" data-chip="${key}" title="${esc(c.bit)}${c.owned === false ? " (to buy)" : ""}${c.picked ? " (your pick)" : ""}">${esc(c.bit_label)}</span>`;
}
function updateChips() { $$("[data-chip]").forEach((el) => (el.outerHTML = chip(el.dataset.chip))); }

// ---- Operations: in the order they run; the optional ones are ticked on and off, their settings right under them
function renderOps() {
  const box = $("#pane-ops");
  const o = st.opts, cards = cardMap();
  const S = surf(), block = !!st.part && !isFlat();
  if (block && !st.plan) { box.innerHTML = `<p class="note">The operations appear once the toolpaths are made.</p>`; return; }
  const order = block ? opOrder3().filter((k) => cards[k]) : null;
  let h = "";
  for (const r of block ? rows3d() : OP_ROWS) {
    if (r.key === "holes" && st.plan && !cards.holes) continue;
    if (r.key === "pockets" && !cards.pockets) continue;
    if (r.need && !cards[r.key]) continue;
    const inst = o.addons.map((a, i) => [a, i]).filter(([a]) => a.kind === r.key && !a.hidden);
    const lock = r.key === "surface" && S.locked;
    const on = !r.opt || inst.length > 0 || lock;
    const head = r.opt
      ? `<label class="chk" ${lock ? 'title="On because the stock is thicker than the model"' : ""}><input type="checkbox" data-tog="${r.key}" ${on ? "checked" : ""} ${lock ? "disabled" : ""}><span>${r.label}</span></label>`
      : `<span class="always" title="Always cut"></span><span class="opn">${r.label}</span>`;
    const body = on ? opBody(r, inst, S) : "";
    const ck = r.card || r.key, mv = block && on && MOVE3.includes(ck) && cards[ck];
    const arrows = mv ? `<span class="mv"><button data-mv="${ck}" data-dir="-1" title="Earlier" ${canMove3(order, ck, -1) ? "" : "disabled"}>▴</button><button data-mv="${ck}" data-dir="1" title="Later" ${canMove3(order, ck, 1) ? "" : "disabled"}>▾</button></span>` : "";
    const mins = block && on && cards[ck] ? `<span class="t">${fmtLong(cards[ck].minutes)}</span>` : "";
    h += `<div class="op${on ? " on" : ""}" data-row="${r.key}"><div class="oph">${head}${mins}${arrows}${on ? chip(ck) : ""}</div>${body ? `<div class="opb">${body}</div>` : ""}</div>`;
  }
  if (block) h += `<p class="note">The arrows move Holes before or after the roughing, and the chamfer and text before or after the finishing.</p>`;
  box.innerHTML = h;
  box.querySelectorAll("[data-mv]").forEach((b) => b.onclick = () => { o.order = moveOp3(order, b.dataset.mv, +b.dataset.dir); renderOps(); replan(); });
  box.querySelectorAll("[data-tog]").forEach((cb) => cb.onchange = () => toggleOp(cb.dataset.tog, cb.checked));
  box.querySelectorAll("[data-more]").forEach((b) => b.onclick = () => addOp(b.dataset.more));
  const outl = box.querySelector('[data-row="outline"]');
  if (outl) {
    wireNbs(outl, (k, v) => { o.extra = v; savePrefs(); renderOps(); replan(); });
    outl.querySelectorAll("[data-rst]").forEach((b) => b.onclick = () => { o.extra = DEF.extra; savePrefs(); renderOps(); replan(); });
  }
  const so = box.querySelector("[data-surfopts]");
  if (so) so.onclick = () => addOp("surface");
  const sn = box.querySelector(".surfnums");
  if (sn) {
    const M = modelT(), T = +o.thickness;
    sn.querySelector('[data-k="leaves"]').disabled = S.locked;
    wireNbs(sn, (k, v) => {
      if (S.locked) { if (k === "take") o.thickness = +(M + Math.max(0, v)).toFixed(3); }
      else o.take = +Math.max(0.01, Math.min(T - 0.1, k === "take" ? v : T - v)).toFixed(3);
      renderThick(); renderOps(); savePrefs(); replan();
    });
  }
  box.querySelectorAll(".inst").forEach((el) => {
    const i = +el.dataset.i, a = o.addons[i];
    wireNbs(el, (k, v) => { a[k] = v; replan(); });
    el.querySelectorAll("input.f[data-k], select[data-k]").forEach((inp) => inp.onchange = () => { a[inp.dataset.k] = inp.value; replan(); });
    el.querySelectorAll("[data-seg]").forEach((b) => b.onclick = () => { a[b.dataset.seg] = b.dataset.v; renderOps(); replan(); });
    el.querySelectorAll("[data-rst]").forEach((b) => b.onclick = () => { delete a[b.dataset.rst]; renderOps(); replan(); });
    const chAll = el.querySelector("[data-chall]");
    if (chAll) chAll.onclick = () => {   // every edge back on this (the first) chamfer
      a.off = []; chamferList().slice(1).forEach((b) => (b.edges = []));
      renderOps(); drawEdges(); replan();
    };
    const chNone = el.querySelector("[data-chnone]");
    if (chNone) chNone.onclick = () => {
      if (a === chamferList()[0]) { a.off = allEdges(); chamferList().slice(1).forEach((b) => (b.edges = [])); }
      else a.edges = [];
      renderOps(); drawEdges(); replan();
    };
    const ctr = el.querySelector("[data-centre]");
    if (ctr) ctr.onclick = () => { const c = drawingCentre(); a.x = c[0]; a.y = c[1]; renderOps(); replan(); };
    const pick = el.querySelector("[data-pick]");
    if (pick) pick.onclick = () => { st.expanded = st.expanded === i ? -1 : i; renderOps(); drawEdges(); };
    const del = el.querySelector("[data-del]");
    if (del) del.onclick = () => {
      o.addons.splice(i, 1);
      st.expanded = st.expanded === i ? -1 : st.expanded > i ? st.expanded - 1 : st.expanded;
      renderOps(); drawEdges(); replan();
    };
  });
}
function opBody(r, inst, S) {
  const fonts = st.info?.fonts || [], logos = st.info?.logos || [];
  const many = inst.length > 1;
  const del = `<button class="x" data-del title="Remove this one">✕</button>`;
  if (r.key === "outline") return numRow("Cut past the bottom", "extra", st.opts.extra, 0.1, "mm", 0, DEF.extra);
  if (r.key === "surface") {
    const a = S.add, i = a ? st.opts.addons.indexOf(a) : -1, M = modelT();
    let h = `<div class="g2 surfnums">${numBox("Take off", "take", S.take, 0.05, "mm", 0)}${numBox("Leaves", "leaves", S.leaves, 0.05, "mm", 0.1)}</div>`;
    const sseg = (k, v0, list) => `<div class="mini-seg w3">${list.map(([v, l, tip]) => `<button data-seg="${k}" data-v="${v}" class="${(a[k] ?? v0) === v ? "on" : ""}" title="${esc(tip)}">${l}</button>`).join("")}</div>`;
    const ar = a ? a.area ?? "part" : "part";
    h += a ? `<div class="inst" data-i="${i}">
        ${isFlat() ? `<div class="kv"><span>Area</span>${sseg("area", "part", [["part", "Part", "Only the part and the strip the outline end mill cuts beside it: the quickest"], ["around", "Around", "The part's shape grown by the margin: a clean border round the part"], ["rect", "Rectangle", "A rectangle round the part plus the margin"]])}</div>
        <div class="kv"><span>Pattern</span>${sseg("pattern", "zig", [["zig", "Zigzag", "Rows back and forth: the quickest"], ["one", "One way", "Every row the same way (climb or conventional all along): an even finish"], ["rings", "Rings", "Rings from the edge inwards, following the area's shape: a clean edge, no row ends"]])}</div>` : ""}
        ${numRow("Stepover", "over", a.over ?? (surfBit(cardMap().surface?.bit) ? 70 : 45), 5, "%", 5, surfBit(cardMap().surface?.bit) ? 70 : 45)}${!isFlat() || ar !== "part" ? numRow("Margin round the part", "margin", a.margin ?? 5, 1, "mm", 0, 5) : ""}</div>`
      : `<button class="lnk" data-surfopts>Area, pattern and stepover…</button>`;
    const note = S.locked ? `On because the stock is thicker than the model: the top ${S.take} mm is faced off first, leaving the model's ${M} mm.`
      : M != null ? `A skim off the top: the part ends up ${S.take} mm thinner than the model.`
      : "Faced off the top before anything is cut. Through-cuts still go to the stock bottom.";
    return h + `<p class="note tight${M != null && !S.locked ? " warn" : ""}">${note}</p>`;
  }
  if (r.key === "edge") return inst.map(([a, i]) => `<div class="inst" data-i="${i}">${numRow("Radius", "size", a.size, 0.1, "mm", 0.1)}</div>`).join("");
  if (r.key === "chamfer" && !isFlat()) return inst.map(([a, i]) => `<div class="inst" data-i="${i}">${numRow("Width", "width", a.width, 0.1, "mm", 0.1)}</div>`).join("")
    + `<p class="note tight">Along every rim that breaks the top face: the cavity, the holes and the outline.</p>`;
  if (r.key === "chamfer") return inst.map(([a, i], j) => {
    const act = i === st.expanded, main = j === 0 || a === chamferList()[0];
    const n = chamferEdges(a).length, M = allEdges().length;
    const count = main ? (M ? (n === M ? `All ${M} edges` : `${n} of ${M} edges`) : "Every edge")
      : `${n} edge${n === 1 ? "" : "s"} at this width`;
    const how = main ? "Click an edge in the view to take its chamfer off; click it again to put it back."
      : "Click edges in the view to give them this width; click again to give them back.";
    return `<div class="inst${act ? " active" : ""}" data-i="${i}">${many ? del : ""}${numRow("Width", "width", a.width, 0.1, "mm", 0.1)}
      <div class="kv"><span class="lab">${count}</span><button class="lnk${act ? " on" : ""}" data-pick>${act ? "Done" : "Choose edges"}</button></div>
      ${act ? `<div class="kv">${main ? `<button class="lnk" data-chall>Select all</button><button class="lnk" data-chnone>Deselect all</button>` : `<button class="lnk" data-chnone>Clear</button>`}</div>
      <p class="note tight">${how}</p>` : ""}</div>`;
  }).join("") + `<button class="lnk" data-more="chamfer">+ Another width</button>`;
  const sel = (a, k, list) => `<select data-k="${k}">${list.map((f) => `<option ${f === a[k] ? "selected" : ""}>${esc(f)}</option>`).join("")}</select>`;
  const segs = (a, k, list) => `<div class="mini-seg">${list.map(([v, l]) => `<button data-seg="${k}" data-v="${esc(v)}" class="${a[k] === v ? "on" : ""}" title="${esc(v)}">${l}</button>`).join("")}</div>`;
  if (r.key === "text") return inst.map(([a, i]) => `<div class="inst" data-i="${i}">${many ? del : ""}
      <input class="f" data-k="text" value="${esc(a.text)}" placeholder="Type the text">
      <div class="g2">${sel(a, "font", fonts)}${segs(a, "cut", [["On the line", "Line"], ["Outline", "Outline"], ["Filled", "Filled"]])}</div>
      <div class="g3">${numBox("Height", "size", a.size, 0.5, "mm", 0.5)}${numBox("Depth", "depth", a.depth, 0.05, "mm", 0.01)}${numBox("Turn", "angle", a.angle, 15, "°")}</div>
      <div class="g3">${numBox("X", "x", +(a.x ?? 0).toFixed(1), 0.5, "mm")}${numBox("Y", "y", +(a.y ?? 0).toFixed(1), 0.5, "mm")}<button class="lnk" data-centre title="Put it in the middle of the part">Centre</button></div></div>`).join("")
    + `<p class="note tight">Drag the text in the view to move it; drag the round blue handle to turn it (Shift: any angle). X and Y are from the job zero.</p><button class="lnk" data-more="text">+ Another text</button>`;
  if (r.key === "logo") return inst.map(([a, i]) => `<div class="inst" data-i="${i}">${many ? del : ""}
      <div class="g2">${sel(a, "logo", logos)}${segs(a, "cut", [["Outline", "Outline"], ["Filled", "Filled"]])}</div>
      <div class="g3">${numBox("Width", "size", a.size, 1, "mm", 2)}${numBox("Depth", "depth", a.depth, 0.05, "mm", 0.01)}${numBox("Turn", "angle", a.angle, 15, "°")}</div>
      <div class="g3">${numBox("X", "x", +(a.x ?? 0).toFixed(1), 0.5, "mm")}${numBox("Y", "y", +(a.y ?? 0).toFixed(1), 0.5, "mm")}<button class="lnk" data-centre title="Put it in the middle of the part">Centre</button></div></div>`).join("")
    + `<p class="note tight">Drag the logo in the view to move it; drag the round blue handle to turn it (Shift: any angle). X and Y are from the job zero.</p><button class="lnk" data-more="logo">+ Another logo</button>`;
  return "";
}
function addOp(kind) {
  const c = st.plan ? drawingCentre() : [null, null];
  const a = kind === "text" ? { kind, text: "AGILITE", font: st.info.fonts[0], size: 4, x: c[0], y: c[1], angle: 0, cut: "On the line", depth: 0.2 }
    : kind === "logo" ? { kind, logo: st.info.logos[0], size: 15, x: c[0], y: c[1], angle: 0, cut: "Outline", depth: 0.2, registered: false }
    : kind === "chamfer" ? { kind, width: 0.5, off: [], edges: [] }
    : kind === "edge" ? { kind, size: 0.5 } : { kind: "surface", over: 45, margin: 5 };
  const later = kind === "chamfer" && chamferList().length > 0;   // a further width starts with no edges: choose them
  st.opts.addons.push(a);
  if (later) st.expanded = st.opts.addons.length - 1;
  renderOps(); drawEdges(); replan();
}
function toggleOp(kind, on) {
  const list = st.opts.addons.filter((a) => a.kind === kind);
  if (on && !list.length) { addOp(kind); return; }
  list.forEach((a) => (a.hidden = !on));   // unticked: kept, just left out, so ticking brings it back as it was
  if (!on && kind === "chamfer") st.expanded = -1;
  renderOps(); drawEdges(); replan();
}
const renderAddons = () => renderOps();
function drawingCentre() {
  const o = st.plan?.outlines || [];
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const c of o) for (const [x, y] of c) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  if (Number.isFinite(x0)) return [+((x0 + x1) / 2).toFixed(1), +((y0 + y1) / 2).toFixed(1)];
  const k = st.plan?.stock;   // a block: the middle of the stock
  return k ? [+(k.x0 + k.w / 2).toFixed(1), +(k.y0 + k.h / 2).toFixed(1)] : [0, 0];
}

// ---- Tabs & holding
function renderHold() {
  const box = $("#pane-hold"), o = st.opts;
  let h = "";
  if (!st.part || isFlat()) {
    const win = (o.tabs.windows ?? 0) > 0;
    h += numRow("Tabs per outline", "count", o.tabs.count, 1, "", 0, DEF.count)
      + `<div class="kv${win ? " chg" : ""}"><span>Tabs on internal cuts</span><input type="checkbox" id="wintabs" ${win ? "checked" : ""} title="Hold the cut-out windows inside the part with tabs too, so the slugs don't fly loose"></div>`
      + (win ? numRow("Tabs per internal window", "windows", o.tabs.windows, 1, "", 1, 2) : "")
      + numRow("Tab length", "len", o.tabs.len, 0.5, "mm", 0.5, DEF.len)
      + numRow("Tab thickness", "thick", o.tabs.thick, 0.1, "mm", 0.2, DEF.thick)
      + `<p class="note">In the view: hover an outline or window edge to see where a tab goes, click to drop it there, click a tab to take it off. Drag a tab to slide it along its line.</p>`;
    if (Object.keys(o.tabs.manual || {}).length) h += `<button class="lnk" id="resettabs">↺ Put the tabs back where the program puts them</button>`;
  } else h += `<p class="note">A block is held at the corners with screws or clamps, outside the cut: no tabs.</p>`;
  h += `<p class="note">Drag the green circle in the view to move the tool sensor.</p>`;
  if (ex()) h += `<div id="sensorset"></div>`;
  box.innerHTML = h;
  wireNbs(box, (k, v) => { o.tabs[k] = k === "count" || k === "windows" ? Math.round(v) : v; savePrefs(); renderHold(); replan(); });
  box.querySelectorAll("[data-rst]").forEach((b) => b.onclick = () => { o.tabs[b.dataset.rst] = b.dataset.rst === "windows" ? 2 : DEF[b.dataset.rst]; savePrefs(); renderHold(); renderPresetRow(); replan(); });
  const rt = $("#resettabs");
  if (rt) rt.onclick = () => { o.tabs.manual = {}; renderHold(); replan("Tabs back where the program puts them"); };
  const wt = $("#wintabs");
  if (wt) wt.onchange = () => {
    if (wt.checked) o.tabs.windows = o.tabs.lastWindows || 2;
    else { o.tabs.lastWindows = o.tabs.windows; o.tabs.windows = 0; }
    savePrefs(); renderHold(); renderPresetRow(); replan();
  };
  if (ex()) renderSensorSettings();
}

// ---- Settings: every number the engines use, Bambu-style. Blank = the preset's own value (material, finish, bit),
// shown as the real number from the last plan; a changed one gets a dot and a ↺ back to the preset's value.
const SET2 = [
  ["Cutting", [
    { k: "direction", label: "Cut direction", seg: [["Conventional", "Conventional", "The end mill pushes against the feed: safest on a light machine"], ["Climb", "Climb", "The end mill pulls into the cut: a cleaner wall in aluminium, needs a stiff machine"]] },
    { k: "feed_pct", label: "Feed rate", unit: "%", step: 5, min: 20, why: "of each end mill's own feed" },
    { k: "step_pct", label: "Depth per pass", unit: "%", step: 10, min: 20, why: "of each end mill's own depth" },
    { k: "rpm_pct", label: "Spindle speed", unit: "%", step: 5, min: 40, why: "of each end mill's own rpm" },
    { k: "ramp_set", label: "Ramp-in angle", unit: "°", step: 1, min: 0, why: "0 = each end mill's own" },
  ]],
  ["Finishing pass", [
    { k: "finish", label: "Light last lap on the walls", bool: true },
    { k: "finish_allow", label: "Left for that lap", unit: "mm", step: 0.05, min: 0.02, need: "finish" },
  ]],
  ["Holes, slots and pockets", [
    { k: "slot_center", label: "Narrow slots down the middle", bool: true },
    { k: "slot_allow", label: "Slot may be narrower than the end mill by", unit: "mm", step: 0.05, min: 0, need: "slot_center" },
    { k: "pocket_over", label: "Pocket stepover", unit: "%", step: 5, min: 10 },
  ]],
  ["Machine moves", [
    { k: "safe_z", label: "Safe height over the stock", unit: "mm", step: 1, min: 1 },
    { k: "end_z_mode", label: "At the end, lift to", seg: [["Machine top (G53 Z-3)", "Machine top", "As high as the machine goes (3 mm under the home switch), in machine coordinates: never past UCCNC's soft limit"], ["Custom height", "A set height", "A height above the stock's top. Too high and UCCNC refuses the job (Z soft limit)"]] },
    { k: "end_z", label: "Height at the end", unit: "mm", step: 1, min: 0, need: "end_z_mode", needIs: "Custom height" },
  ]],
];
const SET3 = [
  ["Roughing and finishing", [
    { k: "allowance", label: "Roughing leaves", unit: "mm", step: 0.05, min: 0, why: "for the finishing passes" },
    { k: "rough_over", label: "Roughing stepover", unit: "%", step: 5, min: 5 },
    { k: "scallop", label: "Ridge between finishing passes", unit: "mm", step: 0.005, min: 0.002, why: "smaller = smoother, slower" },
  ]],
];
st.pdef = {};   // the preset's value of each setting, from the last plan made without changing it
st.setq = "";
function renderSettings() {
  const box = $("#pane-set"), o = st.opts, used = st.plan?.params || {};
  for (const k in used) if (!(k in o.params)) st.pdef[k] = used[k];
  const groups = st.part && !isFlat() ? SET3 : SET2;
  const q = st.setq.trim().toLowerCase();
  const val = (r) => r.k in o.params ? o.params[r.k] : used[r.k] ?? st.pdef[r.k];
  let h = `<input class="setsearch" id="setq" placeholder="Search the settings" value="${esc(st.setq)}">`;
  for (const [g, rows] of groups) {
    const show = rows.filter((r) => !q || (g + " " + r.label + " " + (r.why || "")).toLowerCase().includes(q))
      .filter((r) => { if (!r.need) return true; const v = val(SET2.flatMap((x) => x[1]).find((x) => x.k === r.need)); return r.needIs !== undefined ? v === r.needIs : v !== false; });
    if (!show.length) continue;
    h += `<h4>${g}</h4>`;
    for (const r of show) {
      const v = val(r), chg = r.k in o.params, d = st.pdef[r.k];
      const rst = chg ? `<button class="rst" data-prst="${r.k}" title="Back to the preset's ${d === undefined ? "value" : fmtV(d, r)}">↺</button>` : "";
      const lab = `<span title="${esc(r.why || "")}">${r.label}${r.why ? ` <em class="why">${esc(r.why)}</em>` : ""}</span>`;
      if (v === undefined) { h += `<div class="kv">${lab}<span class="val">after the first plan</span></div>`; continue; }
      if (r.seg) h += `<div class="kv${chg ? " chg" : ""}">${lab}${rst}<div class="mini-seg">${r.seg.map(([x, l, tip]) => `<button data-pseg="${r.k}" data-v="${x}" class="${v === x ? "on" : ""}" title="${esc(tip)}">${l}</button>`).join("")}</div></div>`;
      else if (r.bool) h += `<div class="kv${chg ? " chg" : ""}">${lab}${rst}<input type="checkbox" data-pbool="${r.k}" ${v ? "checked" : ""}></div>`;
      else h += `<div class="kv${chg ? " chg" : ""}" data-pnum="${r.k}">${lab}${rst}${nbHTML(r.k, +(+v).toFixed(4), r.step, r.unit, r.min)}</div>`;
    }
  }
  h += `<p class="note">These apply to ${st.part && !isFlat() ? "this 3D block" : "every flat part"}. Each end mill's own speeds and feeds are set under End mills → In the spindle → Speeds and feeds.</p>`;
  box.innerHTML = h;
  const set = (k, v) => {
    const d = st.pdef[k];
    if (d !== undefined && (v === d || (typeof v === "number" && Math.abs(v - d) < 1e-9))) delete o.params[k];
    else o.params[k] = v;
    savePrefs(); renderSettings(); replan();
  };
  const sq = $("#setq");
  sq.oninput = () => { st.setq = sq.value; const p = sq.selectionStart; renderSettings(); const n = $("#setq"); n.focus(); n.setSelectionRange(p, p); };
  box.querySelectorAll("[data-pseg]").forEach((b) => b.onclick = () => set(b.dataset.pseg, b.dataset.v));
  box.querySelectorAll("[data-pbool]").forEach((c) => c.onchange = () => set(c.dataset.pbool, c.checked));
  box.querySelectorAll("[data-prst]").forEach((b) => b.onclick = () => { delete o.params[b.dataset.prst]; savePrefs(); renderSettings(); replan(); });
  box.querySelectorAll("[data-pnum]").forEach((el) => wireNbs(el, (k, v) => set(k, v)));
}
function fmtV(v, r) { return typeof v === "number" ? `${+v.toFixed(4)}${r.unit ? " " + r.unit : ""}` : String(v); }

// ---- Presets: the three finishes are the program's own; the designer's are saved with every setting in them
function changedCount() {
  const o = st.opts;
  let n = Object.keys(o.params || {}).length + Object.keys(o.bits || {}).length;
  if (Math.abs(+o.extra - DEF.extra) > 1e-9) n++;
  for (const k of ["count", "len", "thick", "windows"]) if (Math.abs(+(o.tabs[k] ?? 0) - DEF[k]) > 1e-9) n++;
  if ((o.strategy || "recommended") !== "recommended") n++;
  return n;
}
function presetOf(name) {
  const o = st.opts, sa = o.addons.find((a) => a.kind === "surface");
  return { name, finish: o.finish, strategy: o.strategy || "recommended", extra: o.extra,
    tabs: { count: o.tabs.count, len: o.tabs.len, thick: o.tabs.thick, windows: o.tabs.windows || 0 },
    params: { ...o.params }, surface: sa ? { over: sa.over, margin: sa.margin, area: sa.area, pattern: sa.pattern } : null };
}
function applyPreset(p) {
  const o = st.opts;
  if (st.info.finishes[p.finish]) o.finish = p.finish;
  o.strategy = p.strategy || "recommended"; o.bits = {};
  o.extra = p.extra ?? DEF.extra;
  Object.assign(o.tabs, { count: DEF.count, len: DEF.len, thick: DEF.thick, windows: DEF.windows }, p.tabs || {});
  o.params = { ...(p.params || {}) };
  const sa = o.addons.find((a) => a.kind === "surface");
  if (sa && p.surface) Object.assign(sa, p.surface);
  st.preset = p.name;
}
function renderPresetRow() {
  const box = $("#presetrow");
  if (!box || !st.info) return;
  const list = st.info.prefs?.presets || [], n = changedCount();
  if (st.naming) {
    box.innerHTML = `<input id="pname" placeholder="Name this preset" value="${esc(st.preset || "")}"><button class="lnk" id="psave">Save</button><button class="lnk" id="pcancel">Cancel</button>`;
    const inp = $("#pname"); inp.focus(); inp.select();
    const save = () => {
      const nm = inp.value.trim(); if (!nm) return;
      const pf = st.info.prefs = st.info.prefs || {};
      pf.presets = (pf.presets || []).filter((x) => x.name !== nm).concat([presetOf(nm)]);
      st.preset = nm; st.naming = false;
      api("prefs", { presets: pf.presets }).catch(() => {});
      renderPresetRow(); toast(`Saved as "${nm}"`);
    };
    $("#psave").onclick = save;
    inp.onkeydown = (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") { st.naming = false; renderPresetRow(); } };
    $("#pcancel").onclick = () => { st.naming = false; renderPresetRow(); };
    return;
  }
  const cur = list.find((x) => x.name === st.preset);
  box.innerHTML = (list.length ? `<select id="psel" title="Your saved presets"><option value="">${cur ? "" : "Your presets…"}</option>${list.map((x) => `<option ${cur === x ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>`
      + (cur ? `<button class="rst" id="pdel" title="Delete the preset &quot;${esc(cur.name)}&quot;">✕</button>` : "") : "")
    + (n ? `<span class="chgn" title="Settings changed from the program's own">${n} changed</span><button class="lnk" id="preset0" title="Every setting back to the program's own (the finish stays)">Reset all</button>` : "")
    + `<button class="lnk" id="pnew">${list.length ? "Save…" : "Save as preset…"}</button>`;
  const sel = $("#psel");
  if (sel) sel.onchange = () => {
    const p = list.find((x) => x.name === sel.value);
    if (!p) return;
    applyPreset(p); savePrefs(); renderPresets(); replan(`Preset "${p.name}"`);
  };
  $("#pdel") && ($("#pdel").onclick = () => {
    const pf = st.info.prefs; pf.presets = list.filter((x) => x !== cur); st.preset = null;
    api("prefs", { presets: pf.presets }).catch(() => {}); renderPresetRow();
  });
  $("#preset0") && ($("#preset0").onclick = () => {
    applyPreset({ finish: st.opts.finish }); st.preset = null;
    savePrefs(); renderPresets(); replan("Every setting back to the program's own");
  });
  $("#pnew").onclick = () => { st.naming = true; renderPresetRow(); };
}

// ---- Bits: the strategy, then the bit for each operation (a pick overrides it)
function renderBits() {
  const box = $("#pane-bits"), p = st.plan;
  if (!p) {
    box.innerHTML = `<p class="note">${st.part ? "Working out the end mills…" : "Open a part to see the end mills it needs."}</p>`
      + `<div class="row"><button class="add" id="rackbtn">End mills at the machine…</button></div>`;
    $("#rackbtn").onclick = rackDialog;
    return;
  }
  const cur = st.opts.strategy || p.strategy || "recommended";   // the pick shows at once, before the new plan
  let h = "";
  if (p.strategies) {
    h += `<div class="strats">${p.strategies.map((s) => `<button class="strat${s.key === cur ? " on" : ""}" data-strat="${s.key}" title="${s.exact ? "This plan" : "Estimated from the end mill trials"}">`
      + `<b>${esc(s.label)}</b><span class="t">${s.exact ? "" : "≈ "}${fmtLong(s.time_min)}</span>`
      + `<small>${s.changes ? `${s.changes} end mill change${s.changes === 1 ? "" : "s"}` : "No end mill change"}</small></button>`).join("")}</div>`
      + `<p class="note tight">${STRAT_NOTE[cur] || ""}</p>`;
  }
  const ops = p.ops || [];
  if (ops.length) h += `<h4>End mill per operation</h4>` + ops.map((c) => {
    const mine = !!st.opts.bits?.[c.key];
    const pick = c.choices?.length && !c.readonly
      ? `<select data-op="${c.key}">${c.choices.map((x) => `<option value="${esc(x.name)}" ${x.name === c.bit ? "selected" : ""}>${esc(x.label)}${x.owned ? "" : " (buy)"}</option>`).join("")}</select>`
      : `<span class="chip">${esc(c.bit_label)}</span>`;
    return `<div class="kv bitrow${mine ? " chg" : ""}"><span>${esc(c.label)}</span>${mine ? `<button class="rst" data-unpick="${c.key}" title="Back to the strategy's end mill">↺</button>` : ""}${pick}</div>`;
  }).join("");
  if (p.tools.length) h += `<h4>In the spindle</h4>` + toolsInOrder().map((t, i) => {
    const tl = t.tool, own = (tl.own || []).length > 0;
    return `<div class="bit" title="${esc(t.why)}"><span class="n">T${tNum(tl.name) ?? i + 1}</span>
    <b>${esc(t.label)}${tl.owned === false ? '<span class="tag buy">BUY</span>' : ""}</b><small>${esc(cap(t.what.join(", ")))}</small>${chipLine(p.chip?.[tl.name])}
    <span class="num">${esc(tl.name)} · ${Math.round(tl.rpm)} rpm · feed ${tl.feed} · ${(+tl.step).toFixed(2)} mm/pass${own ? ' · <span class="own">your speeds</span>' : ""}</span>
    ${st.spdOpen === tl.name ? speedForm(tl, p.chip?.[tl.name]) : `<button class="add spd" data-spd="${esc(tl.name)}" title="Tune this end mill's spindle speed, feeds and depth per pass. Saved with the end mill, so every job in ${esc(tl.mat_family || "this material")} uses them.">Speeds and feeds…</button>`}</div>`;
  }).join("");
  h += `<div class="row"><button class="add" id="rackbtn">End mills at the machine…</button>${ex() && st.info.has_gcm ? '<button class="add" id="gcmbtn">Open in G-code Maker</button>' : ""}</div>`
    + (ex() ? `<p class="note">Saves to ${esc(st.info.folder)}</p>` : "");
  box.innerHTML = h;
  box.querySelectorAll("[data-strat]").forEach((b) => b.onclick = () => {
    const had = Object.keys(st.opts.bits || {}).length;
    st.opts.strategy = b.dataset.strat; st.opts.bits = {};
    savePrefs(); renderBits(); replan(had ? "Your end mill picks are cleared" : null);
  });
  box.querySelectorAll("select[data-op]").forEach((s) => s.onchange = () => { st.opts.bits = { ...(st.opts.bits || {}), [s.dataset.op]: s.value }; replan(); });
  box.querySelectorAll("[data-unpick]").forEach((b) => b.onclick = () => { delete st.opts.bits[b.dataset.unpick]; renderBits(); replan(); });
  $("#rackbtn") && ($("#rackbtn").onclick = rackDialog);
  $("#gcmbtn") && ($("#gcmbtn").onclick = () => api("gcm"));
  box.querySelectorAll("[data-spd]").forEach((b) => b.onclick = () => { st.spdOpen = b.dataset.spd; renderBits(); });
  const f = box.querySelector(".spdform");
  if (f) wireSpeedForm(f, p.tools.find((t) => t.tool.name === st.spdOpen)?.tool);
}

// Lev 2026-11-15: tune each end mill's speeds and feeds per material; saved in the rack (the same end mills are
// bought again and again, so the tuned numbers carry over to every job)
const SPD_FIELDS = [["rpm", "Spindle speed", "rpm", 100], ["feed", "Feed", "mm/min", 10], ["plunge_feed", "Plunge feed", "mm/min", 5],
  ["ramp_feed", "Ramp feed", "mm/min", 5], ["step", "Depth per pass", "mm", 0.05]];
function speedForm(tl, chip) {
  const sug = tl.sug || {}, mine = new Set(tl.own || []);
  const fam = tl.mat_family || "this material";
  return `<div class="spdform" data-name="${esc(tl.name)}">${SPD_FIELDS.map(([k, lab, unit, stp]) =>
    `<label>${lab} (${unit})<input type="number" min="0" step="${stp}" data-k="${k}" value="${+(+tl[k]).toFixed(k === "step" ? 2 : 0)}" class="${mine.has(k) ? "mine" : ""}">
      <span class="sug">Program: ${sug[k] != null ? +(+sug[k]).toFixed(k === "step" ? 2 : 0) : "–"}</span></label>`).join("")}
    <label>Chip load<span class="spdchip" style="font-size:13px"></span><span class="sug">Aim ${chip ? chip.aim : "–"} mm per tooth</span></label>
    <div class="acts"><p class="note">For the ${esc(tl.name)} in every ${esc(fam.toLowerCase())} job. The Settings tab's feed and spindle percentages still apply on top.</p>
      <button class="primary" data-act="save">Save</button><button data-act="reset" ${mine.size ? "" : "disabled"}>Program's numbers</button><button data-act="close">Close</button></div></div>`;
}
function wireSpeedForm(f, tl) {
  if (!tl) return;
  const val = (k) => parseFloat(String(f.querySelector(`[data-k="${k}"]`).value).replace(",", "."));
  const chip = () => {
    const fz = val("feed") / Math.max(1, val("rpm") * Math.max(1, tl.flutes || 2));
    const c = st.plan?.chip?.[tl.name], el = f.querySelector(".spdchip");
    if (!Number.isFinite(fz)) { el.textContent = "–"; return; }
    const state = c ? (fz < 0.6 * c.aim ? "thin: it rubs" : fz > 1.6 * c.aim ? "heavy: it may break" : "good") : "";
    el.textContent = `${fz.toFixed(3)} mm/tooth${state ? " · " + state : ""}`;
    el.style.color = state && state !== "good" ? "var(--warn)" : "var(--ok)";
  };
  f.querySelectorAll("input").forEach((i) => i.oninput = () => { i.classList.add("mine"); chip(); });
  chip();
  const send = async (speeds, msg) => {
    const r = await api("speeds", { name: tl.name, material: st.opts.material, speeds });
    if (!r.ok) { toast(r.error || "Couldn't save the speeds"); return; }
    st.info.rack = r.rack; st.spdOpen = null; replan(msg);
  };
  f.querySelector('[data-act="save"]').onclick = () => {
    const sug = tl.sug || {}, speeds = {};
    for (const [k] of SPD_FIELDS) {
      const v = val(k);
      // only the numbers that differ from the program's are kept, so a later better suggestion still reaches the rest
      if (Number.isFinite(v) && v > 0 && Math.abs(v - (+sug[k])) > 1e-6) speeds[k] = v;
    }
    send(speeds, Object.keys(speeds).length ? `Speeds saved for the ${tl.name}` : `The ${tl.name} uses the program's numbers`);
  };
  f.querySelector('[data-act="reset"]').onclick = () => send(null, `The ${tl.name} is back to the program's numbers`);
  f.querySelector('[data-act="close"]').onclick = () => { st.spdOpen = null; renderBits(); };
}

// the chip load of one end mill: good, thin (rubs) or heavy (chatters, breaks), and a deep pass
function chipLine(c) {
  if (!c) return "";
  const word = { ok: "good", thin: "too thin: it rubs", heavy: "too heavy: it may break" }[c.state];
  const cls = c.state === "ok" && !c.deep ? "ok" : "bad";
  return `<span class="chipld ${cls}" title="Chip load = feed ÷ (rpm × flutes). Aim ${c.aim} mm per tooth for this end mill and material. Cutting speed ${c.vc} m/min (range ${c.vc_range.join("-")}).">`
    + `Chip ${c.fz.toFixed(3)} mm/tooth · ${word}${c.deep ? ` · ${(+c.step).toFixed(2)} mm per pass is deep` : ""}</span>`;
}
function renderPlan() {
  const p = st.plan;
  $("#checksec").hidden = !p;
  refreshPanel();
  if (!p) return;
  const ws = [...p.warnings];
  const sen = p.sensor || {};
  if (sen.needs_bed)
    ws.unshift("This job changes end mills, which are measured on the bed sensor, but its position isn't entered yet (Advanced → Tabs & holding → Bed sensor X/Y). Enter it, or choose Fewest end mill changes under End mills. Until then the job can't be sent.");
  $("#checks").innerHTML = ws.length ? ws.map((w) => `<div class="check ${w.startsWith("Buy:") ? "buy" : ""}">${esc(w)}</div>`).join("")
    : `<div class="check ok">✓ Everything checks out.</div>`;
  $("#msensor").textContent = sen.bed_changes ? "Bed tool sensor · measures each new end mill" : sen.bed ? "Bed tool sensor · puck zeroes the first end mill" : "Tool sensor · pauses for each end mill change";
  // what happens
  const changes = Math.max(0, p.tools.length - 1);
  $("#total").innerHTML = `<div><b>${fmtLong(p.time_min)}</b><small>machine time</small></div><div><b>${p.tools.length}</b><small>end mill${p.tools.length === 1 ? "" : "s"}, ${changes} change${changes === 1 ? "" : "s"}</small></div>`;
  const items = [];
  const pauses = p.pauses.map(([k, m]) => ({ k, m, shown: false }));
  p.stages.forEach((s, i) => {
    for (const q of pauses) if (!q.shown && q.k <= s.from) { q.shown = true; items.push({ k: q.k, html: `<li class="pause"><span class="c"></span><span>${esc(q.m)}</span></li>` }); }
    const lab = s.label.replace(/^T(\d+)\s+(.*?):\s*/, "");
    const t = toolForStage(i);
    items.push({ k: s.from, html: `<li data-stage="${i}" class="${st.hiddenStages.has(i) ? "off" : ""}"><span class="c" style="background:${opColour(s.label)}"></span><b>${esc(cap(lab))}</b><span class="t">${fmtLong(s.t1 - s.t0)}</span><small>${esc(t ? `T${tNum(t.name) ?? "?"} · ${labelOf(t)}` : "")}${explain(lab) ? " — " + esc(explain(lab)) : ""}</small></li>` });
  });
  $("#steps").innerHTML = items.map((x) => x.html).join("");
  $$("#steps li[data-stage]").forEach((li) => li.onclick = () => { const s = p.stages[+li.dataset.stage]; if (st.paths) { stopPlay(); setTime(s.t0 + 1e-6); } });
  $("#sendbtn").disabled = !p.nsegs;
}
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;
const labelOf = (t) => (st.plan.tools.find((x) => x.tool.name === t.name) || {}).label || t.name;
function explain(lab) {
  const l = lab.toLowerCase();
  if (l.includes("finishing pass")) return "one light last lap for a clean wall";
  if (l.includes("surfac")) return "faces the top flat so every depth starts from a true surface";
  if (l.includes("hole") || l.includes("slot")) return "cuts the holes and slots from the inside, before the part comes loose";
  if (l.includes("outline")) return "cuts the part out, leaving tabs to hold it";
  if (l.includes("pocket")) return "clears the pockets layer by layer";
  if (l.includes("engrav")) return "engraves the text and logos";
  if (l.includes("chamfer")) return "bevels the top edges";
  if (l.includes("edge")) return "rounds the top edges";
  if (l.includes("rough")) return "removes most of the material in layers, leaving a thin skin";
  if (l.includes("waterline")) return "finishes the steep walls in level loops";
  if (l.includes("parallel")) return "finishes the gentle slopes in rows";
  if (l.includes("finish")) return "the final, smooth pass";
  return "";
}
function explainStage(s, i) { const lab = s.label.replace(/^T(\d+)\s+(.*?):\s*/, ""); return `Step ${i + 1} of ${st.plan.stages.length}: ${cap(lab)}${explain(lab) ? " — " + explain(lab) : ""}`; }

// ------------------------------------------------------------------ modes
function setMode(mode) {
  st.mode = mode;
  layers.edges.visible = mode === "prepare";   // the pick ribbons would hide the real chamfer cut in the preview
  layers.marks.visible = mode === "prepare";   // so would the text's drag box and turn handle
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  $("#right").hidden = mode !== "preview" || !st.plan;
  $("#player").hidden = mode !== "preview" || !st.plan;
  $("#leftlbl").hidden = !(mode === "preview" && st.part?.kind === "3d");
  if (mode !== "prepare" && ghostAt) drawGhost(null);
  if (mode === "prepare") { stopPlay(); layers.tool.visible = false; }
  else if (st.paths) setTime(st.tNow);
  applyVisibility();
  loadBody();
}
$$(".tabs button").forEach((b) => b.onclick = () => setMode(b.dataset.mode));

// ------------------------------------------------------------------ planning and polling
let replanTimer = null;
function payload() {
  const o = { ...st.opts, addons: st.opts.addons.filter((a) => a.kind !== "chamfer") };
  const ch = [], shapes = st.plan?.shapes;
  for (const a of st.opts.addons) {
    if (a.kind !== "chamfer" || a.hidden) continue;
    if (!isFlat() || !shapes?.length) {
      if (a === chamferList()[0] || !isFlat()) o.addons.push({ kind: "chamfer", width: a.width });
    } else {
      const by = {};
      for (const [k, e] of chamferEdges(a)) (by[k] = by[k] || []).push(e);
      for (const k in by) {
        const s = shapes.find((x) => x.key === k);
        ch.push({ key: k, edges: s && by[k].length === s.edges.length ? "all" : by[k], width: a.width });
      }
    }
  }
  if (ch.length) o.chamfers = ch;
  // surfacing: one value, the finished thickness (part_t); the add-on only brings its stepover and margin
  delete o.part_t; delete o.take;
  o.addons = o.addons.map((a) => a.kind === "surface" ? { kind: "surface", over: a.over ?? null, margin: a.margin ?? 5, area: a.area ?? "part", pattern: a.pattern ?? "zig", hidden: a.hidden } : a);
  const S = surf();
  if (S.on) o.part_t = S.leaves;
  o.sheet = { copies: [...copiesOf()], gap: st.opts.sheet?.gap ?? 8 };
  return o;
}
function replan(msg) {
  if (!st.naming) renderPresetRow();
  if (!st.part) return;
  record();
  clearTimeout(replanTimer);
  replanTimer = setTimeout(async () => {
    try { await api("plan", payload()); busy(true); if (msg) toast(msg + " — planning again"); poll(); } catch (e) { toast(e.message); }
  }, 280);
}
function busy(on) { st.busy = on; $("#busybar").classList.toggle("on", on); }
let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  let r;
  try { r = await api("poll", { gen: st.gen }); } catch (e) { pollTimer = setTimeout(poll, 1500); return; }
  busy(r.busy);
  if (r.error) toast("Something went wrong: " + r.error, 6000);
  const partChanged = JSON.stringify(r.part) !== JSON.stringify(st.part);
  if (partChanged) {
    const wasPath = st.part?.path;
    st.part = r.part; showPart();
    // the model's own chamfered (or rounded) top edges: Chamfer comes on at that width, once per part
    const brk = st.part?.edge_break || 0;
    if (brk > 0 && st.part.path !== st.brkFor && !st.opts.addons.some((a) => a.kind === "chamfer")) {
      st.opts.addons.push({ kind: "chamfer", width: brk, off: [], edges: [] });
      toast(`The model's top edges are broken ${brk} mm: Chamfer is on at ${brk} mm (V-bit, after the cut-out).`, 5000);
    }
    if (brk > 0) st.brkFor = st.part.path;
    if (st.part && !r.busy && !r.plan) { renderOps(); replan(); }
    else if (wasPath !== st.part?.path) renderOps();
  }
  if (r.project) {
    applyProject(r.project);
    if (st.part) $("#filechip").textContent = r.project.name;
  }
  $("#savebtn").disabled = !st.part;
  if (r.plan) {
    if (hist.part !== st.part?.path) record();   // where this part's undo history starts
    const firstPlan = !st.plan;
    st.plan = r.plan;
    st.hasSim = r.has_sim;
    st.gen = r.gen;
    // a plan the server kept from earlier settings (another stock thickness): plan again with what the panel shows
    if (firstPlan && !r.busy && isFlat() && r.plan.thickness != null && Math.abs(r.plan.thickness - st.opts.thickness) > 1e-6) replan();
    renderPlan(); drawSensor(); drawMarks(); drawTabs(); drawEdges();
    const unplaced = st.opts.addons.filter((a) => (a.kind === "text" || a.kind === "logo") && a.x == null);
    if (unplaced.length) { const c = drawingCentre(); unplaced.forEach((a) => { a.x = c[0]; a.y = c[1]; }); replan(); }
    st.tNow = 1e9;
    await loadPaths();
    await loadBody();
    setMode(st.mode);
  } else if (r.gen !== st.gen && !r.busy) {
    st.gen = r.gen; st.hasSim = r.has_sim; await loadBody();
  }
  pollTimer = setTimeout(poll, r.busy ? 150 : 1200);
}
function showPart() {
  const p = st.part;
  $("#drop").hidden = !!p;
  $("#partsec").hidden = !p;
  $("#filechip").hidden = !p;
  if (!p) return;
  $("#filechip").textContent = p.name;
  $("#partname").textContent = p.name;
  $("#partsize").textContent = p.size ? `${p.size[0].toFixed(1)} × ${p.size[1].toFixed(1)} × ${p.size[2].toFixed(1)} mm` : p.kind === "2d" ? "DXF drawing" : "Reading…";
  $("#uprow").hidden = p.kind !== "3d";
  renderParts();
  // a STEP plate: the stock starts as thick as the part (thicker stock is surfaced down first)
  if (p.kind === "3d" && p.size && st.thickFor !== p.path) { st.thickFor = p.path; st.opts.thickness = +p.size[2].toFixed(2); }
  if (p.kind === "2d" && st.thickFor !== p.path) {
    // a drawing is cut from sheet: a block material or a 3D part's height left over from the last part goes back
    // to the sheet of the same family and its usual thickness
    const fromStep = st.thickFor && !/\.dxf$/i.test(st.thickFor);
    st.thickFor = p.path;
    const m = st.info.materials[st.opts.material], alu = (m?.gcm || "Aluminium") === "Aluminium";
    if (m?.kind === "block") st.opts.material = alu ? "Aluminium sheet" : "MDF / wood";
    const sheet = st.info.materials[st.opts.material];
    if (sheet?.kind === "sheet" && (fromStep || m?.kind === "block" || st.opts.thickness > 12)) st.opts.thickness = sheet.thickness;
    savePrefs();
  }
  // the material family stays (aluminium, or wood and board); sheet or block follows the part's thickness
  if (p.kind === "3d" && p.size) {
    const m = st.info.materials[st.opts.material], alu = (m?.gcm || "Aluminium") === "Aluminium";
    if (p.size[2] > 12 && m?.kind === "sheet") { st.opts.material = alu ? "Aluminium block" : "Tooling board"; savePrefs(); }
    if (p.size[2] <= 12 && m?.kind === "block") { st.opts.material = alu ? "Aluminium sheet" : "MDF / wood"; savePrefs(); }
  }
  st.plan = null; st.paths = null; clear(layers.paths); clear(layers.sensor); clear(layers.marks);
  loadBody.fitted = null;
  renderPresets(); renderPlan(); setMode("prepare");
}

// ------------------------------------------------------------------ the parts on the sheet
// The first part is the one opened; "+ Add a part" puts more on the same sheet. Each has a number of copies
// (st.opts.sheet.copies, in the same order as the list), laid out automatically with a gap between them.
function sheetParts() { return st.part ? [st.part, ...(st.part.others || [])] : []; }
function copiesOf() {
  const n = sheetParts().length, sh = st.opts.sheet = st.opts.sheet || { copies: [1], gap: 8 };
  sh.copies = (sh.copies || []).slice(0, n);
  while (sh.copies.length < n) sh.copies.push(1);
  return sh.copies;
}
function renderParts() {
  const box = $("#partlist"), parts = sheetParts();
  if (!box || !parts.length) return;
  const cp = copiesOf(), total = cp.reduce((a, b) => a + b, 0), many = total > 1 || parts.length > 1;
  const size = (p) => p.size ? `${p.size[0].toFixed(1)} × ${p.size[1].toFixed(1)} mm` : p.kind === "2d" ? "DXF" : "Reading…";
  $("#partsec .part").hidden = many;   // the list names every part
  let h = "";
  if (many) h += parts.map((p, i) => `<div class="prow" data-pi="${i}"><span class="pn" title="${esc(p.path)}"><b>${esc(p.name)}</b><small>${size(p)}${p.kind === "3d" && p.flat === false ? " · 3D: can't share a sheet" : ""}</small></span>`
    + `${nbHTML("copies", cp[i], 1, "×", 0)}${i ? `<button class="x" data-rm="${i}" title="Take it off the sheet">✕</button>` : `<span class="x"></span>`}</div>`).join("");
  else h += `<div class="kv"><span>Copies</span>${nbHTML("copies", cp[0], 1, "×", 1)}</div>`;
  if (many) h += numRow("Space between parts", "gap", st.opts.sheet.gap ?? 8, 1, "mm", 2, 8);
  h += `<div class="row"><button class="lnk" id="addpart">+ Add a part to the sheet</button></div>`;
  box.innerHTML = h;
  box.querySelectorAll(".prow").forEach((row) => wireNbs(row, (k, v) => { cp[+row.dataset.pi] = Math.round(v); renderParts(); replan(); }));
  const one = box.querySelector(".kv");
  if (one && !many) wireNbs(one, (k, v) => { cp[0] = Math.max(1, Math.round(v)); renderParts(); replan(); });
  box.querySelectorAll(".kv").forEach((row) => { if (row.querySelector('[data-k="gap"]')) wireNbs(row, (k, v) => { st.opts.sheet.gap = v; renderParts(); replan(); }); });
  box.querySelectorAll("[data-rst]").forEach((b) => b.onclick = () => { st.opts.sheet.gap = 8; renderParts(); replan(); });
  box.querySelectorAll("[data-rm]").forEach((b) => b.onclick = async () => {
    const i = +b.dataset.rm;
    cp.splice(i, 1);
    await api("remove", { i }); st.gen = -1; poll();
  });
  $("#addpart").onclick = () => openPart(true);
}

// ------------------------------------------------------------------ files and projects
function freshJob() {   // a new part: what belonged to the last one goes
  Object.assign(st.opts, { addons: [], take: null, order: null, sensor: null, bits: {} });
  st.opts.tabs.manual = {};
  st.opts.sheet = { copies: [1], gap: st.opts.sheet?.gap ?? 8 };
  st.expanded = -1; st.gen = -1;
}
async function upload(file, add = false) {
  busy(true);
  try {
    const buf = await file.arrayBuffer();
    const r = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}${add ? "&add=1" : ""}`, { method: "POST", body: buf, headers: who() });
    if (!r.ok) throw new Error((await r.json()).error);
    if (!add) freshJob();
    poll();
  } catch (e) { toast("Could not open it: " + e.message, 5000); busy(false); }
}
async function openPart(add = false) {
  if (add === true && !st.part) add = false;
  if (IN_APP) {
    try {
      const r = await api("pick", { add: add === true });
      if (r.part) { if (add !== true) freshJob(); st.gen = -1; poll(); }
    } catch (e) { toast(e.message); }
  } else { st.addNext = add === true; $("#fileinput").click(); }
}
$("#fileinput").onchange = (e) => { const f = e.target.files[0]; if (f) upload(f, !!st.addNext); st.addNext = false; e.target.value = ""; };
$("#openbtn").onclick = () => openPart(false); $("#openbig").onclick = () => openPart(false);

// a project opened: its options replace the page's, once per opening
function applyProject(pj) {
  if (!pj?.opts || st.projToken === pj.opened) return;
  st.projToken = pj.opened;
  const keep = { tabs: { count: 3, len: 4, thick: 1, windows: 0 }, addons: [], bits: {}, params: {}, sheet: { copies: [1], gap: 8 } };
  st.opts = { ...st.opts, ...keep, ...JSON.parse(JSON.stringify(pj.opts)) };
  st.thickFor = st.part?.path;   // the project's own thickness, not the part's
  st.expanded = -1; hist.part = null;
  renderPresets(); renderParts(); drawEdges(); replan();
  toast(`Opened the project "${pj.name}"` + (pj.newer?.length ? `. Newer drawings exist for ${pj.newer.join(", ")}: the project keeps the copies saved in it.` : ""), pj.newer?.length ? 8000 : 3000);
}
async function saveProject(as = false) {
  if (!st.part) return;
  try {
    const r = await api("save_project", { opts: st.opts, as });
    if (r.cancelled) return;
    if (!r.ok) { toast(r.error, 5000); return; }
    if (r.download) {   // in a browser: the file comes down as a download
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([r.download], { type: "application/json" }));
      a.download = r.name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      toast(`Saved as ${r.name} (in your downloads)`);
    } else toast("Saved " + r.path);
  } catch (e) { toast("Could not save: " + e.message, 5000); }
}
$("#savebtn").onclick = (e) => saveProject(e.shiftKey);
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("#dragover").classList.add("on"); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dragover").classList.remove("on"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("#dragover").classList.remove("on");
  const f = [...e.dataTransfer.files].find((x) => /\.(dxf|step|stp|stl|acnc)$/i.test(x.name));
  // dropped with Shift held (or onto the parts list): added to the sheet; otherwise it replaces the part
  const add = !!st.part && !/\.acnc$/i.test(f?.name || "") && (e.shiftKey || !!e.target.closest?.("#partsec"));
  if (f) upload(f, add); else toast("Agilite CNC opens DXF, STEP and STL parts and .acnc projects.");
});

// ------------------------------------------------------------------ send, bits, expert
$("#sendbtn").onclick = async () => {
  try {
    const r = await api("send", { material: st.opts.material });
    if (r.cancelled) return;
    if (!r.ok) { toast(r.error, 6000); return; }
    st.info.folder = r.folder; renderBits();
    // in a browser the program comes down as a download, one file per program
    (r.downloads || []).forEach((d, i) => setTimeout(async () => {
      const blob = await (await fetch(d.url)).blob();   // through the page's fetch, so the web app's engine answers it too
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = d.name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    }, i * 400));
    const where = r.downloads ? `Downloaded ${esc(r.downloads.map((d) => d.name).join(", "))} (in your downloads)` : `Saved to ${esc(r.paths.join(", "))}`;
    sheet(`<h2>Ready for the machine</h2><small>${where}</small>
      <ol>${r.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
      <div class="btns" id="usbrow"></div>
      <div class="btns"><button id="cpy">Copy the steps</button><button id="fold">Open the folder</button><button class="primary" id="ok">Done</button></div>`);
    // Lev 2026-11-15: one click to put the program on a USB stick for the machine PC
    const usbRow = (drives) => {
      const row = $("#usbrow"); if (!row) return;
      row.innerHTML = drives.length
        ? drives.map((d) => `<button class="primary" data-usb="${esc(d.drive)}">Copy to USB ${esc(d.drive.replace("\\", ""))} ${esc(d.label)}</button>`).join("")
        : `<span class="note">Plug in a USB stick to copy the program to it.</span> <button id="usbre">Look again</button>`;
      row.querySelectorAll("[data-usb]").forEach((b) => b.onclick = async () => {
        b.disabled = true;
        const c = await api("copy_usb", { drive: b.dataset.usb, paths: r.paths });
        b.disabled = false;
        if (!c.ok) { toast(c.error, 6000); return; }
        b.textContent = `On the USB stick ✓ (${c.paths.map((p) => p.split("\\").pop()).join(", ")})`;
      });
      $("#usbre") && ($("#usbre").onclick = async () => usbRow((await api("usb")).drives || []));
    };
    if (WEB) $("#usbrow").remove(); else usbRow(r.usb || []);   // the web app can't see USB sticks: the download goes on one
    $("#cpy").onclick = () => { navigator.clipboard.writeText(`${st.part.name} (${st.opts.material}, ${st.opts.finish})\n` + r.steps.map((s, i) => `${i + 1}. ${s}`).join("\n")); toast("Steps copied"); };
    if (r.downloads) $("#fold").remove(); else $("#fold").onclick = () => api("folder", { path: r.folder });
    $("#ok").onclick = closeSheet;
  } catch (e) { toast(e.message); }
};
function sheet(html) { $("#sheet").innerHTML = html; $("#modal").hidden = false; }
function closeSheet() { $("#modal").hidden = true; }
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeSheet(); };
$("#expert").onchange = () => { savePrefs(); renderPanel(); };   // Advanced: rpm/feed/step, the sensor, the rack
function renderSensorSettings() {
  const pf = st.info.prefs || {}, box = $("#sensorset");
  if (!box) return;
  const mode = pf.sensor_mode || st.info.machine.sensor_mode || "stock", bs = pf.bed_sensor || [null, null];
  box.innerHTML = `<h4>Tool sensor</h4>
    <div class="mini-seg" id="senmode"><button data-v="bed" class="${mode === "bed" ? "on" : ""}">On the bed</button><button data-v="stock" class="${mode === "stock" ? "on" : ""}">Puck on the stock</button></div>
    ${mode === "bed" ? `<div class="g2">${numBox("Bed sensor X", "bx", bs[0] ?? "", 0.1, "mm")}${numBox("Bed sensor Y", "by", bs[1] ?? "", 0.1, "mm")}</div>
    <p class="note tight">Machine coordinates of the sensor's centre: home, jog the end mill over it, read X and Y in UCCNC's machine coordinates. The puck zeroes the first end mill on the stock; each later end mill change goes to the bed sensor and runs ${esc(st.info.machine.bed_macro || "M20201")}.</p>`
    : `<p class="note tight">Every end mill is measured with the puck on the stock (M31), on a spot no end mill cuts.</p>`}`;
  const save = () => { api("prefs", { sensor_mode: pf.sensor_mode, bed_sensor: pf.bed_sensor }).then(() => replan("Tool sensor changed")); };
  box.querySelectorAll("#senmode button").forEach((b) => b.onclick = () => { pf.sensor_mode = b.dataset.v; st.info.prefs = pf; renderSensorSettings(); save(); });
  box.querySelectorAll(".nb").forEach((nb) => {
    const inp = nb.querySelector("input");
    const set = () => {
      const v = String(inp.value).trim() === "" ? null : parseFloat(String(inp.value).replace(",", "."));
      const cur = pf.bed_sensor || [null, null];
      cur[inp.dataset.k === "bx" ? 0 : 1] = Number.isFinite(v) ? v : null;
      pf.bed_sensor = cur; st.info.prefs = pf; save();
    };
    nb.querySelectorAll("[data-dir]").forEach((b) => b.onclick = (e) => { e.preventDefault(); inp.value = String(+((parseFloat(inp.value) || 0) + +b.dataset.dir * +nb.dataset.step).toFixed(1)); set(); });
    inp.onchange = set;
  });
}
// ---- the bits at the machine: tick what you have, add a bit you bought, edit or remove one
const VB = "V-bit / chamfer", SB = "Surfacing bit";
const BIT_TYPES = [["Flat end mill", "Flat"], ["Ball nose", "Ball nose"], [VB, "V-bit"], [SB, "Surfacing"]];
// the rack's categories, in this order
const BIT_CATS = [["Flat end mill", "Flat end mills"], ["Ball nose", "Ball nose end mills"], [VB, "V-bits"],
  [SB, "Surfacing bits: only face the top, never cut parts out"]];
const surfBit = (name) => (st.info?.rack || []).some((b) => b.name === name && b.tool_type === SB);
function bitSpec(b) {
  const cut = b.tool_type === SB ? `${b.flutes} insert${b.flutes === 1 ? "" : "s"}` : `${b.flutes} flute${b.flutes === 1 ? "" : "s"} · ${b.flute_len} mm flutes`;
  return `Ø${b.tool_d}${b.tool_type === VB ? ` · ${b.tool_angle ?? 90}°` : ""} · ${cut} · reaches ${b.reach} mm · ${b.shank_d} mm shank · ${b.tool_mat}${cutsLabel(b) ? " · " + cutsLabel(b) : ""}`;
}
// which materials a bit may cut: every one, or only wood (MDF, plywood, tooling board) or only aluminium
const WOOD = "Wood / MDF / plywood", ALU = "Aluminium";
const CUTS = [["all", "Every material"], ["wood", "Wood only"], ["alu", "Aluminium only"]];
const cutsKey = (b) => !b.cuts?.length ? "all" : b.cuts.includes(ALU) && b.cuts.includes(WOOD) ? "all" : b.cuts.includes(WOOD) ? "wood" : "alu";
const cutsLabel = (b) => ({ wood: "wood only", alu: "aluminium only" })[cutsKey(b)] || "";
function bitName(b) {
  if (b.tool_type === SB) return `${+b.tool_d} mm surfacing bit ${b.flutes} inserts`;
  const t = b.tool_type === "Ball nose" ? "ball nose" : b.tool_type === VB ? `${b.tool_angle ?? 90} deg V-bit` : "flat";
  return `${+b.tool_d} mm ${t} ${b.flutes}F ${String(b.tool_mat || "carbide").toLowerCase()}`;
}
async function rackDialog() {
  if (WEB) { toast("The end mill rack is the one at the machine: Lev keeps it, and the web app gets it when he publishes. Your own speeds are under In the spindle.", 7000); return; }
  const { rack } = await api("rack");
  const draw = () => {
    const row = (b, i) => `<div class="rackrow"><label><input type="checkbox" data-i="${i}" ${b.owned !== false ? "checked" : ""}><span><b>${esc(b.name)}</b><br><small>${esc(bitSpec(b))}</small></span></label>
        <span class="rackact"><button class="link" data-edit="${i}">Edit</button><button class="link" data-del="${i}" title="Remove this end mill">✕</button></span></div>`;
    const known = BIT_CATS.map(([t]) => t);
    const groups = BIT_CATS.map(([t, label]) => [label, rack.map((b, i) => [b, i]).filter(([b]) => b.tool_type === t)])
      .concat([["Other", rack.map((b, i) => [b, i]).filter(([b]) => !known.includes(b.tool_type))]])
      .filter(([, list]) => list.length);
    sheet(`<h2>End mills at the machine</h2><small>Tick the end mills you have. An unticked end mill is only used when nothing else works, and the plan says to buy it.</small>
      <div class="rack">${groups.map(([label, list]) => `<h4 class="rackcat">${esc(label)}</h4>` + list.map(([b, i]) => row(b, i)).join("")).join("")}</div>
      <div class="btns"><button class="add" id="addbit">+ Add an end mill</button><span style="flex:1"></span><button id="cn">Cancel</button><button class="primary" id="sv">Save</button></div>`);
    $$(".rack input[type=checkbox]").forEach((c) => c.onchange = () => (rack[+c.dataset.i].owned = c.checked));
    $$("[data-edit]").forEach((b) => b.onclick = () => bitForm(rack, +b.dataset.edit, draw));
    $$("[data-del]").forEach((b) => b.onclick = () => { rack.splice(+b.dataset.del, 1); draw(); });
    $("#addbit").onclick = () => bitForm(rack, -1, draw);
    $("#cn").onclick = closeSheet;
    $("#sv").onclick = async () => { await api("rack", { rack }); st.info.rack = rack; closeSheet(); replan("End mills saved"); };
  };
  draw();
}
function bitForm(rack, i, back) {
  const b = i >= 0 ? { ...rack[i] } : { tool_type: "Flat end mill", tool_d: 6, flutes: 2, flute_len: 15, reach: 22, shank_d: 6, tool_mat: "Carbide", tool_angle: 90, owned: true };
  const auto = i < 0 || b.name === bitName(b);   // the name follows the sizes until it is typed by hand
  const num = (k, label, unit, step) => `<label class="nbl"><span>${label}</span><span class="nb" data-step="${step}"><input data-f="${k}" value="${b[k] ?? ""}" inputmode="decimal"><em>${unit}</em></span></label>`;
  sheet(`<h2>${i >= 0 ? "Edit end mill" : "Add an end mill"}</h2><small>From the end mill's packet or a calliper. Reach is how far the end mill sticks out of the collet nut and can still cut.</small>
    <div class="bitform">
      <label class="nbl"><span>Type</span><div class="mini-seg" id="btype">${BIT_TYPES.map(([v, l]) => `<button data-v="${v}" class="${b.tool_type === v ? "on" : ""}">${l}</button>`).join("")}</div></label>
      ${num("tool_d", "Diameter", "mm", 0.5)}
      <div id="vrow" ${b.tool_type === VB ? "" : "hidden"}>${num("tool_angle", "V angle", "°", 15)}</div>
      <div id="flrow">${num("flutes", b.tool_type === SB ? "Inserts" : "Flutes", "", 1)}</div>
      ${num("flute_len", b.tool_type === SB ? "Cutting height" : "Flute length", "mm", 1)}
      ${num("reach", "Reach", "mm", 1)}
      ${num("shank_d", "Shank", "mm", 0.5)}
      <label class="nbl"><span>Material</span><div class="mini-seg" id="bmat">${["Carbide", "HSS"].map((m) => `<button data-v="${m}" class="${b.tool_mat === m ? "on" : ""}">${m}</button>`).join("")}</div></label>
      <label class="nbl"><span>Cuts</span><div class="mini-seg" id="bcuts">${CUTS.map(([v, l]) => `<button data-v="${v}" class="${cutsKey(b) === v ? "on" : ""}">${l}</button>`).join("")}</div></label>
      <label class="nbl"><span>Name</span><input id="bname" value="${esc(b.name || bitName(b))}"></label>
      <label class="chk"><input type="checkbox" id="bown" ${b.owned !== false ? "checked" : ""}> I have this end mill</label>
      <p class="note" id="bnote"></p>
    </div>
    <div class="btns"><button id="bk">Back</button><span style="flex:1"></span><button class="primary" id="bok">${i >= 0 ? "Done" : "Add"}</button></div>`);
  let named = !auto;
  const read = () => {
    $$(".bitform [data-f]").forEach((inp) => { const v = parseFloat(String(inp.value).replace(",", ".")); b[inp.dataset.f] = Number.isFinite(v) ? v : null; });
    b.flutes = Math.round(b.flutes || 0);
    if (!named) $("#bname").value = bitName(b);
    const why = [];
    for (const [k, l] of [["tool_d", "diameter"], ["flutes", "flutes"], ["flute_len", "flute length"], ["reach", "reach"], ["shank_d", "shank"]]) if (!(b[k] > 0)) why.push(`the ${l}`);
    const shankMax = st.info.machine?.collet_max_shank;
    const warn = shankMax && b.shank_d > shankMax ? `The collet takes shanks up to ${shankMax} mm.` : b.reach && b.flute_len && b.reach < b.flute_len ? "Reach is shorter than the flutes: is that right?" : "";
    $("#bnote").textContent = why.length ? `Fill in ${why.join(", ")}.` : warn;
    $("#bok").disabled = why.length > 0;
  };
  $$(".bitform [data-f]").forEach((inp) => inp.oninput = read);
  $("#bname").oninput = () => (named = true);
  $$("#btype button").forEach((x) => x.onclick = (e) => { e.preventDefault(); b.tool_type = x.dataset.v; $$("#btype button").forEach((y) => y.classList.toggle("on", y === x)); $("#vrow").hidden = b.tool_type !== VB; $("#flrow .nbl span").textContent = b.tool_type === SB ? "Inserts" : "Flutes"; read(); });
  $$("#bcuts button").forEach((x) => x.onclick = (e) => {
    e.preventDefault();
    b.cuts = { all: [], wood: [WOOD], alu: [ALU] }[x.dataset.v];
    $$("#bcuts button").forEach((y) => y.classList.toggle("on", y === x));
  });
  $$("#bmat button").forEach((x) => x.onclick = (e) => { e.preventDefault(); b.tool_mat = x.dataset.v; $$("#bmat button").forEach((y) => y.classList.toggle("on", y === x)); read(); });
  read();
  $("#bk").onclick = back;
  $("#bok").onclick = () => {
    read(); if ($("#bok").disabled) return;
    const name = $("#bname").value.trim() || bitName(b);
    if (rack.some((x, k) => k !== i && x.name === name)) { $("#bnote").textContent = "Another end mill already has this name."; return; }
    const out = { name, tool_type: b.tool_type, tool_d: b.tool_d, flutes: b.flutes, flute_len: b.flute_len, reach: b.reach, shank_d: b.shank_d, tool_mat: b.tool_mat, owned: $("#bown").checked };
    if (b.tool_type === VB) out.tool_angle = b.tool_angle || 90;
    out.cuts = b.cuts?.length ? b.cuts : [];
    if (i >= 0 && !out.cuts.length) delete rack[i].cuts;
    if (i >= 0) rack[i] = { ...rack[i], ...out }; else rack.push(out);
    back();
  };
}

// ------------------------------------------------------------------ start
window.__acnc = { st, layers, camera, controls, scene, renderer, redraw: () => redraw() };   // for debugging in the browser
// who is working: asked once in each browser, remembered there; the app window opens as the first profile
function pickUser(users, first) {
  return new Promise((done) => {
    sheet(`<h2>${first ? "Who's working?" : "Switch designer"}</h2>
      <small>Your part, presets, speeds and preferences are kept under your name. The end mill rack is shared: a change to it is for everyone.</small>
      <div class="btns userlist">${users.map((u) => `<button data-u="${esc(u)}" class="${u === USER ? "primary" : ""}">${esc(u)}</button>`).join("")}</div>
      <div class="btns"><input id="newuser" maxlength="40" placeholder="New designer's name"><button id="adduser">Add</button>
      ${first ? "" : `<button id="ucancel">Cancel</button>`}</div>`);
    const go = (u) => { u = u.trim().replace(/[\\/:*?"<>|.]/g, ""); if (!u) return; store.set("acnc_user", u); closeSheet(); done(u); };
    $$("#sheet [data-u]").forEach((b) => b.onclick = () => go(b.dataset.u));
    $("#adduser").onclick = () => go($("#newuser").value);
    $("#newuser").onkeydown = (e) => { if (e.key === "Enter") go($("#newuser").value); };
    $("#ucancel") && ($("#ucancel").onclick = () => { closeSheet(); done(null); });
    if (first) $("#modal").onclick = null;   // a choice is needed
  });
}
$("#userbtn").onclick = async () => {
  const { users } = await api("users");
  const u = await pickUser(users, false);
  if (u && u !== USER) location.reload();   // the page starts again as that designer
};
(async function start() {
  const { users } = await api("users");
  if (!USER || !users.includes(USER)) {
    if (USER && !users.includes(USER)) USER = "";   // a profile someone removed
    USER = IN_APP && users.length ? users[0] : await pickUser(users, true);
    $("#modal").onclick = (e) => { if (e.target.id === "modal") closeSheet(); };
  }
  $("#userbtn").textContent = USER; $("#userbtn").hidden = false;
  st.info = await api("state");
  const pf = st.info.prefs || {};
  if (pf.material && st.info.materials[pf.material]) st.opts.material = pf.material;
  if (pf.finish && st.info.finishes[pf.finish]) st.opts.finish = pf.finish;
  if (pf.thickness) st.opts.thickness = pf.thickness;
  if (pf.extra != null) st.opts.extra = pf.extra;
  if (pf.tabs) Object.assign(st.opts.tabs, pf.tabs);
  if (["fewest", "recommended", "fastest"].includes(pf.strategy)) st.opts.strategy = pf.strategy;
  if (pf.params && typeof pf.params === "object") st.opts.params = { ...pf.params };
  $("#expert").checked = !!pf.expert;
  renderPresets();
  resize(); fit();
  poll();
})();
