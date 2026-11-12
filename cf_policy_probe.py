"""
CloudFront signed-policy probe
==============================
Token ki CloudFront cookie ek *signed policy* hai. Uska exact Resource
path decode karke wahi path maangte hain — playlist worker ne key URI
rewrite kiya tha, original CloudFront playlist/key abhi bhi signed
cookie ke saath khul sakti hai.
"""

import asyncio
import base64
import json
import os
import sys
from urllib.parse import urlparse

import downloader as d

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def b64fix(s):
    s = s.replace("-", "+").replace("_", "/").replace("~", "=")
    return s + "=" * (-len(s) % 4)


async def main():
    from playwright.async_api import async_playwright
    from urllib.parse import parse_qs, urlparse as up

    target = os.environ.get("TARGET_URL", "").strip()
    tok = parse_qs(up(target).query).get("playerToken", [None])[0]
    if not tok:
        print("[-] playerToken nahi mila")
        return 2
    data = d.decrypt_player_token(tok) or {}
    cookie = data.get("cookie") or ""
    video_url = data.get("videoUrl") or ""
    uuid = video_url.split("/stream/")[-1].split("/")[0]
    print(f"[*] uuid={uuid}")

    jar = {}
    for part in cookie.split(";"):
        if "=" in part:
            n, v = part.strip().split("=", 1)
            jar[n] = v
    print(f"[*] cookie names: {list(jar)}")

    pol = jar.get("CloudFront-Policy", "")
    host, prefix = "", ""
    if pol:
        try:
            raw = base64.b64decode(b64fix(pol)).decode("utf-8", "replace")
            print(f"[*] POLICY: {raw[:600]}")
            js = json.loads(raw[:raw.rfind('}') + 1])
            st = js["Statement"][0]
            res = st["Resource"]
            if isinstance(res, list):
                res = res[0]
            print(f"[*] RESOURCE: {res}")
            u = urlparse(res)
            host, prefix = u.netloc, u.path.rstrip("*").rstrip("/")
            print(f"[*] host={host} prefix={prefix}")
        except Exception as e:
            print(f"[-] policy decode: {str(e)[:120]}")

    paths = []
    if prefix:
        if prefix.endswith((".m3u8", ".ts", ".key", ".mp4")):
            paths.append(prefix)
        for tail in ["/master.m3u8", "/hls/720/main.m3u8", "/main.m3u8",
                     "/enc.key", "/key", "/hls/720/enc.key",
                     "/bfa6e07b-8ad3-4308-8aa3-5441a6e61eca/master.m3u8",
                     f"/bfa6e07b-8ad3-4308-8aa3-5441a6e61eca/hls/720/enc.key",
                     f"/bfa6e07b-8ad3-4308-8aa3-5441a6e61eca/enc.key"]:
            paths.append(prefix + tail)
    paths = list(dict.fromkeys(paths))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        h = host or "d1d34p8vz63oiq.cloudfront.net"
        cks = [{"name": k, "value": v, "domain": h, "path": "/"}
               for k, v in jar.items()]
        if cks:
            try:
                await ctx.add_cookies(cks)
                print(f"[*] {len(cks)} CF cookies set for {h}")
            except Exception as e:
                print(f"[-] cookie set: {str(e)[:100]}")

        for path in paths:
            url = f"https://{h}{path}"
            try:
                r = await ctx.request.get(url, timeout=25000)
                b = await r.body()
                print(f"[{r.status}] len={len(b)} {path}", flush=True)
                if r.status == 200:
                    if b"#EXTM3U" in b:
                        print(b[:900].decode("utf-8", "replace"), flush=True)
                    elif len(b) in (16, 32):
                        print(f"[+++] KEY: {b.hex()}", flush=True)
                        with open("hls_key.txt", "w") as f:
                            f.write(b.hex())
                    elif b[:1] == b"\x47":
                        print("[+++] plain TS segment", flush=True)
            except Exception as e:
                print(f"[err] {path} {str(e)[:70]}", flush=True)

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
