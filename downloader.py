"""
Lecture Downloader — v3 (StudyRatna playerToken support)
=========================================================
StudyRatna (app.studyratna.org) ek SPA hai — video URL page HTML me nahi hota,
frontend JS playerToken se backend API call karke video details leta hai.
Isliye v3 me:

  - Har API/JSON response log hota hai (api_responses.log) — video URL
    aksar wahi se milta hai
  - Page HTML ke andar chhupe hue stream/embed URLs regex se nikale jate hain
  - Page ka visible text hamesha save hota hai (page_text.txt) — token
    expired/invalid ya login wall ka error wahi dikhega
  - Console errors + failed requests bhi log hote hain
  - Logs me playerToken REDACTED kar diya jata hai (token leak na ho)

Agar token expired ho ya IP-bound ho to page par error dikhega —
page_text.txt / screenshot.png dekh kar pata chalega.
"""

import asyncio
import os
import re
import shutil
import sys

import yt_dlp
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "60"))
DEBUG_FILES = ["screenshot.png", "page.html", "network.log",
               "api_responses.log", "page_text.txt"]

HLS_HINTS = (".m3u8", "manifest", ".mpd")
MEDIA_HINTS = (".mp4", ".webm", ".mkv", ".mov", "videoplayback", "googlevideo")
EMBED_HINTS = ("youtube.com/embed", "youtu.be", "player.vimeo.com", "vimeo.com/")
API_HINTS = ("api", "studyratna", "lecture", "video", "player", "token",
             "stream", "course", "chapter", "lesson")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

network_log_lines = []
api_log_lines = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def redact(text: str) -> str:
    """Logs me playerToken / lambe hex tokens chhupao (leak na ho)."""
    text = re.sub(r"(playerToken=)[A-Za-z0-9%._\-]+", r"\1REDACTED", text)
    text = re.sub(r"\b[0-9a-f]{64,}:[0-9a-f]{64,}\b", "TOKEN_REDACTED", text)
    return text


def log_network(line: str):
    network_log_lines.append(redact(line))
    print(redact(line), flush=True)


def log_api(line: str):
    api_log_lines.append(redact(line))
    print(redact(line), flush=True)


def is_hls(url: str) -> bool:
    return any(h in url.lower() for h in HLS_HINTS)


def is_media(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in MEDIA_HINTS) or is_hls(u)


def is_embed(url: str) -> bool:
    return any(h in url.lower() for h in EMBED_HINTS)


def is_api(url: str) -> bool:
    return any(h in url.lower() for h in API_HINTS)


URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


def find_stream_urls(text: str):
    """Kisi text/JSON/HTML ke andar chhupe stream/embed URLs nikalo."""
    found = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip("),;]}")
        if is_media(u) or is_embed(u):
            if u not in found:
                found.append(u)
    return found


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def extract_dom_sources(page):
    try:
        return await page.evaluate(
            """() => {
                const out = { videos: [], sources: [], iframes: [] };
                document.querySelectorAll('video').forEach(v => {
                    if (v.currentSrc) out.videos.push(v.currentSrc);
                    if (v.src) out.videos.push(v.src);
                });
                document.querySelectorAll('source').forEach(s => {
                    if (s.src) out.sources.push(s.src);
                });
                document.querySelectorAll('iframe').forEach(f => {
                    if (f.src) out.iframes.push(f.src);
                });
                return out;
            }"""
        )
    except Exception:
        return {"videos": [], "sources": [], "iframes": []}


async def try_click_play(page):
    try:
        await page.click("video", timeout=3000)
        print("[*] Clicked <video> element", flush=True)
        return True
    except Exception:
        pass
    selectors = [
        "button:has-text('Play')", "button:has-text('play')",
        "[aria-label='Play']", "[aria-label='play']",
        ".play-button", ".play-btn", ".vjs-big-play-button",
        ".ytp-play-button", ".jw-big-play-button",
        "button.play", ".play",
    ]
    for sel in selectors:
        try:
            await page.click(sel, timeout=2000)
            print(f"[*] Clicked play button: {sel}", flush=True)
            return True
        except Exception:
            continue
    try:
        await page.keyboard.press("Space")
        print("[*] Pressed Space key (play try)", flush=True)
    except Exception:
        pass
    return False


