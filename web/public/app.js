/*
 * Lecture Relay — browser mode (primary)
 * ======================================
 * Kyun browser me:
 *   player.studyratna.org Cloudflare ke peeche hai aur datacenter IPs ko 502 deta
 *   hai — GitHub runner (US), free proxies, aur Vercel ka bom1 (Mumbai) sab
 *   datacenter hain, isliye sab fail. Aapka phone/laptop residential India IP hai,
 *   jahan player chal raha hai — bas wahi rasta use karna hai.
 *
 * Isliye:
 *   - token DECRYPT yahin hota hai (WebCrypto AES-256-CBC) — token server par
 *     jaata hi nahi
 *   - playlist + segments + AES-128 key seedha browser fetch karta hai
 *   - TS -> MP4 mux.js se, save File System Access API ya blob se
 *
 * Server mode (fallback) tab bhi hai: agar kabhi datacenter se chal jaaye.
 */

const SEED = "5d90298d9598093cf8cb4b55434de5bc";
const APP_ORIGIN = "https://app.studyratna.org/";
const CONCURRENCY = 6;

const $ = (id) => document.getElementById(id);
const state = { token: "", payload: null, videoUrl: "", plan: null, busy: false, mux: null };

/* ----------------------------------------------------------------- logging */

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

/* -------------------------------------------------------------- token crypto */

const hexToBuf = (hex) => {
  const h = hex.trim();
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
};
const b64ToBuf = (b64) => {
  let s = b64.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
};

function normalizeToken(raw) {
  const v = (raw || "").trim();
  const m = v.match(/playerToken=([^&\s#]+)/);
  return m ? decodeURIComponent(m[1]) : v;
}

async function decryptToken(rawToken) {
  const tok = normalizeToken(rawToken);
  const [ivHex, ctHex] = tok.split(":");
  if (!ivHex || !ctHex) throw new Error("token format galat — 'iv:ct' chahiye");
  const keyRaw = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(SEED));
  const key = await crypto.subtle.importKey("raw", keyRaw, "AES-CBC", false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-CBC", iv: hexToBuf(ivHex) }, key, hexToBuf(ctHex));
  const data = JSON.parse(new TextDecoder().decode(plain));
  const info = { data, videoUrl: data.videoUrl || data.url || "" };

  const init = data.initData ? decodeURIComponent(data.initData) : "";
  const auth = init.match(/auth_date=(\d+)/);
  if (auth) {
    const ms = Number(auth[1]) * 1000;
    info.authDate = new Date(ms).toISOString();
    info.authAgeMinutes = Math.round((Date.now() - ms) / 60000);
  }
  const cookie = data.cookie || "";
  const pol = cookie.match(/CloudFront-Policy=([^;]+)/);
  if (pol) {
    try {
      const p = JSON.parse(new TextDecoder().decode(b64ToBuf(pol[1])));
      const less = p.Statement[0].Condition.DateLessThan["AWS:EpochTime"];
      info.cf = { iso: new Date(less * 1000).toISOString(), valid: less * 1000 > Date.now() };
    } catch (_) { /* ignore */ }
  }
  return info;
}

/* ----------------------------------------------------------------- fetch */

/** Browser se seedha player. 3 strategies — jo chale wahi report karo. */
async function tryFetch(url, kind = "text") {
  const attempts = [
    { label: "cors+omit", init: { mode: "cors", credentials: "omit" } },
    { label: "cors+include", init: { mode: "cors", credentials: "include" } },
    { label: "no-referrer", init: { mode: "cors", credentials: "include", referrerPolicy: "no-referrer" } },
  ];
  const results = [];
  for (const a of attempts) {
    try {
      const r = await fetch(url, {
        ...a.init,
        headers: { accept: "*/*" },
        referrer: APP_ORIGIN,
        signal: AbortSignal.timeout(25000),
      });
      const acao = r.headers.get("access-control-allow-origin");
      const info = { how: a.label, status: r.status, acao, type: r.type };
      results.push(info);
      if (r.status === 200) {
        return { ok: true, how: a.label, status: r.status, acao,
                 body: kind === "buf" ? new Uint8Array(await r.arrayBuffer()) : await r.text() };
      }
    } catch (e) {
      results.push({ how: a.label, error: String(e && e.message || e).slice(0, 80) });
    }
  }
  return { ok: false, results };
}

/* ---------------------------------------------------------------- playlists */

function parseMaster(text, base) {
  const out = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i].trim();
    if (!l.startsWith("#EXT-X-STREAM-INF")) continue;
    const bw = Number((l.match(/BANDWIDTH=(\d+)/) || [])[1] || 0);
    const res = (l.match(/RESOLUTION=(\d+x\d+)/) || [])[1] || "";
    let j = i + 1;
    while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith("#"))) j++;
    if (lines[j]) out.push({ bandwidth: bw, resolution: res, url: new URL(lines[j].trim(), base).href });
  }
  return out;
}

