"""
AES key proxy probe
===================
player.studyratna.org playlist me key URI rewrite karke
`https://player.studyratna.org/?url=<penpencil key url>` bana deta hai.
Ye proxy 401 deta hai — kaun se header/cookie se 200 deta hai, wo dhoondho.
"""

import asyncio
import os
import sys

import downloader as d

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def main():
    from urllib.parse import parse_qs, quote, urlparse

    target = os.environ.get("TARGET_URL", "")
    tok = parse_qs(urlparse(target).query).get("playerToken", [None])[0]
    if not tok:
        print("[-] TARGET_URL chahiye")
        return 2
    data = d.decrypt_player_token(tok)
    uuid = data["videoUrl"].split("/stream/")[-1].split("/")[0]
    init = data.get("initData") or ""

    pen = (f"https://api.penpencil.co/v1/videos/get-hls-key"
           f"?videoKey={uuid}&key=enc.key")
    proxy_variants = [
        ("proxy ?url=", f"https://player.studyratna.org/?url={quote(pen, safe='')}"),
        ("proxy /stream?url=", f"https://player.studyratna.org/stream?url={quote(pen, safe='')}"),
        ("proxy /proxy?url=", f"https://player.studyratna.org/proxy?url={quote(pen, safe='')}"),
        ("proxy ?url= double", f"https://player.studyratna.org/?url={quote(quote(pen, safe=''), safe='')}"),
        ("direct penpencil", pen),
    ]

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        await ctx.set_extra_http_headers({
            "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org",
        })
        page = await ctx.new_page()
        try:
            await page.goto("https://app.studyratna.org/", timeout=60000,
                            wait_until="domcontentloaded")
        except Exception:
            pass

        header_sets = [
            ("no extra", {}),
            ("x-init-data", {"x-init-data": init}),
            ("auth tma", {"Authorization": f"tma {init}"}),
            ("both", {"x-init-data": init, "Authorization": f"tma {init}"}),
            ("app cookie-ish", {"x-requested-with": "XMLHttpRequest"}),
        ]

        for label, url in proxy_variants:
            for hlabel, extra in header_sets:
                try:
                    r = await ctx.request.get(url, headers=extra, timeout=30000)
                    body = await r.body()
                    print(f"[{label} | {hlabel}] HTTP {r.status} len={len(body)} "
                          f"{body[:140]!r}", flush=True)
                    if r.status == 200 and len(body) in (16, 32):
                        print(f"\n[+] KEY: {body.hex()}", flush=True)
                        with open("hls_key.txt", "w") as f:
                            f.write(body.hex())
                        await browser.close()
                        return 0
                except Exception as e:
                    print(f"[{label} | {hlabel}] error {str(e)[:90]}", flush=True)
        await browser.close()
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