async def save_debug(page, reason: str):
    print(f"\n[-] Debug dump ({reason}):", flush=True)
    try:
        await page.screenshot(path="screenshot.png", full_page=True)
        print("    -> screenshot.png saved", flush=True)
    except Exception as e:
        print(f"    -> screenshot fail: {e}", flush=True)
    try:
        html = await page.content()
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(redact(html))
        print(f"    -> page.html saved ({len(html)} chars)", flush=True)
    except Exception as e:
        print(f"    -> page.html fail: {e}", flush=True)
    try:
        text = await page.inner_text("body")
        with open("page_text.txt", "w", encoding="utf-8") as f:
            f.write(redact(text))
        print(f"    -> page_text.txt saved ({len(text)} chars)", flush=True)
    except Exception as e:
        print(f"    -> page_text fail: {e}", flush=True)
    with open("network.log", "w", encoding="utf-8") as f:
        f.write("\n".join(network_log_lines))
    with open("api_responses.log", "w", encoding="utf-8") as f:
        f.write("\n".join(api_log_lines))
    print("    -> network.log + api_responses.log saved", flush=True)


# ---------------------------------------------------------------------------
# yt-dlp download
# ---------------------------------------------------------------------------

def download_with_ytdlp(url: str, headers: dict | None = None, label: str = "stream"):
    print(f"[*] Downloading ({label}) via yt-dlp: {redact(url[:120])}...", flush=True)
    if is_hls(url) and not shutil.which("ffmpeg"):
        print("[!] WARNING: ffmpeg nahi mila — HLS download fail ho sakta hai!", flush=True)
    ydl_opts = {
        "outtmpl": "downloaded_lecture.%(ext)s",
        "format": "best/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "verbose": False,
        "retries": 3,
        "fragment_retries": 3,
    }
    if headers:
        ydl_opts["http_headers"] = headers
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"[-] yt-dlp download fail: {redact(str(e))}", flush=True)
        return False
    downloaded = [f for f in os.listdir(".") if f.startswith("downloaded_lecture.")]
    if downloaded:
        print(f"[+] Download Finished! File(s): {downloaded}", flush=True)
        return True
    print("[-] yt-dlp chala lekin downloaded_lecture.* file nahi bani.", flush=True)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    if not TARGET_URL:
        print("[-] ERROR: TARGET_URL env var set nahi hai!", flush=True)
        return 2

    print(f"[*] Targeting URL: {redact(TARGET_URL[:80])}... (token redacted)", flush=True)
    print(f"[*] Stream ke liye max wait: {WAIT_SECONDS}s", flush=True)

    stream_url = None
    embed_url = None
    browser_headers = {"User-Agent": USER_AGENT, "Referer": TARGET_URL}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="en-IN",
        )
        page = await context.new_page()

        def handle_request(request):
            nonlocal stream_url, embed_url
            url = request.url
            log_network(f"[REQ] {request.method} {url[:200]}")
            if is_hls(url) and not stream_url:
                stream_url = url
                print(f"\n[+] Captured HLS Stream URL: {redact(stream_url)}\n", flush=True)
            elif is_media(url) and not stream_url:
                stream_url = url
                print(f"\n[+] Captured Media Stream URL: {redact(stream_url)}\n", flush=True)
            elif is_embed(url) and not embed_url:
                embed_url = url
                print(f"\n[+] Captured Embed URL: {redact(embed_url)}\n", flush=True)

        async def handle_response(response):
            nonlocal stream_url, embed_url
            try:
                url = response.url
                if not is_api(url):
                    return
                ctype = (response.headers.get("content-type") or "")
                if "json" not in ctype and "text" not in ctype:
                    return
                body = await response.text()
                log_api(f"[API {response.status}] {url[:150]}")
                log_api(f"    body[:2000]: {body[:2000]}")
                # API response ke andar video URL chhupa ho sakta hai
                for u in find_stream_urls(body):
                    if is_embed(u) and not embed_url:
                        embed_url = u
                        print(f"\n[+] Embed URL API response se mila: {redact(u)}\n", flush=True)
                    elif is_media(u) and not stream_url:
                        stream_url = u
                        print(f"\n[+] Stream URL API response se mila: {redact(u)}\n", flush=True)
            except Exception:
                pass

        def handle_console(msg):
            log_api(f"[CONSOLE {msg.type}] {msg.text[:300]}")

        def handle_pageerror(err):
            log_api(f"[PAGEERROR] {str(err)[:300]}")

        def handle_requestfailed(request):
            log_network(f"[FAILED] {request.method} {request.url[:150]} :: {request.failure}")

        page.on("request", handle_request)
        page.on("response", handle_response)
        page.on("console", handle_console)
        page.on("pageerror", handle_pageerror)
        page.on("requestfailed", handle_requestfailed)

        # --- Page load ---------------------------------------------------
        try:
            await page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[-] Page load error: {redact(str(e))}", flush=True)

        try:
            title = await page.title()
        except Exception:
            title = "(title nahi mil paya)"
        print(f"[*] Page title: {title}", flush=True)
        print(f"[*] Final URL (redirect ke baad): {redact(page.url[:120])}", flush=True)

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("[*] networkidle wait timeout (ignore)", flush=True)

        # Page ka visible text turant check karo — token error yahi dikhega
        try:
            text = await page.inner_text("body")
            print(f"\n[*] Page text (first 1000 chars):\n{redact(text[:1000])}\n", flush=True)
            low = text.lower()
            for kw in ("invalid", "expired", "unauthorized", "login", "not found",
                       "token", "session", "forbidden", "access denied"):
                if kw in low:
                    print(f"[!] Page text me keyword mila: '{kw}' — token/login issue ho sakta hai", flush=True)
                    break
        except Exception:
            pass

        # HTML ke andar chhupe URLs bhi check karo
        try:
            html = await page.content()
            for u in find_stream_urls(html):
                if is_embed(u) and not embed_url:
                    embed_url = u
                    print(f"\n[+] Embed URL page HTML se mila: {redact(u)}\n", flush=True)
                elif is_media(u) and not stream_url:
                    stream_url = u
                    print(f"\n[+] Stream URL page HTML se mila: {redact(u)}\n", flush=True)
        except Exception:
            pass

        # --- Play click + stream polling -------------------------------
        await try_click_play(page)

        waited = 0
        while waited < WAIT_SECONDS and not stream_url and not embed_url:
            await asyncio.sleep(2)
            waited += 2
            dom = await extract_dom_sources(page)
            for src in dom["videos"] + dom["sources"]:
                if src and not src.startswith("blob:") and is_media(src):
                    stream_url = src
                    print(f"\n[+] Stream URL DOM se mili: {redact(stream_url)}\n", flush=True)
                    break
            for src in dom["iframes"]:
                if is_embed(src):
                    embed_url = src
                    print(f"\n[+] Embed URL DOM se mila: {redact(embed_url)}\n", flush=True)
                    break
            if waited % 10 == 0 and not stream_url:
                await try_click_play(page)
            if waited % 10 == 0:
                print(f"[*] ...stream ka wait ho raha hai ({waited}s / {WAIT_SECONDS}s)", flush=True)

        try:
            cookies = await context.cookies()
            if cookies:
                browser_headers["Cookie"] = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies
                )
        except Exception:
            pass

        # Hamesha debug files save karo (success par bhi useful hain)
        await save_debug(page, "final state")
        await browser.close()

    # --- Download ---------------------------------------------------------
    if stream_url:
        if download_with_ytdlp(stream_url, browser_headers, label="captured stream"):
            return 0
    elif embed_url:
        if download_with_ytdlp(embed_url, browser_headers, label="embed"):
            return 0
    else:
        print("[-] Browser se stream nahi mili — yt-dlp fallback try kar raha hoon...", flush=True)
        if download_with_ytdlp(TARGET_URL, browser_headers, label="original URL fallback"):
            return 0

    print("\n[-] Stream URL not found / download fail.", flush=True)
    print("    Debug files: screenshot.png, page.html, page_text.txt, network.log, api_responses.log", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