function pickVariant(vs) {
  if (!vs.length) return null;
  const h = (v) => Number((v.resolution.split("x")[1] || 0));
  const hd = vs.filter((v) => h(v) >= 700 && h(v) <= 800);
  const pool = (hd.length ? hd : vs).slice().sort((a, b) => b.bandwidth - a.bandwidth);
  return pool[0];
}

function parseMedia(text, base) {
  const segments = [];
  let key = null, map = null, dur = 0, seq = 0;
  for (const raw of text.split(/\r?\n/)) {
    const l = raw.trim();
    if (!l) continue;
    if (l.startsWith("#EXT-X-MEDIA-SEQUENCE:")) seq = Number(l.split(":")[1]) || 0;
    else if (l.startsWith("#EXT-X-KEY:")) {
      const method = (l.match(/METHOD=([^,]+)/) || [])[1] || "NONE";
      const uri = (l.match(/URI="([^"]+)"/) || [])[1];
      const iv = (l.match(/IV=0x([0-9a-fA-F]+)/) || [])[1] || null;
      key = method.toUpperCase() === "NONE" ? null
        : { method: method.toUpperCase(), url: uri ? new URL(uri, base).href : null, iv };
    } else if (l.startsWith("#EXT-X-MAP:")) {
      const uri = (l.match(/URI="([^"]+)"/) || [])[1];
      if (uri) map = new URL(uri, base).href;
    } else if (l.startsWith("#EXTINF:")) dur = parseFloat((l.split(":")[1] || "0").split(",")[0]) || 0;
    else if (!l.startsWith("#")) {
      segments.push({ url: new URL(l, base).href, dur, seq: seq + segments.length, key });
      dur = 0;
    }
  }
  return { segments, map };
}

function ivFor(seg) {
  if (seg.key && seg.key.iv) return hexToBuf(seg.key.iv.padStart(32, "0"));
  const iv = new Uint8Array(16);
  let n = seg.seq;
  for (let i = 15; i >= 0 && n > 0; i--) { iv[i] = n & 0xff; n = Math.floor(n / 256); }
  return iv;
}

/* ------------------------------------------------------------------- mux.js */

function loadMuxjs() {
  if (state.mux) return Promise.resolve(state.mux);
  const sources = ["/api/relay?action=muxjs", "https://cdn.jsdelivr.net/npm/mux.js@7.1.1/dist/mux.min.js"];
  return new Promise((resolve, reject) => {
    const next = (i) => {
      if (i >= sources.length) return reject(new Error("mux.js load nahi hua (network?)"));
      const s = document.createElement("script");
      s.src = sources[i];
      s.onload = () => { state.mux = window.muxjs; resolve(window.muxjs); };
      s.onerror = () => next(i + 1);
      document.head.appendChild(s);
    };
    next(0);
  });
}

/* --------------------------------------------------------------- 1. inspect */

async function doInspect() {
  clearLog();
  const token = normalizeToken($("tok").value);
  if (!token) return log("pehle lecture link / token paste karein", "bad");
  state.token = token;
  try {
    const info = await decryptToken(token);
    state.payload = info.data;
    state.videoUrl = info.videoUrl;
    log(`token decrypt ho gaya (browser me hi, server ko nahi bheja) · auth ${info.authAgeMinutes} min purana`, "ok");
    log("videoUrl: " + (info.videoUrl || "(nahi mila)"));
    if (info.cf) {
      log(`token ke andar CloudFront signature: ${info.cf.iso} ` + (info.cf.valid ? "(valid)" : "(purana — player khud sign karta hai)"), "dim");
    }
    log(`cookie: ${(state.payload.cookie || "").length} bytes · bahut se cases me zaroorat nahi padti`, "dim");
    $("url").textContent = info.videoUrl || "";
    return info;
  } catch (e) {
    log("token decrypt fail: " + e.message, "bad");
  }
}

/* -------------------------------------------------------- 2. player test */

