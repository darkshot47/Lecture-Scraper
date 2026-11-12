"""
Player open probe (anti-bot bypass)
===================================
app.studyratna.org `disable-devtool` script load karta hai jo automated
browser detect karke marketing page par redirect kar deta hai. Usse block
karke asli player khulta hai — aur hls.js khud AES key fetch karta hai,
jo hum network se capture kar lete hain.
"""

import asyncio
import os
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BLOCK = [
    "**/disable-devtool**",
    "**/npm/disable-devtool**",
]

# Anti-detection: common headless fingerprints hatao
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(p)
);
// debugger-loop based detection ko dead karo
window.__stopDebugger = true;
"""


async def main():
    from urllib.parse import parse_qs, urlparse

    import downloader as d

    target = os.environ.get("TARGET_URL", "")
    tok = parse_qs(urlparse(target).query).get("playerToken", [None])[0]
    data = d.decrypt_player_token(tok) if tok else None
    uuid = data["videoUrl"].split("/stream/")[-1].split("/")[0] if data else ""
    print(f"[*] uuid: {uuid}", flush=True)

    from playwright.async_api import async_playwright

    key_hex = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ])
        ctx = await browser.new_context(
            user_agent=UA, locale="en-IN",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"})
        await ctx.add_init_script(STEALTH_JS)

        # Ek hi handler: anti-bot script abort + marketing redirect neutralise
        async def guard(route):
            u = route.request.url
            rt = route.request.resource_type
            if "disable-devtool" in u:
                print(f"[*] blocked anti-bot script", flush=True)
                return await route.abort()
            if rt == "document" and urlparse(u).netloc in (
                    "studyratna.org", "www.studyratna.org"):
                print(f"[*] neutralised redirect -> {u[:60]}", flush=True)
                return await route.fulfill(status=200,
                                           content_type="text/html",
                                           body="<html><body>ok</body></html>")
            return await route.continue_()
        await ctx.route("**/*", guard)

        page = await ctx.new_page()

        def on_response(resp):
            nonlocal key_hex
            u = resp.url
            if "player.studyratna.org" in u or "penpencil" in u:
                try:
                    print(f"[RESP<-] {resp.status} len=? {u[:130]}", flush=True)
                except Exception:
                    pass
            if "get-hls-key" in u or ("player.studyratna.org/?url=" in u):
                status = resp.status
                print(f"[KEY-RESP] {status} {u[:130]}", flush=True)
                if status == 200:
                    try:
                        body = resp.body()
                        if len(body) in (16, 32):
                            key_hex = body.hex()
                            print(f"[+] KEY CAUGHT: {key_hex}", flush=True)
                    except Exception:
                        pass

        page.on("response", on_response)

        def on_req(req):
            if "player.studyratna.org" in req.url or "penpencil" in req.url:
                print(f"[REQ->] {req.method} {req.url[:150]}", flush=True)
        page.on("request", on_req)

        def on_console(msg):
            t = msg.text
            if t and not t.startswith("[Telegram") and "clarity" not in t.lower():
                print(f"[console:{msg.type}] {t[:200]}", flush=True)

        page.on("console", on_console)
        page.on("pageerror", lambda e: print(f"[pageerror] {str(e)[:200]}", flush=True))

        # video element shadow DOM me bhi ho sakta hai
        shadow_js = """() => {
            const out = [];
            const walk = (root) => {
                root.querySelectorAll('video').forEach(v => out.push(v.currentSrc || v.src));
                root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
            };
            walk(document);
            return out;
        }"""

        try:
            await page.goto(target, timeout=90000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[*] goto: {str(e)[:120]}", flush=True)

        for i in range(10):
            await page.wait_for_timeout(3000)
            try:
                info = await page.evaluate("""() => ({
                    url: location.href,
                    videos: (() => {
                        const out = [];
                        const walk = (root) => {
                            root.querySelectorAll('video').forEach(v => out.push(v.currentSrc || v.src));
                            root.querySelectorAll('*').forEach(el => {
                                if (el.shadowRoot) walk(el.shadowRoot); });
                        };
                        walk(document);
                        return out;
                    })(),
                    title: document.title,
                    text: (document.body.innerText || '').slice(0, 200)
                })""")
            except Exception:
                info = {}
            print(f"[*] {3*(i+1)}s url={str(info.get('url'))[:60]} "
                  f"videos={info.get('videos')}", flush=True)
            if info.get("videos") or key_hex:
                break

        if key_hex:
            with open("hls_key.txt", "w") as f:
                f.write(key_hex)
        else:
            # player se key na mile to proxy/session se try
            print("[*] direct key catch nahi hua — page state dekho", flush=True)
            try:
                txt = await page.inner_text("body")
                print(f"[page text] {txt[:400]}", flush=True)
            except Exception:
                pass

        await browser.close()
    return 0 if key_hex else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
