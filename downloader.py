"""
Lecture Downloader v3 - browser-session based, honest diagnostics.

v1 problem (logs se): 10s blind wait, pehli `.m3u8`/`manifest` request pakadna,
cookies/headers yt-dlp ko nahi dena, ffmpeg missing, aur fail hone par bhi exit 0.

v3 flow
-------
1. `TARGET_URL` me diya `?playerToken=...` decrypt karke **sirf diagnostics**
   nikaalte hain: lecture ka naam, ids, aur token ke andar wale CloudFront
   cookie ki expiry. (Koi key forge nahi - bas report.)
2. Real headless Chromium me lecture khulta hai, player ko play karaya jata hai
   (autoplay + play buttons), har request/response log hota hai.
3. Sabse accha stream URL (master.m3u8 > variant > mpd > mp4) chunte hain,
   browser ke cookies (Netscape file) + Referer/Origin/UA ke saath yt-dlp chalate hain.
4. Backup rasta: app ka apna `playback-manifest` refresh endpoint (jaisa site karti
   hai) se fresh signed URL + cookie lene ki koshish.
5. Report (`run_report.md`) likhte hain, token/cookie redact karte hain, aur video
   na bane to non-zero exit dete hain (workflow red - jhoothi success nahi).

Ye script kya NAHI karti: reverse-engineered AES/HLS key forging, CloudFront
signature forge, DRM/paywall ya geo-block bypass. Access ke liye aapka apna valid
authorized session chahiye.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import shutil
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
ATTEMPTS = max(1, int(os.environ.get("ATTEMPTS", "2")))
OUTPUT_BASE = "downloaded_lecture"
COOKIE_FILE = Path(".cookies.txt")
DEBUG_DIR = Path("debug")
REPORT_FILE = Path("run_report.md")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Sirf diagnostics ke liye: site ka public web-token seed (decryptForWeb ka port).
WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"
API_KEY_SEED = "939567f60cc4728ee81fc28fae7458af"
MANIFEST_API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"

HLS_RE = re.compile(r"\.m3u8(?:\?|$)", re.IGNORECASE)
DASH_RE = re.compile(r"\.mpd(?:\?|$)", re.IGNORECASE)
MEDIA_RE = re.compile(r"\.(?:mp4|webm|mkv|mov)(?:\?|$)", re.IGNORECASE)
URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)

EXPIRED_PATTERNS = (
    "session has expired",
    "please log in again",
    "log in again",
    "token expired",
    "invalid token",
    "unauthorized",
    "access denied",
    "not authorized",
)

PLAY_SELECTORS = (
    "button[aria-label*='play' i]",
    ".vjs-big-play-button",
    "[class*='play-button' i]",
    "[class*='playButton' i]",
    "video",
)

START = time.monotonic()
NOTES: list[str] = []


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


def log(msg: object) -> None:
    line = redact(msg)
    print(f"[{time.monotonic() - START:6.1f}s] {line}", flush=True)


def note(msg: object) -> None:
    """Report me ek bullet add karo (logs ke liye bhi)."""
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
        f"- python: {sys.version.split()[0]}",
        f"- ffmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}",
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stream_score(url: str) -> int:
    u = url.lower()
    if HLS_RE.search(u):
        if "master" in u:
            return 100
        if "playlist" in u or "index" in u:
            return 90
        return 80
    if DASH_RE.search(u):
        return 60
    if MEDIA_RE.search(u):
        return 40
    return 0


# ---------------------------------------------------------------------------
# Token diagnostics (read-only; koi key forge nahi)
# ---------------------------------------------------------------------------

def token_diagnostics() -> dict:
    """playerToken decrypt karke sirf report karo ki andar kya hai / kya expire hua."""
    info: dict = {}
    try:
        qs = parse_qs(urlparse(TARGET_URL).query)
        raw = (qs.get("playerToken") or [""])[0]
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
        note(f"Token decrypt OK | lecture: {topic}")
        note(f"IDs: batch={data.get('batchId')} subject={data.get('subjectId')} "
             f"schedule={data.get('scheduleId')} type={data.get('type')}")
        if data.get("videoUrl"):
            note(f"Token ke andar stream URL: {data['videoUrl']}")
        cookie = data.get("cookie") or ""
        pol = re.search(r"CloudFront-Policy=([^;]+)", cookie)
        if pol:
            try:
                v = pol.group(1).replace("-", "+").replace("_", "/")
                v += "=" * (-len(v) % 4)
                raw_policy = b64decode(v).decode("utf-8", "replace")
                policy, _ = json.JSONDecoder().raw_decode(raw_policy)
                cond = policy["Statement"][0]["Condition"]
                less = cond.get("DateLessThan", {}).get("AWS:EpochTime")
                greater = cond.get("DateGreaterThan", {}).get("AWS:EpochTime")
                res = policy["Statement"][0].get("Resource")
                note(f"CloudFront signed URL ki limit: {ts(less)} (abhi {ts(now_utc().timestamp())})")
                note(f"CloudFront resource: {res}")
                if less and int(less) < now_utc().timestamp():
                    note(f"!! Token ke andar wala CloudFront signature ALREADY EXPIRE hai "
                         f"({ts(less)}). Isliye 403 aa raha hai: token naya hai par "
                         f"usme embedded signed-URL purana hai.")
                if greater and int(greater) > now_utc().timestamp():
                    note("!! CloudFront policy ka DateGreaterThan future me hai "
                         "(signature abhi valid nahi ho sakta).")
            except Exception as exc:
                note(f"CloudFront policy parse fail: {exc}")
        elif cookie:
            note("Cookie mila par CloudFront-Policy nahi - parse skip.")
        else:
            note("Token me cookie nahi hai.")
    except Exception as exc:
        note(f"Token diagnostics fail: {type(exc).__name__}: {exc}")
    return info


# ---------------------------------------------------------------------------
# HTTP helpers (plain urllib; browser nahi chahiye)
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
    """App ke encrypted API response ko wahi tarah decrypt karo jise web client karta hai."""
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
    """Site ka apna playback-manifest endpoint - fresh signed URL + cookie."""
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
    base = {
        "User-Agent": USER_AGENT,
        "Referer": "https://app.studyratna.org/",
        "Origin": "https://app.studyratna.org",
    }
    for label, extra in variants.items():
        headers = {**base, **extra}
        code, body, _ = http_get(url, headers)
        text = body.decode("utf-8", "replace")
        note(f"manifest[{label}] HTTP {code or 'ERR'} :: {redact(text)[:200]}")
        if code != 200:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if payload.get("encrypted"):
            decrypted = decrypt_api_response(payload["encrypted"], init_data) or {}
            if not decrypted:
                continue
            note("manifest response encrypted - app ke key se decrypt ho gaya.")
            payload = decrypted
        data = payload.get("data") or payload
        video_url = data.get("videoUrl")
        if video_url:
            note(f"manifest se fresh URL mila: {video_url}")
            return video_url, data.get("cookie")
    return None, None


# ---------------------------------------------------------------------------
# Browser capture
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
    lines = [
        "# Netscape HTTP Cookie File\n",
        "# Session credentials - do not commit or upload.\n",
    ]
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
        lines.append(
            f"{domain}\t{include_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n"
        )
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


async def browser_capture() -> tuple[Capture, dict]:
    capture = Capture()
    cookies: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--no-sandbox"],
        )
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

            try:
                cookies = await context.cookies()
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
                await asyncio.sleep(0.3)
                await page.screenshot(path=str(DEBUG_DIR / "screenshot.png"), timeout=15000)
            except Exception:
                pass
        finally:
            await context.close()
            await browser.close()
    return capture, {"cookies": cookies}


# ---------------------------------------------------------------------------
# Debug artifacts + download
# ---------------------------------------------------------------------------

def write_debug(capture: Capture) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "network.log").write_text(
        "\n".join(redact(u) for u in capture.requests) + "\n", encoding="utf-8"
    )
    (DEBUG_DIR / "responses.log").write_text(
        "\n".join(f"{s} {redact(u)}" for u, s in capture.response_status.items()) + "\n",
        encoding="utf-8",
    )
    (DEBUG_DIR / "page.html").write_text(redact(capture.page_html), encoding="utf-8")
    (DEBUG_DIR / "page_text.txt").write_text(redact(capture.page_text), encoding="utf-8")
    (DEBUG_DIR / "candidates.txt").write_text(
        "\n".join(f"{stream_score(u)} {redact(u)}" for u in capture.candidates) + "\n",
        encoding="utf-8",
    )


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

    files = [
        Path(f) for f in glob.glob(f"{OUTPUT_BASE}.*")
        if not f.endswith((".part", ".ytdl", ".temp"))
    ]
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
        note("TARGET_URL khali hai. repository_dispatch/workflow_dispatch me bhejo.")
        write_report("NO-INPUT")
        return 2
    if "playerToken=" not in TARGET_URL:
        note("URL me playerToken nahi - player authorized na ho.")

    info = token_diagnostics()

    last_detail = ""
    for attempt in range(1, ATTEMPTS + 1):
        note(f"--- attempt {attempt}/{ATTEMPTS} ---")
        try:
            capture, extra = await browser_capture()
        except Exception as exc:
            note(f"browser capture fail: {type(exc).__name__}: {exc}")
            time.sleep(5)
            continue

        write_debug(capture)
        text = (capture.page_text or "").lower()
        if any(p in text for p in EXPIRED_PATTERNS):
            note("Page ne 'session/token expired' ya 'unauthorized' dikhaya.")
        if capture.candidates:
            note("browser ne stream request(s) pakdi: " + ", ".join(
                redact(c)[:120] for c in capture.candidates[:5]))
        else:
            note("browser me koi .m3u8/.mpd/media request nahi aayi.")
        for url, status in list(capture.response_status.items()):
            if stream_score(url) > 0:
                note(f"stream response {status} :: {redact(url)[:140]}")

        cookie_path = write_netscape_cookies(extra.get("cookies", []))
        note(f"browser cookies: {len(extra.get('cookies', []))}  -> cookiefile: "
             f"{'YES' if cookie_path else 'NO'}")

        streams: list[tuple[str, dict, str]] = []
        best = capture.best()
        if best:
            streams.append((best, capture.headers_for(best), "browser"))

        # Token ke andar ka URL + manifest refresh (app jaisa hi) - backup raste
        token_url = (info or {}).get("videoUrl")
        token_cookie = (info or {}).get("cookie")
        if token_url:
            hdr = {"User-Agent": USER_AGENT,
                   "Referer": "https://app.studyratna.org/",
                   "Origin": "https://app.studyratna.org"}
            if token_cookie:
                hdr["Cookie"] = token_cookie
            if not best or token_url != best:
                streams.append((token_url, hdr, "token"))

        for url, hdr, label in list(streams):
            ok = await asyncio.to_thread(probe, url, hdr, label)
            if not ok:
                note(f"stream [{label}] HTTP fail - koi aur rasta try kar rahe hain.")

        fresh_url, fresh_cookie = manifest_refresh(info)
        if fresh_url:
            hdr = {"User-Agent": USER_AGENT,
                   "Referer": "https://app.studyratna.org/",
                   "Origin": "https://app.studyratna.org"}
            if fresh_cookie:
                hdr["Cookie"] = fresh_cookie
            streams.insert(0, (fresh_url, hdr, "manifest"))

        for url, hdr, label in streams:
            if download(url, hdr, cookie_path, label):
                if cookie_path:
                    try:
                        cookie_path.unlink()
                    except OSError:
                        pass
                write_report("SUCCESS", f"downloaded from {label}: {url}")
                return 0

        last_detail = f"attempt {attempt}: koi bhi stream download nahi hui"
        if cookie_path:
            try:
                cookie_path.unlink()
            except OSError:
                pass
        if attempt < ATTEMPTS:
            note("5s baad dobara try (token/cookie refresh ho sakta hai)...")
            await asyncio.sleep(5)

    note("Saare raste fail. Sabse aam karan: embedded CloudFront signature expire, "
         "PW session expired, ya datacenter IP block (India-only content).")
    write_report("FAIL", last_detail)
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
