"""
manifest_fresh.py — naya CloudFront cookie mint karne ki koshish
===============================================================
App jab lecture kholta hai to `/v1/pw/playback-manifest/...` call karta hai
jo FRESH videoUrl + CloudFront cookie deta hai. Token ki cookie expire ho
jati hai, isliye yahan har header/body variant try hota hai.

Safal: fresh_cookie.txt + fresh_video_url.txt
"""

import base64
import datetime
import json
import os
import sys

import requests

import downloader as d

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"


def b64fix(s):
    s = s.replace("-", "+").replace("_", "/").replace("~", "=")
    return s + "=" * (-len(s) % 4)


def policy_info(cookie: str):
    for part in (cookie or "").split(";"):
        if "CloudFront-Policy" in part and "=" in part:
            raw = part.split("=", 1)[1].strip()
            try:
                js = json.loads(base64.b64decode(b64fix(raw))
                                .decode("utf-8", "replace").split("?")[0])
                exp = js["Statement"][0]["Condition"]["DateLessThan"][
                    "AWS:EpochTime"]
                exp_dt = datetime.datetime.utcfromtimestamp(exp)
                now = datetime.datetime.utcnow()
                left = (exp_dt - now).total_seconds() / 3600
                print(f"[*] CloudFront cookie expiry: "
                      f"{exp_dt.strftime('%Y-%m-%d %H:%M:%SZ')} "
                      f"({left:+.2f} h abhi se)", flush=True)
                if left < 0:
                    print("[!] COOKIE EXPIRE HO CHUKI HAI — link purana hai",
                          flush=True)
                return left
            except Exception as e:
                print(f"[*] policy parse: {str(e)[:80]}", flush=True)
    return None


def main():
    target = os.environ.get("TARGET_URL", "").strip()
    from urllib.parse import parse_qs, urlparse
    tok = parse_qs(urlparse(target).query).get("playerToken", [None])[0]
    if not tok:
        print("[-] playerToken nahi mila")
        return 2
    data = d.decrypt_player_token(tok) or {}
    init = data.get("initData") or ""
    ids = [data.get("batchId"), data.get("subjectId"), data.get("scheduleId")]
    print(f"[*] ids: {ids} initData len={len(init)}", flush=True)
    policy_info(data.get("cookie") or "")

    if not init or not all(ids):
        print("[-] token me initData/ids nahi")
        return 1

    url = f"{API}/{ids[0]}/{ids[1]}/{ids[2]}"
    print(f"[*] API: {url}", flush=True)

    header_sets = {
        "x-init-data": {"x-init-data": init},
        "tma": {"Authorization": f"tma {init}"},
        "both": {"x-init-data": init, "Authorization": f"tma {init}"},
        "x-telegram": {"x-telegram-init-data": init},
        "init-data": {"init-data": init},
        "x-initdata": {"x-initdata": init},
        "x-user-init": {"x-user-init-data": init},
        "telegram": {"telegram-init-data": init},
    }
    base = {"User-Agent": UA, "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org", "Accept": "application/json",
            "client-id": "5eb393ee95fab7468a79d189", "client-type": "WEB",
            "client-version": "1.0"}

    def handle(body, tag):
        if not isinstance(body, dict):
            print(f"    {tag}: non-dict", flush=True)
            return None
        if body.get("encrypted"):
            body = d.decrypt_api(body["encrypted"], init) or {}
        err = body.get("error") or {}
        if err:
            msg = (err.get("message") if isinstance(err, dict) else str(err))
            print(f"    {tag}: ERR {str(msg)[:90]}", flush=True)
            return None
        payload = body.get("data") or {}
        vu = payload.get("videoUrl")
        ck = payload.get("cookie") or ""
        if vu:
            print(f"    {tag}: FRESH videoUrl mila", flush=True)
            print(f"      {vu[:120]}", flush=True)
            policy_info(ck)
            with open("fresh_video_url.txt", "w") as f:
                f.write(vu)
            with open("fresh_cookie.txt", "w") as f:
                f.write(ck)
            return vu
        print(f"    {tag}: data me videoUrl nahi "
              f"({list(payload)[:6]})", flush=True)
        return None

    for tag, hdrs in header_sets.items():
        h = {**base, **hdrs}
        try:
            r = requests.get(url, headers=h, timeout=30)
            try:
                body = r.json()
            except Exception:
                body = None
            print(f"[{r.status_code}] GET {tag}", flush=True)
            if body is None:
                print(f"    body: {r.text[:150]}", flush=True)
            elif handle(body, f"GET {tag}"):
                return 0
        except Exception as e:
            print(f"[err] GET {tag}: {str(e)[:90]}", flush=True)

    for tag, hdrs in list(header_sets.items())[:3]:
        h = {**base, **hdrs, "Content-Type": "application/json"}
        try:
            r = requests.post(url, headers=h,
                              json={"initData": init,
                                    "batchId": ids[0], "subjectId": ids[1],
                                    "scheduleId": ids[2]}, timeout=30)
            try:
                body = r.json()
            except Exception:
                body = None
            print(f"[{r.status_code}] POST {tag}", flush=True)
            if body is None:
                print(f"    body: {r.text[:150]}", flush=True)
            elif handle(body, f"POST {tag}"):
                return 0
        except Exception as e:
            print(f"[err] POST {tag}: {str(e)[:90]}", flush=True)

    print("[-] fresh cookie nahi mili", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