async function doTest() {
  if (!state.videoUrl && !(await doInspect())) return;
  log("— aapke internet se player test (yahi asli test hai) —");
  const master = state.videoUrl;
  const r1 = await tryFetch(master);
  if (!r1.ok) {
    log(`master -> FAIL`, "bad");
    for (const a of r1.results || []) log(`   ${a.how}: ${a.status ? "HTTP " + a.status + (a.acao ? " ACAO=" + a.acao : " (no CORS header)") : a.error}`, "dim");
    log("", "dim");
    log("Matlab: aapke browser tak bhi playlist nahi pahunch rahi, ya CORS block kar raha hai.", "bad");
    log("Neeche 'Playlist naye tab me kholo' dabayein — agar wahan #EXTM3U text dikhe to sirf CORS issue hai.", "dim");
    return;
  }
  log(`master -> 200 (${r1.how}, ${r1.body.length} bytes)`, "ok");

  const variants = parseMaster(r1.body, master);
  let mediaUrl = master, mediaText = r1.body, variant = null;
  if (variants.length) {
    variant = pickVariant(variants);
    log(`variants: ${variants.map((v) => v.resolution || v.bandwidth).join(", ")}`, "dim");
    const r2 = await tryFetch(variant.url);
    if (!r2.ok) return log(`variant ${variant.resolution} -> FAIL`, "bad");
    log(`variant ${variant.resolution} -> 200 (${r2.body.length} bytes)`, "ok");
    mediaUrl = variant.url;
    mediaText = r2.body;
  }
  const { segments, map } = parseMedia(mediaText, mediaUrl);
  if (!segments.length) return log("playlist me segments nahi mile", "bad");
  const dur = Math.round(segments.reduce((a, s) => a + s.dur, 0));
  state.plan = { segments, map, mediaUrl, variant, kind: map ? "fmp4" : "ts", dur };
  log(`segments: ${segments.length} · ${Math.floor(dur / 60)}m${dur % 60}s · ` +
      `encrypted=${segments[0].key ? "AES-128" : "no"} · kind=${state.plan.kind}`, "ok");
  log("SAB THEEK — ab 'MP4 download' dabayein", "ok");
}

/* --------------------------------------------------------- 3. MP4 download */

async function fetchSegment(seg, keyCache) {
  const r = await tryFetch(seg.url, "buf");
  if (!r.ok) throw new Error(`HTTP ${r.status || "CORS/network"}`);
  let data = r.body;
  if (seg.key && seg.key.method === "AES-128") {
    if (!seg.key.url) throw new Error("key URI nahi");
    let kb = keyCache.get(seg.key.url);
    if (!kb) {
      const kr = await tryFetch(seg.key.url, "buf");
      if (!kr.ok) throw new Error("key fetch fail");
      kb = kr.body;
      keyCache.set(seg.key.url, kb);
    }
    const ck = await crypto.subtle.importKey("raw", kb, "AES-CBC", false, ["decrypt"]);
    try {
      data = new Uint8Array(await crypto.subtle.decrypt({ name: "AES-CBC", iv: ivFor(seg) }, ck, data));
    } catch (_) {
      throw new Error("AES decrypt fail (padding)");
    }
  }
  return data;
}

