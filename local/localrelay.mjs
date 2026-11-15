#!/usr/bin/env node
/**
 * Lecture → MP4 : Local Relay
 * ===========================
 * Kyun local:
 *   player.studyratna.org Cloudflare ke peeche hai aur CORS sirf apne origin
 *   (app.studyratna.org) ko response padhne deta hai. Browser ka `fetch` kisi
 *   doosre origin se "Failed to fetch" deta hai — chahe aapka IP kitna bhi sahi
 *   ho. Server-side request par CORS lagta HI NAHI, aur ye script aapke PC se
 *   chalti hai, matlab IP bhi aapka hi rehta hai.
 *
 * Chalane ka tarika (Node 18+):
 *   node localrelay.mjs
 *   phir browser me -> http://localhost:8787
 *
 * Koi npm install nahi, koi dependency nahi. Sirf Node ke built-ins.
 *
 * Kya karta hai:
 *   GET  /                     UI
 *   GET  /api/inspect?token=   token decrypt (aapke PC par) + cookie expiry
 *   GET  /api/selftest?token=  player reachability — asli HTTP status
 *   GET  /api/plan?token=      master -> 720p -> segments + AES key info
 *   GET  /api/chunk?token=&from=N   ~3 MB decrypted segments (binary)
 *   GET  /api/muxjs            mux.js (local + cached; CDN fallback)
 *   GET  /api/fetch?u=         raw passthrough (playlist debug ke liye)
 */

import crypto from "node:crypto";
import http from "node:http";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

/* ------------------------------------------------------------------ config */

const PORT = Number(process.env.PORT || 8787);
const HOST = process.env.HOST || "127.0.0.1";
const TOKEN_SEED = "5d90298d9598093cf8cb4b55434de5bc";
const APP_ORIGIN = "https://app.studyratna.org";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const CHUNK_BYTES = 3 * 1024 * 1024;
const PLAN_TTL_MS = 10 * 60 * 1000;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const planCache = new Map();
const muxCache = { buf: null, at: 0 };

/* ------------------------------------------------------------------ helpers */

const log = (...a) => console.log("[relay]", ...a);

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
  const t0 = Date.now();
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  const text = await r.text();
  return {
    status: r.status,
    text,
    url: r.url || url,
    ms: Date.now() - t0,
    server: r.headers.get("server"),
    ctype: r.headers.get("content-type"),
  };
}

async function getBuf(url, cookie) {
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  return { status: r.status, buf: Buffer.from(await r.arrayBuffer()), url: r.url || url };
}

/* ------------------------------------------------------------ token decrypt */

function decryptToken(rawToken) {
  const tok = decodeURIComponent(String(rawToken || "").trim());
  const [ivHex, ctHex] = tok.split(":");
  if (!ivHex || !ctHex) throw new Error("token format galat (iv:ct chahiye)");
  const key = crypto.createHash("sha256").update(TOKEN_SEED).digest();
  const d = crypto.createDecipheriv("aes-256-cbc", key, Buffer.from(ivHex, "hex"));
  const plain = Buffer.concat([d.update(Buffer.from(ctHex, "hex")), d.final()]);
  const data = JSON.parse(plain.toString("utf8"));
  const info = {
    data,
    cookie: data.cookie || "",
    videoUrl: data.videoUrl || data.url || findDeep(data, "videoUrl"),
    tokenKeys: Object.keys(data),
  };
  const m = (data.initData ? decodeURIComponent(data.initData) : "").match(/auth_date=(\d+)/);
  if (m) {
    info.authDate = new Date(Number(m[1]) * 1000).toISOString();
    info.authAgeMinutes = Math.round((Date.now() - Number(m[1]) * 1000) / 60000);
  }
  return info;
}

function cfExpiry(cookie) {
  const pol = String(cookie || "").match(/CloudFront-Policy=([^;]+)/);
  if (!pol) return null;
  try {
    const b64 = pol[1].replace(/-/g, "+").replace(/_/g, "/");
    const p = JSON.parse(Buffer.from(b64, "base64").toString("utf8"));
    const less = p.Statement[0].Condition.DateLessThan["AWS:EpochTime"];
    return { iso: new Date(less * 1000).toISOString(), valid: less * 1000 > Date.now() };
  } catch {
    return null;
  }
}

