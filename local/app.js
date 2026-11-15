/*
 * Lecture → MP4 — local UI
 * ========================
 * Yahan sab kuch LOCAL relay (aapka PC) se hota hai, browser se nahi:
 *   - playlist/segments local server maangta hai -> CORS lagta hi nahi
 *   - token local relay ko jaata hai (aapka hi PC)
 *   - TS -> MP4 mux.js se browser me
 *
 * Vercel version se farq: wahan browser fetch karta tha aur CORS par fail hota
 * tha. Yahan wahi kaam local server karta hai.
 */

const $ = (id) => document.getElementById(id);
const state = { token: "", plan: null, busy: false, mux: null };

function log(msg, cls) {
  const el = $("log");
  const d = document.createElement("div");
  if (cls) d.className = cls;
  d.textContent = msg;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}
const clearLog = () => { $("log").innerHTML = ""; };
function setBar(pct, label) {
  const b = $("bar");
  b.style.width = Math.max(0, Math.min(100, pct)) + "%";
  b.textContent = label || (pct > 3 ? Math.round(pct) + "%" : "");
}
function normalizeToken(raw) {
  const v = (raw || "").trim();
  const m = v.match(/playerToken=([^&\s#]+)/);
  return m ? decodeURIComponent(m[1]) : v;
}

async function api(params) {
  const r = await fetch("/api?" + new URLSearchParams(params));
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("json")) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
  const j = await r.json();
  if (j.ok === false) throw new Error(j.error || "server error");
  return j;
}

/* ------------------------------------------------------------- 1. inspect */

async function doInspect() {
  const token = normalizeToken($("tok").value);
  if (!token) return log("pehle lecture link / token paste karein", "bad");
  state.token = token;
  try {
    const j = await api({ action: "inspect", token });
    log(`token OK · decrypt local PC par hua · auth ${j.authAgeMinutes} min purana`, "ok");
    log("videoUrl: " + j.videoUrl);
    if (j.cloudfront) {
      log(`token ke andar CloudFront signature: ${j.cloudfront.iso} ` +
          (j.cloudfront.valid ? "(valid)" : "(purana — player khud sign karta hai, koi dikkat nahi)"), "dim");
    }
    log(`cookie: ${j.cookieLen} bytes — player ko dene ke liye local relay use karega`, "dim");
    $("url").textContent = j.videoUrl || "";
    return j;
  } catch (e) {
    log("inspect fail: " + e.message, "bad");
  }
}

/* ------------------------------------------------------------ 2. selftest */

async function doTest() {
  const token = state.token || normalizeToken($("tok").value);
  if (!token) return log("pehle token daalein", "bad");
  state.token = token;
  log("— player test: requests AAPKE PC se ja rahi hain (local relay) —");
  try {
    const j = await api({ action: "selftest", token });
    if (j.tokenError) return log("token error: " + j.tokenError, "bad");
    for (const p of j.probes) {
      const ok = p.status === 200;
      log(`${p.name.padEnd(8)} -> ${p.status} ${p.bytes || 0}B ${p.ms || ""}ms ${p.server || ""}`,
          ok ? "ok" : "bad");
      if (p.head && !ok) log("   " + p.head.slice(0, 120), "dim");
    }
    if (!j.probes.some((p) => p.status === 200)) {
      log("player ne yahan bhi mana kiya — network block ho sakta hai (VPN/mobile data try karein)", "bad");
      return;
    }
    log("PLAYER KHUL GAYA ✅ ab plan banayenge", "ok");
    const plan = await api({ action: "plan", token });
    state.plan = { ...plan, token };
    const mm = Math.floor(plan.durationSec / 60), ss = plan.durationSec % 60;
    log(`plan: ${plan.segments} segments · ${mm}m${ss}s · ${plan.variant ? plan.variant.resolution : "?"}` +
        ` · kind=${plan.kind} · aes=${plan.encrypted}`, "ok");
    log("ab '3 · MP4 download' dabayein", "ok");
    return j;
  } catch (e) {
    log("selftest fail: " + e.message, "bad");
  }
}

/* --------------------------------------------------------------- 3. mux.js */

function loadMuxjs() {
  if (state.mux) return Promise.resolve(state.mux);
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/api?action=muxjs";
    s.onload = () => { state.mux = window.muxjs; resolve(window.muxjs); };
    s.onerror = () => reject(new Error("mux.js load fail"));
    document.head.appendChild(s);
  });
}

/* --------------------------------------------------------- 4. download mp4 */

