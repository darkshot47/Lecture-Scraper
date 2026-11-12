"""
Lecture Downloader v4 - download + in-page playback recording.

Aapki baat: "site pe recording wagera hota hai". Iska seedha matlab ye hai ki
jab aap lecture dekhte hain, player hls.js se segments laakar **page ke andar hi
decrypt** kar deta hai aur `SourceBuffer.appendBuffer()` me deta hai. Wahi data
screen-recording jaisa capture kiya ja sakta hai - bas lossless, kyunki wo
compressed media chunks hain, pixels nahi.

v4 ke raste (upar se neeche, jo pehle chale wahi final):
  A. yt-dlp direct  - browser ke cookies + Referer/Origin ke saath HLS download.
  B. In-page record - player chalta hai, MSE chunks capture hote hain aur
     MediaRecorder se WebM banta hai. Segments / HLS key alag se nahi chahiye.
  C. Manifest refresh - app ka apna playback-manifest endpoint fresh signed
     URL + cookie deta hai (jab token ke andar wali cookie expire ho).
  D. Token diagnostics - report karta hai token kab bana aur uske andar ka
     CloudFront signature kab expire hua (403 ka asli karan yahi tha).

Honest limits: signature forge, key extraction, DRM/paywall/geo bypass nahi.
Agar aap app me lecture play nahi kar sakte, to yahan bhi nahi chalega.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64decode
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yt_dlp
from playwright.async_api import async_playwright

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
WAIT_SECONDS = max(5, int(os.environ.get("WAIT_SECONDS", "60")))
RECORD_SECONDS = max(15, int(os.environ.get("RECORD_SECONDS", "1200")))
ATTEMPTS = max(1, int(os.environ.get("ATTEMPTS", "2")))
RECORD_MODE = os.environ.get("RECORD_MODE", "1") not in ("0", "false", "no", "")
OUTPUT_BASE = "downloaded_lecture"
COOKIE_FILE = Path(".cookies.txt")
DEBUG_DIR = Path("debug")
REPORT_FILE = Path("run_report.md")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"
API_KEY_SEED = "939567f60cc4728ee81fc28fae7458af"
MANIFEST_API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"

HLS_RE = re.compile(r"\.m3u8(?:\?|$)", re.IGNORECASE)
DASH_RE = re.compile(r"\.mpd(?:\?|$)", re.IGNORECASE)
MEDIA_RE = re.compile(r"\.(?:mp4|webm|mkv|mov)(?:\?|$)", re.IGNORECASE)
URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)

EXPIRED_PATTERNS = (
    "session has expired", "please log in again", "log in again",
    "token expired", "invalid token", "unauthorized", "access denied",
    "not authorized",
)

PLAY_SELECTORS = (
    "button[aria-label*='play' i]",
    ".vjs-big-play-button",
    "[class*='play-button' i]",
    "[class*='playButton' i]",
    "video",
)

# Playback recorder: MSE chunks (decrypted, compressed) + MediaRecorder fallback.
RECORDER_JS = r"""
(() => {
  if (window.__rec) return 'already';
  const state = {chunks: [], mime: null, total: 0, started: 0,
                 rec: null, recChunks: [], recMime: null, mode: null};
  window.__rec = state;

  // 1) hls.js / MSE: appendBuffer me jo data aata hai wahi final media hai.
  const SB = window.SourceBuffer && window.SourceBuffer.prototype;
  if (SB && SB.appendBuffer) {
    const orig = SB.appendBuffer;
    SB.appendBuffer = function (buf) {
      try {
        const u8 = new Uint8Array(buf.buffer ? buf.buffer.slice(
          buf.byteOffset, buf.byteOffset + buf.byteLength) : buf);
        state.chunks.push(u8);
        state.total += u8.length;
        state.mode = 'mse';
      } catch (e) {}
      return orig.call(this, buf);
    };
  }
  const MS = window.MediaSource && window.MediaSource.prototype;
  if (MS && MS.addSourceBuffer) {
    const origAdd = MS.addSourceBuffer;
    MS.addSourceBuffer = function (m) {
      state.mime = state.mime || m;
      return origAdd.call(this, m);
    };
  }

  // 2) Fallback: video element ka live stream MediaRecorder se WebM me.
  try {
    const vids = document.querySelectorAll('video');
    for (const v of vids) {
      if (!v.captureStream) continue;
      const stream = v.captureStream();
      const types = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus',
                     'video/webm'];
      let picked = null;
      for (const t of types) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) { picked = t; break; }
      }
      if (!picked) break;
      const rec = new MediaRecorder(stream, {mimeType: picked, videoBitsPerSecond: 2500000});
      rec.ondataavailable = (e) => { if (e.data && e.data.size) state.recChunks.push(e.data); };
      rec.start(2000);
      state.rec = rec;
      state.recMime = picked;
      state.mode = state.mode ? 'mse+recorder' : 'recorder';
      break;
    }
  } catch (e) { window.__recError = String(e); }
  state.started = Date.now();
  return state.mode || 'none';
})()
"""

DUMP_JS = r"""
() => {
  const s = window.__rec || {};
  const out = {};
  const chunks = (s.chunks || []).splice(0, 1e9);
  let total = 0; for (const u of chunks) total += u.length;
  const merged = new Uint8Array(total);
  let off = 0;
  for (const u of chunks) { merged.set(u, off); off += u.length; }
  let bin = '';
  const CH = 8192;
  for (let i = 0; i < merged.length; i += CH) {
    bin += String.fromCharCode.apply(null, merged.subarray(i, i + CH));
  }
  out.mseB64 = bin ? btoa(bin) : '';
  out.mseBytes = total;
  out.mseMime = s.mime || '';
  return out;
}
"""

STATUS_JS = r"""
() => {
  const s = window.__rec || {};
  const v = document.querySelector('video');
  let cur = 0, dur = 0, buffered = 0, ended = false, err = null;
  if (v) {
    cur = v.currentTime || 0; dur = v.duration || 0; ended = !!v.ended;
    try { if (v.buffered.length) buffered = v.buffered.end(v.buffered.length - 1); } catch (e) {}
    if (v.error) err = v.error.code + ':' + (v.error.message || '');
  }
  return {mode: s.mode || 'none', mseBytes: s.total || 0, mseBuf: (s.chunks||[]).length,
          recBytes: (s.recChunks || []).reduce((a, b) => a + (b.size || 0), 0),
          recState: s.rec ? s.rec.state : null, current: cur, duration: dur,
          bufferedEnd: buffered, ended: ended, error: err,
          readyState: v ? v.readyState : -1,
          src: v ? String(v.currentSrc || v.src || '').slice(0, 160) : ''};
}
"""


# ---------------------------------------------------------------------------
# Logging / redaction / report
# ---------------------------------------------------------------------------

def redact(text: object) -> str:
    s = str(text)
    s = re.sub(r"(playerToken=)[^&\s\"'<>]+", r"\1<REDACTED>", s, flags=re.I)
    s = re.sub(r"(token=)[A-Za-z0-9%._~+\-/=]{24,}", r"\1<REDACTED>", s, flags=re.I)
    for name in ("cookie", "authorization", "x-init-data", "signature",
                 "cloudfront-signature", "cloudfront-key-pair-id", "cloudfront-policy"):
        s = re.sub(
            rf"(\b{re.escape(name)}\s*[:=]\s*)([^\r\n,;]+)",
            r"\1<REDACTED>", s, flags=re.I,
        )
    s = re.sub(r"\b[0-9a-f]{64,}(?::[0-9a-f]{64,})?\b", "<REDACTED>", s, flags=re.I)
    return s


START = time.monotonic()
NOTES: list[str] = []


def log(msg: object) -> None:
    print(f"[{time.monotonic() - START:6.1f}s] {redact(msg)}", flush=True)


def note(msg: object) -> None:
    text = redact(msg)
    NOTES.append(text)
    log(text)


def write_report(status: str, detail: str = "") -> None:
    lines = [
        "# Run report",
        "",
        f"- result: **{status}**",
        f"- time: {datetime.now(timezone.utc).isoformat()}",
        f"- url host: {urlparse(TARGET_URL).netloc if TARGET_URL else '(none)'}",
        f"- ffmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}",
        f"- record mode: {RECORD_MODE} (max {RECORD_SECONDS}s)",
        "",
        "## Findings",
        "",
    ]
    lines += [f"- {n}" for n in NOTES] or ["- (none)"]
    if detail:
        lines += ["", "## Detail", "", "```", redact(detail)[:6000], "```"]
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n===== REPORT START =====", flush=True)
    print(REPORT_FILE.read_text(encoding="utf-8"), flush=True)
    print("===== REPORT END =====", flush=True)


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def ts(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stream_score(url: str) -> int:
    u = url.lower()
    if HLS_RE.search(u):
        if "master" in u:
            return 100
        if "/hls/" in u or "main.m3u8" in u:
            return 95
        if "playlist" in u or "index" in u:
            return 90
        return 80
    if DASH_RE.search(u):
        return 60
    if MEDIA_RE.search(u):
        return 40
    return 0


# ---------------------------------------------------------------------------
# Token diagnostics (read-only)
# ---------------------------------------------------------------------------

def token_diagnostics() -> dict:
    info: dict = {}
    try:
        raw = (parse_qs(urlparse(TARGET_URL).query).get("playerToken") or [""])[0]
        if not raw:
            note("URL me playerToken param nahi mila.")
            return info
        token = unquote(raw)
        if ":" not in token:
            note("playerToken format unexpected (no ':' separator).")
            return info
        iv_hex, ct_hex = token.split(":", 1)
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad
        except ImportError:
            note("pycryptodome nahi hai - token diagnostics skip.")
            return info
        key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
        pt = unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(
            bytes.fromhex(ct_hex)), 16)
        data = json.loads(pt.decode("utf-8", "replace"))
        info = data

        lecture = data.get("lecture") or {}
        topic = (lecture.get("topic")
                 or (lecture.get("videoDetails") or {}).get("name") or "(unknown)")
        duration = (lecture.get("videoDetails") or {}).get("duration")
        note(f"Token decrypt OK | lecture: {topic} ({duration})")
        note(f"IDs: batch={data.get('batchId')} subject={data.get('subjectId')} "
             f"schedule={data.get('scheduleId')} type={data.get('type')}")

        init = unquote(data.get("initData") or "")
        ad = (parse_qs(init).get("auth_date") or [""])[0]
        if ad:
            note(f"Token/session banaya gaya: {ts(ad)} (age "
                 f"{round((now_ts() - int(ad)) / 60, 1)} min)")

        if data.get("videoUrl"):
            note(f"Token ke andar stream URL: {data['videoUrl']}")

        cookie = data.get("cookie") or ""
        pol = re.search(r"CloudFront-Policy=([^;]+)", cookie)
        if pol:
            v = pol.group(1).replace("-", "+").replace("_", "/")
            v += "=" * (-len(v) % 4)
            policy, _ = json.JSONDecoder().raw_decode(b64decode(v).decode("utf-8", "replace"))
            st = policy["Statement"][0]
            cond = st.get("Condition") or {}
            less = cond.get("DateLessThan", {}).get("AWS:EpochTime")
            greater = cond.get("DateGreaterThan", {}).get("AWS:EpochTime")
            note(f"CloudFront signed URL valid till: {ts(less)} (abhi {ts(now_ts())})")
            note(f"CloudFront resource: {st.get('Resource')}")
            if less and int(less) < now_ts():
                note(f"!! Token ke andar wala CloudFront signature EXPIRE hai "
                     f"({ts(less)}). Isliye 403/502 aata hai - token naya hone se "
                     f"farak nahi padta. Fix: manifest refresh (neeche try hota hai) "
                     f"ya app se fresh playback ke baad naya token.")
            if greater and int(greater) > now_ts():
                note("!! CloudFront policy ka DateGreaterThan future me hai "
                     "(signature abhi valid hi nahi ho sakta).")
        elif cookie:
            note("Cookie mila par CloudFront-Policy nahi - parse skip.")
        else:
            note("Token me cookie nahi hai.")
    except Exception as exc:
        note(f"Token diagnostics fail: {type(exc).__name__}: {exc}")
    return info


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, headers: dict, timeout: int = 30) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4000), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(1000)
        except Exception:
            pass
        return exc.code, body, dict(exc.headers or {})
    except Exception as exc:
        return 0, str(exc).encode(), {}


def probe(url: str, headers: dict, label: str) -> bool:
    code, body, _ = http_get(url, headers)
    snippet = redact(body[:160].decode("utf-8", "replace")).replace("\n", " ")
    note(f"probe[{label}] HTTP {code or 'ERR'} :: {snippet[:120]}")
    return code == 200


def decrypt_api_response(enc: str, init_data: str) -> dict | None:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        seed = API_KEY_SEED + (init_data or "")[:50]
        key = hashlib.sha256(seed.encode()).digest()
        iv_hex, ct_hex = enc.split(":", 1)
        pt = unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(
            bytes.fromhex(ct_hex)), 16)
        return json.loads(pt.decode("utf-8", "replace"))
    except Exception as exc:
        note(f"manifest decrypt fail: {type(exc).__name__}: {exc}")
        return None


def manifest_refresh(info: dict) -> tuple[str | None, str | None]:
    """App ka apna playback-manifest endpoint - fresh signed URL + cookie."""
    init_data = (info or {}).get("initData") or ""
    batch = (info or {}).get("batchId")
    subject = (info or {}).get("subjectId")
    schedule = (info or {}).get("scheduleId")
    if not (init_data and batch and subject and schedule):
        note("Manifest refresh skip (ids/initData adhoora).")
        return None, None
    url = f"{MANIFEST_API}/{batch}/{subject}/{schedule}"
    variants = {
        "x-init-data": {"x-init-data": init_data, "Accept": "application/json"},
        "authorization-tma": {"Authorization": f"tma {init_data}", "Accept": "application/json"},
        "both": {"x-init-data": init_data, "Authorization": f"tma {init_data}",
                 "Accept": "application/json"},
    }
    base = {"User-Agent": USER_AGENT, "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org"}
    for label, extra in variants.items():
        code, body, _ = http_get(url, {**base, **extra})
        text = body.decode("utf-8", "replace")
        note(f"manifest[{label}] HTTP {code or 'ERR'} :: {redact(text)[:200]}")
        if code != 200:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if payload.get("encrypted"):
            payload = decrypt_api_response(payload["encrypted"], init_data) or {}
        data = payload.get("data") or payload
        if data.get("videoUrl"):
            note(f"manifest se fresh URL mila: {data['videoUrl']}")
            return data["videoUrl"], data.get("cookie")
    return None, None


# ---------------------------------------------------------------------------
# Browser capture (+ optional playback recording)
# ---------------------------------------------------------------------------

class Capture:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.candidates: list[str] = []
        self.headers_by_url: dict[str, dict] = {}
        self.page_text = ""
        self.page_html = ""
        self.response_status: dict[str, int] = {}

    def on_request(self, request) -> None:
        try:
            url = request.url
        except Exception:
            return
        self.requests.append(url)
        if stream_score(url) > 0:
            if url not in self.candidates:
                self.candidates.append(url)
                log(f"[+] stream candidate: {url}")
            try:
                self.headers_by_url[url] = dict(request.headers)
            except Exception:
                self.headers_by_url[url] = {}

    def on_response(self, response) -> None:
        try:
            self.response_status[response.url] = response.status
            if stream_score(response.url) > 0 or response.status >= 400:
                log(f"[net] {response.status} {response.url}")
        except Exception:
            pass

    def best(self) -> str | None:
        if not self.candidates:
            return None
        return max(self.candidates, key=lambda u: (stream_score(u), -len(u)))

    def headers_for(self, url: str) -> dict:
        raw = self.headers_by_url.get(url, {})
        wanted = ("user-agent", "referer", "origin", "accept", "accept-language")
        out = {}
        for key, value in raw.items():
            if key.lower() in wanted and value:
                out[key.lower()] = value
        if not out.get("user-agent"):
            out["user-agent"] = USER_AGENT
        if TARGET_URL:
            parsed = urlparse(TARGET_URL)
            if parsed.netloc:
                out.setdefault("referer", f"{parsed.scheme}://{parsed.netloc}/")
                out.setdefault("origin", f"{parsed.scheme}://{parsed.netloc}")
        return out


def write_netscape_cookies(cookies: list[dict]) -> Path | None:
    if not cookies:
        return None
    lines = ["# Netscape HTTP Cookie File\n",
             "# Session credentials - do not commit or upload.\n"]
    for c in cookies:
        domain = str(c.get("domain", "")).strip()
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path", "/")) or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = int(c.get("expires") or 0)
        if expiry < 0:
            expiry = 0
        name = str(c.get("name", "")).replace("\t", " ")
        value = str(c.get("value", "")).replace("\t", " ")
        lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
    COOKIE_FILE.write_text("".join(lines), encoding="utf-8")
    try:
        COOKIE_FILE.chmod(0o600)
    except OSError:
        pass
    return COOKIE_FILE


async def nudge_player(page) -> None:
    for frame in page.frames:
        try:
            await frame.evaluate(
                """() => {
                    document.querySelectorAll('video').forEach(v => {
                        v.muted = true;
                        const p = v.play();
                        if (p && typeof p.catch === 'function') p.catch(() => {});
                    });
                }"""
            )
        except Exception:
            pass
        for selector in PLAY_SELECTORS:
            try:
                loc = frame.locator(selector).first
                if await loc.count() > 0:
                    await loc.click(timeout=1000, force=True)
                    break
            except Exception:
                continue


def dump_mse_blob(page_blob: str, path: Path) -> int:
    """Page se aaya base64 MSE data -> raw fMP4/TS file."""
    from base64 import b64decode as _b64
    data = _b64(page_blob)
    path.write_bytes(data)
    return len(data)


def remux_to_mp4(src: Path) -> Path | None:
    """fMP4/TS bytes ko playable MP4 me badlo (ffmpeg)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        note("ffmpeg nahi mila - capture remux nahi ho payega.")
        return None
    out = Path("downloaded_lecture.mp4")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
           "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as exc:
        note(f"ffmpeg remux fail: {exc}")
        return None
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
        note(f"ffmpeg remux output nahi bana :: {redact(proc.stderr)[-300:]}")
        try:
            out.unlink()
        except OSError:
            pass
        return None
    note(f"remux OK: {out} ({out.stat().st_size / (1024 * 1024):.1f} MB)")
    return out


