#!/usr/bin/env node
/**
 * keyprobe.mjs — AES key ka raasta dhoondhne wala diagnostic (phone par chalayein).
 *
 *   node keyprobe.mjs "<studyratna lecture link>"
 *
 * Ye kya karta hai:
 *   1. token decrypt karta hai (koi secret print nahi)
 *   2. master + media playlist laata hai aur EXT-X-KEY line jaisi hai waisi dikhata hai
 *   3. segment 0 ke pehle bytes dekhta hai (encrypted hai ya plain TS)
 *   4. ~10 key URL × 6 header set try karta hai, har jawab ka status + WWW-Authenticate
 *      dikhata hai (401 ka asli reason wahin likha hota hai)
 *   5. jo bhi chal jaye -> cache/hlskey.bin me save + "KEY MILI" line
 *
 * Output chhota rehta hai: sirf zaroori lines.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const TOKEN_SEED = "5d90298d9598093cf8cb4b55434de5bc";
const APP_ORIGIN = "https://app.studyratna.org";
const PLAYER_ORIGIN = "https://player.studyratna.org";
const PW_ORG = "5eb393ee95fab7468a79d189"; // pw.live brand id — penpencil API client-id
const UA =
  "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/130.0.0.0 Mobile Safari/537.36";

const CACHE_DIR = path.join(process.cwd(), ".lecture-cache");

/* ------------------------------------------------------------------ token */

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
  if (/^https?:\/\//i.test(s)) throw new Error("link me playerToken= nahi mila");
  const parts = s.split(":");
  if (parts.length !== 2) throw new Error(`token galat (${parts.length} hisse)`);
  const [iv, ct] = parts.map((x) => x.trim());
  if (!/^[0-9a-fA-F]{32}$/.test(iv)) throw new Error(`IV galat (${iv.length} hex)`);
  if (!/^[0-9a-fA-F]+$/.test(ct) || ct.length % 32 !== 0) {
    throw new Error(`encrypted part galat (${ct.length} hex)`);
  }
  return { iv, ct };
}

function decryptToken(raw) {
  const { iv, ct } = resolveToken(raw);
  const key = crypto.createHash("sha256").update(TOKEN_SEED).digest();
  const d = crypto.createDecipheriv("aes-256-cbc", key, Buffer.from(iv, "hex"));
  const plain = Buffer.concat([d.update(Buffer.from(ct, "hex")), d.final()]);
  return JSON.parse(plain.toString("utf8"));
}

/* ------------------------------------------------------------------- http */

const baseHeaders = () => ({
  "user-agent": UA,
  accept: "*/*",
  "accept-language": "en-IN,en;q=0.9",
});

async function req(url, headers = {}) {
  try {
    const r = await fetch(url, {
      headers: { ...baseHeaders(), ...headers },
      redirect: "follow",
      signal: AbortSignal.timeout(25000),
    });
    const buf = Buffer.from(await r.arrayBuffer());
    let setck = [];
    try {
      setck = typeof r.headers.getSetCookie === "function" ? r.headers.getSetCookie() : [];
    } catch { /* ignore */ }
    return { ok: true, status: r.status, buf, headers: r.headers, finalUrl: r.url, setck };
  } catch (e) {
    return { ok: false, status: 0, buf: Buffer.alloc(0), err: e.message };
  }
}

/* ---------------------------------------------------------------- helpers */

const out = (s = "") => process.stdout.write(s + "\n");
const hex = (b, n = 16) => b.subarray(0, n).toString("hex");
const C = {
  d: (s) => `\x1b[2m${s}\x1b[0m`,
  b: (s) => `\x1b[1m${s}\x1b[0m`,
  g: (s) => `\x1b[32m${s}\x1b[0m`,
  r: (s) => `\x1b[31m${s}\x1b[0m`,
  y: (s) => `\x1b[33m${s}\x1b[0m`,
};

/* ------------------------------------------------------------------- main */

