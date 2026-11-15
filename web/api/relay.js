/**
 * Lecture Relay — Vercel Function, region bom1 (Mumbai)
 * ====================================================
 * Permanent fix ka core idea:
 *
 *   GitHub runner US me hai  -> player.studyratna.org 502 deta hai
 *   Free proxies             -> TLS/CONNECT fail, 90% dead
 *   Vercel ka bom1 (Mumbai)  -> function ka outbound IP INDIA ka hai, managed,
 *                               stable, free tier me 100 GB/maheena
 *
 * Isliye player host ka "India chahiye" gate khud pass ho jaata hai — koi proxy
 * pool, koi scan, koi merge nahi. Function do kaam karta hai:
 *
 *   1. token decrypt + playlist parse  (plan)
 *   2. segment bytes laakar AES-128 decrypt, ~3 MB ke chunks me browser ko de
 *      (Vercel ka response limit 4.5 MB hai, isliye chunked)
 *
 * Browser TS chunks ko jod ke mux.js se MP4 banata hai aur seedha device par
 * save karta hai. Kuch bhi server par store nahi hota.
 *
 * Honest reporting: har stage ka status/HTTP code JSON me jaata hai.
 */

const crypto = require("crypto");

const TOKEN_SEED = "5d90298d9598093cf8cb4b55434de5bc";
const APP_ORIGIN = "https://app.studyratna.org";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const CHUNK_BYTES = 3 * 1024 * 1024; // 3 MB (Vercel limit 4.5 MB)
const PLAN_TTL_MS = 10 * 60 * 1000;

const planCache = new Map(); // tokenHash -> {plan, at}
const muxCache = { buf: null, at: 0 };

/* ------------------------------------------------------------------ token */

function decryptToken(rawToken) {
  const tok = decodeURIComponent(String(rawToken || "").trim());
  const [ivHex, ctHex] = tok.split(":");
  if (!ivHex || !ctHex) throw new Error("token format galat (iv:ct)");
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
  } catch (_) {
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

/* ------------------------------------------------------------------- http */

function headersFor(cookie) {
  const h = {
    "user-agent": UA,
    accept: "*/*",
    "accept-language": "en-IN,en;q=0.9",
    referer: APP_ORIGIN + "/",
  };
  if (cookie) h.cookie = cookie;
  return h;
}

async function getText(url, cookie) {
  const t0 = Date.now();
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  const text = await r.text();
  return { status: r.status, text, url: r.url || url, ms: Date.now() - t0, server: r.headers.get("server") };
}

async function getBuf(url, cookie) {
  const r = await fetch(url, { headers: headersFor(cookie), redirect: "follow" });
  const buf = Buffer.from(await r.arrayBuffer());
  return { status: r.status, buf, url: r.url || url };
}

/* ------------------------------------------------------------------ m3u8 */

function parseMaster(text) {
  const lines = text.split(/\r?\n/);
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line.startsWith("#EXT-X-STREAM-INF")) continue;
    const bw = Number((line.match(/BANDWIDTH=(\d+)/) || [])[1] || 0);
    const res = (line.match(/RESOLUTION=(\d+x\d+)/) || [])[1] || "";
    let j = i + 1;
    while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith("#"))) j++;
    if (lines[j]) out.push({ bandwidth: bw, resolution: res, uri: lines[j].trim() });
  }
  return out;
}

function pickVariant(variants, baseUrl) {
  if (!variants.length) return null;
  const h = (v) => Number((v.resolution.split("x")[1] || 0));
  const hd = variants.filter((v) => h(v) >= 700 && h(v) <= 800);
  const pool = (hd.length ? hd : variants).slice().sort((a, b) => b.bandwidth - a.bandwidth);
  const win = pool[0];
  return { ...win, url: new URL(win.uri, baseUrl).toString() };
}