LAUNCH_ARGS = ["--autoplay-policy=no-user-gesture-required", "--no-sandbox",
               "--use-fake-ui-for-media-stream", "--disable-dev-shm-usage"]


async def launch_browser(p):
    """Pehle real Chrome (H.264/AAC codecs ke saath), phir bundled Chromium.

    Bundled Chromium me proprietary codecs (H.264/AAC) nahi hote, isliye
    avc1 wali HLS stream wahan MSE me append hi nahi hoti - aur capture khali
    reh jaata hai. Isliye channel priority zaroori hai.
    """
    last_exc = None
    for channel in ("chrome", "chrome-beta", None):
        try:
            kwargs = {"headless": True, "args": LAUNCH_ARGS}
            if channel:
                kwargs["channel"] = channel
            browser = await p.chromium.launch(**kwargs)
            note(f"browser launch OK: {channel or 'bundled chromium'}")
            return browser
        except Exception as exc:
            last_exc = exc
            log(f"[!] launch {channel or 'bundled chromium'} fail: {type(exc).__name__}")
    raise RuntimeError(f"koi browser launch nahi hua: {last_exc}")


CODEC_JS = r"""(list) => list.map(m => [m, !!(window.MediaSource && MediaSource.isTypeSupported(m))])"""

CODECS = [
    'video/mp4; codecs="avc1.42c01e"',
    'video/mp4; codecs="avc1.64001e"',
    'video/mp4; codecs="avc1.42E01E, mp4a.40.2"',
    'video/webm; codecs="vp9"',
    'video/webm; codecs="vp8"',
]


