// Agilite CNC web app: the page's /api, /bin and /dl requests go to the engine running in this browser
// (worker.js) instead of a server. Loaded before app.js; app.js itself is the same page as on Lev's PC.
window.ACNC_WEB = true;
(() => {
  const worker = new Worker("worker.js");
  const waiting = new Map();
  let seq = 0, busyCalls = 0, lastPoll = null, prog = [0, ""];

  // the first visit downloads Python and the engine (~30 MB, then cached): say so
  const cover = document.createElement("div");
  cover.id = "webload";
  cover.innerHTML = `<div><b>Agilite CNC</b><span>Loading the engine…</span><i><u></u></i><small>The first time takes a little while; after that it opens quickly.</small></div>`;
  const style = document.createElement("style");
  style.textContent = `#webload{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(244,245,247,.96);font:14px "Segoe UI",system-ui,sans-serif;color:#1d2733}
    #webload>div{display:flex;flex-direction:column;gap:10px;width:min(340px,86vw)}#webload b{font-size:20px}
    #webload i{display:block;height:6px;border-radius:3px;background:#dfe3e8;overflow:hidden}#webload u{display:block;height:100%;width:3%;background:#1f6fd1;transition:width .3s}
    #webload small{color:#66707c}`;
  const show = () => { if (!cover.isConnected && document.body) { document.head.appendChild(style); document.body.appendChild(cover); } };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", show) : show();

  worker.onmessage = (e) => {
    const d = e.data;
    if (d.log != null) { console.log(d.log); return; }
    if (d.progress) {
      prog = d.progress;
      if (cover.isConnected) { cover.querySelector("span").textContent = prog[1] + "…"; cover.querySelector("u").style.width = Math.round(prog[0] * 100) + "%"; }
      return;
    }
    if (d.ready) { cover.remove(); prog = [0, ""]; return; }
    if (d.failed) { show(); cover.querySelector("span").textContent = "The engine couldn't load: " + d.failed + ". Reload the page to try again."; return; }
    const done = waiting.get(d.id);
    waiting.delete(d.id);
    done && done(d);
  };
  const ask = (msg) => new Promise((res) => { const id = ++seq; waiting.set(id, res); msg.id = id; worker.postMessage(msg, msg.body ? [msg.body.buffer] : []); });
  window.__acncDebug = async (code) => new TextDecoder().decode((await ask({ debug: code })).data);
  const json = (o) => new Response(JSON.stringify(o), { status: 200, headers: { "Content-Type": "application/json" } });

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!/^\/(api|bin|dl)\//.test(url)) return realFetch(input, init);
    const poll = url === "/api/poll";
    // the engine works on one thing at a time: while it plans, the page's polls are answered here
    if (poll && busyCalls) {
      const j = { ...(lastPoll || { gen: -1 }), busy: true, progress: prog[0], text: prog[1], error: null };
      delete j.plan;
      return json(j);
    }
    const h = init.headers || {};
    let body = init.body;
    if (typeof body === "string") body = new TextEncoder().encode(body);
    else if (body instanceof ArrayBuffer) body = new Uint8Array(body);
    else if (body && !(body instanceof Uint8Array)) body = new Uint8Array(await new Response(body).arrayBuffer());
    if (!poll) busyCalls++;
    try {
      const r = await ask({ method: init.method || "GET", url, user: decodeURIComponent(h["X-ACNC-User"] || ""), body: body || null });
      if (poll && r.status === 200) { try { lastPoll = JSON.parse(new TextDecoder().decode(r.data)); } catch { /* keep the last */ } }
      return new Response(r.data, { status: r.status, headers: { "Content-Type": r.ctype } });
    } finally { if (!poll) busyCalls--; }
  };
})();
