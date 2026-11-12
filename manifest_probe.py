"""
playback-manifest API probe
===========================
Token ke andar jo `initData` hai usse studyratna ka playback-manifest endpoint
call karo -> fresh videoUrl + cookie mil sakti hai (token ki cookie stale ho
jaye to bhi kaam aata hai).

Chalane ke liye: TARGET_URL env (playerToken wala link) ya LECTURE_ID env.
"""

import json
import os
import sys

import requests

import downloader as d

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"


def main():
    from urllib.parse import parse_qs, urlparse

    url = os.environ.get("TARGET_URL", "")
    tok = parse_qs(urlparse(url).query).get("playerToken", [None])[0]
    if not tok:
        print("[-] playerToken nahi mila (TARGET_URL chahiye)", flush=True)
        return 2

    data = d.decrypt_player_token(tok)
    if not data:
        print("[-] token decrypt fail", flush=True)
        return 1

    init = data.get("initData") or ""
    batch, subject, sched = (data.get("batchId"), data.get("subjectId"),
                             data.get("scheduleId"))
    print(f"[*] initData length: {len(init)}", flush=True)
    print(f"[*] ids: {batch} / {subject} / {sched}", flush=True)
    print(f"[*] initData preview: {init[:120]}", flush=True)

    # initData ke andar telegram user kaun hai
    try:
        from urllib.parse import parse_qs as pq
        inner = pq(init)
        print(f"[*] initData keys: {list(inner.keys())}", flush=True)
        if "auth_date" in inner:
            import datetime
            ts = int(inner["auth_date"][0])
            print(f"[*] initData auth_date: {datetime.datetime.utcfromtimestamp(ts)} UTC",
                  flush=True)
    except Exception:
        pass

    variants = [
        ("x-init-data + tma", {"x-init-data": init, "Authorization": f"tma {init}"}),
        ("x-init-data only", {"x-init-data": init}),
        ("bearer", {"Authorization": f"Bearer {init}"}),
        ("cookie init", {"Cookie": f"initData={init}"}),
    ]
    base = {"User-Agent": UA, "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org", "Accept": "application/json"}

    for label, extra in variants:
        headers = {**base, **extra}
        try:
            r = requests.get(f"{API}/{batch}/{subject}/{sched}",
                             headers=headers, timeout=30)
        except Exception as e:
            print(f"[*] {label}: error {str(e)[:100]}", flush=True)
            continue
        print(f"[*] {label}: HTTP {r.status_code} len={len(r.content)}", flush=True)
        if r.status_code != 200:
            print(f"    body[:200]: {r.content[:200]!r}", flush=True)
            continue

        try:
            body = r.json()
        except Exception:
            print(f"    non-json body[:300]: {r.content[:300]!r}", flush=True)
            continue

        print(f"    keys: {list(body.keys())}", flush=True)
        if body.get("encrypted"):
            dec = d.decrypt_api_response(body["encrypted"], init)
            print(f"    decrypted: {json.dumps(dec)[:600] if dec else 'FAIL'}",
                  flush=True)
            body = dec or {}

        payload = body.get("data") or {}
        print(f"    data keys: {list(payload.keys())}", flush=True)
        if payload.get("videoUrl"):
            print(f"\n[+] FRESH videoUrl: {payload['videoUrl']}", flush=True)
            with open("fresh_video_url.txt", "w") as f:
                f.write(json.dumps(payload))
            if payload.get("cookie"):
                print("[+] fresh cookie bhi mili", flush=True)
            return 0

    print("\n[-] Manifest API se videoUrl nahi mila.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