function parseMedia(text, baseUrl) {
  const segments = [];
  let key = null, map = null, dur = 0, seq = 0;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
      seq = Number(line.split(":")[1]) || 0;
    } else if (line.startsWith("#EXT-X-KEY:")) {
      const method = (line.match(/METHOD=([^,]+)/) || [])[1] || "NONE";
      const uri = (line.match(/URI="([^"]+)"/) || [])[1] || null;
      const iv = (line.match(/IV=0x([0-9a-fA-F]+)/) || [])[1] || null;
      key = method.toUpperCase() === "NONE" ? null : {
        method: method.toUpperCase(),
        uri: uri ? new URL(uri, baseUrl).toString() : null,
        iv,
      };
    } else if (line.startsWith("#EXT-X-MAP:")) {
      const uri = (line.match(/URI="([^"]+)"/) || [])[1];
      if (uri) map = { uri: new URL(uri, baseUrl).toString() };
    } else if (line.startsWith("#EXTINF:")) {
      dur = parseFloat((line.split(":")[1] || "0").split(",")[0]) || 0;
    } else if (!line.startsWith("#")) {
      segments.push({ url: new URL(line, baseUrl).toString(), dur, seq: seq + segments.length, key });
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

function aesDecrypt(keyBuf, data, iv, explicitIv) {
  if (!explicitIv) {
    // HLS default: PKCS#7 padding (auto). Kuch CDN bina padding ke bhejte hain.
    try {
      const d = crypto.createDecipheriv("aes-128-cbc", keyBuf, iv);
      return Buffer.concat([d.update(data), d.final()]);
    } catch (_) {
      const d = crypto.createDecipheriv("aes-128-cbc", keyBuf, iv);
      d.setAutoPadding(false);
      return Buffer.concat([d.update(data), d.final()]);
    }
  }
  const d = crypto.createDecipheriv("aes-128-cbc", keyBuf, iv);
  d.setAutoPadding(false);
  return Buffer.concat([d.update(data), d.final()]);
}

/* ------------------------------------------------------------------ plan */

async function buildPlan(token) {
  const info = decryptToken(token);
  if (!info.videoUrl) throw new Error("token me videoUrl nahi");
  const cookie = info.cookie;

  const master = await getText(info.videoUrl, cookie);
  if (master.status !== 200) {
    const err = new Error(`master HTTP ${master.status} (${master.server || "?"})`);
    err.report = { stage: "master", status: master.status, server: master.server };
    throw err;
  }
  let mediaUrl = master.url, mediaText = master.text, variant = null;
  const variants = parseMaster(master.text);
  if (variants.length) {
    const v = pickVariant(variants, master.url);
    const r2 = await getText(v.url, cookie);
    if (r2.status !== 200) {
      const err = new Error(`variant HTTP ${r2.status}`);
      err.report = { stage: "variant", status: r2.status, url: v.url };
      throw err;
    }
    variant = { resolution: v.resolution, bandwidth: v.bandwidth };
    mediaUrl = r2.url;
    mediaText = r2.text;
  }
  const { segments, map } = parseMedia(mediaText, mediaUrl);
  if (!segments.length) throw new Error("playlist me segments nahi");

  const kind = map || /\.m4s|\.mp4/.test(segments[0].url) ? "fmp4" : "ts";
  return {
    info, cookie, segments, map, kind, variant,
    totalDuration: Math.round(segments.reduce((a, s) => a + s.dur, 0)),
    encrypted: Boolean(segments[0].key),
    keyUri: segments[0].key ? segments[0].key.uri : null,
    mediaUrl,
    region: process.env.VERCEL_REGION || "?",
  };
}

async function planFor(token) {
  const k = crypto.createHash("sha1").update(String(token)).digest("hex");
  const hit = planCache.get(k);
  if (hit && Date.now() - hit.at < PLAN_TTL_MS) return hit.plan;
  const plan = await buildPlan(token);
  planCache.set(k, { plan, at: Date.now() });
  if (planCache.size > 20) planCache.delete(planCache.keys().next().value);
  return plan;
}

/* --------------------------------------------------------------- mux.js */

async function fetchMuxjs() {
  if (muxCache.buf && Date.now() - muxCache.at < 6 * 3600e3) return muxCache.buf;
  const urls = [
    "https://cdn.jsdelivr.net/npm/mux.js@7.1.1/dist/mux.min.js",
    "https://unpkg.com/mux.js@7.1.1/dist/mux.min.js",
  ];
  let lastErr = null;
  for (const u of urls) {
    try {
      const r = await fetch(u);
      if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
      const buf = Buffer.from(await r.arrayBuffer());
      if (buf.length < 20000) throw new Error(`too small ${buf.length}`);
      muxCache.buf = buf;
      muxCache.at = Date.now();
      return buf;
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error("mux.js fetch fail");
}

/* -------------------------------------------------------------- handlers */

async function handle(req, res, q) {
  const action = q.get("action") || "";

  if (action === "inspect") {
    const info = decryptToken(q.get("token") || "");
    const cf = cfExpiry(info.cookie);
    return json(res, {
      ok: true,
      region: process.env.VERCEL_REGION || "?",
      authDate: info.authDate,
      authAgeMinutes: info.authAgeMinutes,
      videoUrl: info.videoUrl,
      cloudfront: cf,
      cookieLen: info.cookie.length,
      tokenKeys: info.tokenKeys,
    });
  }

  if (action === "selftest") {
    const token = q.get("token");
    const out = { region: process.env.VERCEL_REGION || "?", probes: [] };
    let cookie = "", videoUrl = null;
    if (token) {
      const info = decryptToken(token);
      cookie = info.cookie;
      videoUrl = info.videoUrl;
    }
    const targets = [];
    if (videoUrl) {
      targets.push(["master", videoUrl]);
      targets.push(["hls720", videoUrl.replace("master.m3u8", "hls/720/main.m3u8")]);
    }
    if (q.get("u")) targets.push(["custom", q.get("u")]);
    for (const [name, url] of targets) {
      const t = Date.now();
      try {
        const r = await getText(url, cookie);
        out.probes.push({
          name, status: r.status, bytes: r.text.length, ms: Date.now() - t,
          server: r.server || null, head: r.text.slice(0, 120).replace(/\n/g, " "),
        });
      } catch (e) {
        out.probes.push({ name, status: "ERR", error: String(e.message).slice(0, 120) });
      }
    }
    return json(res, out);
  }

  if (action === "plan") {
    const plan = await planFor(q.get("token") || "");
    return json(res, {
      ok: true,
      region: plan.region,
      videoUrl: plan.info.videoUrl,
      variant: plan.variant,
      kind: plan.kind,
      segments: plan.segments.length,
      durationSec: plan.totalDuration,
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
    let bytes = 0, i = from, failed = 0, done = false;
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
            data = aesDecrypt(kb, data, ivFor(seg), Boolean(seg.key.iv));
          }
        } catch (e) {
          if (attempt === 1) { failed++; failDetail.push({ i, err: String(e.message).slice(0, 60) }); }
          data = null;
        }
      }
      if (data) {
        if (parts.length && bytes + data.length > CHUNK_BYTES) {
          // agla segment is chunk me nahi aayega
          break;
        }
        parts.push(data);
        bytes += data.length;
      }
      i++;
      if (bytes >= CHUNK_BYTES) break;
    }
    done = i >= segments.length;

    // fMP4 me init segment pehle chunk me hi bhejna hai
    if (kind === "fmp4" && plan.map && from === 0) {
      try {
        const mr = await getBuf(plan.map.uri, cookie);
        if (mr.status === 200) parts.unshift(mr.buf);
      } catch (_) {}
    }

    const body = Buffer.concat(parts);
    res.writeHead(200, {
      "content-type": kind === "fmp4" ? "video/mp4" : "video/mp2t",
      "x-from": String(from),
      "x-next": String(i),
      "x-count": String(parts.length),
      "x-done": done ? "1" : "0",
      "x-total": String(segments.length),
      "x-failed": String(failed),
      "x-fail-sample": encodeURIComponent(JSON.stringify(failDetail.slice(0, 4))),
      "x-region": plan.region,
      "access-control-allow-origin": "*",
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

  return json(res, {
    ok: true,
    endpoints: ["inspect", "selftest", "plan", "chunk", "muxjs"],
    region: process.env.VERCEL_REGION || "?",
  });
}

function json(res, obj, status = 200) {
  const body = JSON.stringify(obj, null, 2);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "cache-control": "no-store",
  });
  res.end(body);
}

module.exports = async function handler(req, res) {
  try {
    const url = new URL(req.url, `https://${req.headers.host || "localhost"}`);
    await handle(req, res, url.searchParams);
  } catch (e) {
    const status = e.report ? 502 : 500;
    json(res, {
      ok: false,
      error: String(e.message || e),
      report: e.report || null,
      stack: String(e.stack || "").split("\n").slice(0, 4).join("\n"),
    }, status);
  }
};
