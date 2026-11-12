"""
Lecture Downloader v2 - browser-session based, token safe.

Kyun ye rewrite zaroori tha
---------------------------
Purana downloader (v1) sirf 10 second wait karke *pehli* request pakadta tha
jisme ".m3u8" ya "manifest" string ho. StudyRatna jaisi SPA/Telegram Mini-App
me ye kaam nahi karta:

  * page ke HTML me video nahi hoti; player JS se, user ke Play ke baad load hoti hai
  * "manifest" match hone par `/manifest.json` (PWA file) pakdi ja sakti thi
  * stream CloudFront/session cookies aur Referer/Origin headers maangta hai,
    jo yt-dlp ko kabhi nahi diye gaye
  * ffmpeg installed hi nahi tha -> HLS merge fail
  * failure par script 0 return karti thi, isliye workflow "green" dikhta tha
    jabki koi video banti hi nahi thi

v2 kya karta hai
----------------
  1. Diya gaya `TARGET_URL` (aapke apne logged-in account se nikala *fresh*
     `?playerToken=...` link) real headless Chromium me kholta hai.
  2. Player ko user ki tarah play karata hai (video element + play buttons),
     har request/response ko log karta hai, aur browser dwara actually play
     ki ja rahi HLS/DASH/media URL ko uske headers + cookies ke saath pakadta hai.
  3. Browser context ke cookies Netscape cookie-file me likhta hai aur yt-dlp ko
     Referer/Origin/User-Agent + cookiefile ke saath chala kar MP4 banata hai.
  4. Token/cookie/signature log se REDACT karta hai; debug artifacts likhta hai;
     aur video na banne par non-zero exit code deta hai (workflow red).

Ye script kya NAHI karti hai: koi reverse-engineered encryption key, forged
CloudFront cookie, session replay, HLS decryption-key extraction ya geo-block
bypass nahi. Access ke liye hamesha aapka apna valid, authorized session chahiye.
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from playwright.async_api import async_playwright

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
WAIT_SECONDS = max(5, int(os.environ.get("WAIT_SECONDS", "45")))
OUTPUT_BASE = "downloaded_lecture"
COOKIE_FILE = Path(".cookies.txt")  # sensitive: gitignored, kabhi upload mat karo
DEBUG_DIR = Path("debug")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Playlist/manifest.json wale false positive se bachne ke liye exact extensions.
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


# ---------------------------------------------------------------------------
# Logging / redaction
# ---------------------------------------------------------------------------

def redact(text: object) -> str:
    """Token, cookie aur signature kabhi log file me plain na jayein."""
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
    print(f"[{time.monotonic() - START:6.1f}s] {redact(msg)}", flush=True)


def stream_score(url: str) -> int:
    """Sabse pehle master playlist, phir koi bhi HLS, phir DASH, phir direct file."""
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
# Browser capture
# ---------------------------------------------------------------------------

class Capture:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.candidates: list[str] = []          # scorer ke liye URLs
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
            # Browser ke apne headers (Referer/Origin/Cookie/UA) preserve karo.
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
        """yt-dlp ke liye sirf safe, useful headers."""
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
                if not out.get("referer"):
                    out["referer"] = f"{parsed.scheme}://{parsed.netloc}/"
                if not out.get("origin"):
                    out["origin"] = f"{parsed.scheme}://{parsed.netloc}"
        return out


def write_netscape_cookies(cookies: list[dict]) -> Path | None:
    """Playwright context cookies -> yt-dlp readable Netscape cookie file."""
    if not cookies:
        return None
    lines = [
        "# Netscape HTTP Cookie File\n",
        "# This file contains session credentials - do not commit or upload.\n",
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
    """Player tabhi stream mangta hai jab use play kiya jaye - user jaisa click."""
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
                    # Cookies abhi set ho rahe honge - thoda sa extra grace.
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
                await page.screenshot(path=str(DEBUG_DIR / "screenshot.png"), timeout=10000)
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


def download(url: str, headers: dict, cookie_path: Path | None) -> bool:
    if not shutil.which("ffmpeg"):
        log("[!] ffmpeg nahi mila - HLS ko MP4 me merge nahi ho payega.")
    log(f"[*] yt-dlp start: {url}")

    opts = {
        "outtmpl": f"{OUTPUT_BASE}.%(ext)s",
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 5,
        "http_headers": headers,
        "overwrites": True,
        "quiet": False,
        "no_warnings": False,
    }
    if cookie_path and cookie_path.exists():
        opts["cookiefile"] = str(cookie_path)
        log(f"[*] cookiefile use ho raha hai ({len(cookie_path.read_text().splitlines())} lines)")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log(f"[-] yt-dlp fail: {exc}")
        return False

    files = [
        Path(f) for f in glob.glob(f"{OUTPUT_BASE}.*")
        if not f.endswith((".part", ".ytdl", ".temp"))
    ]
    if not files:
        log("[-] yt-dlp chala par output file nahi bani.")
        return False
    for f in sorted(files, key=lambda x: x.stat().st_size, reverse=True):
        log(f"[+] File: {f} ({f.stat().st_size / (1024 * 1024):.1f} MB)")
    if max(f.stat().st_size for f in files) < 100_000:
        log("[-] Output file bahut chhoti hai - likely error page, video nahi.")
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async() -> int:
    DEBUG_DIR.mkdir(exist_ok=True)
    if not TARGET_URL:
        log("[-] TARGET_URL khali hai. Fresh playerToken wala URL do.")
        return 2
    if "playerToken=" not in TARGET_URL and "token=" not in TARGET_URL:
        log("[!] URL me playerToken nahi mila - player shayad authorized na ho.")
    if not shutil.which("ffmpeg"):
        log("[!] WARNING: ffmpeg missing. Workflow me `apt-get install ffmpeg` chahiye.")

    capture, extra = await browser_capture()
    write_debug(capture)

    text = capture.page_text.lower()
    if any(p in text for p in EXPIRED_PATTERNS):
        log("[-] Site ne 'session/token expired' ya 'unauthorized' dikhaya.")
        log("    => App me apne account se lecture khol kar NAYA playerToken link banao.")
        return 3

    url = capture.best()
    if not url:
        # Last resort: page text/HTML me koi media URL? (SPA kabhi inline deti hai)
        for source in (capture.page_html, capture.page_text):
            for match in URL_IN_TEXT_RE.finditer(redact(source)):
                candidate = match.group(0).rstrip("),;]}")
                if stream_score(candidate) > 0:
                    capture.candidates.append(candidate)
                    break
        url = capture.best()
    if not url:
        log("[-] Stream URL nahi mili. debug/network.log aur debug/page_text.txt dekho.")
        log("    Sabse aam karan: purana/expired token, ya player play hi nahi hua.")
        return 4

    log(f"[+] Selected stream: {url}")
    cookie_path = write_netscape_cookies(extra.get("cookies", []))
    headers = capture.headers_for(url)
    log(f"[*] Headers: {', '.join(sorted(headers))}")

    ok = download(url, headers, cookie_path)
    if cookie_path:
        try:
            cookie_path.unlink()  # session credentials disk par na chhodein
        except OSError:
            pass
    if not ok:
        log("[-] Download fail. 403/502 aaye to token fresh karo ya apne network se run karo.")
        return 5

    log("[+] Lecture download ho gayi.")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log(f"[-] Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
