"""
player.studyratna.org root page + JS analysis
=============================================
`?url=` proxy kaun se auth ke saath chalta hai — root page ke JS se pata
chal sakta hai (signed param / header / cookie).
"""

import asyncio
import re
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PATTERNS = ["?url=", "Unauthorised", "get-hls-key", "proxy", "authorization",
            "x-api-key", "signature", "hmac", "enc.key", "playerToken"]


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        await ctx.set_extra_http_headers({
            "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org"})

        for path in ["/", "/index.html", "/player", "/robots.txt"]:
            url = f"https://player.studyratna.org{path}"
            try:
                r = await ctx.request.get(url, timeout=30000)
                body = await r.body()
                print(f"\n=== {url} -> HTTP {r.status} len={len(body)} "
                      f"ctype={r.headers.get('content-type')}", flush=True)
                text = body[:1500].decode("utf-8", "replace")
                print(text, flush=True)
                if r.status == 200 and len(body) > 200:
                    # script srcs nikaalo
                    full = body.decode("utf-8", "replace")
                    srcs = re.findall(r'src="([^"]+\.js[^"]*)"', full)[:6]
                    print(f"[srcs] {srcs}", flush=True)
                    for s in srcs:
                        su = s if s.startswith("http") else (
                            "https://player.studyratna.org" + (
                                s if s.startswith("/") else "/" + s))
                        try:
                            jr = await ctx.request.get(su, timeout=30000)
                            js = (await jr.body()).decode("utf-8", "replace")
                            print(f"[js] {su} -> {jr.status} len={len(js)}",
                                  flush=True)
                            for pat in PATTERNS:
                                for m in list(re.finditer(re.escape(pat), js))[:2]:
                                    st = max(0, m.start() - 200)
                                    print(f"  [{pat}] ...{js[st:m.end()+200]}...",
                                          flush=True)
                        except Exception as e:
                            print(f"[js] {su} error {str(e)[:80]}", flush=True)
            except Exception as e:
                print(f"[{url}] error {str(e)[:100]}", flush=True)

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
