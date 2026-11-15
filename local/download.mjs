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

function headersFor(cookie) {
  const h = {
    "user-agent": UA,
    accept: "*/*",
    "accept-language": "en-IN,en;q=0.9",
    referer: APP_ORIGIN + "/",
    origin: APP_ORIGIN,
  };
  if (cookie) h.cookie = cookie;
  return h;
}

async function getText(url, cookie) {
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  return { status: r.status, text: await r.text(), url: r.url || url };
}

async function getBuf(url, cookie) {
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  return { status: r.status, buf: Buffer.from(await r.arrayBuffer()), url: r.url || url };
}

/* ------------------------------------------------------------ token decrypt */

function decryptToken(raw) {
  const tok = decodeURIComponent(String(raw || "").trim());
  const [ivHex, ctHex] = tok.split(":");
  if (!ivHex || !ctHex) throw new Error("token format galat (iv:ct chahiye)");
  const key = crypto.createHash("sha256").update(TOKEN_SEED).digest();
  const d = crypto.createDecipheriv("aes-256-cbc", key, Buffer.from(ivHex, "hex"));
  const plain = Buffer.concat([d.update(Buffer.from(ctHex, "hex")), d.final()]);
  const data = JSON.parse(plain.toString("utf8"));
  return { data, cookie: data.cookie || "", videoUrl: data.videoUrl || data.url || "" };
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

/* ------------------------------------------------------------------- state */

function statePath() { return path.join(CACHE_DIR, "state.json"); }
function segPath(i) { return path.join(CACHE_DIR, `seg-${String(i).padStart(4, "0")}.bin`); }

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

  async function getKey(uri) {
    let k = keyCache.get(uri);
    if (k) return k;
    const r = await getBuf(uri, cookie);
    if (r.status !== 200) {
      throw new Error(`key HTTP ${r.status} (${new URL(uri).host})`);
    }
    k = r.buf;
    keyCache.set(uri, k);
    return k;
  }

  let done = have, bytes = 0, fail = 0, cursor = 0;
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
    while (cursor < total) {
      const i = cursor++;
      const sp = segPath(i);
      if (fs.existsSync(sp) && fs.statSync(sp).size > 500) continue; // resume
      const seg = segments[i];
      let data = null;
      for (let attempt = 0; attempt < 3 && data === null; attempt++) {
        try {
          const r = await getBuf(seg.url, cookie);
          if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
          data = r.buf;
          if (seg.key && seg.key.method === "AES-128" && seg.key.uri) {
            const k = await getKey(seg.key.uri);
            data = aesDecrypt(k, data, ivFor(seg));
          }
        } catch (e) {
          data = null;
          if (attempt === 2) {
            fail++;
            process.stdout.write(`\n  ${C.bad("seg " + i + " fail")} ${C.dim(e.message)}`);
          }
        }
      }
      if (data) {
        fs.writeFileSync(sp, data);
        bytes += data.length;
      }
      done++;
      status(done);
    }
  }

  await Promise.all(Array.from({ length: Math.min(args.workers, total) }, worker));
  status(total, true);
  process.stdout.write("\n");

  const got = Array.from({ length: total }, (_, i) => fs.existsSync(segPath(i)) && fs.statSync(segPath(i)).size > 500);
  const gotCount = got.filter(Boolean).length;
  say("");
  say(C.ok(`  ✓ ${gotCount}/${total} segments ready`) + C.dim(`  ${mb(bytes + 0)} · fail=${fail}`));
  if (gotCount === 0) {
    say("");
    say(C.bad("  ✗ Ek bhi segment nahi mila — kuch banane ko nahi hai."));
    say(C.dim("    Aksar wajah: key endpoint (API) block, ya network badal chuka hai."));
    say(C.dim("    WiFi <-> mobile data badal ke dobara try karein."));
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
