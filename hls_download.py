"""
Simple HLS downloader (yt-dlp ka 403 fix)
=========================================
yt-dlp HLS fragments par kabhi CloudFront cookie nahi bhejta, isliye
download beech me "HTTP 403" par ruk jata hai. Ye module har request
(playlist + segments + key) par khud headers bhejta hai, retries karta hai,
aur segments ko order me concat kar deta hai. Agar ffmpeg mila to MP4
remux bhi kar deta hai.
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests

CHUNK = 1024 * 256
RETRIES = 4


def _get(session, url, headers, timeout=45):
    last = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        except Exception as e:
            last = str(e)[:120]
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch fail ({last}): {url[:130]}")


def _lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_master(text):
    """Master playlist -> [(bandwidth, uri)]; media playlist -> []"""
    variants, pending_bw = [], None
    for ln in _lines(text):
        if ln.startswith("#EXT-X-STREAM-INF"):
            for part in ln.split(":", 1)[1].split(","):
                if part.startswith("BANDWIDTH="):
                    try:
                        pending_bw = int(part.split("=")[1])
                    except ValueError:
                        pending_bw = 0
        elif not ln.startswith("#"):
            variants.append((pending_bw or 0, ln))
            pending_bw = None
    return variants


def _abs_url(base, uri):
    if uri.startswith("http"):
        return uri
    from urllib.parse import urljoin
    return urljoin(base, uri)


def download_hls(url: str, headers: dict, out_path: str = "downloaded_lecture.ts") -> str:
    """HLS stream download karke file path return karo."""
    session = requests.Session()
    session.headers.update(headers)

    print(f"[*] HLS: fetching master playlist...", flush=True)
    master = _get(session, url, headers).decode("utf-8", "replace")

    variants = parse_master(master)
    media_url = url
    if variants:
        variants.sort(key=lambda v: v[0], reverse=True)
        bw, uri = variants[0]
        media_url = _abs_url(url, uri)
        print(f"[+] Variant chuna: bandwidth={bw} -> {media_url[:120]}", flush=True)

    media = _get(session, media_url, headers).decode("utf-8", "replace")
    print(f"[*] Media playlist (first 1200 chars):\n{media[:1200]}\n", flush=True)
    lines = _lines(media)

    key_uri, key_iv = None, None
    segments = []
    for ln in lines:
        if ln.startswith("#EXT-X-KEY"):
            attrs = {}
            for part in ln.split(":", 1)[1].split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip().strip('"')
            if attrs.get("METHOD") not in (None, "NONE"):
                key_uri = _abs_url(media_url, attrs.get("URI", ""))
                key_iv = attrs.get("IV")
        elif not ln.startswith("#"):
            segments.append(_abs_url(media_url, ln))

    if not segments:
        raise RuntimeError("Media playlist me koi segment nahi mila")

    # Duration estimate
    dur = 0.0
    for ln in lines:
        if ln.startswith("#EXTINF:"):
            try:
                dur += float(ln.split(":")[1].split(",")[0])
            except ValueError:
                pass
    print(f"[+] Segments: {len(segments)} | duration ≈ {dur/60:.1f} min", flush=True)

    key = None
    # Pehle ek segment dekh lo — agar TS sync byte (0x47) hai to stream
    # asal me unencrypted hai aur KEY line ka koi matlab nahi.
    encrypted = bool(key_uri)
    probe = _get(session, segments[0], headers)
    if probe[:1] == b"\x47":
        print("[+] Segment 0 MPEG-TS hai (0x47 sync) — stream unencrypted, "
              "key skip.", flush=True)
        encrypted = False
    else:
        print(f"[*] Segment 0 ke first bytes: {probe[:16].hex()}", flush=True)

    if encrypted and key_uri:
        print(f"[+] Encrypted stream (AES-128), key fetch: {key_uri[:160]}", flush=True)
        try:
            key = fetch_key_with_variants(session, key_uri, headers)
        except Exception as e:
            print(f"[-] {e}", flush=True)
            print("[*] Key ke bina hi download try kar raha hoon...", flush=True)
            key = None

    parts = [None] * len(segments)
    done = {"n": 0}

    def fetch_one(i):
        data = _get(session, segments[i], headers)
        if key:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad
            iv = bytes.fromhex(key_iv.replace("0x", "")) if key_iv else \
                i.to_bytes(16, "big")
            try:
                data = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(data), 16)
            except Exception:
                pass
        parts[i] = data
        done["n"] += 1
        if done["n"] % 10 == 0 or done["n"] == len(segments):
            pct = done["n"] * 100 // len(segments)
            mb = sum(len(p) for p in parts if p) / (1024 * 1024)
            print(f"[*]   {done['n']}/{len(segments)} segments ({pct}%) "
                  f"— {mb:.1f} MB", flush=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch_one, range(len(segments))))

    missing = [i for i, p in enumerate(parts) if p is None]
    if missing:
        raise RuntimeError(f"{len(missing)} segments fail hue: {missing[:10]}")

    with open(out_path, "wb") as f:
        for p in parts:
            f.write(p)
    size = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[+] Saved {out_path} ({size:.1f} MB)", flush=True)

    # ffmpeg ho to MP4 remux (stream copy, fast)
    mp4 = "downloaded_lecture.mp4"
    if _has_ffmpeg():
        print("[*] ffmpeg se MP4 remux...", flush=True)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", out_path,
                 "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4],
                check=True, timeout=1200,
            )
            print(f"[+] MP4 ready: {mp4} "
                  f"({os.path.getsize(mp4)/(1024*1024):.1f} MB)", flush=True)
            return mp4
        except Exception as e:
            print(f"[-] remux fail ({str(e)[:150]}) — .ts rakha ja raha hai", flush=True)
    else:
        print("[*] ffmpeg nahi mila — .ts file hi final (VLC/player me chalega)", flush=True)
    return out_path


def fetch_key_with_variants(session, key_url: str, base_headers: dict):
    """Key endpoint (api.penpencil.co) 403 deta hai — header combos try karo."""
    from urllib.parse import urlparse

    host = urlparse(key_url).netloc
    variants = [
        ("as-is (app headers)", dict(base_headers)),
        ("penpencil referer", {**base_headers, "Referer": "https://www.penpencil.co/",
                               "Origin": "https://www.penpencil.co"}),
        ("no cookie", {k: v for k, v in base_headers.items() if k.lower() != "cookie"}),
        ("ua only", {"User-Agent": base_headers.get("User-Agent", "Mozilla/5.0")}),
        ("app referer no origin", {"User-Agent": base_headers.get("User-Agent", "Mozilla/5.0"),
                                   "Referer": "https://app.studyratna.org/"}),
        ("penpencil origin + app referer", {**base_headers,
                                            "Origin": "https://www.penpencil.co"}),
    ]
    for label, hdrs in variants:
        try:
            r = session.get(key_url, headers=hdrs, timeout=30)
            print(f"[*] key try [{label}] -> HTTP {r.status_code} ({len(r.content)} bytes) "
                  f"host={host}", flush=True)
            if r.status_code == 200 and len(r.content) in (16, 32):
                print(f"[+] KEY mil gaya ({label})", flush=True)
                return r.content
        except Exception as e:
            print(f"[*] key try [{label}] -> error {str(e)[:80]}", flush=True)
    raise RuntimeError("HLS decryption key nahi mila (saare header combos 403)")


def _has_ffmpeg() -> bool:
    from shutil import which
    return which("ffmpeg") is not None
