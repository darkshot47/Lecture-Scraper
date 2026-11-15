/*
 * Lecture Relay — browser side
 * ============================
 * Server (Vercel, Mumbai) se segments chunks me aate hain. Yahan:
 *   TS  -> mux.js transmux -> fMP4
 *   fMP4-> seedha jod ke, File System Access API (ya Blob) se MP4 save
 *
 * Kuch store nahi hota; token sirf is page ke memory me rehta hai.
 */

const $ = (id) => document.getElementById(id);
const state = { token: "", plan: null, busy: false };

function log(msg, cls) {
  const el = $("log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function normalizeToken(raw) {
  const v = (raw || "").trim();
  const m = v.match(/playerToken=([^&\s]+)/);
  return m ? m[1] : v;
}

async function api(params) {
  const qs = new URLSearchParams(params).toString();
  const r = await fetch("/api/relay?" + qs);
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("json")) {
    const txt = await r.text();
    throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
  }
  const j = await r.json();
  if (j.ok === false) throw new Error(j.error || "server error");
  return j;
}

/* ------------------------------------------------------------- 1. inspect */

async function doInspect() {
  const token = normalizeToken($("tok").value);
  if (!token) return log("token daalein", "bad");
  state.token = token;
  try {
    const j = await api({ action: "inspect", token });
    log(`token OK · region=${j.region} · auth ${j.authAgeMinutes} min purana`, "ok");
    if (j.cloudfront) {
      log(`token ke andar CloudFront signature: ${j.cloudfront.iso} ` +
          (j.cloudfront.valid ? "(valid)" : "(expired — par player host khud sign karta hai)"), "dim");
    }
    log("videoUrl: " + j.videoUrl);
    return j;
  } catch (e) {
    log("inspect fail: " + e.message, "bad");
  }
}

/* ------------------------------------------------------------ 2. selftest */

async function doSelftest() {
  const token = normalizeToken($("tok").value) || state.token;
  if (!token) return log("pehle token daalein", "bad");
  state.token = token;
  try {
    const j = await api({ action: "selftest", token });
    log(`== player reachability (region ${j.region}) ==`);
    for (const p of j.probes) {
      const ok = p.status === 200;
      log(`${p.name.padEnd(8)} -> ${p.status} ${p.bytes || 0}B ${p.ms || ""}ms ${p.server || ""}`,
          ok ? "ok" : "bad");
      if (!ok && p.error) log("   " + p.error, "dim");
    }
    const any = j.probes.some((p) => p.status === 200);
    if (any) log("INDIA SE PLAYER KHUL GAYA ✅ ab MP4 bana sakte hain", "ok");
    else log("player ne yahan bhi mana kiya — agla step: region badalna", "bad");
    return j;
  } catch (e) {
    log("selftest fail: " + e.message, "bad");
  }
}

/* ---------------------------------------------------------------- 3. plan */

async function doPlan() {
  const token = state.token || normalizeToken($("tok").value);
  if (!token) return log("pehle token daalein", "bad");
  try {
    const j = await api({ action: "plan", token });
    state.plan = j;
    log(`plan OK · ${j.segments} segments · ${Math.round(j.durationSec / 60)} min · ` +
        `kind=${j.kind} · aes=${j.encrypted} · host=${j.host}`, "ok");
    return j;
  } catch (e) {
    log("plan fail: " + e.message, "bad");
  }
}

/* ----------------------------------------------------- 4. full MP4 build */

let muxjsPromise = null;
function loadMuxjs() {
  if (!muxjsPromise) {
    muxjsPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/api/relay?action=muxjs";
      s.onload = () => resolve(window.muxjs);
      s.onerror = () => reject(new Error("mux.js load fail"));
      document.head.appendChild(s);
    });
  }
  return muxjsPromise;
}

function makeTransmuxer(mux) {
  const T = mux.mp4.Transmuxer;
  const t = new T({ remux: true, keepOriginalTimestamps: true });
  const out = { init: null, parts: [] };
  t.on("data", (seg) => {
    if (!out.init && seg.initSegment) out.init = new Uint8Array(seg.initSegment);
    if (seg.data) out.parts.push(new Uint8Array(seg.data));
  });
  return { t, out };
}

