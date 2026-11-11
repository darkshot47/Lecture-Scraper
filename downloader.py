"""
Lecture Downloader — v4 (StudyRatna playerToken, direct AES decrypt)
====================================================================

Asli wajah kyun pehle lecture nahi milta tha:
  StudyRatna ek Telegram Mini App (Next.js SPA) hai. Page ke HTML me video
  nahi hoti. `?playerToken=...` ek AES-encrypted payload hai jisme seedha
  video URL + CloudFront signed cookie hota hai. Player usi ko decrypt karta
  hai. Isliye browser me play button dhoondhne se kuch nahi milta.

v4 strategy:
  1. playerToken decrypt karo (AES-256-CBC, key = SHA256("5d902..."), IV:CT)
     -> {type, initData, lecture, videoUrl, cookie, ...}
  2. videoUrl (master.m3u8) + CloudFront cookie se yt-dlp download
  3. Agar videoUrl token me nahi hai -> playback-manifest API try karo
     (response bhi AES-encrypted hota hai: key = SHA256("9395..." + initData[:50]))
  4. Agar token hi nahi -> normal browser fallback (play click + network sniff)
  5. Fail hone par exit code 1 (build red) + debug files
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from urllib.parse import parse_qs, unquote, urlparse

import yt_dlp
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGET_URL = os.environ.get("TARGET_URL", "").strip()
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "60"))

# StudyRatna constants (JS bundle se nikale gaye)
WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"   # decryptForWeb
API_KEY_SEED = "939567f60cc4728ee81fc28fae7458af"         # decrypt(enc, initData)
MANIFEST_API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HLS_HINTS = (".m3u8", "manifest", ".mpd")
MEDIA_HINTS = (".mp4", ".webm", ".mkv", ".mov", "videoplayback", "googlevideo")
EMBED_HINTS = ("youtube.com/embed", "youtu.be", "player.vimeo.com", "vimeo.com/")

network_log_lines = []


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def _aes_decrypt(key: bytes, enc: str):
    """'<iv_hex>:<ciphertext_hex>' -> dict (JS wala AES-CBC/Pkcs7 format)."""
    try:
        iv_hex, ct_hex = enc.split(":")
        iv, ct = bytes.fromhex(iv_hex), bytes.fromhex(ct_hex)
        pt = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), 16)
        return json.loads(pt.decode("utf-8", "replace"))
    except Exception as e:
        print(f"[-] AES decrypt fail: {e}", flush=True)
        return None


def decrypt_player_token(token: str):
    """?playerToken=... ko decrypt karo (decryptForWeb ka Python port)."""
    token = unquote(token).strip()
    # URL me '+' aa sakta hai (space) — hex me nahi hota, par safety
    token = token.replace(" ", "+")
    key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
    return _aes_decrypt(key, token)


def decrypt_api_response(enc: str, init_data: str):
    """API ka {"encrypted": "..."} response decrypt karo."""
    seed = API_KEY_SEED + (init_data or "")[:50]
    key = hashlib.sha256(seed.encode()).digest()
    return _aes_decrypt(key, enc)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def is_hls(url: str) -> bool:
    return any(h in url.lower() for h in HLS_HINTS)


def is_media(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in MEDIA_HINTS) or is_hls(u)


def is_embed(url: str) -> bool:
    return any(h in url.lower() for h in EMBED_HINTS)


def redact(text: str) -> str:
    text = re.sub(r"(playerToken=)[A-Za-z0-9%._\-]+", r"\1REDACTED", str(text))
    text = re.sub(r"\b[0-9a-f]{64,}:[0-9a-f]{64,}\b", "TOKEN_REDACTED", text)
    return text


def log_network(line: str):
    network_log_lines.append(redact(line))
    print(redact(line), flush=True)


URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


def find_stream_urls(text: str):
    found = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip("),;]}")
        if (is_media(u) or is_embed(u)) and u not in found:
            found.append(u)
    return found


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------

def download_with_ytdlp(url: str, headers: dict | None = None, label: str = "stream"):
    print(f"[*] Downloading ({label}): {redact(url[:150])}", flush=True)
    if headers:
        print(f"    headers: {', '.join(k for k in headers)}", flush=True)
    if is_hls(url) and not shutil.which("ffmpeg"):
        print("[!] WARNING: ffmpeg nahi mila — HLS merge fail ho sakta hai!", flush=True)

    ydl_opts = {
        "outtmpl": "downloaded_lecture.%(ext)s",
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 5,
        "verbose": False,
        "progress": True,
    }
    if headers:
        ydl_opts["http_headers"] = headers

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"[-] yt-dlp fail: {redact(str(e))[:500]}", flush=True)
        return False

    files = [f for f in os.listdir(".") if f.startswith("downloaded_lecture.")]
    if files:
        for f in files:
            size = os.path.getsize(f) / (1024 * 1024)
            print(f"[+] Downloaded: {f} ({size:.1f} MB)", flush=True)
        return True
    print("[-] yt-dlp chala lekin file nahi bani.", flush=True)
    return False


def cloudfront_candidates(video_url: str, cookie: str | None):
    """
    player.studyratna.org aksar Cloudflare par 502 deta hai. Token ka
    CloudFront-Policy cookie batata hai asli distribution host kaunsa hai —
    us direct host par same path try karo.
    """
    import base64
    import json

    cands = [video_url]
    if not cookie:
        return cands
    m = re.search(r"CloudFront-Policy=([^;]+)", cookie)
    if not m:
        return cands
    policy_str = m.group(1)
    # CloudFront apna base64 use karta hai: variant try karo
    variants = [
        policy_str.replace("-", "+").replace("_", "/").replace("~", "="),
        policy_str.replace("-", "+").replace("~", "/").replace("_", "="),
        policy_str.replace("~", "="),
    ]
    host, res_path = None, ""
    for raw in variants:
        raw = raw + "=" * (-len(raw) % 4)
        try:
            decoded = base64.b64decode(raw, validate=False).decode("utf-8", "replace")
        except Exception:
            continue
        # JSON parse ho jaye to best
        try:
            policy = json.loads(decoded[:decoded.rfind("}") + 1])
            res_url = policy["Statement"][0]["Resource"]
        except Exception:
            mm = re.search(r"https://([\w.\-]+cloudfront\.net)(/[^\s\"'\\]*)", decoded)
            res_url = f"https://{mm.group(1)}{mm.group(2) or ''}" if mm else None
        if res_url:
            res_url = res_url.replace("/*", "").rstrip("*")
            host = urlparse(res_url).netloc
            res_path = urlparse(res_url).path or ""
            break

    if host:
        path = urlparse(video_url).path
        print(f"[+] CloudFront distribution: {host} (resource path: {res_path})", flush=True)
        cands.append(f"https://{host}{path}")
        # player host par '/stream' prefix hota hai, CloudFront par nahi:
        # /stream/<uuid>/master.m3u8  ->  /<uuid>/master.m3u8
        seg = res_path.rstrip("/").split("/")[-1]
        if seg and seg in path:
            tail = path.split(seg, 1)[1]
            alt = f"https://{host}{res_path.rstrip('/')}{tail}"
            if alt not in cands:
                cands.append(alt)
    else:
        print("[*] CloudFront policy parse nahi hui — sirf original URL.", flush=True)
    return cands


def pick_working_url(video_url: str, cookie: str | None, headers: dict) -> str | None:
    """Har candidate URL probe karo, jo chale wahi return karo."""
    for i, url in enumerate(cloudfront_candidates(video_url, cookie), 1):
        print(f"\n[*] Candidate #{i}: {redact(url)}", flush=True)
        if probe_stream(url, headers):
            print(f"[+] Working URL: {redact(url)}", flush=True)
            return url
    return None


def probe_stream(url: str, headers: dict) -> bool:
    """Stream URL ko seedha fetch karke dekho — 403/404 turant pata chal jayega."""
    import urllib.error
    import urllib.request

    print(f"[*] PROBE: {url[:120]}", flush=True)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(600).decode("utf-8", "replace")
            print(f"[+] PROBE OK: HTTP {r.status} | "
                  f"content-type={r.headers.get('content-type')} | "
                  f"len≈{r.headers.get('content-length')}", flush=True)
            print(f"    body[:400]: {body[:400]}", flush=True)
        return True
    except urllib.error.HTTPError as e:
        print(f"[-] PROBE FAIL: HTTP {e.code} {e.reason}", flush=True)
        try:
            print(f"    body[:300]: {e.read(300).decode('utf-8', 'replace')}", flush=True)
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"[-] PROBE FAIL: {redact(str(e))[:300]}", flush=True)
        return False


def build_headers(cookie: str | None) -> dict:
    h = {
        "User-Agent": USER_AGENT,
        "Referer": "https://app.studyratna.org/",
        "Origin": "https://app.studyratna.org",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


# ---------------------------------------------------------------------------
# StudyRatna token flow
# ---------------------------------------------------------------------------

def try_token_download() -> bool:
    """playerToken se seedha video download. Success par True."""
    qs = parse_qs(urlparse(TARGET_URL).query)
    token = (qs.get("playerToken") or [None])[0]
    if not token:
        print("[*] URL me playerToken nahi hai — token flow skip.", flush=True)
        return False

    print(f"[*] playerToken mila (length {len(token)}) — decrypt kar raha hoon...", flush=True)
    data = decrypt_player_token(token)
    if not data:
        print("[-] playerToken decrypt fail (token corrupt/expired?).", flush=True)
        return False

    lecture = data.get("lecture") or {}
    topic = (lecture.get("topic")
             or (lecture.get("videoDetails") or {}).get("name")
             or lecture.get("name") or "(unknown lecture)")
    print(f"[+] Token decrypted! type={data.get('type')}", flush=True)
    print(f"[+] Lecture: {topic}", flush=True)
    print(f"[+] Batch: {data.get('batchId')} | Subject: {data.get('subjectId')} "
          f"| Schedule: {data.get('scheduleId')}", flush=True)

    video_url = data.get("videoUrl")
    cookie = data.get("cookie")
    print(f"[+] videoUrl in token: {'YES' if video_url else 'NO'}", flush=True)
    print(f"[+] CloudFront cookie: {'YES' if cookie else 'NO'}", flush=True)

    # Agar token me videoUrl nahi hai to playback-manifest API se lo
    if not video_url:
        video_url = fetch_manifest_video_url(data)

    if not video_url:
        print("[-] Token me bhi videoUrl nahi mila.", flush=True)
        return False

    print(f"[+] Stream URL: {redact(video_url)}", flush=True)

    headers = build_headers(cookie)
    working = pick_working_url(video_url, cookie, headers)
    if not working:
        # Sab candidate URLs fail — phir bhi yt-dlp ek baar try kar le
        working = video_url
        print("[-] Koi bhi URL probe me pass nahi hua — yt-dlp phir bhi try.",
              flush=True)

    # Pehla try: apna HLS downloader (har request par cookie jata hai)
    if is_hls(working):
        try:
            from hls_download import download_hls
            out = download_hls(working, headers)
            if out and os.path.exists(out):
                with open("lecture_info.txt", "w", encoding="utf-8") as f:
                    f.write(f"Lecture: {topic}\nStream: {video_url}\nFile: {out}\n")
                print(f"[+] SUCCESS via HLS downloader: {out}", flush=True)
                return True
        except Exception as e:
            print(f"[-] HLS downloader fail: {redact(str(e))[:300]}", flush=True)

    if download_with_ytdlp(working, headers, label="StudyRatna stream (yt-dlp fallback)"):
        with open("lecture_info.txt", "w", encoding="utf-8") as f:
            f.write(f"Lecture: {topic}\nStream: {video_url}\n")
        return True
    return False


def fetch_manifest_video_url(data: dict):
    """playback-manifest API se videoUrl lao (agar token me nahi tha)."""
    init_data = data.get("initData") or ""
    batch = data.get("batchId")
    subject = data.get("subjectId")
    schedule = data.get("scheduleId")
    if not (init_data and batch and subject and schedule):
        print("[*] Manifest API ke liye ids/initData adhoora — skip.", flush=True)
        return None

    import urllib.request

    url = f"{MANIFEST_API}/{batch}/{subject}/{schedule}"
    print(f"[*] Manifest API call: {url}", flush=True)
    req = urllib.request.Request(url, headers={
        "x-init-data": init_data,
        "Authorization": f"tma {init_data}",
        "User-Agent": USER_AGENT,
        "Referer": "https://app.studyratna.org/",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[-] Manifest API fail: {redact(str(e))[:300]}", flush=True)
        return None

    if body.get("encrypted"):
        body = decrypt_api_response(body["encrypted"], init_data) or {}
    if body.get("data", {}).get("videoUrl"):
        return body["data"]["videoUrl"]
    print(f"[-] Manifest response me videoUrl nahi: {redact(str(body))[:300]}", flush=True)
    return None


# ---------------------------------------------------------------------------
# Browser fallback (generic sites)
# ---------------------------------------------------------------------------

async def browser_fallback() -> str | None:
    """Normal browser se stream dhoondho (token flow fail hone par)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[*] playwright installed nahi hai — browser fallback skip.", flush=True)
        return None

    stream_url = None
    embed_url = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1280, "height": 720},
            locale="en-IN",
        )
        page = await context.new_page()

        def on_request(request):
            nonlocal stream_url, embed_url
            url = request.url
            log_network(f"[REQ] {request.method} {url[:200]}")
            if is_hls(url) and not stream_url:
                stream_url = url
            elif is_media(url) and not stream_url:
                stream_url = url
            elif is_embed(url) and not embed_url:
                embed_url = url

        page.on("request", on_request)

        try:
            await page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            print(f"[-] Page load: {redact(str(e))[:200]}", flush=True)

        print(f"[*] Page title: {await page.title()}", flush=True)

        # Play click try
        for sel in ["video", "[aria-label='Play']", ".vjs-big-play-button",
                    ".ytp-play-button", "button:has-text('Play')"]:
            try:
                await page.click(sel, timeout=2000)
                print(f"[*] Clicked: {sel}", flush=True)
                break
            except Exception:
                continue

        waited, dom_stream = 0, None
        while waited < WAIT_SECONDS and not stream_url and not embed_url:
            await asyncio.sleep(2)
            waited += 2
            try:
                dom = await page.evaluate(
                    """() => {
                        const v = [];
                        document.querySelectorAll('video, source').forEach(
                            el => { if (el.src) v.push(el.src);
                                    if (el.currentSrc) v.push(el.currentSrc); });
                        document.querySelectorAll('iframe').forEach(
                            el => { if (el.src) v.push(el.src); });
                        return v;
                    }"""
                )
                for src in dom:
                    if not src or src.startswith("blob:"):
                        continue
                    if is_media(src):
                        dom_stream = src
                        break
                    if is_embed(src) and not embed_url:
                        embed_url = src
            except Exception:
                pass
            if dom_stream:
                stream_url = dom_stream
                break

        # Debug files hamesha
        try:
            await page.screenshot(path="screenshot.png", full_page=True)
            with open("page.html", "w", encoding="utf-8") as f:
                f.write(redact(await page.content()))
            with open("page_text.txt", "w", encoding="utf-8") as f:
                f.write(redact(await page.inner_text("body")))
        except Exception:
            pass
        with open("network.log", "w", encoding="utf-8") as f:
            f.write("\n".join(network_log_lines))

        cookies = await context.cookies()
        cookie_hdr = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        await browser.close()

    if stream_url:
        return stream_url
    if embed_url:
        return embed_url
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not TARGET_URL:
        print("[-] ERROR: TARGET_URL set nahi hai.", flush=True)
        return 2

    print(f"[*] Target: {redact(TARGET_URL)[:120]}", flush=True)
    print(f"[*] ffmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}", flush=True)

    # 1) StudyRatna token flow (fast, deterministic)
    if "playerToken" in TARGET_URL:
        if try_token_download():
            print("\n[+] SUCCESS — lecture download ho gayi.", flush=True)
            return 0
        print("[-] Token flow se download nahi hua — browser fallback try...", flush=True)

    # 2) Browser fallback
    stream = asyncio.run(browser_fallback())
    if stream:
        if download_with_ytdlp(stream, build_headers(None), label="browser-captured"):
            print("\n[+] SUCCESS — lecture download ho gayi.", flush=True)
            return 0
    else:
        print("[-] Browser se bhi stream nahi mili.", flush=True)

    # 3) Last resort: yt-dlp original URL par (token URLs par bekaar hai)
    if "playerToken" not in TARGET_URL:
        if download_with_ytdlp(TARGET_URL, build_headers(None), label="original URL"):
            print("\n[+] SUCCESS.", flush=True)
            return 0

    print("\n[-] FAIL: lecture download nahi hui.", flush=True)
    print("    Debug: screenshot.png, page.html, page_text.txt, network.log", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
