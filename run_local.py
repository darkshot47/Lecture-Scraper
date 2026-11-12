#!/usr/bin/env python3
"""
StudyRatna Lecture Downloader — LOCAL (one command)
===================================================

Kyun local? StudyRatna ke video segments ka AES key `api.penpencil.co` se
aata hai, jo India ke bahar (US GitHub runner) se 403 deta hai. Aapke
phone/laptop/laptop par (India me) ye khulta hai, isliye yahan sab kuch
ek command me ho jata hai.

Chalane ka tarika
-----------------
    pip install requests pycryptodome
    python run_local.py

Phir lecture URL paste karo (playerToken wala). Output: lecture.mp4

ffmpeg mile to MP4 banega, warna .ts (VLC me chalta hai).
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

try:
    import requests
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    print("Pehle dependencies install karo:\n    pip install requests pycryptodome")
    sys.exit(2)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"   # decryptForWeb
API_KEY_SEED = "939567f60cc4728ee81fc28fae7458af"         # decrypt(enc, initData)
PW_CLIENT_ID = "5eb393ee95fab7468a79d189"                 # PW public web client


# ---------------------------------------------------------------- crypto ----
def aes_decrypt(key: bytes, enc: str):
    try:
        iv_hex, ct_hex = enc.split(":")
        pt = unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex))
                   .decrypt(bytes.fromhex(ct_hex)), 16)
        return json.loads(pt.decode("utf-8", "replace"))
    except Exception:
        return None


def decrypt_player_token(token: str):
    token = unquote(token).strip()
    key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
    return aes_decrypt(key, token)


def decrypt_api(enc: str, init_data: str):
    key = hashlib.sha256((API_KEY_SEED + (init_data or "")[:50]).encode()).digest()
    return aes_decrypt(key, enc)


# ------------------------------------------------------------- utilities ----
def ask_url():
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    print("Lecture URL paste karo (playerToken wali) aur Enter dabao:")
    return input("> ").strip()


def extract_token(url: str):
    tok = parse_qs(urlparse(url).query).get("playerToken", [None])[0]
    if not tok:
        m = re.search(r"(playerToken=)([^&\s]+)", url)
        tok = m.group(2) if m else None
    return tok


def cloudfront_urls(video_url: str, cookie: str):
    """player host + direct CloudFront dono try karne ke liye."""
    urls = [video_url]
    m = re.search(r"CloudFront-Policy=([^;]+)", cookie or "")
    if not m:
        return urls
    raw = m.group(1).replace("-", "+").replace("_", "/").replace("~", "=")
    raw += "=" * (-len(raw) % 4)
    try:
        dec = base64.b64decode(raw).decode("utf-8", "replace")
        policy = json.loads(dec[:dec.rfind("}") + 1])
        resource = policy["Statement"][0]["Resource"]
        host = urlparse(resource.replace("/*", "")).netloc
    except Exception:
        return urls
    if not host:
        return urls
    path = urlparse(video_url).path
    urls.append(f"https://{host}{path}")
    uuid = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                     path)
    if uuid:
        tail = path.split(uuid.group(0))[-1]
        urls.append(f"https://{host}/{uuid.group(0)}{tail}")
    return urls


def headers_for(cookie: str):
    h = {
        "User-Agent": USER_AGENT,
        "Referer": "https://app.studyratna.org/",
        "Origin": "https://app.studyratna.org",
        "Accept": "*/*",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def pick_playlist(urls, headers, session):
    for u in urls:
        try:
            r = session.get(u, headers=headers, timeout=30)
        except Exception:
            continue
        if r.status_code == 200 and "#EXTM3U" in r.text[:200]:
            print(f"[+] Playlist OK: {u[:110]}")
            return u, r.text
        print(f"[*] playlist try {u[:80]} -> HTTP {r.status_code}")
    return None, None


def fetch(session, url, headers, retries=4):
    last = None
    for i in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=45)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep(1.2 * (i + 1))
                continue
            break
        except Exception as e:
            last = str(e)[:100]
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"fetch fail ({last}): {url[:110]}")


# ------------------------------------------------------------------- key ----
def get_key(session, key_url: str, headers: dict, uuid: str,
            cf_host: str = "", probe: bytes = b"", iv: bytes = bytes(16)):
    """
    AES key lao. Order:
      1. signed CloudFront paths (cookie ke saath) — sabse likely
      2. player.studyratna.org proxy
      3. api.penpencil.co (PW ka API)
    """
    cands = []
    if cf_host and uuid:
        cands += [f"https://{cf_host}/{uuid}/enc.key",
                  f"https://{cf_host}/{uuid}/hls/720/enc.key",
                  f"https://{cf_host}/{uuid}/key"]
    if uuid:
        cands += [f"https://player.studyratna.org/stream/{uuid}/enc.key",
                  f"https://player.studyratna.org/stream/{uuid}/hls/720/enc.key"]
    cands.append(key_url)
    if uuid:
        cands.append(f"https://api.penpencil.co/v1/videos/get-hls-key"
                     f"?videoKey={uuid}&key=enc.key")
    variants = [
        {**headers, "client-id": PW_CLIENT_ID, "client-type": "WEB",
         "client-version": "1.0"},
        headers,
        {"User-Agent": USER_AGENT},
    ]
    for url in cands:
        for hdrs in variants:
            try:
                r = session.get(url, headers=hdrs, timeout=25)
            except Exception as e:
                print(f"[*] key error: {str(e)[:80]}")
                continue
            if r.status_code == 200 and len(r.content) in (16, 32):
                if probe and not looks_like_ts(r.content, iv, probe):
                    print(f"[*] key {url[:60]} -> mili par decrypt fail")
                    continue
                print(f"[+] AES key mil gayi ({len(r.content)} bytes)")
                print(f"    KEY HEX: {r.content.hex()}")
                return r.content
            print(f"[*] key {url[:60]} -> HTTP {r.status_code}")
    return None


def looks_like_ts(key, iv, segment):
    head = segment[:188]
    head += b"\x00" * (-len(head) % 16)
    return AES.new(key, AES.MODE_CBC, iv).decrypt(head)[:1] == b"\x47"


def refresh_from_manifest(data):
    """playback-manifest API se fresh videoUrl + cookie (stale token ke liye)."""
    init = data.get("initData") or ""
    ids = (data.get("batchId"), data.get("subjectId"), data.get("scheduleId"))
    if not init or not all(ids):
        return None
    api = (f"https://api-lite.studyratna.org/v1/pw/playback-manifest/"
           f"{ids[0]}/{ids[1]}/{ids[2]}")
    try:
        r = requests.get(api, headers={
            "x-init-data": init, "Authorization": f"tma {init}",
            "User-Agent": USER_AGENT, "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org"}, timeout=30)
        body = r.json()
    except Exception as e:
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.json()
                err = body.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                print(f"[-] StudyRatna: {msg}")
                if msg and "PW Session" in msg:
                    print("    >>> PW login expire ho gaya hai.")
                    print("    >>> App me PW account dobara login karo, phir naya link banao.")
            except Exception:
                print(f"    manifest fail: {str(e)[:120]}")
        else:
            print(f"    manifest fail: {str(e)[:120]}")
        return None

    if body.get("encrypted"):
        body = decrypt_api(body["encrypted"], init) or {}

    err = body.get("error") or {}
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        print(f"[-] StudyRatna: {msg}")
        if msg and "PW Session" in msg:
            print("    >>> PW (Physics Wallah) login expire ho gaya hai.")
            print("    >>> App me PW account dobara login/link karo, phir naya link banao.")
        return None

    payload = body.get("data") or {}
    if payload.get("videoUrl"):
        print("    fresh videoUrl mil gaya")
        return payload["videoUrl"], payload.get("cookie")
    return None


# -------------------------------------------------------------- download ----
def download(playlist_url: str, playlist_text: str, headers: dict,
             out_ts: str = "lecture.ts"):
    session = requests.Session()

    # variant chuno (sabse high bandwidth)
    variants, bw = [], None
    for ln in playlist_text.splitlines():
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            bw = int(m.group(1)) if m else 0
        elif ln and not ln.startswith("#"):
            variants.append((bw or 0, ln.strip()))
    media_url, media_text = playlist_url, playlist_text
    if variants:
        variants.sort(reverse=True)
        media_url = urljoin(playlist_url, variants[0][1])
        media_text = fetch(session, media_url, headers).decode("utf-8", "replace")
        print(f"[+] Variant: {variants[0][1]} (bw={variants[0][0]})")

    key_url, iv_hex, segs = None, None, []
    for ln in media_text.splitlines():
        if ln.startswith("#EXT-X-KEY"):
            m = re.search(r'URI="([^"]+)"', ln)
            if m:
                key_url = urljoin(media_url, m.group(1))
            m = re.search(r"IV=([0-9a-fA-Fx]+)", ln)
            if m:
                iv_hex = m.group(1)
        elif ln and not ln.startswith("#"):
            segs.append(urljoin(media_url, ln.strip()))
    if not segs:
        raise RuntimeError("playlist me segments nahi mile")

    dur = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", media_text))
    print(f"[+] Segments: {len(segs)} | duration ≈ {dur/60:.1f} min")

    iv = bytes.fromhex(iv_hex.replace("0x", "")) if iv_hex else bytes(16)
    first = fetch(session, segs[0], headers)
    unencrypted = first[:1] == b"\x47"

    key = None
    if not unencrypted and key_url:
        uuid = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            media_url)
        cf_m = re.search(r"(d[0-9a-z]+)\.cloudfront\.net", media_url)
        cf_host = f"{cf_m.group(1)}.cloudfront.net" if cf_m else ""
        key = get_key(session, key_url, headers,
                      uuid.group(0) if uuid else "", cf_host=cf_host,
                      probe=first, iv=iv)
        if key and not looks_like_ts(key, iv, first):
            print("[-] Milī key se decrypt nahi hua — dusri key try karo")
            key = None
        if not key:
            raise RuntimeError(
                "AES key nahi mili. Ye endpoint sirf India IP se khulta hai — "
                "VPN off karke / mobile data par try karo.")

    parts = [None] * len(segs)
    done = {"n": 0}

    def one(i):
        data = first if i == 0 else fetch(session, segs[i], headers)
        if key:
            data = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(data), 16)
        parts[i] = data
        done["n"] += 1
        if done["n"] % 10 == 0 or done["n"] == len(segs):
            mb = sum(len(p) for p in parts if p) / (1024 * 1024)
            print(f"    {done['n']}/{len(segs)} segments — {mb:.1f} MB")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(len(segs))))

    with open(out_ts, "wb") as f:
        for p in parts:
            f.write(p)
    print(f"[+] Saved {out_ts} ({os.path.getsize(out_ts)/1048576:.1f} MB)")
    return out_ts


def to_mp4(ts_path: str) -> str:
    mp4 = os.path.splitext(ts_path)[0] + ".mp4"
    if not shutil.which("ffmpeg"):
        print("[!] ffmpeg nahi mila — .ts hi rakha hai (VLC/YouTube me chalta hai)")
        print("    ffmpeg install karke dobara chalao: MP4 ban jayega")
        return ts_path
    print("[*] MP4 bana raha hoon (ffmpeg)...")
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ts_path,
                        "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4],
                       check=True)
        size = os.path.getsize(mp4) / 1048576
        print(f"[+] MP4 ready: {mp4} ({size:.1f} MB)")
        return mp4
    except Exception as e:
        print(f"[-] ffmpeg fail: {str(e)[:150]}")
        return ts_path


# ------------------------------------------------------------------ main ----
def main():
    url = ask_url()
    token = extract_token(url)
    if not token:
        print("[-] URL me playerToken nahi mila.")
        return 2

    print("[*] Token decrypt kar raha hoon...")
    data = decrypt_player_token(token)
    if not data:
        print("[-] Token decrypt fail. Naya link bhejo (token ~2 ghante me expire hota hai).")
        return 1

    lecture = data.get("lecture") or {}
    title = (lecture.get("topic") or (lecture.get("videoDetails") or {}).get("name")
             or "lecture")
    print(f"[+] Lecture: {title}")

    # optional: playback-manifest se fresh videoUrl (agar token me na ho)
    video_url = data.get("videoUrl")
    if not video_url:
        init = data.get("initData", "")
        api = ("https://api-lite.studyratna.org/v1/pw/playback-manifest/"
               f"{data.get('batchId')}/{data.get('subjectId')}/{data.get('scheduleId')}")
        try:
            r = requests.get(api, headers={
                "x-init-data": init, "Authorization": f"tma {init}",
                "User-Agent": USER_AGENT}, timeout=30)
            body = r.json()
            if body.get("encrypted"):
                body = decrypt_api(body["encrypted"], init) or {}
            video_url = (body.get("data") or {}).get("videoUrl")
        except Exception as e:
            print(f"[-] manifest fail: {str(e)[:120]}")

    if not video_url:
        print("[-] videoUrl nahi mila.")
        return 1

    cookie = data.get("cookie") or ""
    headers = headers_for(cookie)
    session = requests.Session()

    print("[*] Playlist dhoondh raha hoon...")
    playlist_url, playlist_text = pick_playlist(
        cloudfront_urls(video_url, cookie), headers, session)

    if not playlist_url:
        # Token ki cookie stale -> manifest API se fresh URL + cookie
        print("[*] Token ka URL 403 de raha hai — fresh URL maang raha hoon...")
        fresh = refresh_from_manifest(data)
        if fresh:
            video_url, new_cookie = fresh
            cookie = new_cookie or cookie
            headers = headers_for(cookie)
            playlist_url, playlist_text = pick_playlist(
                cloudfront_urls(video_url, cookie), headers, session)

    if not playlist_url:
        print("[-] Playlist nahi mili. Link expire ho gaya hoga — naya link bhejo.")
        return 1

    ts = download(playlist_url, playlist_text, headers)
    final = to_mp4(ts)

    safe = re.sub(r"[^A-Za-z0-9]+", "_", title)[:60].strip("_")
    if safe:
        target = f"{safe}{os.path.splitext(final)[1]}"
        try:
            os.replace(final, target)
            final = target
        except Exception:
            pass
    print(f"\n✅ DONE: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