async def browser_capture(record: bool) -> tuple[Capture, dict]:
    capture = Capture()
    result: dict = {"cookies": [], "recorded_bytes": 0, "record_meta": {}}
    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await context.new_page()
        page.on("request", capture.on_request)
        page.on("response", capture.on_response)
        try:
            codecs = await page.evaluate(CODEC_JS, CODECS)
            result["codecs"] = codecs
            h264 = any(ok for m, ok in codecs if "avc1" in m)
            note("codec support: " + ", ".join(
                f"{m.split('codecs=')[-1][:22]}={'yes' if ok else 'NO'}" for m, ok in codecs))
            if not h264:
                note("!! is browser me H.264 (avc1) nahi hai - lecture ka HLS "
                     "(H.264/TS) MSE me append nahi hoga, capture khali rahega. "
                     "Isliye Chrome channel install karna zaroori hai.")
        except Exception:
            pass
        if record:
            try:
                await page.add_init_script(script=RECORDER_JS)
                note("in-page recorder inject ho gaya (MSE + MediaRecorder).")
            except Exception as exc:
                note(f"recorder inject fail: {exc}")
        try:
            log(f"[*] Opening lecture page (token redacted): {TARGET_URL.split('?')[0]}")
            try:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                log(f"[!] page.goto: {exc}")

            deadline = time.monotonic() + WAIT_SECONDS
            last_nudge = 0.0
            while time.monotonic() < deadline:
                if time.monotonic() - last_nudge >= 3:
                    last_nudge = time.monotonic()
                    await nudge_player(page)
                if capture.best():
                    await asyncio.sleep(2)
                    break
                await asyncio.sleep(1)
            else:
                log(f"[-] {WAIT_SECONDS}s me koi stream request nahi mili.")

            if record:
                # Recorder ko playback dekhne do; jitna buffer banega utna capture.
                rec_deadline = time.monotonic() + RECORD_SECONDS
                last_print = 0.0
                last_bytes = 0
                last_growth = time.monotonic()
                while time.monotonic() < rec_deadline:
                    try:
                        status = await page.evaluate(STATUS_JS)
                    except Exception as exc:
                        note(f"recorder status fail: {exc}")
                        break
                    result["record_meta"] = status
                    if time.monotonic() - last_print >= 15:
                        last_print = time.monotonic()
                        log(f"[rec] mode={status.get('mode')} "
                            f"mse={(status.get('mseBytes') or 0) / 1e6:.1f}MB "
                            f"rec={(status.get('recBytes') or 0) / 1e6:.1f}MB "
                            f"t={status.get('current'):.1f}/{status.get('duration'):.1f} "
                            f"buf={status.get('bufferedEnd'):.1f} "
                            f"err={status.get('error')}")
                    grown = ((status.get("mseBytes") or 0)
                             + (status.get("recBytes") or 0))
                    if grown > last_bytes:
                        last_bytes = grown
                        last_growth = time.monotonic()
                    dur = status.get("duration") or 0
                    buf = status.get("bufferedEnd") or 0
                    if status.get("ended"):
                        note("playback end tak pahunch gaya (capture complete).")
                        break
                    if dur and float("inf") > dur > 0 and buf >= dur - 1.0:
                        note(f"poora lecture buffer ho gaya ({buf:.1f}s) - capture rok rahe hain.")
                        await asyncio.sleep(3)
                        break
                    if grown > 0 and time.monotonic() - last_growth > 90:
                        note("90s tak naya media data nahi aaya (stream stall) - "
                             "jo capture hua wahi use kar rahe hain.")
                        break
                    if (status.get("mseBytes") or 0) + (status.get("recBytes") or 0) == 0 \
                            and time.monotonic() - last_nudge > 15 and not capture.best():
                        await nudge_player(page)
                        last_nudge = time.monotonic()
                    await asyncio.sleep(3)

                try:
                    dumped = await page.evaluate(DUMP_JS)
                    if dumped.get("mseB64"):
                        raw_path = DEBUG_DIR / "capture_raw.mp4"
                        size = dump_mse_blob(dumped["mseB64"], raw_path)
                        result["recorded_bytes"] = size
                        result["record_meta"]["mseDumpBytes"] = size
                        result["record_meta"]["mseMime"] = dumped.get("mseMime")
                        note(f"MSE capture dump: {size / 1e6:.1f} MB "
                             f"(mime={dumped.get('mseMime') or 'unknown'})")
                except Exception as exc:
                    note(f"MSE dump fail: {exc}")

            try:
                result["cookies"] = await context.cookies()
            except Exception as exc:
                log(f"[!] cookies read fail: {exc}")
            try:
                capture.page_text = await page.inner_text("body", timeout=5000)
            except Exception:
                pass
            try:
                capture.page_html = await page.content()
            except Exception:
                pass
            try:
                await page.screenshot(path=str(DEBUG_DIR / "screenshot.png"), timeout=15000)
            except Exception:
                pass
        finally:
            await context.close()
            await browser.close()
    return capture, result


