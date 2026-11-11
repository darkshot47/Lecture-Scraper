"""
Lecture Downloader — fixed version
===================================
Purane version ki problems (kyun "Stream URL not found" aata tha):
  1. Sirf ".m3u8" / "manifest" URL dhundhta tha. Agar site YouTube, Vimeo,
     ya direct MP4 use karti hai to stream kabhi milegi hi nahi.
  2. Play button kabhi click nahi hota tha. Player start hi nahi hota,
     to stream request fire hi nahi hoti — 10 second wait bekaar jata tha.
  3. Koi debug info nahi milti thi — lecture kyun nahi mila, pata hi nahi chalta.
  4. yt-dlp ka outtmpl galat tha ('downloaded_lecture.mp4'), isliye asli file
     ka naam/extension alag banta tha aur artifact upload nahi hota tha.
  5. Fail hone par bhi script exit 0 karta tha, isliye GitHub build green
     (success) dikhta tha jabki lecture mila hi nahi.

Is version me:
  - m3u8, mpd, mp4, webm, YouTube/Vimeo embeds — sab detect hote hain
  - Play button auto-click hota hai, video element se src extract hota hai
  - Har network request log hoti hai (network.log)
  - Fail hone par screenshot.png + page.html save hota hai (debug ke liye)
  - yt-dlp fallback: agar browser se stream na mile to original URL par
    direct yt-dlp try hota hai
  - Fail hone par exit code 1 — build ab sach me fail dikhega
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
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "60"))  # stream ke liye max wait
DEBUG_DIR_FILES = ["screenshot.png", "page.html", "network.log"]

# Stream URL pehchanne ke hints (priority order me check hote hain)
HLS_HINTS = (".m3u8", "manifest", ".mpd")          # sabse pehle ye dhundho
MEDIA_HINTS = (".mp4", ".webm", ".mkv", ".mov", "videoplayback", "googlevideo")
EMBED_HINTS = ("youtube.com/embed", "youtu.be", "player.vimeo.com", "vimeo.com/")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

network_log_lines = []   # har request ka log (network.log me save hoga)


def log_network(line: str):
    network_log_lines.append(line)
    print(line, flush=True)


def is_hls(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in HLS_HINTS)


def is_media(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in MEDIA_HINTS) or is_hls(u)


def is_embed(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in EMBED_HINTS)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def extract_dom_sources(page):
    """Page ke andar <video>, <source> aur <iframe> se src nikaalo."""
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
    """Player ka Play button / video click karne ki koshish karo."""
    # 1) Video element par seedha click (sabse common case)
    try:
        await page.click("video", timeout=3000)
        print("[*] Clicked <video> element", flush=True)
        return True
    except Exception:
        pass

    # 2) Play button ke common selectors
    selectors = [
        "button:has-text('Play')",
        "button:has-text('play')",
        "[aria-label='Play']",
        "[aria-label='play']",
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

    # 3) Space key (kuch players space se play hote hain)
    try:
        await page.keyboard.press("Space")
        print("[*] Pressed Space key (play try)", flush=True)
    except Exception:
        pass
    return False


async def save_debug(page, reason: str):
    """Fail hone par screenshot + HTML save karo taaki pata chale kyun fail hua."""
    print(f"\n[-] Debug dump ({reason}):", flush=True)
    try:
        await page.screenshot(path="screenshot.png", full_page=True)
        print("    -> screenshot.png saved", flush=True)
    except Exception as e:
        print(f"    -> screenshot fail: {e}", flush=True)
    try:
        html = await page.content()
        with open("page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"    -> page.html saved ({len(html)} chars)", flush=True)
    except Exception as e:
        print(f"    -> page.html fail: {e}", flush=True)
    with open("network.log", "w", encoding="utf-8") as f:
        f.write("\n".join(network_log_lines))
    print("    -> network.log saved", flush=True)


# ---------------------------------------------------------------------------
# yt-dlp download
# ---------------------------------------------------------------------------

def download_with_ytdlp(url: str, headers: dict | None = None, label: str = "stream"):
    """yt-dlp se download karo. Success par True, fail par False."""
    print(f"[*] Downloading ({label}) via yt-dlp: {url[:120]}...", flush=True)

    if is_hls(url) and not shutil.which("ffmpeg"):
        print("[!] WARNING: ffmpeg nahi mila — HLS (m3u8) download fail ho sakta hai!", flush=True)

    ydl_opts = {
        # FIX: purana 'downloaded_lecture.mp4' galat tha — yt-dlp extension
        # badal deta hai. Ab proper template use karo:
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
        print(f"[-] yt-dlp download fail: {e}", flush=True)
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
        print("    GitHub Actions me workflow_dispatch se URL do, ya locally:", flush=True)
        print("    TARGET_URL='https://...' python downloader.py", flush=True)
        return 2

    print(f"[*] Targeting URL: {TARGET_URL}", flush=True)
    print(f"[*] Stream ke liye max wait: {WAIT_SECONDS}s", flush=True)

    stream_url = None        # HLS / MP4 / media stream
    embed_url = None         # YouTube / Vimeo iframe
    browser_headers = {"User-Agent": USER_AGENT, "Referer": TARGET_URL}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
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
                print(f"\n[+] Captured HLS Stream URL: {stream_url}\n", flush=True)
            elif is_media(url) and not stream_url:
                stream_url = url
                print(f"\n[+] Captured Media Stream URL: {stream_url}\n", flush=True)
            elif is_embed(url) and not embed_url:
                embed_url = url
                print(f"\n[+] Captured Embed URL: {embed_url}\n", flush=True)

        page.on("request", handle_request)

        # --- Page load ---------------------------------------------------
        try:
            await page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[-] Page load error: {e}", flush=True)

        try:
            title = await page.title()
        except Exception:
            title = "(title nahi mil paya)"
        print(f"[*] Page title: {title}", flush=True)
        print(f"[*] Final URL (redirect ke baad): {page.url}", flush=True)

        # Network idle ka wait (best-effort, fail ho to ignore)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("[*] networkidle wait timeout (ignore, aage badho)", flush=True)

        # --- Play click + stream polling -------------------------------
        await try_click_play(page)

        waited = 0
        while waited < WAIT_SECONDS and not stream_url and not embed_url:
            await asyncio.sleep(2)
            waited += 2

            # DOM se video/iframe src check karo
            dom = await extract_dom_sources(page)
            for src in dom["videos"] + dom["sources"]:
                if src and not src.startswith("blob:") and is_media(src):
                    stream_url = src
                    print(f"\n[+] Stream URL DOM se mili: {stream_url}\n", flush=True)
                    break
            for src in dom["iframes"]:
                if is_embed(src):
                    embed_url = src
                    print(f"\n[+] Embed URL DOM se mila: {embed_url}\n", flush=True)
                    break

            # Har ~10s me dobara play try karo (player late load hota hai)
            if waited % 10 == 0 and not stream_url:
                await try_click_play(page)

            if waited % 10 == 0:
                print(f"[*] ...stream ka wait ho raha hai ({waited}s / {WAIT_SECONDS}s)", flush=True)

        # Browser session ke cookies/headers yt-dlp ko do (signed URL ke liye)
        try:
            cookies = await context.cookies()
            if cookies:
                browser_headers["Cookie"] = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies
                )
        except Exception:
            pass

        if not stream_url and not embed_url:
            # Page ka text snippet print karo — login wall / error dikhega
            try:
                text = (await page.inner_text("body"))[:1500]
                print(f"\n[*] Page text (first 1500 chars):\n{text}\n", flush=True)
            except Exception:
                pass
            await save_debug(page, "stream nahi mili")

        await browser.close()

    # --- Download ---------------------------------------------------------
    # Priority: HLS/media stream > embed (YouTube/Vimeo) > original URL fallback
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
    print("    Debug files check karo: screenshot.png, page.html, network.log", flush=True)
    print("    (GitHub Actions me ye 'debug-logs' artifact me upload honge)", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
