#!/usr/bin/env node
/**
 * Lecture → MP4 : Terminal downloader (phone/PC, no browser needed)
 * ==================================================================
 * Node 18+ only. Koi npm install nahi. Koi browser nahi.
 *
 * Kyun ye:
 *   - Requests server-side jaati hain, isliye player ka CORS/Cloudflare gate
 *     lagta hi nahi (aapka India IP use hota hai)
 *   - Har segment disk par alag save hota hai (*.part), isliye session band
 *     ho jaye to dobara chalane par jahan chhoda tha wahin se shuru hota hai
 *   - End me ffmpeg se MP4 (phone ki Gallery me dikhne wala), ffmpeg na ho to
 *     .ts (VLC me turant chalta hai)
 *
 * Istemal:
 *   node download.mjs "<lecture link ya token>"
 *   node download.mjs "<...>" -o MyLecture.mp4
 *   node download.mjs --resume          # pehle wala adhoora download continue
 *
 * Flags:
 *   -o, --out <file>     output name (default: lecture.mp4)
 *   -w, --workers <n>    parallel segments (default 6)
 *   -q, --quality <h>    720 (default) | 1080 | 480 | best
 *       --no-ffmpeg      sirf segments + .ts, MP4 mat banao
 *       --resume         purane adhoora download continue karo
 *       --fresh          cache delete karke shuru se
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

/* ------------------------------------------------------------------ config */

const TOKEN_SEED = "5d90298d9598093cf8cb4b55434de5bc";
const APP_ORIGIN = "https://app.studyratna.org";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

const CACHE_DIR = path.join(process.cwd(), ".lecture-cache");

/* -------------------------------------------------------------------- args */

function parseArgs(argv) {
  const a = { token: "", out: "lecture.mp4", workers: 6, quality: "720", ffmpeg: true, resume: false, fresh: false };
  for (let i = 0; i < argv.length; i++) {
    const v = argv[i];
    if (v === "-o" || v === "--out") a.out = argv[++i];
    else if (v === "-w" || v === "--workers") a.workers = Math.max(1, Number(argv[++i]) || 6);
    else if (v === "-q" || v === "--quality") a.quality = String(argv[++i] || "720");
    else if (v === "--no-ffmpeg") a.ffmpeg = false;
    else if (v === "--resume") a.resume = true;
    else if (v === "--fresh") a.fresh = true;
    else if (v === "-h" || v === "--help") a.help = true;
    else if (!v.startsWith("-") && !a.token) a.token = v;
  }
  return a;
}

const args = parseArgs(process.argv.slice(2));

if (args.help) {
  console.log(`
  Lecture → MP4 (terminal)

    node download.mjs "<lecture link ya playerToken>"
    node download.mjs "<...>" -o Lecture.mp4 -w 8 -q 720
    node download.mjs --resume        # adhoora download continue
    node download.mjs --fresh         # cache saaf karke shuru se

  -o  output file      -w  parallel segments (default 6)
  -q  quality (720/1080/480)         --no-ffmpeg  sirf .ts banao
`);
  process.exit(0);
}

/* -------------------------------------------------------------------- util */

const C = {
  dim: (s) => `\x1b[2m${s}\x1b[0m`,
  ok: (s) => `\x1b[32m${s}\x1b[0m`,
  bad: (s) => `\x1b[31m${s}\x1b[0m`,
  warn: (s) => `\x1b[33m${s}\x1b[0m`,
  bold: (s) => `\x1b[1m${s}\x1b[0m`,
};
const say = (...a) => console.log(...a);
const mb = (n) => (n / 1048576).toFixed(1) + " MB";

/**
 * Cookie jar — player playlist/serve karte waqt `Set-Cookie` bhej sakta hai
 * (Cloudflare access cookie, hls session cookie). Wo cookie key request me
 * chahiye hoti hai. Pehle hum sirf token wali cookie bhejte the, isliye key 401.
 */
const jar = new Map();

function jarHeader() {
  return [...jar].map(([k, v]) => `${k}=${v}`).join("; ");
}

function absorbCookies(res) {
  let list = [];
  try {
    if (typeof res.headers.getSetCookie === "function") list = res.headers.getSetCookie();
    else {
      const one = res.headers.get("set-cookie");
      if (one) list = [one];
    }
  } catch { /* ignore */ }
  for (const c of list) {
    const pair = String(c).split(";")[0];
    const i = pair.indexOf("=");
    if (i > 0) jar.set(pair.slice(0, i).trim(), pair.slice(i + 1).trim());
  }
}