# ---------------------------------------------------------------------------
# Debug artifacts + download
# ---------------------------------------------------------------------------

def write_debug(capture: Capture) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "network.log").write_text(
        "\n".join(redact(u) for u in capture.requests) + "\n", encoding="utf-8")
    (DEBUG_DIR / "responses.log").write_text(
        "\n".join(f"{s} {redact(u)}" for u, s in capture.response_status.items()) + "\n",
        encoding="utf-8")
    (DEBUG_DIR / "page.html").write_text(redact(capture.page_html), encoding="utf-8")
    (DEBUG_DIR / "page_text.txt").write_text(redact(capture.page_text), encoding="utf-8")
    (DEBUG_DIR / "candidates.txt").write_text(
        "\n".join(f"{stream_score(u)} {redact(u)}" for u in capture.candidates) + "\n",
        encoding="utf-8")


def download(url: str, headers: dict, cookie_path: Path | None, label: str) -> bool:
    if not shutil.which("ffmpeg"):
        note("ffmpeg nahi mila - HLS merge fail ho sakta hai.")
    note(f"yt-dlp try [{label}]: {url}")
    opts = {
        "outtmpl": f"{OUTPUT_BASE}.%(ext)s",
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 5,
        "http_headers": headers,
        "overwrites": True,
        "quiet": False,
        "no_warnings": False,
    }
    if cookie_path and cookie_path.exists():
        opts["cookiefile"] = str(cookie_path)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        note(f"yt-dlp [{label}] fail: {type(exc).__name__}: {exc}")
        return False
    files = [Path(f) for f in glob.glob(f"{OUTPUT_BASE}.*")
             if not f.endswith((".part", ".ytdl", ".temp"))]
    if not files:
        note(f"yt-dlp [{label}] chala par output file nahi bani.")
        return False
    biggest = max(files, key=lambda x: x.stat().st_size)
    note(f"output: {biggest} ({biggest.stat().st_size / (1024 * 1024):.1f} MB)")
    if biggest.stat().st_size < 200_000:
        note("output bahut chhoti hai - error page lagti hai, video nahi.")
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> int:
    DEBUG_DIR.mkdir(exist_ok=True)
    if not TARGET_URL:
        note("TARGET_URL khali hai. workflow_dispatch/repository_dispatch me bhejo.")
        write_report("NO-INPUT")
        return 2
    if "playerToken=" not in TARGET_URL:
        note("URL me playerToken nahi - player authorized na ho.")

    info = token_diagnostics()

    # Manifest refresh pehle: agar token ki cookie expire hai to yahi bachaata hai.
    fresh_url, fresh_cookie = manifest_refresh(info)
    refreshed: list[tuple[str, dict, str]] = []
    if fresh_url:
        hdr = {"User-Agent": USER_AGENT, "Referer": "https://app.studyratna.org/",
               "Origin": "https://app.studyratna.org"}
        if fresh_cookie:
            hdr["Cookie"] = fresh_cookie
        refreshed.append((fresh_url, hdr, "manifest"))

    for attempt in range(1, ATTEMPTS + 1):
        note(f"--- attempt {attempt}/{ATTEMPTS} ---")
        try:
            capture, extra = await browser_capture(record=RECORD_MODE)
        except Exception as exc:
            note(f"browser capture fail: {type(exc).__name__}: {exc}")
            await asyncio.sleep(5)
            continue

        write_debug(capture)
        if any(p in (capture.page_text or "").lower() for p in EXPIRED_PATTERNS):
            note("Page ne 'session/token expired' ya 'unauthorized' dikhaya.")
        if capture.candidates:
            note("browser ne stream request(s) pakdi: "
                 + ", ".join(redact(c)[:120] for c in capture.candidates[:5]))
        else:
            note("browser me koi .m3u8/.mpd/media request nahi aayi.")
        for url, status in capture.response_status.items():
            if stream_score(url) > 0:
                note(f"stream response {status} :: {redact(url)[:140]}")
        if capture.requests:
            key_reqs = [u for u in capture.requests
                        if "/keys/hls/" in u or "get-hls-key" in u]
            if key_reqs:
                note(f"app ka HLS key request bhi gaya: {redact(key_reqs[0])[:140]}")

        rec = extra.get("record_meta") or {}
        note(f"recorder: mode={rec.get('mode')} mse={(rec.get('mseBytes') or 0)/1e6:.1f}MB "
             f"mediarec={(rec.get('recBytes') or 0)/1e6:.1f}MB "
             f"duration={rec.get('duration')} buffered={rec.get('bufferedEnd')} "
             f"error={rec.get('error')}")

        cookie_path = write_netscape_cookies(extra.get("cookies") or [])
        note(f"browser cookies: {len(extra.get('cookies') or [])} -> cookiefile: "
             f"{'YES' if cookie_path else 'NO'}")

        # A. yt-dlp (browser headers/cookies ke saath)
        streams: list[tuple[str, dict, str]] = []
        best = capture.best()
        if best:
            streams.append((best, capture.headers_for(best), "browser"))
        streams += refreshed
        token_url = (info or {}).get("videoUrl")
        token_cookie = (info or {}).get("cookie")
        if token_url:
            hdr = {"User-Agent": USER_AGENT, "Referer": "https://app.studyratna.org/",
                   "Origin": "https://app.studyratna.org"}
            if token_cookie:
                hdr["Cookie"] = token_cookie
            if all(u != token_url for u, _, _ in streams):
                streams.append((token_url, hdr, "token"))

        for url, hdr, label in streams:
            probe(url, hdr, label)

        for url, hdr, label in streams:
            if download(url, hdr, cookie_path, label):
                if cookie_path:
                    try:
                        cookie_path.unlink()
                    except OSError:
                        pass
                write_report("SUCCESS", f"downloaded from {label}: {url}")
                return 0

        # B. In-page playback recording (screen-recording ka lossless version)
        raw = DEBUG_DIR / "capture_raw.mp4"
        if raw.exists() and raw.stat().st_size > 200_000:
            out = remux_to_mp4(raw)
            if out:
                if cookie_path:
                    try:
                        cookie_path.unlink()
                    except OSError:
                        pass
                write_report("SUCCESS", "in-page playback capture (MSE) -> MP4")
                return 0
            # remux fail: raw bytes hi rakho, par honest fail
            note(f"raw capture rakha hai: {raw} ({raw.stat().st_size/1e6:.1f} MB)")
        elif rec.get("recBytes"):
            note("MediaRecorder data mila par final WebM assemble nahi hua "
                 "(is build me sirf MSE path dump hota hai).")
        else:
            note("in-page recording me koi media data nahi aaya - matlab player ne "
                 "segments hi load nahi kiye (403/session/geo).")

        if cookie_path:
            try:
                cookie_path.unlink()
            except OSError:
                pass
        if attempt < ATTEMPTS:
            note("5s baad dobara try...")
            await asyncio.sleep(5)

    note("Saare raste fail. Sabse aam karan: embedded CloudFront signature expire, "
         "PW session expired, ya India-only datacenter block. App me lecture play "
         "ho raha hai to India ke normal network se same script chalao.")
    write_report("FAIL", "download aur in-page capture dono se video nahi bani")
    return 5


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        note(f"Unexpected error: {type(exc).__name__}: {exc}")
        try:
            write_report("ERROR")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
