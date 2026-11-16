// Agilite CNC: GPU material-removal preview.
//
// The stock is a height field (one float per cell, top of the material, mm) kept in a float render
// target. Every cutting move is stamped into it on the GPU with MIN blending: an instanced quad that
// covers the move's swept footprint, whose fragment shader writes the bottom of the tool at that cell
// (flat, ball or V profile, z interpolated along the move). A max-pyramid (quadtree) is rebuilt after
// each stamp, and the stock is drawn by raymarching that pyramid from a box proxy, so walls are exact
// per pixel, holes are see-through and the shading normals come from the smoothed field.
//
// Z is up, units mm, playback time in machine minutes (as app.js).
import * as THREE from "three";

const EMPTY = -1.0e4;          // "no material" (fits a half float)
const TOP = 0.01;              // box proxy top, just above the stock top (z = 0)
const MOVE_TEX_W = 4096;       // moves texture width (3 RGBA texels per cutting move)
const CHUNK = 65536;           // instances per stamp draw call

const toolCode = (t) => {
  const s = String(t?.tool_type || "").toLowerCase();
  if (s.startsWith("ball")) return 1;
  if (s.startsWith("v")) return 2;
  return 0;
};

// ------------------------------------------------------------------ shaders
const STAMP_VS = /* glsl */`
uniform highp sampler2D uMoves;
uniform int uBase, uTexW, uSingle;
uniform vec4 uA, uB, uC;
uniform vec4 uMap;        // world origin xy, 1 / covered size xy
uniform float uMargin;
varying vec2 vP;
flat varying vec4 vA;
flat varying vec4 vB;
flat varying float vK;
void main() {
  vec4 A, B, C;
  if (uSingle == 1) { A = uA; B = uB; C = uC; }
  else {
    int i = (uBase + gl_InstanceID) * 3;
    A = texelFetch(uMoves, ivec2(i % uTexW, i / uTexW), 0); i++;
    B = texelFetch(uMoves, ivec2(i % uTexW, i / uTexW), 0); i++;
    C = texelFetch(uMoves, ivec2(i % uTexW, i / uTexW), 0);
  }
  float R = B.w;
  vec2 d = B.xy - A.xy; float L = length(d);
  vec2 u = L > 1e-6 ? d / L : vec2(1.0, 0.0);
  vec2 n = vec2(-u.y, u.x);
  float m = R + uMargin;
  vec2 p = A.xy + u * mix(-m, L + m, position.x) + n * (position.y * m);
  vP = p; vA = A; vB = B; vK = C.x;
  gl_Position = vec4((p - uMap.xy) * uMap.zw * 2.0 - 1.0, 0.0, 1.0);
}`;

const STAMP_FS = /* glsl */`
varying vec2 vP;
flat varying vec4 vA;
flat varying vec4 vB;
flat varying float vK;
float prof(float type, float R, float r) {
  if (type < 1.5) return R - sqrt(max(R * R - r * r, 0.0));   // ball
  return r * vK;                                               // V-bit
}
void main() {
  vec2 p = vP;
  vec3 a = vA.xyz, b = vB.xyz;
  float R = vB.w, type = vA.w;
  vec2 e = a.xy - p, v = b.xy - a.xy;
  float A2 = dot(v, v), Bq = 2.0 * dot(e, v), Cq = dot(e, e) - R * R;
  float s0, s1;
  if (A2 < 1e-10) { if (Cq > 0.0) discard; s0 = 0.0; s1 = 1.0; }
  else {
    float disc = Bq * Bq - 4.0 * A2 * Cq;
    if (disc < 0.0) discard;
    float sq = sqrt(disc);
    s0 = max((-Bq - sq) / (2.0 * A2), 0.0);
    s1 = min((-Bq + sq) / (2.0 * A2), 1.0);
    if (s0 > s1) discard;
  }
  float h;
  if (type < 0.5) h = min(mix(a.z, b.z, s0), mix(a.z, b.z, s1));      // flat: exact
  else {
    h = 1e9;
    for (int i = 0; i <= 8; i++) {
      float s = mix(s0, s1, float(i) / 8.0);
      h = min(h, mix(a.z, b.z, s) + prof(type, R, length(a.xy + v * s - p)));
    }
    float sp = A2 > 1e-10 ? clamp(-dot(e, v) / A2, s0, s1) : s0;
    h = min(h, mix(a.z, b.z, sp) + prof(type, R, length(a.xy + v * sp - p)));
  }
  gl_FragColor = vec4(h, 0.0, 0.0, 1.0);
}`;

