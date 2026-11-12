"""
keygrab.py — app page ke andar se AES key capture (non-invasive)
===============================================================
Page ko bilkul nahi chhedte: anti-bot script block + marketing redirect
stub, baaki sab as-is. Har response observe karte hain; 16/32-byte body
milte hi wo AES key hai -> hls_key.txt.

Key mile ke baad segments ka kaam pw_download.py karta hai (wo 106
segments pehle bhi la chuka hai).
"""

import asyncio
import base64
import os
import sys
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN','en']});
window.chrome = window.chrome || {runtime: {}};
"""

VIDEO_JS = """() => {
  const out = [];
  const walk = (r) => { r.querySelectorAll('video').forEach(v => out.push(v));
    r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  const v = out[0];
  if (!v) return {found: false};
  let be = 0;
  try { if (v.buffered.length) be = v.buffered.end(v.buffered.length - 1); } catch (e) {}
  return {found: true, duration: v.duration || 0, bufferedEnd: be};
}"""


def save_key(b: bytes, src: str):
    if len(b) in (16, 32):
        hx = b.hex()
        with open("hls_key.txt", "w") as f:
            f.write(hx)
        print(f"[+++] KEY CAPTURED ({len(b)}B) from {src[:120]}", flush=True)
        print(f"[+++] KEY HEX: {hx}", flush=True)
        return True
    return False


async def attempt(browser, target, n):
    state = {"key": False}
    ctx = await browser.new_context(user_agent=UA, locale="en-IN",
                                    viewport={"width": 1366, "height": 768})
    await ctx.add_init_script(STEALTH_JS)

    async def guard(route):
        u = route.request.url
        if "disable-devtool" in u:
            return await route.abort()
        if (route.request.resource_type == "document"
                and urlparse(u).netloc in ("studyratna.org", "www.studyratna.org")):
            return await route.fulfill(status=200, content_type="text/html",
                                       body="<html><body>ok</body></html>")
        return await route.continue_()

    await ctx.route("**/*", guard)
    page = await ctx.new_page()

    async def inspect(resp):
        u = resp.url
        try:
            cl = int(resp.headers.get("content-length") or 0)
        except Exception:
            cl = 0
        cand = ("url=" in u or "key" in u.lower() or 0 < cl <= 64
                or ".m3u8" in u or ".ts" in u)
        if not cand:
            return
        try:
            body = await resp.body()
        except Exception:
            return
        tag = u.split("?")[0][-45:]
        if ".m3u8" in u:
            print(f"    [{resp.status}] PLAYLIST {len(body)}B {tag}", flush=True)
            if body[:7] == b"#EXTM3U":
                txt = body.decode("utf-8", "replace")
                for ln in txt.splitlines():
                    if ln.startswith("#EXT-X-KEY"):
                        print(f"      KEY-LINE: {ln[:200]}", flush=True)
            return
        if ".ts" in u:
            return
        print(f"    [{resp.status}] len={len(body)} {u[:150]}", flush=True)
        if save_key(body, u):
            state["key"] = True
        elif len(body) <= 64 and body:
            try:
                txt = body.decode("utf-8", "replace")
                if txt.startswith("{"):
                    print(f"      body: {txt[:120]}", flush=True)
            except Exception:
                pass

    page.on("response", lambda r: asyncio.create_task(inspect(r)))

    try:
        await page.goto(target, timeout=90000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"    goto: {str(e)[:100]}", flush=True)

    for i in range(24):
        await page.wait_for_timeout(3000)
        try:
            info = await page.evaluate(VIDEO_JS)
        except Exception:
            info = {}
        d = info.get("duration") or 0
        if i % 3 == 0:
            print(f"    {3*(i+1)}s dur={d} buf={info.get('bufferedEnd')}",
                  flush=True)
        if state["key"]:
            print("    key mil gayi — ruk raha hoon", flush=True)
            break
        if d > 1 and (info.get("bufferedEnd") or 0) > 8:
            print("    playback chalu (media aa raha) — key request dekh raha",
                  flush=True)

    await ctx.close()
    return state["key"]


async def main():
    from playwright.async_api import async_playwright

    target = os.environ.get("TARGET_URL", "").strip()
    if "playerToken" not in target:
        print("[-] TARGET_URL chahiye")
        return 2
    if os.path.exists("hls_key.txt"):
        os.remove("hls_key.txt")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required"])
        ok = False
        for n in range(4):
            print(f"[*] attempt {n+1}/4", flush=True)
            try:
                if await attempt(browser, target, n):
                    ok = True
                    break
            except Exception as e:
                print(f"    attempt error: {str(e)[:120]}", flush=True)
        await browser.close()
    print("[=] KEY MILI" if ok else "[=] key nahi mili", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