/** token wali cookie + jar ki cookies ek saath */
function mergeCookie(cookie) {
  const extra = jarHeader();
  if (cookie && extra) return `${cookie}; ${extra}`;
  return cookie || extra;
}

function headersFor(cookie) {
  const h = {
    "user-agent": UA,
    accept: "*/*",
    "accept-language": "en-IN,en;q=0.9",
    referer: APP_ORIGIN + "/",
    origin: APP_ORIGIN,
  };
  const c = mergeCookie(cookie);
  if (c) h.cookie = c;
  return h;
}

async function getText(url, cookie) {
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  absorbCookies(r);
  return { status: r.status, text: await r.text(), url: r.url || url };
}

async function getBuf(url, cookie) {
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  absorbCookies(r);
  return { status: r.status, buf: Buffer.from(await r.arrayBuffer()), url: r.url || url };
}

/* ------------------------------------------------------------ token decrypt */

function resolveToken(raw) {
  let s = String(raw || "").trim().replace(/^["']|["']$/g, "");
  const m = s.match(/playerToken=([^&\s"'#]+)/i);
  if (m) {
    s = m[1];
    try { s = decodeURIComponent(s); } catch { /* already decoded */ }
  } else if (!s.includes(":") && /%3A/i.test(s)) {
    try { s = decodeURIComponent(s); } catch { /* ignore */ }
  }
  s = s.trim();
  if (/^https?:\/\//i.test(s)) {
    throw new Error("link me playerToken= nahi mila — StudyRatna ka poora lecture link chahiye");
  }
  const parts = s.split(":");
  if (parts.length !== 2) {
    throw new Error(`token galat (${parts.length} hisse mile, 2 chahiye) — poora lecture link paste karein`);
  }
  const [iv, ct] = parts.map((x) => x.trim());
  if (!/^[0-9a-fA-F]{32}$/.test(iv)) {
    throw new Error(`IV galat (32 hex chahiye, mile ${iv.length}) — link adhoora paste hua`);
  }
  if (!/^[0-9a-fA-F]+$/.test(ct) || ct.length < 32 || ct.length % 32 !== 0) {
    throw new Error(`encrypted part galat (${ct.length} hex) — link poora copy karein`);
  }
  return { iv, ct };
}

function decryptToken(raw) {
  const { iv: ivHex, ct: ctHex } = resolveToken(raw);
  const key = crypto.createHash("sha256").update(TOKEN_SEED).digest();
  const d = crypto.createDecipheriv("aes-256-cbc", key, Buffer.from(ivHex, "hex"));
  const plain = Buffer.concat([d.update(Buffer.from(ctHex, "hex")), d.final()]);
  const data = JSON.parse(plain.toString("utf8"));
  return { data, cookie: data.cookie || "", videoUrl: data.videoUrl || data.url || "" };
}


/**
 * Key endpoint ke liye auth strategies.
 *
 * Live test: playlist 200, segments 200, par AES key endpoint 401 deta hai —
 * token 3 min purana hone ke baad bhi. Matlab 401 ka matlab sirf "purana token"
 * nahi hai; endpoint ko kuch aur chahiye (init data / player referer / api key).
 * Isliye yahan 6 variants try hote hain aur jo 200 de wahi jeetta hai.
 */
function headerVariants(cookie, initData, authToken) {
  const base = {
    accept: "*/*",
    "accept-language": "en-IN,en;q=0.9",
    "user-agent": UA,
  };
  const tma = initData ? encodeURIComponent(initData) : "";
  const out = [
    // 1: app context
    { label: "app-referer", h: { ...base, referer: APP_ORIGIN + "/", origin: APP_ORIGIN } },
    // 2: player context (key player host se aa rahi hai)
    { label: "player-referer", h: { ...base, referer: "https://player.studyratna.org/", origin: "https://player.studyratna.org" } },
    // 3: penpencil api context (asli key server)
    { label: "api-referer", h: { ...base, referer: "https://www.penpencil.co/", origin: "https://www.penpencil.co" } },
    // 4: Telegram init data header
    tma && { label: "tma-header", h: { ...base, referer: APP_ORIGIN + "/", "x-init-data": tma } },
    // 5: Bearer init data
    tma && { label: "bearer-initdata", h: { ...base, referer: APP_ORIGIN + "/", authorization: "Bearer " + initData } },
    // 6: pw client headers (penpencil API ka client-id)
    {
      label: "pw-client",
      h: {
        ...base,
        referer: APP_ORIGIN + "/",
        "client-id": "5eb393ee95fab7468a79d189",
        "client-type": "WEB",
        "client-version": "5.0.0",
      },
    },
    // 7: browser auth header (agar token me jwt ho)
    authToken && {
      label: "auth-bearer",
      h: { ...base, referer: APP_ORIGIN + "/", authorization: "Bearer " + authToken },
    },
  ].filter(Boolean);
  const merged = mergeCookie(cookie);
  if (merged) for (const v of out) v.h.cookie = merged;
  return out;
}


/**
 * keyHunt() — jab get-hls-key 401/403 de to key ghusaane ke aur raaste.
 *
 * Raaste (sasta → mehnga):
 *   1. key URI ko path/query alag tarah se bhejna (path form kabhi khula hota hai)
 *   2. player ke proxy endpoints (/ ?url=, /proxy?url=, /hls/, /key)
 *   3. CloudFront origin par direct (CDN host)
 *   4. player JS bundle scan -> key URL / header ka pattern
 *
 * Har candidate `headerVariants()` ke saath try hota hai. Jo 16/32 bytes de,
 * wahi key. True/False lautata hai.
 */
async function keyHunt(keyUri, cookie, initData, authToken, log = () => {}, probeRaw = null, probeIv = null) {
  const variants = headerVariants(cookie, initData, authToken);
  let videoKey = "?";
  try {
    const u = new URL(keyUri);
    videoKey = u.searchParams.get("videoKey") || u.pathname.split("/").filter(Boolean).pop();
  } catch { /* ignore */ }

  const playerOrigin = (() => {
    try { const u = new URL(keyUri); return u.origin; } catch { return "https://player.studyratna.org"; }
  })();

  const cands = [];
  const add = (u) => { if (u && /^https?:\/\//.test(u) && !cands.includes(u)) cands.push(u); };

  // 1) wahi endpoint, alag shape
  try {
    const u = new URL(keyUri);
    u.pathname = "/v1/videos/get-hls-key";
    add(u.toString());
    u.searchParams.set("key", "enc-720.key"); add(u.toString());
    u.searchParams.set("key", "hls.key"); add(u.toString());
    u.searchParams.set("key", "key.bin"); add(u.toString());
  } catch { /* ignore */ }

  // 2) player proxy
  add(`${playerOrigin}/?url=${encodeURIComponent(keyUri)}`);
  add(`${playerOrigin}/proxy?url=${encodeURIComponent(keyUri)}`);
  add(`${playerOrigin}/hls/${videoKey}/enc.key`);
  add(`${playerOrigin}/key/${videoKey}`);
  add(`${playerOrigin}/stream/${videoKey}/enc.key`);
  add(`${playerOrigin}/stream/${videoKey}/hls/720/enc.key`);

  // 3) cloudfront origin
  add(`https://d1d34p8vz63oiq.cloudfront.net/${videoKey}/enc.key`);
  add(`https://d1d34p8vz63oiq.cloudfront.net/${videoKey}/hls/720/enc.key`);
  add(`https://d1d34p8vz63oiq.cloudfront.net/${videoKey}/hls/720/enc-720.key`);

  // 4) player JS bundle se naye raaste
  const jsSeen = new Set();
  const scanJs = async (pageUrl) => {
    const pg = await getText(pageUrl, cookie);
    if (pg.status !== 200) return;
    const srcs = [...pg.text.matchAll(/<script[^>]+src=["']([^"']+)["']/gi)].map((m) => m[1]);
    for (const src of srcs.slice(0, 6)) {
      let u; try { u = new URL(src, pageUrl).toString(); } catch { continue; }
      if (jsSeen.has(u)) continue;
      jsSeen.add(u);
      const js = await getText(u, cookie);
      if (js.status !== 200) continue;
      const t = js.text;
      // key URL patterns
      for (const m of t.matchAll(/["'`]([^"'`\s]{0,120}(?:get-hls-key|enc\.key|hls-key)[^"'`\s]{0,80})["'`]/gi)) {
        let cand = m[1];
        if (cand.startsWith("/")) cand = playerOrigin + cand;
        if (/^https?:/.test(cand)) add(cand);
      }
      // auth header patterns
      const hdrHints = [...t.matchAll(/(authorization|x-api-key|client-id|token)\s*["']?\s*[:=]\s*["']([A-Za-z0-9 _\-.]{8,120})["']/gi)];
      for (const h of hdrHints.slice(0, 4)) {
        log(`    js hint: ${h[1]} = ${h[2].slice(0, 40)}…`);
      }
    }
  };
  await scanJs(playerOrigin + "/").catch(() => {});

  log(`    ${cands.length} key URL candidates, ${variants.length} header sets`);

  for (const cand of cands) {
    for (const v of variants) {
      try {
        const r = await fetch(cand, { headers: v.h, redirect: "follow", signal: AbortSignal.timeout(12000) });
        const buf = Buffer.from(await r.arrayBuffer());
        if (r.status === 200 && (buf.length === 16 || buf.length === 32)) {
          if (probeRaw && !keyWorks(buf, probeRaw, probeIv)) continue; // 200 par galat key
          log(`    ✓ HIT ${v.label} ${cand.slice(0, 70)}`);
          return buf;
        }
      } catch { /* skip */ }
    }
  }
  return null;
}

/* ------------------------------------------------------------------- m3u8 */

function parseMaster(text) {
  const out = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i].trim();
    if (!l.startsWith("#EXT-X-STREAM-INF")) continue;
    const bw = Number((l.match(/BANDWIDTH=(\d+)/) || [])[1] || 0);
    const res = (l.match(/RESOLUTION=(\d+x\d+)/) || [])[1] || "";
    let j = i + 1;
    while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith("#"))) j++;
    if (lines[j]) out.push({ bandwidth: bw, resolution: res, uri: lines[j].trim() });
  }
  return out;
}

function pickVariant(vs, base, quality) {
  if (!vs.length) return null;
  const h = (v) => Number((v.resolution.split("x")[1] || 0));
  let pool = vs.slice();
  if (quality === "best") {
    pool.sort((a, b) => h(b) - h(a) || b.bandwidth - a.bandwidth);
  } else {
    const want = Number(quality) || 720;
    pool.sort((a, b) => Math.abs(h(a) - want) - Math.abs(h(b) - want) || b.bandwidth - a.bandwidth);
  }
  const win = pool[0];
  return { ...win, url: new URL(win.uri, base).toString() };
}

function parseMedia(text, base) {
  const segments = [];
  let key = null, dur = 0, seq = 0;
  for (const raw of text.split(/\r?\n/)) {
    const l = raw.trim();
    if (!l) continue;
    if (l.startsWith("#EXT-X-MEDIA-SEQUENCE:")) seq = Number(l.split(":")[1]) || 0;
    else if (l.startsWith("#EXT-X-KEY:")) {
      const method = (l.match(/METHOD=([^,]+)/) || [])[1] || "NONE";
      const uri = (l.match(/URI="([^"]+)"/) || [])[1] || null;
      const iv = (l.match(/IV=0x([0-9a-fA-F]+)/) || [])[1] || null;
      key = method.toUpperCase() === "NONE" ? null : {
        method: method.toUpperCase(),
        uri: uri ? new URL(uri, base).toString() : null,
        iv,
      };
    } else if (l.startsWith("#EXTINF:")) dur = parseFloat((l.split(":")[1] || "0").split(",")[0]) || 0;
    else if (!l.startsWith("#")) {
      segments.push({ url: new URL(l, base).toString(), dur, seq: seq + segments.length, key });
      dur = 0;
    }
  }
  return { segments };
}

function ivFor(seg) {
  if (seg.key && seg.key.iv) return Buffer.from(seg.key.iv.padStart(32, "0"), "hex");
  const iv = Buffer.alloc(16);
  let n = seg.seq;
  for (let i = 15; i >= 0 && n > 0; i--) { iv[i] = n & 0xff; n = Math.floor(n / 256); }
  return iv;
}

function aesDecrypt(keyBuf, data, iv) {
  try {
    const d = crypto.createDecipheriv("aes-128-cbc", keyBuf, iv);
    return Buffer.concat([d.update(data), d.final()]);
  } catch {
    const d = crypto.createDecipheriv("aes-128-cbc", keyBuf, iv);
    d.setAutoPadding(false);
    return Buffer.concat([d.update(data), d.final()]);
  }
}

/**
 * AES key sahi hai ya nahi — asli test: pehla block decrypt karke MPEG-TS sync
 * byte (0x47) dhoondna. Galat 16 bytes bhi "200 OK" me aa sakte hain.
 */
function keyWorks(keyBuf, raw, iv) {
  try {
    const n = Math.min(raw.length, 188 * 5);
    const first = aesDecrypt(keyBuf, raw.subarray(0, 16 * Math.ceil(n / 16)), iv);
    // TS packets 188 bytes ke hote hain; kisi bhi offset par 3 lagataar sync byte
    for (let off = 0; off < 188; off++) {
      if (first[off] === 0x47 && first[off + 188] === 0x47 && first[off + 376] === 0x47) return true;
    }
    return false;
  } catch {
    return false;
  }
}

/* ------------------------------------------------------------------- state */

function statePath() { return path.join(CACHE_DIR, "state.json"); }
function segPath(i) { return path.join(CACHE_DIR, `seg-${String(i).padStart(4, "0")}.bin`); }
function rawSegPath(i) { return path.join(CACHE_DIR, `raw-${String(i).padStart(4, "0")}.bin`); }

function loadState() {
  try { return JSON.parse(fs.readFileSync(statePath(), "utf8")); } catch { return null; }
}
function saveState(s) {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  fs.writeFileSync(statePath(), JSON.stringify(s, null, 2));
}

/* ------------------------------------------------------------------- ffmpeg */

function findFfmpeg() {
  for (const bin of ["ffmpeg", "ffmpeg-6", "ffmpeg-7"]) {
    const r = spawnSync(bin, ["-version"], { stdio: "ignore" });
    if (r.status === 0) return bin;
  }
  // Termux ke common paths
  for (const p of ["/data/data/com.termux/files/usr/bin/ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]) {
    if (fs.existsSync(p)) {
      const r = spawnSync(p, ["-version"], { stdio: "ignore" });
      if (r.status === 0) return p;
    }
  }
  return null;
}

/* -------------------------------------------------------------------- main */

async function main() {
  let cached = loadState();

  if (args.resume && !cached) {
    say(C.bad("  Koi adhoora download nahi mila. Pehle normal command chalayein."));
    process.exit(1);
  }
  if (args.fresh && fs.existsSync(CACHE_DIR)) {
    fs.rmSync(CACHE_DIR, { recursive: true, force: true });
    cached = null;
    say(C.dim("  purana cache delete kiya"));
  }

  // token: CLI ya cache se (resume ke liye)
  let token = args.token;
  if (!token && cached) {
    token = cached.token;
    say(C.dim("  --resume: pehle wala download continue ho raha hai"));
  }
  if (!token) {
    say(C.bad('  Token chahiye. Istemal: node download.mjs "<lecture link>"'));
    process.exit(1);
  }

  /* ---------------------------------------------------------------- 1. token */
  let info;
  try {
    info = decryptToken(token);
  } catch (e) {
    say(C.bad("  token decrypt fail: ") + e.message);
    process.exit(1);
  }
  if (!info.videoUrl) {
    say(C.bad("  token me videoUrl nahi mila"));
    process.exit(1);
  }
  const m = (info.data.initData ? decodeURIComponent(info.data.initData) : "").match(/auth_date=(\d+)/);
  const ageMin = m ? Math.round((Date.now() - Number(m[1]) * 1000) / 60000) : null;
  say(C.ok("  ✓ token decrypt") + C.dim(`  videoUrl mil gaya${ageMin !== null ? ` · auth ${ageMin} min purana` : ""}`));

  const cookie = info.cookie;
  const initData = info.data && info.data.initData ? decodeURIComponent(info.data.initData) : "";
  const authToken = info.data && (info.data.jwt || info.data.accessToken || info.data.token) || "";

  /* ------------------------------------------------------------- 2. playlist */
  let master;
  try {
    master = await getText(info.videoUrl, cookie);
  } catch (e) {
    say(C.bad("  playlist fetch fail: ") + e.message);
    process.exit(1);
  }
  if (master.status !== 200) {
    say(C.bad(`  ✗ player ne ${master.status} diya`));
    say(C.dim("    Network badal ke try karein (WiFi <-> mobile data), ya VPN."));
    process.exit(1);
  }
  say(C.ok("  ✓ player playlist 200") + C.dim(`  ${master.text.length} bytes`));

  const variants = parseMaster(master.text);
  let mediaUrl = master.url, mediaText = master.text, chosen = null;
  if (variants.length) {
    chosen = pickVariant(variants, master.url, args.quality);
    const r2 = await getText(chosen.url, cookie);
    if (r2.status !== 200) {
      say(C.bad(`  ✗ variant ${chosen.resolution} -> ${r2.status}`));
      process.exit(1);
    }
    mediaUrl = r2.url;
    mediaText = r2.text;
    say(C.ok(`  ✓ quality ${chosen.resolution || chosen.bandwidth}`) +
        C.dim(`  (${variants.map((v) => v.resolution).filter(Boolean).join(", ")})`));
  }

  const { segments } = parseMedia(mediaText, mediaUrl);
  if (!segments.length) {
    say(C.bad("  playlist me segments nahi mile"));
    process.exit(1);
  }
  const totalSec = Math.round(segments.reduce((a, s) => a + s.dur, 0));
  const total = segments.length;
  say(C.ok(`  ✓ ${total} segments`) + C.dim(`  · ${Math.floor(totalSec / 60)}m${totalSec % 60}s` +
      ` · ${segments[0].key ? "AES-128 encrypted" : "plain"}`));

  /* --------------------------------------------------------------- 3. cache */
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  const fingerprint = crypto.createHash("sha1").update(mediaUrl + total).digest("hex").slice(0, 12);
  const state = { token, mediaUrl, total, fingerprint, at: Date.now() };

  let have = 0;
  for (let i = 0; i < total; i++) {
    if (fs.existsSync(segPath(i)) && fs.statSync(segPath(i)).size > 500) have++;
  }
  if (have > 0) {
    say(C.warn(`  ↻ ${have}/${total} segments pehle se hain — wahin se continue`));
  }
  saveState(state);

  /* ------------------------------------------------------------ 4. segments */
  const keyCache = new Map();
  const ua = Math.floor((Date.now() >> 10) & 0xff);
  const fc = `\x1b[${ua % 6 + 2}m`;

  let fatal = null;

  const keyDiag = [];
  let keyStrategy = null;
  let keySaid = false;
  let huntTried = false;
  let keySweep = null; // ek hi sweep — 4 workers ek saath na karein (warna 401 x4)

  // keyprobe.mjs / pichhla run jo key chhod gaya ho use pehle try karo
  const KEY_FILE = path.join(CACHE_DIR, "hlskey.bin");
  function keyFromDisk() {
    try {
      const b = fs.readFileSync(KEY_FILE);
      if (b.length === 16 || b.length === 32) return b;
    } catch { /* nahi hai */ }
    return null;
  }
  let diskKeyBad = false;

  async function getKey(uri, probeRaw = null, probeIv = null) {
    if (fatal) throw fatal;
    let k = keyCache.get(uri);
    if (k) return k;
    const disk = keyFromDisk();
    if (disk && (!probeRaw || keyWorks(disk, probeRaw, probeIv))) {
      keyCache.set(uri, disk);
      if (!getKey.diskSaid) {
        getKey.diskSaid = true;
        say(C.dim("  key cache/hlskey.bin se li gayi"));
      }
      return disk;
    }
    if (disk && !diskKeyBad) {
      diskKeyBad = true;
      say(C.dim("  cache/hlskey.bin ki key is video par kaam nahi kar rahi — ignore"));
    }

    const variants = keyStrategy
      ? [keyStrategy]
      : headerVariants(cookie, initData, authToken);

    if (keySweep) return keySweep;
    keySweep = (async () => {
      let lastStatus = 0, lastBody = "";
      for (const v of variants) {
        const r = await fetch(uri, { headers: v.h, redirect: "follow" });
        absorbCookies(r);
        const buf = Buffer.from(await r.arrayBuffer());
        if (!keyDiag.some((d) => d.v === v.label)) keyDiag.push({ v: v.label, status: r.status, len: buf.length });
        if (r.status === 200 && (buf.length === 16 || buf.length === 32) && probeRaw) {
          // asli test — is key se segment decrypt hota hai?
          if (!keyWorks(buf, probeRaw, probeIv)) {
            if (!keySaid) { keySaid = true; process.stdout.write(`\n  ${C.dim("  (" + v.label + " ne 200 diya par key galat hai — aage)")}`); }
            lastStatus = 200; lastBody = "wrong key"; continue;
          }
        }
        if (r.status === 200 && buf.length >= 16) {
          keyStrategy = v;
          if (!keySaid) {
            keySaid = true;
            process.stdout.write(
              `\n  ${C.ok("✓ AES key mil gayi")} ${C.dim("(" + v.label + ", " + buf.length + " bytes)")}`
            );
          }
          keyCache.set(uri, buf);
          return buf;
        }
        lastStatus = r.status;
        lastBody = buf.toString("utf8").slice(0, 200);
      }

      // koi bhi valid key nahi mili (401/403 ya 200-par-galat) -> alag raaste try karo
      if (!huntTried) {
        huntTried = true;
        process.stdout.write(`\n  ${C.dim("key 401 — aur raaste try kar raha hoon…")}`);
        const hunted = await keyHunt(uri, cookie, initData, authToken, (m) => say(m), probeRaw, probeIv).catch(() => null);
        if (hunted) {
          keyStrategy = { label: "keyHunt", h: variants[0].h };
          keyCache.set(uri, hunted);
          fs.mkdirSync(CACHE_DIR, { recursive: true });
          fs.writeFileSync(path.join(CACHE_DIR, "hlskey.bin"), hunted);
          process.stdout.write(`\n  ${C.ok("✓ AES key mil gayi")} ${C.dim("(keyHunt, " + hunted.length + " bytes)")}`);
          return hunted;
        }
      }

      const host = (() => { try { return new URL(uri).host; } catch { return "?"; } })();
      let err;
      if (lastStatus === 401 || lastStatus === 403 || lastStatus === 200) {
        err = new Error(
          lastStatus === 200
            ? `AES key endpoint ne 200 diya par key galat hai (${host})`
            : `AES key -> HTTP ${lastStatus} (${host}) — koi bhi auth variant kaam nahi kiya`
        );
        err.expired = true;
        err.diag = keyDiag;
        err.host = host;
        err.body = lastBody;
        fatal = err;
      } else {
        err = new Error(`key HTTP ${lastStatus} (${host})`);
      }
      keySweep = null;
      throw err;
    })();
    return keySweep;
  }

  // resume par pehle se maujood segments ka size bhi gino (warna 0.0 MB dikhta hai)
  let bytes = 0;
  for (let i = 0; i < total; i++) {
    try { bytes += fs.statSync(segPath(i)).size; } catch { /* missing */ }
  }
  let done = have, fail = 0, cursor = 0;
  const t0 = Date.now();

  function status(doneN, force = false) {
    if (!force && doneN % 5 !== 0 && doneN !== total) return;
    const sec = Math.max(1, (Date.now() - t0) / 1000);
    const rate = (doneN - have) / sec;
    const eta = rate > 0.05 ? Math.round((total - doneN) / rate) : null;
    const pct = Math.round((doneN / total) * 100);
    const barLen = 22, filled = Math.round((doneN / total) * barLen);
    const bar = "█".repeat(filled) + "░".repeat(barLen - filled);
    const etaTxt = eta === null ? "--" : `${Math.floor(eta / 60)}m${String(eta % 60).padStart(2, "0")}s`;
    process.stdout.write(
      `\r  ${bar} ${String(pct).padStart(3)}%  ${doneN}/${total}  ${mb(bytes)}  ` +
      `fail=${fail}  ${rate.toFixed(1)}/s  ETA ${etaTxt}   `);
  }

  status(have, true);

  async function worker() {
    while (cursor < total && !fatal) {
      const i = cursor++;
      const sp = segPath(i);
      if (fs.existsSync(sp) && fs.statSync(sp).size > 500) continue; // resume
      const seg = segments[i];
      const encrypted = Boolean(seg.key && seg.key.method === "AES-128" && seg.key.uri);
      const rawPath = rawSegPath(i);
      let data = null;
      for (let attempt = 0; attempt < 3 && data === null && !fatal; attempt++) {
        try {
          // 1) segment lao + encrypted cache karo (key expire ho to bhi data bache)
          let raw = null;
          if (fs.existsSync(rawPath) && fs.statSync(rawPath).size > 500) {
            raw = fs.readFileSync(rawPath);       // pehle wale run ka encrypted data
          } else {
            const r = await getBuf(seg.url, cookie);
            if (r.status !== 200) throw new Error(`segment HTTP ${r.status}`);
            raw = r.buf;
            fs.writeFileSync(rawPath, raw);       // encrypted hi — naye token se reuse hoga
          }
          // 2) phir key — agar segment already plain MPEG-TS hai (0x47) to key ki zaroorat nahi
          const plainTs = raw.length > 188 && raw[0] === 0x47;
          const k = (encrypted && !plainTs) ? await getKey(seg.key.uri, raw, ivFor(seg)) : null;
          data = k ? aesDecrypt(k, raw, ivFor(seg)) : raw;
        } catch (e) {
          data = null;
          if (fatal) break;
          if (attempt === 2) {
            fail++;
            process.stdout.write(`\n  ${C.bad("seg " + i + " fail")} ${C.dim(e.message)}`);
          }
        }
      }
      if (data) {
        fs.writeFileSync(sp, data);
        bytes += data.length;
      } else if (fatal) {
        // key nahi mili — encrypted data cache me hai, agle run me kaam aayega
      }
      done++;
      status(done);
    }
  }

  await Promise.all(Array.from({ length: Math.min(args.workers, total) }, worker));
  status(total, true);
  process.stdout.write("\n");

  if (fatal && fatal.expired) {
    const rawCount = Array.from({ length: total }, (_, i) =>
      fs.existsSync(rawSegPath(i)) && fs.statSync(rawSegPath(i)).size > 500).filter(Boolean).length;
    say("");
    say(C.bad("  ✗ AES KEY NAHI MILI (401)"));
    say(C.dim("    Segments aa rahe hain, bas decrypt karne wali key nahi mili."));
    say(C.dim("    Ye zaroori nahi ke link purana ho — endpoint header/cookie maang raha hai."));
    say("");
    if (rawCount > 0) {
      say(C.ok(`  ↻ ${rawCount}/${total} encrypted segments save ho gaye`) +
          C.dim("  (.lecture-cache/raw-*.bin)"));
      say(C.dim("    Naye link se wahi command chalayein — download dobara nahi hoga,"));
      say(C.dim("    sirf key aayegi aur file turant ban jayegi."));
    }
    say("");
    say(C.bold("  Key endpoint ne kaha nahi: ") + C.dim(fatal.host || "?"));
    if (fatal.diag) {
      for (const d of fatal.diag) {
        say(C.dim(`    ${d.v.padEnd(16)} -> HTTP ${d.status} (${d.len} bytes)`));
      }
    }
    if (fatal.body) say(C.dim("    body: " + fatal.body.replace(/\s+/g, " ").slice(0, 120)));
    say("");
    say(C.bold("  Kya karein:"));
    say("    1. Vercel site se try karein (browser mode): " + C.dim("https://lecture-relay.vercel.app"));
    say("    2. Naya link — StudyRatna lecture dobara kholein");
    say("    3. Ye diagnostics mujhe bhej dein (upar ki lines)");
    say("");
    process.exit(3);
  }

  const got = Array.from({ length: total }, (_, i) => fs.existsSync(segPath(i)) && fs.statSync(segPath(i)).size > 500);
  const gotCount = got.filter(Boolean).length;
  say("");
  say(C.ok(`  ✓ ${gotCount}/${total} segments ready`) + C.dim(`  ${mb(bytes + 0)} · fail=${fail}`));
  if (gotCount === 0) {
    say("");
    say(C.bad("  ✗ Ek bhi segment nahi mila — kuch banane ko nahi hai."));
    say(C.dim("    Upar ke 'seg N fail' lines dekhein — asli wajah wahin likhi hai."));
    say(C.dim("    Key 401 -> token/session; HTTP 502 -> network."));
    process.exit(2);
  }
  if (gotCount < total) {
    say(C.warn(`  ${total - gotCount} segments nahi mile.`));
    say(C.dim("    Dobara wahi command chalayein (ya --resume) — sirf missing segments aayenge."));
  }

  /* -------------------------------------------------------------- 5. output */
  const base = args.out.replace(/\.(mp4|ts|mkv)$/i, "");
  const tsFile = path.resolve(base + ".ts");
  const outFile = path.resolve(args.out.endsWith(".mp4") ? args.out : base + ".mp4");

  say(C.dim("  segments jod rahe hain…"));
  const ws = fs.createWriteStream(tsFile);
  for (let i = 0; i < total; i++) {
    if (!got[i]) continue;
    ws.write(fs.readFileSync(segPath(i)));
  }
  await new Promise((res) => ws.end(res));
  say(C.ok(`  ✓ ${path.basename(tsFile)} bana`) + C.dim(`  ${mb(fs.statSync(tsFile).size)}`));

  const ff = args.ffmpeg ? findFfmpeg() : null;
  if (!ff) {
    if (args.ffmpeg) {
      say(C.warn("  ffmpeg nahi mila — .ts file hi bani hai (VLC me chalti hai)"));
      say(C.dim("  MP4 ke liye: pkg install ffmpeg -y && node download.mjs --resume"));
    }
    say("");
    say(C.bold("  Ho gaya: ") + tsFile);
  } else {
    say(C.dim("  ffmpeg se MP4 ban raha hai (audio/video copy, koi re-encode nahi)…"));
    const r = spawnSync(ff, [
      "-y", "-hide_banner", "-loglevel", "error",
      "-i", tsFile, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", outFile,
    ], { stdio: ["ignore", "inherit", "inherit"] });

    if (r.status === 0 && fs.existsSync(outFile) && fs.statSync(outFile).size > 100000) {
      try { fs.unlinkSync(tsFile); } catch { /* ignore */ }
      say("");
      say(C.bold("  ✅ Ho gaya: ") + C.ok(outFile));
      say(C.dim(`     ${mb(fs.statSync(outFile).size)} · Gallery/VLC me khul jayega`));
      say(C.dim(`     cache (optional): ${CACHE_DIR} — delete kar sakte hain`));
    } else {
      say(C.bad("  ffmpeg fail hua — .ts file rakhi hai (VLC me chalti hai)"));
      say(C.dim("     " + tsFile));
    }
  }
}

main().catch((e) => {
  console.error(C.bad("  fatal: ") + (e && e.stack || e));
  process.exit(1);
});
