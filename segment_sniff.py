"""
Live diagnostic: player host + CloudFront, Indian proxy ke through aur direct -
alag-alag, taaki pata chale asli blocker kaun hai.

Player ka flow (logs se): master -> hls/720/main.m3u8 -> segments -> key
Yahan hum teeno layers ka status dekhte hain, har proxy ke liye.

Token ki zaroorat: sirf videoUrl + cookie ke liye (token decrypt).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from base64 import b64decode
from urllib.parse import parse_qs, unquote, urlparse

import requests

import proxies

WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def decrypt_token(url: str) -> dict:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    tok = unquote((parse_qs(urlparse(url).query).get("playerToken") or [""])[0])
    iv, ct = tok.split(":", 1)
    key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
    return json.loads(unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv))
                            .decrypt(bytes.fromhex(ct)), 16).decode())


def try_get(url: str, proxy: str | None, cookie: str = "", timeout: int = 25) -> dict:
    p = {"http": proxy, "https": proxy} if proxy else {}
    h = {"User-Agent": UA, "Referer": "https://app.studyratna.org/",
         "Origin": "https://app.studyratna.org"}
    if cookie:
        h["Cookie"] = cookie
    try:
        r = requests.get(url, proxies=p, headers=h, timeout=timeout, allow_redirects=True)
        body = r.content[:200]
        return {"status": r.status_code, "len": len(r.content),
                "head": body[:120].decode("utf-8", "replace").replace("\n", " "),
                "server": r.headers.get("server", "")}
    except Exception as exc:
        return {"status": "ERR", "err": f"{type(exc).__name__}: {str(exc)[:90]}"}


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("usage: segment_sniff.py <lecture-url>")
        return 2

    info = decrypt_token(url)
    video_url = info.get("videoUrl") or ""
    cookie = info.get("cookie") or ""
    print(f"[i] videoUrl: {video_url}")
    print(f"[i] cookie present: {bool(cookie)}")

    # host variants: player host aur direct cloudfront
    pol = re.search(r"CloudFront-Policy=([^;]+)", cookie)
    cf_host = ""
    if pol:
        v = pol.group(1).replace("-", "+").replace("_", "/")
        v += "=" * (-len(v) % 4)
        policy, _ = json.JSONDecoder().raw_decode(b64decode(v).decode())
        m = re.search(r"https://([^/]+)/", policy["Statement"][0]["Resource"])
        cf_host = m.group(1) if m else ""
    key = video_url.split("/stream/")[-1].split("/")[0] if "/stream/" in video_url else ""

    targets = [("player-master", video_url)]
    if cf_host and key:
        targets.append(("cf-master", f"https://{cf_host}/{key}/master.m3u8"))
        targets.append(("cf-hls720", f"https://{cf_host}/{key}/hls/720/main.m3u8"))
    targets.append(("player-hls720", video_url.replace("master.m3u8", "hls/720/main.m3u8")))

    pool = proxies.ensure_pool(min_count=2, rounds=2)
    cands = [p["proxy"] for p in pool][:6]
    print(f"[i] proxies: {cands}")

    for label, target in targets:
        for proxy in [None] + cands:
            tag = f"{label:14s} via {proxy or 'DIRECT':32s}"
            res = try_get(target, proxy, cookie if "cf-" in label else "")
            print(f"{tag} -> {res.get('status')} {res.get('len', '')} "
                  f"{res.get('server', '')} {res.get('err', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
