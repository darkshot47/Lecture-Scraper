"""
StudyRatna HLS downloader via Playwright request context
======================================================
Kyun Playwright? `player.studyratna.org` plain requests / yt-dlp ko Cloudflare
502 deta hai, par real Chromium (HTTP/2 + Chrome TLS fingerprint + cookies) ke
request context se 200 deta hai. Isi context se playlist, AES key aur saare
segments fetch karte hain — phir AES-128 decrypt karke MP4 banate hain.
"""

import asyncio
import os
import subprocess
import sys
from urllib.parse import urljoin

import downloader as d

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

OUT_TS = "downloaded_lecture.ts"
OUT_MP4 = "downloaded_lecture.mp4"


def log(msg):
    print(msg, flush=True)


async def fetch(ctx, url, label="", retries=5):
    """Cloudflare 403/502 flaky hai — retries ke saath fetch."""
    last = (0, b"")
    for attempt in range(retries):
        try:
            # headers pass nahi karte: context ke Referer/Origin override na hon
            r = await ctx.request.get(url, timeout=60000)
            body = await r.body()
            sc = r.headers.get("set-cookie", "")
            log(f"    [{label}] HTTP {r.status} len={len(body)}"
                + (f" set-cookie={sc[:80]}" if sc else ""))
            last = (r.status, body)
            if r.status == 200:
                return r.status, body
        except Exception as e:
            log(f"    [{label}] error {str(e)[:90]}")
        await asyncio.sleep(1.5 * (attempt + 1))
    return last


def parse_master(text):
    variants, bw = [], None
    for ln in text.splitlines():
        if ln.startswith("#EXT-X-STREAM-INF"):
            for part in ln.split(":", 1)[1].split(","):
                if part.startswith("BANDWIDTH="):
                    try:
                        bw = int(part.split("=")[1])
                    except ValueError:
                        bw = 0
        elif ln and not ln.startswith("#"):
            variants.append((bw or 0, ln.strip()))
            bw = None
    variants.sort(reverse=True)
    return variants


def parse_media(text):
    key_uri, iv_hex, segs, dur = None, None, [], 0.0
    for ln in text.splitlines():
        if ln.startswith("#EXT-X-KEY"):
            attrs = {}
            for part in ln.split(":", 1)[1].split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip().strip('"')
            if attrs.get("METHOD") not in (None, "NONE"):
                key_uri = attrs.get("URI")
                iv_hex = attrs.get("IV")
        elif ln.startswith("#EXTINF:"):
            try:
                dur += float(ln.split(":")[1].split(",")[0])
            except ValueError:
                pass
        elif ln and not ln.startswith("#"):
            segs.append(ln.strip())
    return key_uri, iv_hex, segs, dur


