"""
player.studyratna.org player page probe
=======================================
Playlist key URI ko `player.studyratna.org/?url=...` par rewrite karta hai —
matlab asli player bhi wahin hai. Us page ko playerToken ke saath kholte hain;
shayad wo session cookie set kare jo key proxy ko authorize kare.
"""

import asyncio
import os
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
"""


async def main():
    from urllib.parse import parse_qs, quote, urlparse

    import downloader as d

    target = os.environ.get("TARGET_URL", "")
    tok = parse_qs(urlparse(target).query).get("playerToken", [None])[0]
    if not tok:
        print("[-] TARGET_URL chahiye")
        return 2
    data = d.decrypt_player_token(tok)
    uuid = data["videoUrl"].split("/stream/")[-1].split("/")[0]
    pen = (f"https://api.penpencil.co/v1/videos/get-hls-key"
           f"?videoKey={uuid}&key=enc.key")
    key_proxy = f"https://player.studyratna.org/?url={quote(pen, safe='')}"

    player_url = f"https://player.studyratna.org/?playerToken={quote(tok, safe='')}"
    print(f"[*] player url len: {len(player_url)}", flush=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN",
                                        viewport={"width": 1366, "height": 768})
        await ctx.add_init_script(STEALTH)

        async def guard(route):
            u = route.request.url
            if "disable-devtool" in u:
                return await route.abort()
            if route.request.resource_type == "document" and \
                    urlparse(u).netloc in ("studyratna.org", "www.studyratna.org"):
                return await route.fulfill(status=200, content_type="text/html",
                                           body="<html><body>ok</body></html>")
            return await route.continue_()
        await ctx.route("**/*", guard)

        page = await ctx.new_page()

        def on_resp(r):
            if "player.studyratna.org" in r.url or "penpencil" in r.url:
                print(f"[RESP] {r.status} {r.url[:120]}", flush=True)

        page.on("response", on_resp)
        page.on("console", lambda m: print(f"[c:{m.type}] {m.text[:150]}", flush=True)
                if m.text and "Telegram" not in m.text else None)

        try:
            await page.goto(player_url, timeout=90000, wait_until="domcontentloaded")
            print("[*] player page loaded", flush=True)
        except Exception as e:
            print(f"[*] goto error: {str(e)[:120]}", flush=True)

        for i in range(8):
            await page.wait_for_timeout(3000)
            try:
                info = await page.evaluate("""() => ({
                    url: location.href.slice(0, 90),
                    title: document.title,
                    vids: (() => { const o=[]; const w=(r)=>{r.querySelectorAll('video')
                        .forEach(v=>o.push((v.currentSrc||v.src).slice(0,60)));
                        r.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)w(e.shadowRoot);});};
                        w(document); return o; })(),
                    text: (document.body.innerText || '').slice(0, 300)
                })""")
            except Exception:
                info = {}
            print(f"[*] {3*(i+1)}s {info.get('url')} vids={info.get('vids')}", flush=True)
            if info.get("vids"):
                print(f"[page text] {info.get('text')}", flush=True)
                break

        print("\n=== cookies ===", flush=True)
        try:
            for c in await ctx.cookies():
                print(f"  {c['domain']} {c['name']}={c['value'][:50]}", flush=True)
        except Exception:
            pass

        # key proxy with session state
        r = await ctx.request.get(key_proxy, timeout=30000)
        b = await r.body()
        print(f"\n[key proxy] HTTP {r.status} len={len(b)} {b[:150]!r}", flush=True)
        if r.status == 200 and len(b) in (16, 32):
            print(f"[+] KEY: {b.hex()}", flush=True)
            with open("hls_key.txt", "w") as f:
                f.write(b.hex())

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