const QUAD_VS = /* glsl */`void main() { gl_Position = vec4(position.xy, 0.0, 1.0); }`;

// a slug that falls out: its area (triangles in world mm) written as "no material"
const DROP_VS = /* glsl */`
uniform vec4 uMap;
void main() { gl_Position = vec4((position.xy - uMap.xy) * uMap.zw * 2.0 - 1.0, 0.0, 1.0); }`;
const DROP_FS = /* glsl */`void main() { gl_FragColor = vec4(${EMPTY.toFixed(1)}, 0.0, 0.0, 1.0); }`;

const REDUCE_FS = /* glsl */`
uniform highp sampler2D uSrc;
uniform int uFirst;
uniform ivec2 uStock;
uniform float uBot;
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy) * 2;
  float m = ${EMPTY.toFixed(1)};
  for (int j = 0; j < 2; j++) for (int i = 0; i < 2; i++) {
    ivec2 q = c + ivec2(i, j);
    float h = texelFetch(uSrc, q, 0).r;
    if (uFirst == 1 && (q.x >= uStock.x || q.y >= uStock.y || h <= uBot)) h = ${EMPTY.toFixed(1)};
    m = max(m, h);
  }
  gl_FragColor = vec4(m, 0.0, 0.0, 1.0);
}`;

const RENDER_VS = /* glsl */`
varying vec3 vL;
void main() { vL = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

const RENDER_FS = /* glsl */`
uniform highp sampler2D uH0;
uniform highp sampler2D uH1;
uniform highp sampler2D uH2;
uniform highp sampler2D uH3;
uniform highp sampler2D uH4;
uniform highp sampler2D uH5;
uniform highp sampler2D uH6;
uniform highp sampler2D uH7;
uniform int uLevels;
uniform ivec2 uN, uPad;
uniform vec2 uCell, uOrig;
uniform float uBot;
uniform vec3 uCam, uCamDir;
uniform int uOrtho;
uniform mat4 uMVP;
uniform vec3 uColour;
uniform vec3 uSun, uFill;
varying vec3 vL;
const float EMPTY = ${EMPTY.toFixed(1)};

float h0(ivec2 c) {
  float h = EMPTY;
  if (c.x >= 0 && c.y >= 0 && c.x < uN.x && c.y < uN.y) {
    h = texelFetch(uH0, c, 0).r;
    if (h <= uBot) h = EMPTY;
  }
  return h;
}
float hl(int lev, ivec2 c) {
  float h = EMPTY;
  if (lev == 0) h = h0(c);
  else if (c.x >= 0 && c.y >= 0 && c.x < (uPad.x >> lev) && c.y < (uPad.y >> lev)) {
    if (lev == 1) h = texelFetch(uH1, c, 0).r;
    else if (lev == 2) h = texelFetch(uH2, c, 0).r;
    else if (lev == 3) h = texelFetch(uH3, c, 0).r;
    else if (lev == 4) h = texelFetch(uH4, c, 0).r;
    else if (lev == 5) h = texelFetch(uH5, c, 0).r;
    else if (lev == 6) h = texelFetch(uH6, c, 0).r;
    else h = texelFetch(uH7, c, 0).r;
  }
  return h;
}
// smooth (bilinear) height for normals; empty cells read as a little below the bottom
float hs(ivec2 c) { return max(h0(c), uBot - 0.4); }
float hb(vec2 q) {
  vec2 f = q - 0.5; ivec2 i = ivec2(floor(f)); vec2 w = fract(f);
  return mix(mix(hs(i), hs(i + ivec2(1, 0)), w.x), mix(hs(i + ivec2(0, 1)), hs(i + ivec2(1, 1)), w.x), w.y);
}
vec2 grad(vec2 q, float s) {   // Sobel on the bilinear field, mm per mm
  float a = hb(q + vec2(-s, -s)), b = hb(q + vec2(0.0, -s)), c = hb(q + vec2(s, -s));
  float d = hb(q + vec2(-s, 0.0)),                        f = hb(q + vec2(s, 0.0));
  float g = hb(q + vec2(-s, s)), h = hb(q + vec2(0.0, s)), k = hb(q + vec2(s, s));
  float gx = ((c + 2.0 * f + k) - (a + 2.0 * d + g)) / (8.0 * s);
  float gy = ((g + 2.0 * h + k) - (a + 2.0 * b + c)) / (8.0 * s);
  return vec2(gx / uCell.x, gy / uCell.y);
}

