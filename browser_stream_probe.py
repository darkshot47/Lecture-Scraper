"""
player.studyratna.org ko real Chromium se kholo
===============================================
Datacenter IP par plain requests ko Cloudflare 502 deta hai, par real browser
JS-challenge pass kar sakta hai. Agar playlist khul gayi, to usme key URI
same-origin (rewritten) ho sakti hai — phir AES key bhi mil jayegi.
"""

import json
import os
import sys

import downloader as d

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def parse_cookies(cookie_str):
    out = []
    for part in (cookie_str or "").split(";"):
        if "=" in part:
            name, val = part.strip().split("=", 1)
            out.append({"name": name, "value": val,
                        "domain": "player.studyratna.org", "path": "/"})
    return out


def main():
    from urllib.parse import parse_qs, urlparse

    url = os.environ.get("TARGET_URL", "")
    tok = parse_qs(urlparse(url).query).get("playerToken", [None])[0]
    if not tok:
        print("[-] TARGET_URL chahiye", flush=True)
        return 2
    data = d.decrypt_player_token(tok)
    if not data:
        print("[-] token decrypt fail", flush=True)
        return 1

    playlist = data["videoUrl"]
    uuid = playlist.split("/stream/")[-1].split("/")[0]
    print(f"[*] playlist: {playlist}", flush=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-IN",
                                  viewport={"width": 1366, "height": 768})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        cookies = parse_cookies(data.get("cookie") or "")
        if cookies:
            try:
                ctx.add_cookies(cookies)
                print(f"[*] {len(cookies)} cookies set (player domain)", flush=True)
            except Exception as e:
                print(f"[*] cookie set fail: {str(e)[:100]}", flush=True)

        page = ctx.new_page()

        # 0) pehle app page (Cloudflare cookies set ho jayen)
        try:
            rapp = page.goto("https://app.studyratna.org/", timeout=60000,
                             wait_until="domcontentloaded")
            print(f"[app-first] HTTP {rapp.status if rapp else 'n/a'}", flush=True)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[app-first] error {str(e)[:120]}", flush=True)

        # 0b) in-page fetch: playlist + manifest (browser context, cf cookies)
        js = """async (u) => {
            try {
                const r = await fetch(u, {credentials: 'include'});
                const t = await r.text();
                return {status: r.status, len: t.length, body: t.slice(0, 900)};
            } catch (e) { return {error: String(e)}; }
        }"""
        for label, u in [("fetch playlist", playlist),
                         ("fetch variant", playlist.replace("master.m3u8",
                                                            "hls/720/main.m3u8"))]:
            try:
                info = page.evaluate(js, u)
                print(f"[{label}] {json.dumps(info)[:700]}", flush=True)
            except Exception as e:
                print(f"[{label}] error {str(e)[:120]}", flush=True)

        init = data.get("initData") or ""
        man = ("https://api-lite.studyratna.org/v1/pw/playback-manifest/"
               f"{data.get('batchId')}/{data.get('subjectId')}/{data.get('scheduleId')}")
        try:
            info = page.evaluate("""async ([u, init]) => {
                try {
                    const r = await fetch(u, {credentials: 'include',
                        headers: {'x-init-data': init, 'Authorization': 'tma ' + init}});
                    const t = await r.text();
                    return {status: r.status, body: t.slice(0, 600)};
                } catch (e) { return {error: String(e)}; }
            }""", [man, init])
            print(f"[fetch manifest] {json.dumps(info)[:700]}", flush=True)
        except Exception as e:
            print(f"[fetch manifest] error {str(e)[:120]}", flush=True)

        def show(label, resp):
            if not resp:
                print(f"[{label}] no response", flush=True)
                return None
            try:
                body = resp.body()
            except Exception:
                body = b""
            print(f"[{label}] HTTP {resp.status} len={len(body)} "
                  f"ctype={resp.headers.get('content-type')}", flush=True)
            if body and len(body) < 4000:
                print(f"    {body[:800].decode('utf-8', 'replace')}", flush=True)
            return body

        # 1) master playlist
        try:
            r = page.goto(playlist, timeout=60000, wait_until="domcontentloaded")
            body = show("master", r)
        except Exception as e:
            print(f"[master] error {str(e)[:150]}", flush=True)
            body = None

        if body and b"#EXTM3U" in body:
            text = body.decode("utf-8", "replace")
            variant = None
            for ln in text.splitlines():
                if ln and not ln.startswith("#"):
                    variant = ln.strip()
                    break
            base = playlist.rsplit("/", 1)[0]
            media_url = f"{base}/{variant}"
            print(f"[*] variant: {media_url}", flush=True)
            r2 = page.goto(media_url, timeout=60000, wait_until="domcontentloaded")
            mbody = show("media", r2)
            if mbody:
                mtext = mbody.decode("utf-8", "replace")
                for ln in mtext.splitlines():
                    if ln.startswith("#EXT-X-KEY"):
                        print(f"[+] KEY LINE: {ln[:250]}", flush=True)
        else:
            print("[*] playlist nahi khuli — Cloudflare block ya 502", flush=True)

        # 2b) Playwright APIRequestContext (CORS bypass, cf cookies carry)
        try:
            ctx.set_extra_http_headers({
                "Referer": "https://app.studyratna.org/",
                "Origin": "https://app.studyratna.org",
                "Accept": "*/*",
            })
            for label, u in [
                ("api-req master", playlist),
                ("api-req variant", playlist.replace("master.m3u8", "hls/720/main.m3u8")),
                ("api-req cf", "https://d1d34p8vz63oiq.cloudfront.net/"
                 f"{uuid}/master.m3u8"),
                ("api-req key", "https://api.penpencil.co/v1/videos/get-hls-key"
                 f"?videoKey={uuid}&key=enc.key"),
            ]:
                try:
                    r = ctx.request.get(u, timeout=30000)
                    body = r.body()
                    print(f"[{label}] HTTP {r.status} len={len(body)} "
                          f"ctype={r.headers.get('content-type')}", flush=True)
                    if len(body) < 1500:
                        print(f"    {body[:400]!r}", flush=True)
                except Exception as e:
                    print(f"[{label}] error {str(e)[:120]}", flush=True)
        except Exception as e:
            print(f"[api-req] setup error {str(e)[:120]}", flush=True)

        # 3) app page bhi try karo (kya mini-app load hota hai)
        try:
            r3 = page.goto("https://app.studyratna.org/", timeout=45000,
                           wait_until="domcontentloaded")
            print(f"[app] HTTP {r3.status if r3 else 'n/a'}", flush=True)
        except Exception as e:
            print(f"[app] error {str(e)[:100]}", flush=True)

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