async function main() {
  const raw = process.argv.slice(2).join(" ").trim();
  out(C.b("\n=== 1. TOKEN ==="));
  if (!raw) {
    out(C.y("  link do: node keyprobe.mjs \"<link>\""));
    process.exit(1);
  }
  let info;
  try {
    info = decryptToken(raw);
    out(C.g("  ✓ token decrypt ho gaya"));
    out(C.d(`  videoUrl: ${info.videoUrl || "(khaali)"}`));
    out(C.d(`  cookie: ${info.cookie ? info.cookie.length + " bytes" : "nahi"}`));
  } catch (e) {
    out(C.r("  ✗ token: " + e.message));
    process.exit(1);
  }

  const cookie = info.cookie || "";
  const masterUrl = info.videoUrl || info.url;
  if (!masterUrl) { out(C.r("  token me videoUrl nahi hai")); process.exit(1); }

  /* ---- 2. playlists ---- */
  out(C.b("\n=== 2. PLAYLIST ==="));
  const appH = { referer: APP_ORIGIN + "/", origin: APP_ORIGIN };
  if (cookie) appH.cookie = cookie;

  const master = await req(masterUrl, appH);
  out(`  master: HTTP ${master.status}${master.ok ? "" : " " + (master.err || "")}`);
  if (!master.ok || master.status !== 200) {
    out(C.r("  master hi nahi mila — ruk raha hoon. Link naya karein."));
    process.exit(1);
  }
  const masterText = master.buf.toString("utf8");

  // variant chuno (sabse zyada bandwidth)
  let variantUrl = null, best = -1;
  const lines = masterText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith("#EXT-X-STREAM-INF:")) {
      const bw = Number((lines[i].match(/BANDWIDTH=(\d+)/) || [])[1] || 0);
      const res = (lines[i].match(/RESOLUTION=(\d+x\d+)/) || [])[1] || "?";
      const next = (lines[i + 1] || "").trim();
      if (next && !next.startsWith("#") && bw > best) {
        best = bw;
        variantUrl = new URL(next, masterUrl).toString();
      }
      out(C.d(`  variant ${res} bw=${bw}`));
    }
  }
  if (!variantUrl) {
    variantUrl = masterUrl; // playlist hi media playlist hai
    out(C.d("  master me variant nahi — media playlist hi hai"));
  }

  const media = await req(variantUrl, appH);
  out(`  media: HTTP ${media.status}  ${C.d(variantUrl.slice(0, 90))}`);
  const mediaText = media.buf.toString("utf8");
  const keyLines = mediaText.split(/\r?\n/).filter((l) => l.includes("EXT-X-KEY"));
  out(C.b("  EXT-X-KEY lines:"));
  for (const l of keyLines) out("    " + l);

  const segLine = mediaText.split(/\r?\n/).find((l) => l.trim() && !l.startsWith("#"));
  const segUrl = segLine ? new URL(segLine.trim(), variantUrl).toString() : null;
  out(C.d(`  pehla segment: ${segUrl ? segUrl.slice(0, 110) : "?"}`));

  // key URI
  let keyUri = null;
  if (keyLines.length) {
    keyUri = (keyLines[0].match(/URI="([^"]+)"/) || [])[1] || null;
    if (keyUri) keyUri = new URL(keyUri, variantUrl).toString();
  }
  out(C.d(`  key URI: ${keyUri || "(koi nahi)"}`));

  /* ---- 3. segment encrypted? ---- */
  out(C.b("\n=== 3. SEGMENT ==="));
  let segRes = null;
  if (segUrl) {
    const s = await req(segUrl, appH);
    segRes = s;
    out(`  seg0: HTTP ${s.status}, ${s.buf.length} bytes, first=${hex(s.buf)}`);
    // TS sync 3 packets tak check karo (encrypted data me 0x47 accidentally aa sakta hai)
    const isTs = (b) => {
      for (let off = 0; off < 188; off++) {
        if (b[off] === 0x47 && b[off + 188] === 0x47 && b[off + 376] === 0x47) return true;
      }
      return false;
    };
    if (isTs(s.buf)) {
      out(C.g("  ✓ plain MPEG-TS — key ki zaroorat hi nahi!"));
      out(C.b("  Aage: node download.mjs \"<link>\" --fresh  (key skip ho jayegi)"));
    } else if (s.buf.length) {
      out(C.y("  encrypted lag raha hai (0x47 sync nahi) → key chahiye"));
    }
  }

  /* ---- 3b. Set-Cookie (bahut important: key request me chahiye ho sakti hai) ---- */
  const allSet = [...(master.setck || []), ...(media.setck || []), ...(segRes ? segRes.setck || [] : [])];
  if (allSet.length) {
    out(C.y("  Set-Cookie mili → inhe key request me bhejna pad sakta hai:"));
    for (const c of allSet) out(C.d("    " + c.slice(0, 110)));
  } else {
    out(C.d("  Set-Cookie: koi nahi"));
  }

  /* ---- 3c. player JS scan ---- */
  out(C.b("\n=== 3c. PLAYER JS ==="));
  const jsUrls = new Set();
  for (const pageUrl of [masterUrl, variantUrl]) {
    const pg = await req(pageUrl.replace(/\/[^/]*$/, "/"), appH);
    if (pg.status === 200) {
      const txt = pg.buf.toString("utf8");
      for (const m of txt.matchAll(/<script[^>]+src=["']([^"']+)["']/gi)) {
        try { jsUrls.add(new URL(m[1], pageUrl).toString()); } catch { /* ignore */ }
      }
    }
  }
  out(C.d(`  ${jsUrls.size} script mile`));
  for (const js of [...jsUrls].slice(0, 6)) {
    const r = await req(js, appH);
    if (r.status !== 200) { out(C.d(`  js ${r.status} ${js.slice(0, 70)}`)); continue; }
    const t = r.buf.toString("utf8");
    out(C.d(`  js ${r.buf.length}b ${js.slice(0, 70)}`));
    const hits = new Set();
    for (const m of t.matchAll(/["'`]([^"'`\s]{0,140}(?:get-hls-key|enc\.key|hls-key)[^"'`\s]{0,90})["'`]/gi)) hits.add(m[1]);
    for (const m of t.matchAll(/(authorization|x-api-key|client-id|client-type|initData|encryptionUri)/gi)) hits.add(m[1]);
    for (const h of [...hits].slice(0, 25)) out(C.d("    → " + h.slice(0, 130)));
  }
  if (!jsUrls.size) out(C.d("  (player HTML hi nahi mila — /stream/ path HTML nahi deta)"));

  /* ---- 4. key URL candidates ---- */
  out(C.b("\n=== 4. KEY URL CANDIDATES ==="));
  const videoKey =
    (masterUrl.match(/stream\/([0-9a-f-]{36})/i) || [])[1] ||
    (info.lecture && info.lecture.videoKey) || "?";

  const cfHost = "https://d1d34p8vz63oiq.cloudfront.net";
  const urls = [
    keyUri,
    `${PLAYER_ORIGIN}/?url=${encodeURIComponent(keyUri || "")}`,
    `${PLAYER_ORIGIN}/proxy?url=${encodeURIComponent(keyUri || "")}`,
    `${PLAYER_ORIGIN}/stream/${videoKey}/enc.key`,
    `${PLAYER_ORIGIN}/stream/${videoKey}/hls/720/enc.key`,
    `${PLAYER_ORIGIN}/stream/${videoKey}/hls/enc.key`,
    `${cfHost}/${videoKey}/enc.key`,
    `${cfHost}/${videoKey}/hls/720/enc.key`,
    `https://api.penpencil.co/v1/videos/get-hls-key?videoKey=${videoKey}&key=enc.key&keyId=enc.key`,
  ].filter(Boolean);

  // header sets: sabse sambhavit pehle
  const authToken = info.jwt || info.accessToken || info.token || "";
  const setCookieAll = "";
  const headerSets = [
    ["app+player-referer", { ...appH, referer: PLAYER_ORIGIN + "/" }],
    [
      "pw-client-headers",
      {
        ...appH,
        "client-id": PW_ORG,
        "client-type": "WEB",
        "client-version": "5.0.0",
      },
    ],
    [
      "pw-client+bearer",
      {
        ...appH,
        "client-id": PW_ORG,
        "client-type": "WEB",
        ...(authToken ? { authorization: "Bearer " + authToken } : {}),
      },
    ],
    ["no-cookie", { referer: PLAYER_ORIGIN + "/" }],
    ["x-api-key", { ...appH, "x-api-key": PW_ORG, "client-id": PW_ORG }],
    ["bare", {}],
  ];

  let found = null;
  for (const u of urls) {
    if (!u || !/^https?:\/\//.test(u)) continue;
    for (const [label, h] of headerSets) {
      const r = await req(u, h);
      const wa = r.headers && r.headers.get ? r.headers.get("www-authenticate") || "" : "";
      const ct = r.headers && r.headers.get ? r.headers.get("content-type") || "" : "";
      const isKey = r.ok && r.status === 200 && (r.buf.length === 16 || r.buf.length === 32);
      const tag = isKey ? C.g("KEY!!") : r.ok ? (r.status === 200 ? C.y("200") : C.r(String(r.status))) : C.r("ERR");
      out(
        `  ${tag.padEnd(12)} ${String(r.buf.length).padStart(6)}b  ${label.padEnd(18)} ${C.d(u.slice(0, 72))}`
      );
      if (wa) out(C.d(`        WWW-Authenticate: ${wa.slice(0, 120)}`));
      if (r.ok && r.status !== 200) {
        const body = r.buf.toString("utf8").replace(/\s+/g, " ").slice(0, 90);
        if (body && !/^<(!doctype|html)/i.test(body)) out(C.d(`        body: ${body}`));
      }
      if (isKey) { found = { key: r.buf, url: u, label }; break; }
    }
    if (found) break;
  }

  /* ---- 5. result ---- */
  out(C.b("\n=== 5. RESULT ==="));
  if (found) {
    out(C.g("  ✓ KEY MILI"));
    out(`    URL   : ${found.url}`);
    out(`    headers: ${found.label}`);
    out(`    bytes : ${found.key.length}`);
    fs.mkdirSync(CACHE_DIR, { recursive: true });
    const kp = path.join(CACHE_DIR, "hlskey.bin");
    fs.writeFileSync(kp, found.key);
    out(C.b(`\n  Ab chalayein:`));
    out(`    node download.mjs "<link>" --fresh`);
    out(C.d(`    (key ${kp} me save ho gayi — script khud utha legi)`));
  } else {
    out(C.r("  ✗ koi key URL/header combo kaam nahi kiya"));
    out(C.d("  Upar ki lines screenshot karke bhej dein — WWW-Authenticate se pata"));
    out(C.d("  chalega kaunsa header chahiye (Bearer / Basic / cookie)."));
  }
  out("");
}

main().catch((e) => {
  out(C.r("probe fail: " + e.message));
  process.exit(1);
});
