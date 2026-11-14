"""
Manifest refresh - India proxy ke through, BILKUL waisa hi jaise app karta hai.

Kyun: token ke andar ka CloudFront signature server-side stale hai (har naye
token me bhi 2026-11-11 wala). Par app aapke phone par lecture chalata hai,
matlab play karte waqt app ka apna endpoint fresh signed URL deta hai.

Yahan hum wahi endpoint (api-lite playback-manifest) alag-alag headers ke saath
IN proxy se call karte hain, aur response ko app wali AES key se decrypt karte
hain. Agar fresh videoUrl+cookie mil gaya -> usse download.

usage: python manifest_india.py <lecture-url>
"""
from __future__ import annotations

import hashlib
import json
import sys

import requests

import proxies

WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"
API_KEY_SEED = "939567f60cc4728ee81fc28fae7458af"
MANIFEST_API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def decrypt_token(url: str) -> dict:
    from urllib.parse import parse_qs, unquote, urlparse
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    tok = unquote((parse_qs(urlparse(url).query).get("playerToken") or [""])[0])
    iv, ct = tok.split(":", 1)
    key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
    return json.loads(unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv))
                            .decrypt(bytes.fromhex(ct)), 16).decode())


def decrypt_api(enc: str, init_data: str) -> dict | None:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    try:
        key = hashlib.sha256((API_KEY_SEED + (init_data or "")[:50]).encode()).digest()
        iv, ct = enc.split(":", 1)
        pt = unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv)).decrypt(
            bytes.fromhex(ct)), 16)
        return json.loads(pt.decode("utf-8", "replace"))
    except Exception as exc:
        print(f"    decrypt fail: {exc}")
        return None


METHODS = [
    ("GET  tma", "GET", {"Authorization": "tma {init}"}),
    ("GET  x-init-data", "GET", {"x-init-data": "{init}"}),
    ("GET  both", "GET", {"Authorization": "tma {init}", "x-init-data": "{init}"}),
    ("GET  raw-init", "GET", {"x-init-data": "{raw}"}),
    ("POST tma", "POST", {"Authorization": "tma {init}", "Content-Type": "application/json"}),
    ("POST x-init-data", "POST", {"x-init-data": "{init}", "Content-Type": "application/json"}),
]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: manifest_india.py <lecture-url>")
        return 2
    url = sys.argv[1]
    info = decrypt_token(url)
    init = info.get("initData") or ""
    from urllib.parse import unquote
    raw_init = unquote(init)
    batch, subject, schedule = info.get("batchId"), info.get("subjectId"), info.get("scheduleId")
    endpoint = f"{MANIFEST_API}/{batch}/{subject}/{schedule}"
    print(f"[i] endpoint: {endpoint}")
    print(f"[i] initData len: {len(init)} (raw {len(raw_init)})")
    print(f"[i] token videoUrl: {info.get('videoUrl')}")

    pool = proxies.ensure_pool(min_count=2, rounds=1)
    cands = [p["proxy"] for p in pool][:4] + [None]
    print(f"[i] proxies: {cands}")

    for proxy in cands:
        px = {"http": proxy, "https": proxy} if proxy else {}
        for label, method, hdrs in METHODS:
            h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                 "Referer": "https://app.studyratna.org/",
                 "Origin": "https://app.studyratna.org"}
            for k, v in hdrs.items():
                h[k] = v.format(init=init, raw=raw_init)
            try:
                if method == "GET":
                    r = requests.get(endpoint, headers=h, proxies=px, timeout=30)
                else:
                    r = requests.post(endpoint, headers=h, proxies=px, timeout=30,
                                      json={"initData": init})
                body = r.text[:200]
            except Exception as exc:
                print(f"  {str(proxy):32s} {label:18s} ERR {type(exc).__name__}")
                continue
            print(f"  {str(proxy):32s} {label:18s} HTTP {r.status_code} :: {body[:140]}")
            if r.status_code != 200:
                continue
            try:
                payload = r.json()
            except Exception:
                continue
            if payload.get("encrypted"):
                payload = decrypt_api(payload["encrypted"], init) or {}
            data = payload.get("data") or payload
            if data.get("videoUrl"):
                print("\n*** FRESH URL MILA ***")
                print("videoUrl:", data["videoUrl"])
                print("cookie present:", bool(data.get("cookie")))
                out = {"videoUrl": data["videoUrl"], "cookie": data.get("cookie") or ""}
                open("manifest_result.json", "w").write(json.dumps(out, indent=1))
                print("saved: manifest_result.json")
                return 0
    print("\n[-] koi method fresh URL nahi de paya")
    return 1


if __name__ == "__main__":
    sys.exit(main())