void main() {
  vec3 ro = uOrtho == 1 ? vL - uCamDir * 1.0e4 : uCam;
  vec3 rdw = uOrtho == 1 ? uCamDir : vL - uCam;
  vec3 o = vec3((ro.xy - uOrig) / uCell, ro.z);
  vec3 d = vec3(rdw.xy / uCell, rdw.z);
  d = vec3(abs(d.x) < 1e-9 ? 1e-9 : d.x, abs(d.y) < 1e-9 ? 1e-9 : d.y, abs(d.z) < 1e-9 ? 1e-9 : d.z);
  vec3 bmin = vec3(0.0, 0.0, uBot), bmax = vec3(vec2(uN), ${TOP.toFixed(3)});
  vec3 t1 = (bmin - o) / d, t2 = (bmax - o) / d;
  vec3 tn = min(t1, t2), tf = max(t1, t2);
  float tEnter = max(max(tn.x, tn.y), tn.z), tExit = min(min(tf.x, tf.y), tf.z);
  int axis = tn.x >= tn.y && tn.x >= tn.z ? 0 : (tn.y >= tn.z ? 1 : 2);
  bool inside = tEnter < 0.0;
  tEnter = max(tEnter, 0.0);
  if (tEnter > tExit) discard;

  float t = tEnter;
  int lev = uLevels;
  int kind = -1;            // 0 top, 1 wall, 2 box face
  bool atEntry = !inside;
  vec2 bias = sign(d.xy) * 2e-3;
  for (int it = 0; it < 320; it++) {
    vec3 p = o + d * t;
    if (t > tExit) break;
    float size = float(1 << lev);
    ivec2 cell = ivec2(floor((p.xy + bias) / size));
    float h = hl(lev, cell);
    vec2 cmin = vec2(cell) * size;
    vec2 txy = ((cmin + step(0.0, d.xy) * size) - o.xy) / d.xy;
    float tc = min(min(txy.x, txy.y), tExit);
    float zIn = p.z, zOut = o.z + d.z * tc;
    if (min(zIn, zOut) > h) {           // the ray stays above this cell: skip it
      t = tc; atEntry = false;
      if (lev < uLevels) lev++;
      continue;
    }
    if (zIn > h - 1e-4 && d.z < 0.0) {  // the ray goes down through h inside this cell
      float th = (h - o.z) / d.z;
      if (th > t) { t = th; atEntry = false; }
      if (lev == 0) { kind = 0; break; }
      lev--; continue;
    }
    if (lev == 0) { kind = atEntry && axis != 2 ? 2 : (atEntry && axis == 2 ? 3 : 1); break; }
    lev--;
  }
  if (kind < 0) discard;

  vec3 pc = o + d * t;                                  // cell coords
  vec3 P = vec3(uOrig + pc.xy * uCell, pc.z);           // local mm
  vec3 n;
  float tint = 1.0;
  bool cut = false;
  if (kind == 2) { n = vec3(0.0); n[axis] = -sign(d[axis]); }
  else if (kind == 3) { n = vec3(0.0, 0.0, -1.0); tint = 0.85; }
  else {
    // one smooth normal for floors, slopes and walls alike: the cell staircase never shows
    vec2 g = grad(pc.xy, 1.25);
    n = normalize(vec3(-g, 1.0));
    if (n.z < 0.5) {                   // a wall: take its direction from a wider kernel, so curved walls shade smoothly
      vec2 gw = grad(pc.xy, 3.0);
      if (dot(gw, gw) > 1e-6) {
        n = normalize(vec3(-gw, 1.0));
        // a near-vertical wall is shaded as exactly vertical, so the cell staircase can't stripe it
        n = normalize(vec3(n.xy, n.z * smoothstep(0.12, 0.35, n.z)));
      }
    }
    if (kind == 1 && dot(g, g) < 1e-6) {
      vec2 fr = abs(fract(pc.xy + 0.5) - 0.5);
      n = fr.x < fr.y ? vec3(-sign(d.x), 0.0, 0.0) : vec3(0.0, -sign(d.y), 0.0);
    }
    cut = pc.z < -0.01 || n.z < 0.99;
    tint = mix(0.86, 1.0, smoothstep(0.2, 0.6, n.z));   // steep walls a touch darker
  }

  vec3 V = normalize(-rdw);
  vec3 base = uColour;
  if (cut) base = min(base * 1.10 + 0.035, vec3(1.0));
  // three's physical lights, approximated: hemisphere 1.35 (sky white, ground 0x8a9099), sun 1.6, fill 0.45
  vec3 ground = vec3(0.254, 0.279, 0.319);
  vec3 hemi = mix(ground, vec3(1.0), n.z * 0.5 + 0.5) * 1.35;
  float ns = max(dot(n, uSun), 0.0), nf = max(dot(n, uFill), 0.0);
  vec3 irr = hemi + vec3(1.6 * ns + 0.45 * nf);
  vec3 col = base * tint * 0.9 * irr * 0.3183;
  vec3 H = normalize(uSun + V);
  float spec = pow(max(dot(n, H), 0.0), 48.0) * ns;
  vec3 Hf = normalize(uFill + V);
  spec += 0.3 * pow(max(dot(n, Hf), 0.0), 48.0) * nf;
  col += vec3(0.16 * spec);
  // a faint dark rim where a cut meets the untouched top reads like a real edge
  gl_FragColor = vec4(col, 1.0);
  vec4 clip = uMVP * vec4(P, 1.0);
  gl_FragDepth = clamp(clip.z / clip.w * 0.5 + 0.5, 0.0, 1.0);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}`;

// ------------------------------------------------------------------ the class
export class StockSim {
  constructor(renderer, { stock, colour = [0.78, 0.8, 0.84], maxCells = 4_000_000 } = {}) {
    this.renderer = renderer;
    this.gl = renderer.getContext();
    this.stock = { ...stock };
    const ext = renderer.extensions;
    if (!ext.has("EXT_color_buffer_float")) throw new Error("StockSim needs WebGL2 EXT_color_buffer_float");
    this.floatBlend = ext.has("EXT_float_blend");
    if (this.floatBlend) ext.get("EXT_float_blend");
    const type = this.floatBlend ? THREE.FloatType : THREE.HalfFloatType;
    this.type = type;

    // grid: cells cover the stock exactly, padded up to a multiple of 2^levels
    const { w, h, t } = this.stock;
    const maxTex = renderer.capabilities.maxTextureSize;
    let c = Math.sqrt((w * h) / maxCells);
    c = Math.max(c, 0.02, w / (maxTex - 128), h / (maxTex - 128));
    const nx = Math.max(8, Math.round(w / c)), ny = Math.max(8, Math.round(h / c));
    let L = 0;
    while (L < 7 && (Math.min(nx, ny) >> (L + 1)) >= 2) L++;
    const q = 1 << L;
    const px = Math.ceil(nx / q) * q, py = Math.ceil(ny / q) * q;
    Object.assign(this, { nx, ny, px, py, levels: L, cx: w / nx, cy: h / ny });

    const rtOpts = { type, format: THREE.RedFormat, depthBuffer: false, stencilBuffer: false,
      minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, generateMipmaps: false };
    this.rt = new THREE.WebGLRenderTarget(px, py, rtOpts);
    this.pyr = [];
    for (let k = 1; k <= L; k++) this.pyr.push(new THREE.WebGLRenderTarget(px >> k, py >> k, rtOpts));

    this.camOrtho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

    // stamping
    const qg = new THREE.InstancedBufferGeometry();
    qg.setAttribute("position", new THREE.Float32BufferAttribute([0, -1, 0, 1, -1, 0, 1, 1, 0, 0, 1, 0], 3));
    qg.setIndex([0, 1, 2, 0, 2, 3]);
    qg.instanceCount = 0;
    this.stampMat = new THREE.ShaderMaterial({
      vertexShader: STAMP_VS, fragmentShader: STAMP_FS,
      uniforms: {
        uMoves: { value: null }, uBase: { value: 0 }, uTexW: { value: MOVE_TEX_W }, uSingle: { value: 0 },
        uA: { value: new THREE.Vector4() }, uB: { value: new THREE.Vector4() }, uC: { value: new THREE.Vector4() },
        uMap: { value: new THREE.Vector4(this.stock.x0, this.stock.y0, 1 / (px * this.cx), 1 / (py * this.cy)) },
        uMargin: { value: 1.5 * Math.max(this.cx, this.cy) },
      },
      blending: THREE.CustomBlending, blendEquation: THREE.MinEquation,
      blendSrc: THREE.OneFactor, blendDst: THREE.OneFactor,
      depthTest: false, depthWrite: false, side: THREE.DoubleSide,
    });
    this.stampMesh = new THREE.Mesh(qg, this.stampMat);
    this.stampMesh.frustumCulled = false;
    this.stampScene = new THREE.Scene(); this.stampScene.add(this.stampMesh);

    // pyramid reduction
    this.reduceMat = new THREE.ShaderMaterial({
      vertexShader: QUAD_VS, fragmentShader: REDUCE_FS,
      uniforms: { uSrc: { value: null }, uFirst: { value: 1 }, uStock: { value: new THREE.Vector2(nx, ny) }, uBot: { value: -t + 0.005 } },
      depthTest: false, depthWrite: false, blending: THREE.NoBlending,
    });
    const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.reduceMat);
    quad.frustumCulled = false;
    this.quadScene = new THREE.Scene(); this.quadScene.add(quad);

    // the visible stock: a box proxy, raymarched
    const H = [this.rt.texture, ...this.pyr.map((r) => r.texture)];
    while (H.length < 8) H.push(this.rt.texture);
    const uniforms = {
      uLevels: { value: L }, uN: { value: new THREE.Vector2(nx, ny) }, uPad: { value: new THREE.Vector2(px, py) },
      uCell: { value: new THREE.Vector2(this.cx, this.cy) }, uOrig: { value: new THREE.Vector2(this.stock.x0, this.stock.y0) },
      uBot: { value: -t + 0.005 }, uCam: { value: new THREE.Vector3() }, uCamDir: { value: new THREE.Vector3() },
      uOrtho: { value: 0 }, uMVP: { value: new THREE.Matrix4() }, uColour: { value: new THREE.Vector3(...colour) },
      uSun: { value: new THREE.Vector3(-0.6, -0.9, 1.4).normalize() }, uFill: { value: new THREE.Vector3(0.8, 0.5, 0.6).normalize() },
    };
    H.forEach((tex, i) => (uniforms["uH" + i] = { value: tex }));
    this.uniforms = uniforms;
    // uivec2 uniforms: three uploads Vector2 to ivec2 via uniform2iv
    this.mat = new THREE.ShaderMaterial({ vertexShader: RENDER_VS, fragmentShader: RENDER_FS, uniforms, side: THREE.BackSide });
    const box = new THREE.BoxGeometry(w, h, t + TOP);
    box.translate(this.stock.x0 + w / 2, this.stock.y0 + h / 2, (TOP - t) / 2);
    this.mesh = new THREE.Mesh(box, this.mat);
    const inv = new THREE.Matrix4(), tmp = new THREE.Vector3();
    this.mesh.onBeforeRender = (r, s, cam) => {
      inv.copy(this.mesh.matrixWorld).invert();
      uniforms.uCam.value.setFromMatrixPosition(cam.matrixWorld).applyMatrix4(inv);
      uniforms.uOrtho.value = cam.isOrthographicCamera ? 1 : 0;
      cam.getWorldDirection(tmp); uniforms.uCamDir.value.copy(tmp).transformDirection(inv);
      uniforms.uMVP.value.multiplyMatrices(cam.projectionMatrix, cam.matrixWorldInverse).multiply(this.mesh.matrixWorld);
    };
    this.object3d = new THREE.Group();
    this.object3d.name = "StockSim";
    this.object3d.add(this.mesh);

    // slugs: holes without tabs fall out once the cut round them is through
    this.dropMat = new THREE.ShaderMaterial({
      vertexShader: DROP_VS, fragmentShader: DROP_FS, uniforms: { uMap: this.stampMat.uniforms.uMap },
      blending: THREE.CustomBlending, blendEquation: THREE.MinEquation, blendSrc: THREE.OneFactor, blendDst: THREE.OneFactor,
      depthTest: false, depthWrite: false, side: THREE.DoubleSide,
    });
    this.dropScene = new THREE.Scene();
    this.drops = []; this._lastDropCheck = 0;

    this.n = 0; this._t = -1; this._done = 0;
    this.stats = { lastStampMs: 0, lastMoves: 0, resets: 0 };
    this._clear();
    this._reduce();
  }

  setColour(rgb) { this.uniforms.uColour.value.set(...rgb); }

  /** segs: n×8 floats (x0 y0 z0 x1 y1 z1 stage rapid); cum: n end times (min); stageTools[stage] = {tool_type, tool_d, tool_angle} */
  setMoves(segs, cum, stageTools) {
    const n = Math.min(cum.length, Math.floor(segs.length / 8));
    this.segs = segs; this.cum = cum; this.n = n;
    this.total = n ? cum[n - 1] : 0;
    const prefix = new Int32Array(n + 1);
    let m = 0;
    for (let k = 0; k < n; k++) { prefix[k] = m; if (segs[k * 8 + 7] <= 0.5) m++; }
    prefix[n] = m;
    this.prefix = prefix; this.nCut = m;
    const tools = (stageTools || []).map((tl) => {
      const type = toolCode(tl), R = Math.max(0.05, (+tl?.tool_d || 3) / 2);
      const ang = +tl?.tool_angle || 90;
      const kk = type === 2 ? 1 / Math.tan(THREE.MathUtils.degToRad(Math.max(5, Math.min(175, ang)) / 2)) : 0;
      return [type, R, kk];
    });
    this.tools = tools;
    const toolOf = (si) => tools[si] || tools[0] || [0, 1.5, 0];
    const rows = Math.max(1, Math.ceil((m * 3) / MOVE_TEX_W));
    const data = new Float32Array(MOVE_TEX_W * rows * 4);
    let j = 0;
    for (let k = 0; k < n; k++) {
      const o = k * 8;
      if (segs[o + 7] > 0.5) continue;
      const [type, R, kk] = toolOf(segs[o + 6] | 0);
      data[j] = segs[o]; data[j + 1] = segs[o + 1]; data[j + 2] = segs[o + 2]; data[j + 3] = type;
      data[j + 4] = segs[o + 3]; data[j + 5] = segs[o + 4]; data[j + 6] = segs[o + 5]; data[j + 7] = R;
      data[j + 8] = kk;
      j += 12;
    }
    if (this.moveTex) this.moveTex.dispose();
    const tex = new THREE.DataTexture(data, MOVE_TEX_W, rows, THREE.RGBAFormat, THREE.FloatType);
    tex.minFilter = tex.magFilter = THREE.NearestFilter; tex.generateMipmaps = false; tex.needsUpdate = true;
    this.moveTex = tex;
    this.stampMat.uniforms.uMoves.value = tex;
    this._t = -1; this._done = 0;
    this.drops.forEach((d) => (d.done = false));
    this._clear(); this._reduce();
  }

  /** holes whose middle falls out when cut free: [{outer: [[x, y], ...], inner: [[[x, y], ...], ...] (islands that
   *  stay), samples: [[x, y], ...] (points just inside the outline that must all be cut through)}] */
  setDrops(list) {
    this.dropScene.clear();
    this.drops = [];
    for (const d of list || []) {
      try {
        const V = (p) => new THREE.Vector2(p[0], p[1]);
        const outer = d.outer.map(V), inner = (d.inner || []).map((h) => h.map(V));
        const tris = THREE.ShapeUtils.triangulateShape(outer, inner);
        const all = outer.concat(...inner), pos = [];
        for (const tri of tris) for (const i of tri) pos.push(all[i].x, all[i].y, 0);
        if (!pos.length) continue;
        const g = new THREE.BufferGeometry();
        g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
        const mesh = new THREE.Mesh(g, this.dropMat);
        mesh.frustumCulled = false; mesh.visible = false;
        this.dropScene.add(mesh);
        this.drops.push({ mesh, samples: d.samples, done: false });
      } catch (e) { /* a shape that won't triangulate just stays */ }
    }
  }

  _checkDrops(force) {
    if (!this.drops.length) return;
    const now = performance.now();
    if (!force && now - this._lastDropCheck < 150) return;
    this._lastDropCheck = now;
    const bot = -this.stock.t + 0.02;
    let any = false;
    for (const d of this.drops) {
      if (d.done) continue;
      let free = d.samples.length > 0;
      for (const [x, y] of d.samples) {
        const h = this.heightAt(x, y);
        if (Number.isFinite(h) && h > bot) { free = false; break; }
      }
      if (free) { d.done = true; d.mesh.visible = true; any = true; }
    }
    if (any) {   // draw every fallen slug (drawing one twice changes nothing: MIN blending)
      this.dropScene.children.forEach((m, i) => (m.visible = this.drops[i].done));
      this._withTarget(this.rt, () => this.renderer.render(this.dropScene, this.camOrtho));
    }
  }

  /** show the stock after every move up to time t (minutes); the running move is cut up to the tool */
  setTime(t) {
    if (!this.n) return;
    t = Math.max(0, Math.min(t, this.total));
    if (t === this._t) return;
    const t0 = performance.now();
    let stamped = 0;
    if (t < this._t) { this._clear(); this._done = 0; this.stats.resets++; this.drops.forEach((d) => (d.done = false)); }
    const cum = this.cum;
    let k;
    if (t >= this.total) k = this.n;
    else { let lo = 0, hi = this.n; while (lo < hi) { const mid = (lo + hi) >> 1; if (cum[mid] < t) lo = mid + 1; else hi = mid; } k = lo; }
    if (k > this._done) {
      const a = this.prefix[this._done], b = this.prefix[k];
      stamped += b - a;
      this._stampRange(a, b);
      this._done = k;
    }
    if (k < this.n && this.segs[k * 8 + 7] <= 0.5) {
      const ta = k > 0 ? cum[k - 1] : 0, tb = cum[k];
      const f = tb > ta ? (t - ta) / (tb - ta) : 1;
      if (f > 0) { this._stampPartial(k, Math.min(1, f)); stamped++; }
    }
    this._t = t;
    if (stamped) this._checkDrops(k >= this.n);
    this._reduce();
    this.stats.lastStampMs = performance.now() - t0;   // CPU-side submit time; the GPU runs async
    this.stats.lastMoves = stamped;
  }

  finish() { this.setTime(Infinity); }

  /** read back heights (debug / tests): {nx, ny, cx, cy, x0, y0, data: Float32Array nx*ny, row-major from y0} */
  readHeights() {
    const { nx, ny } = this;
    const buf = new Float32Array(nx * ny * 4);
    this._withTarget(this.rt, () => this.gl.readPixels(0, 0, nx, ny, this.gl.RGBA, this.gl.FLOAT, buf));
    const data = new Float32Array(nx * ny);
    for (let i = 0; i < nx * ny; i++) data[i] = buf[i * 4];
    return { nx, ny, cx: this.cx, cy: this.cy, x0: this.stock.x0, y0: this.stock.y0, data };
  }

  /** height at one point (mm), read from the GPU; NaN outside the stock */
  heightAt(x, y) {
    const i = Math.floor((x - this.stock.x0) / this.cx), j = Math.floor((y - this.stock.y0) / this.cy);
    if (i < 0 || j < 0 || i >= this.nx || j >= this.ny) return NaN;
    const buf = new Float32Array(4);
    this._withTarget(this.rt, () => this.gl.readPixels(i, j, 1, 1, this.gl.RGBA, this.gl.FLOAT, buf));
    return buf[0];
  }

  dispose() {
    this.rt.dispose(); this.pyr.forEach((r) => r.dispose());
    if (this.moveTex) this.moveTex.dispose();
    this.stampMesh.geometry.dispose(); this.stampMat.dispose();
    this.quadScene.children[0].geometry.dispose(); this.reduceMat.dispose();
    this.mesh.geometry.dispose(); this.mat.dispose();
    this.object3d.removeFromParent();
    this.n = 0;
  }

  // ---------------------------------------------------------------- internals
  _withTarget(rt, fn) {
    const r = this.renderer;
    const prevRT = r.getRenderTarget(), prevAuto = r.autoClear;
    r.autoClear = false;
    r.setRenderTarget(rt);
    try { fn(); } finally { r.setRenderTarget(prevRT); r.autoClear = prevAuto; }
  }
  _clear() {
    const r = this.renderer, cc = new THREE.Color(), a = r.getClearAlpha();
    r.getClearColor(cc);
    this._withTarget(this.rt, () => { r.setClearColor(0x000000, 0); r.clear(true, false, false); });
    r.setClearColor(cc, a);
  }
  _stampRange(a, b) {
    const u = this.stampMat.uniforms, g = this.stampMesh.geometry;
    u.uSingle.value = 0;
    this._withTarget(this.rt, () => {
      for (let s = a; s < b; s += CHUNK) {
        u.uBase.value = s;
        g.instanceCount = Math.min(CHUNK, b - s);
        this.renderer.render(this.stampScene, this.camOrtho);
      }
    });
  }
  _stampPartial(k, f) {
    const s = this.segs, o = k * 8, u = this.stampMat.uniforms, g = this.stampMesh.geometry;
    const tl = this.tools[s[o + 6] | 0] || this.tools[0] || [0, 1.5, 0];
    u.uSingle.value = 1;
    u.uA.value.set(s[o], s[o + 1], s[o + 2], tl[0]);
    u.uB.value.set(s[o] + (s[o + 3] - s[o]) * f, s[o + 1] + (s[o + 4] - s[o + 1]) * f, s[o + 2] + (s[o + 5] - s[o + 2]) * f, tl[1]);
    u.uC.value.set(tl[2], 0, 0, 0);
    g.instanceCount = 1;
    this._withTarget(this.rt, () => this.renderer.render(this.stampScene, this.camOrtho));
    u.uSingle.value = 0;
  }
  _reduce() {
    const u = this.reduceMat.uniforms;
    let src = this.rt;
    this.pyr.forEach((dst, i) => {
      u.uSrc.value = src.texture; u.uFirst.value = i === 0 ? 1 : 0;
      this._withTarget(dst, () => this.renderer.render(this.quadScene, this.camOrtho));
      src = dst;
    });
  }
}
