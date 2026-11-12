"""
keygrab.py — app page ke andar key + media capture
==================================================
Pichle run me page ke andar playlist 200 aayi aur hls.js ne decrypt karke
media MSE me daala (3.1 MB) — yaani AES key page ke andar chalti hai.
Ab har media/key request ko route level par retry + pacing ke saath
jaane dete hain, aur 16/32-byte response ko key samajh kar save karte hain.
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
window.__hits = [];
(function () {
  const of = window.fetch;
  if (of) window.fetch = async function (...a) {
    const r = await of.apply(this, a);
    try {
      const u = (typeof a[0] === 'string') ? a[0] : (a[0] && a[0].url) || '';
      const c = r.clone();
      c.arrayBuffer().then(b => {
        if (b.byteLength <= 64)
          window.__hits.push({u: String(u).slice(0, 300), n: b.byteLength,
            b64: btoa(String.fromCharCode.apply(null, new Uint8Array(b)))});
      }).catch(()=>{});
    } catch (e) {}
    return r;
  };
  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__u = u; return oo.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', () => {
      try {
        const t = this.responseType;
        if (t === 'arraybuffer' && this.response && this.response.byteLength <= 64) {
          const b = new Uint8Array(this.response);
          window.__hits.push({u: String(this.__u).slice(0, 300), n: b.length,
            b64: btoa(String.fromCharCode.apply(null, b))});
        } else if ((!t || t === 'text') && typeof this.responseText === 'string'
                   && this.responseText.length <= 44) {
          window.__hits.push({u: String(this.__u).slice(0, 300),
            n: this.responseText.length, txt: this.responseText});
        }
      } catch (e) {}
    });
    return os.apply(this, arguments);
  };
})();
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


def save_key(b: bytes):
    if len(b) in (16, 32):
        hx = b.hex()
        with open("hls_key.txt", "w") as f:
            f.write(hx)
        print(f"[+++] KEY CAPTURED ({len(b)} bytes): {hx}", flush=True)
        return True
    return False


async def main():
    from playwright.async_api import async_playwright

    target = os.environ.get("TARGET_URL", "").strip()
    if "playerToken" not in target:
        print("[-] TARGET_URL chahiye")
        return 2

    got = {"key": False}
    seen = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN",
                                        viewport={"width": 1366, "height": 768})
        await ctx.add_init_script(STEALTH_JS)

        async def handler(route):
            req = route.request
            u = req.url
            rt = req.resource_type
            if "disable-devtool" in u:
                return await route.abort()
            if rt == "document" and urlparse(u).netloc in (
                    "studyratna.org", "www.studyratna.org"):
                return await route.fulfill(status=200, content_type="text/html",
                                           body="<html><body>ok</body></html>")
            interesting = (".m3u8" in u or ".ts" in u or "url=" in u
                           or "enc.key" in u or "get-hls-key" in u)
            if not interesting:
                return await route.continue_()

            short = u.split("/")[-1][:40] or u[:40]
            for attempt in range(10):
                try:
                    resp = await route.fetch(timeout=60000)
                except Exception:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                body = b""
                try:
                    body = await resp.body()
                except Exception:
                    pass
                if resp.status == 200:
                    if "url=" in u or "key" in short.lower():
                        print(f"    [200] {short} len={len(body)} "
                              f"ct={resp.headers.get('content-type')}",
                              flush=True)
                    if 0 < len(body) <= 32:
                        print(f"    [SMALL {len(body)}] {u[:160]} -> "
                              f"{body[:32].hex()}", flush=True)
                        if save_key(body):
                            got["key"] = True
                    if body[:7] == b"#EXTM3U":
                        print(f"    [PLAYLIST] {len(body)} bytes", flush=True)
                    return await route.fulfill(response=resp)
                if short not in seen or attempt == 9:
                    print(f"    [{resp.status}] {short} (try {attempt+1})",
                          flush=True)
                    seen.add(short)
                await asyncio.sleep(1.2 * (attempt + 1))
            return await route.fulfill(status=403, body=b"")

        await ctx.route("**/*", handler)
        page = await ctx.new_page()
        try:
            await page.goto(target, timeout=90000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[*] goto: {str(e)[:100]}", flush=True)

        started = False
        for i in range(30):
            await page.wait_for_timeout(3000)
            try:
                info = await page.evaluate(VIDEO_JS)
            except Exception:
                info = {}
            if (info.get("duration") or 0) > 1:
                if not started:
                    print(f"[+] player chalu: dur={info['duration']:.0f}s",
                          flush=True)
                    started = True
            if i % 4 == 0:
                print(f"    {3*(i+1)}s dur={info.get('duration')} "
                      f"buf={info.get('bufferedEnd')}", flush=True)
            # in-page hooks ke hits check karo
            try:
                hits = await page.evaluate(
                    "() => (window.__hits || []).splice(0, 20)")
            except Exception:
                hits = []
            for h in hits or []:
                u = h.get("u", "")
                print(f"    [hook] n={h.get('n')} {u[:140]}", flush=True)
                if h.get("b64"):
                    try:
                        b = base64.b64decode(h["b64"])
                        if save_key(b):
                            got["key"] = True
                    except Exception:
                        pass
            if got["key"] and (info.get("bufferedEnd") or 0) > 20:
                break

        if not got["key"]:
            print("[*] key direct nahi mili — page ke andar key request "
                  "dhoondh raha hoon...", flush=True)
            try:
                hits = await page.evaluate(
                    "() => (window.__hits || []).splice(0, 50)")
                for h in hits or []:
                    print(f"    [hit] n={h.get('n')} {str(h.get('u'))[:160]}",
                          flush=True)
            except Exception:
                pass

        await browser.close()

    print("[=] KEY MILI" if got["key"] else "[=] key nahi mili", flush=True)
    return 0 if got["key"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