function findDeep(obj, keyName, depth = 0) {
  if (!obj || typeof obj !== "object" || depth > 4) return null;
  for (const k of Object.keys(obj)) {
    if (k === keyName && typeof obj[k] === "string") return obj[k];
    const r = findDeep(obj[k], keyName, depth + 1);
    if (r) return r;
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

function pickVariant(vs, base) {
  if (!vs.length) return null;
  const h = (v) => Number((v.resolution.split("x")[1] || 0));
  const hd = vs.filter((v) => h(v) >= 700 && h(v) <= 800);
  const pool = (hd.length ? hd : vs).slice().sort((a, b) => b.bandwidth - a.bandwidth);
  const win = pool[0];
  return { ...win, url: new URL(win.uri, base).toString() };
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
      const uri = (l.match(/URI="([^"]+)"/) || [])[1] || null;
      const iv = (l.match(/IV=0x([0-9a-fA-F]+)/) || [])[1] || null;
      key = method.toUpperCase() === "NONE" ? null : {
        method: method.toUpperCase(),
        uri: uri ? new URL(uri, base).toString() : null,
        iv,
      };
    } else if (l.startsWith("#EXT-X-MAP:")) {
      const uri = (l.match(/URI="([^"]+)"/) || [])[1];
      if (uri) map = { uri: new URL(uri, base).toString() };
    } else if (l.startsWith("#EXTINF:")) {
      dur = parseFloat((l.split(":")[1] || "0").split(",")[0]) || 0;
    } else if (!l.startsWith("#")) {
      segments.push({ url: new URL(l, base).toString(), dur, seq: seq + segments.length, key });
      dur = 0;
    }
  }
  return { segments, map };
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

/* -------------------------------------------------------------------- plan */

async function buildPlan(token) {
  const info = decryptToken(token);
  if (!info.videoUrl) throw new Error("token me videoUrl nahi mila");
  const cookie = info.cookie;

  const master = await getText(info.videoUrl, cookie);
  if (master.status !== 200) {
    const e = new Error(`master HTTP ${master.status} (${master.server || "?"})`);
    e.report = { stage: "master", status: master.status, server: master.server, head: master.text.slice(0, 300) };
    throw e;
  }
  let mediaUrl = master.url, mediaText = master.text, variant = null;
  const variants = parseMaster(master.text);
  if (variants.length) {
    const v = pickVariant(variants, master.url);
    const r2 = await getText(v.url, cookie);
    if (r2.status !== 200) {
      const e = new Error(`variant HTTP ${r2.status}`);
      e.report = { stage: "variant", status: r2.status, url: v.url };
      throw e;
    }
    variant = { resolution: v.resolution, bandwidth: v.bandwidth };
    mediaUrl = r2.url;
    mediaText = r2.text;
  }
  const { segments, map } = parseMedia(mediaText, mediaUrl);
  if (!segments.length) throw new Error("playlist me segments nahi mile");

  return {
    info, cookie, segments, map, variant,
    kind: map ? "fmp4" : /\.m4s|\.mp4/.test(segments[0].url) ? "fmp4" : "ts",
    durationSec: Math.round(segments.reduce((a, s) => a + s.dur, 0)),
    encrypted: Boolean(segments[0].key),
    keyUri: segments[0].key ? segments[0].key.uri : null,
    mediaUrl,
  };
}

async function planFor(token) {
  const k = crypto.createHash("sha1").update(String(token)).digest("hex");
  const hit = planCache.get(k);
  if (hit && Date.now() - hit.at < PLAN_TTL_MS) return hit.plan;
  const plan = await buildPlan(token);
  planCache.set(k, { plan, at: Date.now() });
  if (planCache.size > 10) planCache.delete(planCache.keys().next().value);
  return plan;
}

/* ------------------------------------------------------------------- mux.js */

async function fetchMuxjs() {
  if (muxCache.buf && Date.now() - muxCache.at < 6 * 3600e3) return muxCache.buf;
  const local = path.join(HERE, "mux.min.js");
  if (fs.existsSync(local)) {
    muxCache.buf = fs.readFileSync(local);
    muxCache.at = Date.now();
    return muxCache.buf;
  }
  const urls = [
    "https://cdn.jsdelivr.net/npm/mux.js@7.1.1/dist/mux.min.js",
    "https://unpkg.com/mux.js@7.1.1/dist/mux.min.js",
  ];
  let last;
  for (const u of urls) {
    try {
      const r = await fetch(u);
      if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
      const buf = Buffer.from(await r.arrayBuffer());
      if (buf.length < 20000) throw new Error("too small");
      try { fs.writeFileSync(local, buf); } catch { /* read-only ok */ }
      muxCache.buf = buf;
      muxCache.at = Date.now();
      log(`mux.js cached (${(buf.length / 1024).toFixed(0)} KB)`);
      return buf;
    } catch (e) { last = e; }
  }
  throw last || new Error("mux.js fetch fail");
}

/* ------------------------------------------------------------------ router */

function sendJson(res, obj, status = 200) {
  const body = JSON.stringify(obj, null, 2);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(body);
}

async function handle(req, res, url) {
  const q = url.searchParams;

  if (url.pathname === "/" || url.pathname === "/index.html") {
    const f = path.join(HERE, "index.html");
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    return res.end(fs.readFileSync(f));
  }
  if (url.pathname === "/app.js" || url.pathname === "/style.css") {
    const f = path.join(HERE, url.pathname.slice(1));
    if (!fs.existsSync(f)) { res.writeHead(404); return res.end("nf"); }
    const type = f.endsWith(".js") ? "application/javascript" : "text/css";
    res.writeHead(200, { "content-type": type + "; charset=utf-8", "cache-control": "no-store" });
    return res.end(fs.readFileSync(f));
  }

  /* ---------------------------------------------------------------- api */
  const action = q.get("action") || "";

  if (action === "inspect") {
    const info = decryptToken(q.get("token") || "");
    return sendJson(res, {
      ok: true,
      mode: "local",
      authDate: info.authDate,
      authAgeMinutes: info.authAgeMinutes,
      videoUrl: info.videoUrl,
      cloudfront: cfExpiry(info.cookie),
      cookieLen: info.cookie.length,
      tokenKeys: info.tokenKeys,
    });
  }

  if (action === "selftest") {
    const out = { mode: "local", probes: [] };
    let cookie = "", videoUrl = null;
    const token = q.get("token");
    if (token) {
      try {
        const info = decryptToken(token);
        cookie = info.cookie;
        videoUrl = info.videoUrl;
      } catch (e) {
        out.tokenError = e.message;
      }
    }
    const targets = [];
    if (videoUrl) {
      targets.push(["master", videoUrl]);
      targets.push(["hls720", videoUrl.replace("master.m3u8", "hls/720/main.m3u8")]);
    }
    if (q.get("u")) targets.push(["custom", q.get("u")]);
    for (const [name, u] of targets) {
      const t = Date.now();
      try {
        const r = await getText(u, cookie);
        out.probes.push({
          name, status: r.status, bytes: r.text.length, ms: Date.now() - t,
          server: r.server, ctype: r.ctype,
          head: r.text.slice(0, 160).replace(/\s+/g, " "),
        });
      } catch (e) {
        out.probes.push({ name, status: "ERR", error: String(e.message).slice(0, 140) });
      }
    }
    return sendJson(res, out);
  }

  if (action === "plan") {
    const plan = await planFor(q.get("token") || "");
    return sendJson(res, {
      ok: true,
      mode: "local",
      videoUrl: plan.info.videoUrl,
      variant: plan.variant,
      kind: plan.kind,
      segments: plan.segments.length,
      durationSec: plan.durationSec,
      encrypted: plan.encrypted,
      keyUri: plan.keyUri,
      host: new URL(plan.mediaUrl).host,
      firstSegments: plan.segments.slice(0, 3).map((s) => s.url),
    });
  }

  if (action === "chunk") {
    const token = q.get("token") || "";
    const from = Math.max(0, Number(q.get("from") || 0));
    const plan = await planFor(token);
    const { segments, cookie, kind } = plan;

    const parts = [];
    const keyCache = new Map();
    let bytes = 0, i = from, failed = 0;
    const failDetail = [];

    while (i < segments.length) {
      const seg = segments[i];
      let data = null;
      for (let attempt = 0; attempt < 2 && data === null; attempt++) {
        try {
          const r = await getBuf(seg.url, cookie);
          if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
          data = r.buf;
          if (seg.key && seg.key.method === "AES-128" && seg.key.uri) {
            let kb = keyCache.get(seg.key.uri);
            if (!kb) {
              const kr = await getBuf(seg.key.uri, cookie);
              if (kr.status !== 200) throw new Error(`key HTTP ${kr.status}`);
              kb = kr.buf;
              keyCache.set(seg.key.uri, kb);
            }
            data = aesDecrypt(kb, data, ivFor(seg));
          }
        } catch (e) {
          if (attempt === 1) { failed++; failDetail.push({ i, err: String(e.message).slice(0, 70) }); }
          data = null;
        }
      }
      if (data) {
        if (parts.length && bytes + data.length > CHUNK_BYTES) break;
        parts.push(data);
        bytes += data.length;
      }
      i++;
      if (bytes >= CHUNK_BYTES) break;
    }

    if (kind === "fmp4" && plan.map && from === 0) {
      try {
        const mr = await getBuf(plan.map.uri, cookie);
        if (mr.status === 200) parts.unshift(mr.buf);
      } catch { /* ignore */ }
    }

    const body = Buffer.concat(parts);
    res.writeHead(200, {
      "content-type": kind === "fmp4" ? "video/mp4" : "video/mp2t",
      "x-from": String(from),
      "x-next": String(i),
      "x-count": String(parts.length),
      "x-done": i >= segments.length ? "1" : "0",
      "x-total": String(segments.length),
      "x-failed": String(failed),
      "x-fail-sample": encodeURIComponent(JSON.stringify(failDetail.slice(0, 4))),
      "cache-control": "no-store",
    });
    return res.end(body);
  }

  if (action === "muxjs") {
    const buf = await fetchMuxjs();
    res.writeHead(200, {
      "content-type": "application/javascript; charset=utf-8",
      "cache-control": "public, max-age=86400",
    });
    return res.end(buf);
  }

  if (action === "fetch") {
    const u = q.get("u");
    if (!u) return sendJson(res, { ok: false, error: "u= chahiye" }, 400);
    const r = await getText(u, q.get("cookie") || "");
    return sendJson(res, {
      ok: r.status === 200, status: r.status, bytes: r.text.length,
      url: r.url, server: r.server, head: r.text.slice(0, 1200),
    });
  }

  return sendJson(res, {
    ok: true, mode: "local",
    endpoints: ["inspect", "selftest", "plan", "chunk", "muxjs", "fetch"],
  });
}

/* -------------------------------------------------------------------- main */

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  handle(req, res, url).catch((e) => {
    const status = e.report ? 502 : 500;
    sendJson(res, {
      ok: false,
      error: String(e.message || e),
      report: e.report || null,
    }, status);
  });
});

server.listen(PORT, HOST, () => {
  const link = `http://localhost:${PORT}`;
  console.log("");
  console.log("  ╔══════════════════════════════════════════════╗");
  console.log("  ║   Lecture → MP4   ·   local relay chal raha  ║");
  console.log("  ╚══════════════════════════════════════════════╝");
  console.log("");
  console.log(`  Browser me kholen:  ${link}`);
  console.log("");
  console.log("  Requests aapke PC se jaati hain (server-side), isliye");
  console.log("  CORS ka koi rok nahi. Band karne ke liye Ctrl+C.");
  console.log("");
});

server.on("error", (e) => {
  if (e.code === "EADDRINUSE") {
    console.error(`\n  Port ${PORT} vyast hai. Doosra port: PORT=8788 node localrelay.mjs\n`);
    process.exit(1);
  }
  throw e;
});
