"""
App session probe
=================
Asli playerToken URL browser me kholo aur dekho app kya auth karta hai
(kौn si API call, kौn sa cookie set hota hai) — taaki key proxy authorize ho.
"""

import asyncio
import os
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

INTERESTING = ("studyratna", "penpencil", "pw.live")


async def main():
    from urllib.parse import parse_qs, quote, urlparse

    import downloader as d

    target = os.environ.get("TARGET_URL", "")
    if not target:
        print("[-] TARGET_URL chahiye")
        return 2
    tok = parse_qs(urlparse(target).query).get("playerToken", [None])[0]
    data = d.decrypt_player_token(tok) if tok else None
    uuid = data["videoUrl"].split("/stream/")[-1].split("/")[0] if data else ""

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN",
                                        viewport={"width": 1366, "height": 768})
        page = await ctx.new_page()

        seen = []

        def on_response(resp):
            u = resp.url
            if any(k in u for k in INTERESTING) and "_next/static" not in u:
                seen.append((resp.request.method, resp.status, u[:160]))

        page.on("response", on_response)

        print(f"[*] opening playerToken URL...", flush=True)
        try:
            await page.goto(target, timeout=90000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[*] goto: {str(e)[:120]}", flush=True)

        # app ko time do: token decrypt + auth + player init
        for i in range(6):
            await page.wait_for_timeout(3000)
            try:
                vids = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('video'))"
                    ".map(v => ({src: v.src, cur: v.currentSrc}))")
            except Exception:
                vids = []
            print(f"[*] {3*(i+1)}s — video elements: {vids}", flush=True)
            if vids:
                break

        print("\n=== interesting responses ===", flush=True)
        for m, s, u in seen:
            print(f"[{s}] {m} {u}", flush=True)

        print("\n=== cookies ===", flush=True)
        try:
            for c in await ctx.cookies():
                print(f"  {c['domain']} {c['name']} = {c['value'][:45]}...", flush=True)
        except Exception:
            pass

        # page text (player error dikha?)
        try:
            txt = await page.inner_text("body")
            print(f"\n=== page text (600) ===\n{txt[:600]}", flush=True)
        except Exception:
            pass

        # key proxy try with session cookies
        pen = (f"https://api.penpencil.co/v1/videos/get-hls-key"
               f"?videoKey={uuid}&key=enc.key")
        proxy = f"https://player.studyratna.org/?url={quote(pen, safe='')}"
        await ctx.set_extra_http_headers({
            "Referer": "https://app.studyratna.org/",
            "Origin": "https://app.studyratna.org"})
        for label, hdrs in [
            ("session cookies", {}),
            ("+ initData", {"x-init-data": (data or {}).get("initData", "")}),
        ]:
            try:
                r = await ctx.request.get(proxy, headers=hdrs, timeout=30000)
                b = await r.body()
                print(f"\n[key proxy | {label}] HTTP {r.status} len={len(b)} {b[:150]!r}",
                      flush=True)
                if r.status == 200 and len(b) in (16, 32):
                    print(f"[+] KEY: {b.hex()}", flush=True)
                    with open("hls_key.txt", "w") as f:
                        f.write(b.hex())
            except Exception as e:
                print(f"[key proxy | {label}] error {str(e)[:100]}", flush=True)

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
