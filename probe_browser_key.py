"""
Real Chromium se HLS key fetch (WAF bypass attempt)
===================================================
api.penpencil.co CloudFront par 403 deta hai jab request datacenter IP /
non-browser TLS se aati hai. Real headless Chromium se fetch karke dekhte hain
(app ke origin se, taaki CORS/cookies natural rahein).

Success par hls_key.txt me key hex save hota hai.
"""

import os
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

APP = "https://app.studyratna.org/"


def main():
    uuid = os.environ.get("VIDEO_KEY", "").strip()
    if not uuid:
        import downloader as d
        from urllib.parse import parse_qs, urlparse
        url = os.environ.get("TARGET_URL", "")
        tok = parse_qs(urlparse(url).query).get("playerToken", [None])[0]
        if tok:
            data = d.decrypt_player_token(tok)
            uuid = data["videoUrl"].split("/stream/")[-1].split("/")[0]
    if not uuid:
        print("[-] VIDEO_KEY / TARGET_URL nahi mila", flush=True)
        return 2

    key_url = (f"https://api.penpencil.co/v1/videos/get-hls-key"
               f"?videoKey={uuid}&key=enc.key")
    print(f"[*] key url: {key_url}", flush=True)

    from playwright.sync_api import sync_playwright

    found = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ])
        ctx = browser.new_context(user_agent=UA, locale="en-IN",
                                  viewport={"width": 1366, "height": 768})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()

        # 1) App origin par jao (cookies / same-site context)
        try:
            page.goto(APP, timeout=60000, wait_until="domcontentloaded")
            print("[*] app page load ok", flush=True)
        except Exception as e:
            print(f"[*] app load issue: {str(e)[:100]}", flush=True)

        # 2) Direct navigation se key lao
        try:
            resp = page.goto(key_url, timeout=45000)
            body = page.evaluate(
                "async () => { const b = await (await fetch(location.href)).arrayBuffer();"
                " return Array.from(new Uint8Array(b)); }"
            ) if resp and resp.status == 200 else None
            print(f"[*] direct goto status: {resp.status if resp else 'n/a'}", flush=True)
            if resp and resp.status == 200:
                raw = resp.body()
                print(f"[*] direct body len={len(raw)} hex={raw[:32].hex()}", flush=True)
                if len(raw) in (16, 32):
                    found = raw
        except Exception as e:
            print(f"[*] direct goto error: {str(e)[:120]}", flush=True)

        # 3) fetch() from app origin
        if not found:
            try:
                page.goto(APP, timeout=45000, wait_until="domcontentloaded")
                info = page.evaluate("""async (u) => {
                    try {
                        const r = await fetch(u, {credentials: 'include'});
                        const b = await r.arrayBuffer();
                        return {status: r.status, len: b.byteLength,
                                bytes: Array.from(new Uint8Array(b))};
                    } catch (e) { return {error: String(e)}; }
                }""", key_url)
                print(f"[*] fetch() from app origin: {info}", flush=True)
                if info.get("status") == 200 and info.get("len") in (16, 32):
                    found = bytes(info["bytes"])
            except Exception as e:
                print(f"[*] fetch error: {str(e)[:120]}", flush=True)

        browser.close()

    if found:
        print(f"\n[+] KEY MIL GAYI: {found.hex()}", flush=True)
        with open("hls_key.txt", "w") as f:
            f.write(found.hex())
        return 0
    print("\n[-] Browser se bhi key nahi mili.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