async function doDownload() {
  if (state.busy) return;
  if (!state.plan) {
    await doTest();
    if (!state.plan) return log("plan nahi bana — pehle player test pass hona chahiye", "bad");
  }
  state.busy = true;
  $("dl").disabled = true;
  const t0 = Date.now();
  try {
    const plan = state.plan;
    const total = plan.segments;
    const kind = plan.kind;
    let from = 0, bytes = 0, failed = 0, chunks = 0;
    const tsParts = [];

    const mux = kind === "ts" ? await loadMuxjs() : null;
    let tx = null, mp4Parts = [], init = null;
    if (mux) {
      const T = mux.mp4.Transmuxer;
      tx = new T({ remux: true, keepOriginalTimestamps: true });
      tx.on("data", (s) => {
        if (!init && s.initSegment) init = new Uint8Array(s.initSegment);
        if (s.data) mp4Parts.push(new Uint8Array(s.data));
      });
    }

    while (from < total) {
      const r = await fetch("/api?" + new URLSearchParams({
        action: "chunk", token: plan.token, from: String(from),
      }));
      if (!r.ok) {
        const t = await r.text();
        throw new Error(`chunk HTTP ${r.status}: ${t.slice(0, 150)}`);
      }
      const buf = new Uint8Array(await r.arrayBuffer());
      if (!buf.length) throw new Error("khali chunk — segments nahi aa rahe");
      const next = Number(r.headers.get("x-next") || from);
      const done = r.headers.get("x-done") === "1";
      failed += Number(r.headers.get("x-failed") || 0);

      if (mux) tx.push(buf);
      else tsParts.push(buf);

      bytes += buf.length;
      chunks++;
      from = next;
      const pct = (from / total) * 100;
      setBar(pct);
      log(`chunk ${chunks} · ${(buf.length / 1048576).toFixed(1)} MB · ` +
          `total ${(bytes / 1048576).toFixed(1)} MB · ${Math.round(pct)}% · fail=${failed}`);
      if (done) break;
    }

    let parts, ext = ".mp4";
    if (mux) {
      tx.flush();
      parts = init ? [init, ...mp4Parts] : mp4Parts;
    } else {
      parts = tsParts;
    }
    const blob = new Blob(parts, { type: "video/mp4" });
    log(`MP4 ready: ${(blob.size / 1048576).toFixed(2)} MB · ${from}/${total} segments · ` +
        `fail=${failed} · ${Math.round((Date.now() - t0) / 1000)}s`, blob.size > 1048576 ? "ok" : "bad");
    setBar(100, "100%");
    if (blob.size < 200 * 1024) throw new Error("file bahut chhoti — segments adhoore");

    const name = `lecture-${new Date().toISOString().slice(0, 10)}${ext}`;
    if (window.showSaveFilePicker) {
      try {
        const h = await window.showSaveFilePicker({
          suggestedName: name,
          types: [{ description: "MP4 video", accept: { "video/mp4": [".mp4"] } }],
        });
        const w = await h.createWritable();
        await w.write(blob);
        await w.close();
        taskDone();
        return log("save ho gaya: " + name, "ok");
      } catch (e) {
        log("save picker cancel — normal download", "dim");
      }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    taskDone();
    log("download shuru: " + name, "ok");
  } catch (e) {
    log("DOWNLOAD FAIL: " + e.message, "bad");
  }
  state.busy = false;
  $("dl").disabled = false;
}

function taskDone() {
  state.busy = false;
  $("dl").disabled = false;
}

/* -------------------------------------------------------- debug: playlist */

async function openPlaylist() {
  if (!state.token) return log("pehle token daalein", "bad");
  try {
    const j = await api({ action: "inspect", token: state.token });
    const media = (j.videoUrl || "").replace("master.m3u8", "hls/720/main.m3u8");
    const r = await api({ action: "fetch", u: media });
    log(`direct playlist -> ${r.status} (${r.bytes} bytes, ${r.server || "?"})`, r.ok ? "ok" : "bad");
    log(r.head.slice(0, 300), "dim");
    if (r.ok) {
      const w = window.open("", "_blank");
      w.document.write(`<pre style="white-space:pre-wrap;font:12px monospace;padding:16px">${r.head
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")}</pre>`);
    }
  } catch (e) {
    log("playlist check fail: " + e.message, "bad");
  }
}

/* ------------------------------------------------------------------ wiring */

$("insp").onclick = doInspect;
$("test").onclick = doTest;
$("dl").onclick = doDownload;
$("open").onclick = openPlaylist;

window.addEventListener("paste", () => setTimeout(() => {
  const t = normalizeToken($("tok").value);
  if (t.includes(":")) log(`token paste hua (${t.length} chars) — "1 · Token check" dabayein`, "dim");
}, 60));

log("Local relay ready — requests aapke PC se ja rahi hain (CORS ka koi rok nahi).", "dim");