async def main():
    from urllib.parse import parse_qs, urlparse

    target = os.environ.get("TARGET_URL", "").strip()
    if not target:
        log("[-] TARGET_URL chahiye")
        return 2

    tok = parse_qs(urlparse(target).query).get("playerToken", [None])[0]
    if not tok:
        log("[-] playerToken nahi mila")
        return 2

    data = d.decrypt_player_token(tok)
    if not data:
        log("[-] token decrypt fail")
        return 1

    lecture = data.get("lecture") or {}
    title = (lecture.get("topic")
             or (lecture.get("videoDetails") or {}).get("name") or "lecture")
    log(f"[+] Lecture: {title}")

    playlist = data["videoUrl"]
    uuid = playlist.split("/stream/")[-1].split("/")[0]
    log(f"[+] Playlist: {playlist}")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN",
                                        viewport={"width": 1366, "height": 768})
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        # token ki cookie (agar valid ho) + extra headers
        cookies = []
        for part in (data.get("cookie") or "").split(";"):
            if "=" in part:
                n, v = part.strip().split("=", 1)
                cookies.append({"name": n, "value": v,
                                "domain": "player.studyratna.org", "path": "/"})
        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                pass
        await ctx.set_extra_http_headers({
            "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org",
            "Accept-Language": "en-IN,en;q=0.9",
        })

        # app page visit (cf cookies ke liye)
        page = await ctx.new_page()
        try:
            await page.goto("https://app.studyratna.org/", timeout=60000,
                            wait_until="domcontentloaded")
        except Exception:
            pass

        try:
            ck = await ctx.cookies()
            log(f"[*] context cookies: {[(c['domain'], c['name']) for c in ck]}")
        except Exception:
            pass

        log("[*] master playlist...")
        status, body = await fetch(ctx, playlist, "master")
        if status != 200:
            log("[-] master playlist fail")
            await browser.close()
            return 1
        master = body.decode("utf-8", "replace")

        variants = parse_master(master)
        media_url = playlist
        if variants:
            bw, uri = variants[0]
            media_url = urljoin(playlist, uri)
            log(f"[+] Variant: {bw} -> {media_url}")

        log("[*] media playlist...")
        status, body = await fetch(ctx, media_url, "media")
        if status != 200:
            log("[-] media playlist fail")
            await browser.close()
            return 1
        media = body.decode("utf-8", "replace")

        key_uri, iv_hex, segs, dur = parse_media(media)
        log(f"[+] Segments: {len(segs)} | duration ≈ {dur/60:.1f} min")
        log(f"[+] Key URI: {key_uri}")
        if not segs:
            await browser.close()
            return 1

        iv = bytes.fromhex(iv_hex.replace("0x", "")) if iv_hex else bytes(16)

        # segment 0 pehle (validation ke liye)
        log("[*] segment 0...")
        status, seg0 = await fetch(ctx, urljoin(media_url, segs[0]), "seg0")
        if status != 200:
            log("[-] segment 0 fail")
            await browser.close()
            return 1

        key = None
        if key_uri and seg0[:1] != b"\x47":
            key_url = urljoin(media_url, key_uri)
            log(f"[*] AES key fetch: {key_url[:140]}")
            status, key_body = await fetch(ctx, key_url, "key")
            if status == 200 and len(key_body) in (16, 32):
                key = key_body
                log(f"[+] KEY: {key.hex()}")
                from Crypto.Cipher import AES
                head = seg0[:188]
                head += b"\x00" * (-len(head) % 16)
                ok = AES.new(key, AES.MODE_CBC, iv).decrypt(head)[:1] == b"\x47"
                log(f"[+] key validated: {ok}")
                if not ok:
                    key = None
                    log("[-] key se decrypt nahi hua")
            else:
                log(f"[-] key nahi mili (HTTP {status}, len={len(key_body)})")

        if seg0[:1] != b"\x47" and not key:
            log("[-] Stream encrypted hai aur key nahi mili — abort")
            await browser.close()
            return 1

        # saare segments (concurrent)
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        parts = [None] * len(segs)
        parts[0] = seg0
        sem = asyncio.Semaphore(8)
        done = {"n": 1}

        async def grab(i):
            async with sem:
                for attempt in range(4):
                    try:
                        r = await ctx.request.get(
                            urljoin(media_url, segs[i]), timeout=60000)
                        if r.status == 200:
                            b = await r.body()
                            if key and i > 0:
                                seg_iv = iv if iv_hex else i.to_bytes(16, "big")
                                try:
                                    b = unpad(AES.new(key, AES.MODE_CBC,
                                                      seg_iv).decrypt(b), 16)
                                except Exception:
                                    return
                            parts[i] = b
                            done["n"] += 1
                            if done["n"] % 10 == 0 or done["n"] == len(segs):
                                mb = sum(len(x) for x in parts if x) / 1048576
                                log(f"    {done['n']}/{len(segs)} — {mb:.1f} MB")
                            return
                    except Exception:
                        pass
                    await asyncio.sleep(1.0 * (attempt + 1))

        if key and seg0[:1] != b"\x47":
            parts[0] = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(seg0), 16)

        await asyncio.gather(*[grab(i) for i in range(1, len(segs))])
        await browser.close()

    missing = [i for i, x in enumerate(parts) if x is None]
    if missing:
        log(f"[-] {len(missing)} segments fail: {missing[:10]}")
        return 1

    with open(OUT_TS, "wb") as f:
        for x in parts:
            f.write(x)
    log(f"[+] {OUT_TS} saved ({os.path.getsize(OUT_TS)/1048576:.1f} MB)")

    # MP4 remux
    from shutil import which
    ffmpeg_bin = which("ffmpeg")
    if not ffmpeg_bin:
        try:
            import imageio_ffmpeg
            ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            log(f"[*] bundled ffmpeg: {ffmpeg_bin}")
        except Exception:
            ffmpeg_bin = None
    if ffmpeg_bin:
        log("[*] ffmpeg MP4 remux...")
        try:
            subprocess.run([ffmpeg_bin, "-y", "-loglevel", "error", "-i", OUT_TS,
                            "-c", "copy", "-bsf:a", "aac_adtstoasc", OUT_MP4],
                           check=True, timeout=1800)
            log(f"[+] {OUT_MP4} ready ({os.path.getsize(OUT_MP4)/1048576:.1f} MB)")
            with open("lecture_info.txt", "w", encoding="utf-8") as f:
                f.write(f"Lecture: {title}\nFile: {OUT_MP4}\n")
            return 0
        except Exception as e:
            log(f"[-] remux fail: {str(e)[:150]}")
    else:
        log("[!] ffmpeg nahi mila — .ts hi final")

    with open("lecture_info.txt", "w", encoding="utf-8") as f:
        f.write(f"Lecture: {title}\nFile: {OUT_TS}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