async function doDownload() {
  if (state.busy) return;
  if (!state.plan) {
    const ok = await doTest();
    if (!state.plan) return log("plan nahi bana — pehle test pass karna hoga", "bad");
  }
  state.busy = true;
  $("dl").disabled = true;
  const t0 = Date.now();
  try {
    const { segments, map, kind } = state.plan;
    const total = segments.length;
    const keyCache = new Map();
    const got = new Array(total).fill(null);
    let fail = 0, bytes = 0, done = 0, cursor = 0;

    log(`download shuru: ${total} segments, ${CONCURRENCY} parallel`);
    async function worker() {
      while (cursor < total) {
        const i = cursor++;
        try {
          const seg = segments[i];
          const data = await fetchSegment(seg, keyCache);
          got[i] = data;
          bytes += data.length;
        } catch (e) {
          fail++;
          if (fail <= 3) log(`  seg ${i} fail: ${e.message}`, "dim");
        } finally {
          done++;
          if (done % 5 === 0 || done === total) {
            const pct = (done / total) * 100;
            setBar(pct);
            const rate = done / Math.max(1, (Date.now() - t0) / 1000);
            log(`${done}/${total} · ${(bytes / 1048576).toFixed(1)} MB · fail=${fail} · ` +
                `${rate.toFixed(1)} seg/s · ${Math.round((Date.now() - t0) / 1000)}s`);
          }
        }
      }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, worker));

    const cov = got.filter(Boolean).length / total;
    log(`segments laaye: ${got.filter(Boolean).length}/${total} (${Math.round(cov * 100)}%), fail=${fail}`,
        cov > 0.95 ? "ok" : "bad");
    if (bytes < 100 * 1024) throw new Error("bahut kam data mila — segments block ho rahe hain");

    let parts;
    if (kind === "ts") {
      const mux = await loadMuxjs();
      log("mux.js se TS -> MP4…");
      const T = mux.mp4.Transmuxer;
      const t = new T({ remux: true, keepOriginalTimestamps: true });
      let init = null;
      const out = [];
      t.on("data", (s) => {
        if (!init && s.initSegment) init = new Uint8Array(s.initSegment);
        if (s.data) out.push(new Uint8Array(s.data));
      });
      for (let i = 0; i < total; i++) if (got[i]) { t.push(got[i]); got[i] = null; }
      t.flush();
      parts = init ? [init, ...out] : out;
    } else {
      parts = [];
      if (map) {
        const mr = await tryFetch(map, "buf");
        if (mr.ok) parts.push(mr.body);
      }
      for (let i = 0; i < total; i++) if (got[i]) parts.push(got[i]);
    }

    const blob = new Blob(parts, { type: "video/mp4" });
    log(`MP4 ready: ${(blob.size / 1048576).toFixed(2)} MB`, "ok");
    setBar(100, "100%");

    const name = `lecture-${new Date().toISOString().slice(0, 10)}.mp4`;
    if (window.showSaveFilePicker) {
      try {
        const h = await window.showSaveFilePicker({
          suggestedName: name,
          types: [{ description: "MP4 video", accept: { "video/mp4": [".mp4"] } }],
        });
        const w = await h.createWritable();
        await w.write(blob);
        await w.close();
        return log("save ho gaya: " + name + " — Gallery/Photos me dikhega", "ok");
      } catch (e) {
        log("save picker cancel — normal download try", "dim");
      }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    log("download shuru: " + name + " (Downloads me jayega, phir Gallery me move kar sakte hain)", "ok");
  } catch (e) {
    log("DOWNLOAD FAIL: " + e.message, "bad");
  }
  state.busy = false;
  $("dl").disabled = false;
}

/* --------------------------------------------------------- diagnostics */

function openPlaylist() {
  if (!state.videoUrl) return log("pehle token check karein", "bad");
  const u = state.videoUrl.replace("master.m3u8", "hls/720/main.m3u8");
  window.open(u, "_blank", "noopener");
  log("playlist naye tab me kholi: " + u, "dim");
  log("agar wahan '#EXTM3U' text dikhe -> IP theek hai, sirf CORS rok raha hai", "dim");
  log("agar 502/403 error page dikhe -> aapka network bhi block hai", "dim");
}

async function serverMode() {
  if (!state.token) await doInspect();
  if (!state.token) return;
  log("— server mode (Vercel) try —", "dim");
  try {
    const r = await fetch("/api/relay?action=selftest&token=" + encodeURIComponent(state.token));
    const j = await r.json();
    log(`server region: ${j.region}`, "dim");
    for (const p of j.probes || []) {
      log(`  ${p.name} -> ${p.status} ${p.bytes || 0}B`, p.status === 200 ? "ok" : "bad");
    }
    if ((j.probes || []).some((p) => p.status === 200)) {
      log("server se bhi chal gaya! 'Server download' try karein", "ok");
    } else {
      log("server se nahi chala (datacenter IP) — browser mode hi rasta hai", "dim");
    }
  } catch (e) {
    log("server mode fail: " + e.message, "bad");
  }
}

/* ------------------------------------------------------------------ wiring */

$("insp").onclick = doInspect;
$("test").onclick = doTest;
$("dl").onclick = doDownload;
$("open").onclick = openPlaylist;
$("srv").onclick = serverMode;

window.addEventListener("paste", () => setTimeout(() => {
  const t = normalizeToken($("tok").value);
  if (t.includes(":")) log(`token paste hua (${t.length} chars) — "1 · Token check" dabayein`, "dim");
}, 60));

log("Ready. Sab kuch aapke browser me chalega — aapka India IP, koi proxy nahi.", "dim");