async function doDownload() {
  if (state.busy) return;
  state.busy = true;
  const btn = $("dl");
  btn.disabled = true;
  const bar = $("bar");
  try {
    if (!state.plan) await doPlan();
    const plan = state.plan;
    if (!plan) throw new Error("plan nahi bana");

    const total = plan.segments;
    const kind = plan.kind;
    let from = 0, got = 0, failed = 0, totalBytes = 0;
    const tsParts = [];          // TS chunks (transmux ke liye)
    let fmp4 = [];               // fMP4 ho to seedhe bytes
    let init = null;

    const mux = kind === "ts" ? await loadMuxjs() : null;
    if (!mux && kind === "ts") throw new Error("mux.js load nahi hua");
    const tx = mux ? makeTransmuxer(mux) : null;

    while (from < total) {
      const r = await fetch("/api/relay?" + new URLSearchParams({
        action: "chunk", token: state.token, from: String(from),
      }));
      if (!r.ok) {
        const t = await r.text();
        throw new Error(`chunk HTTP ${r.status}: ${t.slice(0, 160)}`);
      }
      const buf = new Uint8Array(await r.arrayBuffer());
      const next = Number(r.headers.get("x-next") || from);
      const done = r.headers.get("x-done") === "1";
      failed += Number(r.headers.get("x-failed") || 0);

      if (!buf.length) throw new Error("khali chunk mila — player ne segments nahi diye");

      if (kind === "ts") {
        tsParts.push(buf);
        // streaming transmux: memory kam, aur progress turant
        tx.t.push(buf);
      } else {
        fmp4.push(buf);
      }

      got = next;
      totalBytes += buf.length;
      const pct = Math.min(100, Math.round((got / total) * 100));
      bar.style.width = pct + "%";
      bar.textContent = pct + "%";
      log(`chunk ${from}-${next - 1} · ${(buf.length / 1048576).toFixed(1)} MB · ` +
          `total ${(totalBytes / 1048576).toFixed(1)} MB · ${pct}% · fail=${failed}`);
      from = next;
      if (done) break;
      if (next === from && !buf.length) break;
    }

    let mp4Parts, ext;
    if (kind === "ts") {
      tx.t.flush();
      mp4Parts = (tx.out.init ? [tx.out.init] : []).concat(tx.out.parts);
      ext = ".mp4";
    } else {
      mp4Parts = fmp4;
      ext = ".mp4";
    }
    const blob = new Blob(mp4Parts, { type: "video/mp4" });
    const size = blob.size;
    log(`MP4 ready: ${(size / 1048576).toFixed(2)} MB · ${got}/${total} segments ` +
        `· fail=${failed}`, size > 1048576 ? "ok" : "bad");

    if (size < 200 * 1024) throw new Error("file bahut chhoti — segments adhoore");

    const name = `lecture-${Date.now()}${ext}`;
    if (window.showSaveFilePicker) {
      try {
        const handle = await window.showSaveFilePicker({
          suggestedName: name,
          types: [{ description: "MP4 video", accept: { "video/mp4": [".mp4"] } }],
        });
        const w = await handle.createWritable();
        await w.write(blob);
        await w.close();
        log("save ho gaya: " + name, "ok");
        state.busy = false; btn.disabled = false;
        return;
      } catch (e) {
        log("picker cancel — normal download try", "dim");
      }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    log("download trigger: " + name, "ok");
  } catch (e) {
    log("DOWNLOAD FAIL: " + e.message, "bad");
  }
  state.busy = false;
  btn.disabled = false;
}

/* -------------------------------------------------------------- ui wiring */

$("insp").onclick = doInspect;
$("test").onclick = doSelftest;
$("plan").onclick = doPlan;
$("dl").onclick = doDownload;
$("tok").addEventListener("paste", () => setTimeout(() => {
  const v = normalizeToken($("tok").value);
  if (v && v.includes(":")) log("token paste hua (" + v.length + " chars) — '1. Token check' dabayein", "dim");
}, 50));
log("Ready. Region dikhega: bom1 = Mumbai (India).", "dim");
