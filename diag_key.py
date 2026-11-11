"""
HLS key diagnostic: kyun api.penpencil.co 403 de raha hai?
1. player.studyratna.org ko browser-jaisi headers ke saath maango (proxy
   playlist rewrite karta hai to key same-origin aa sakti hai)
2. penpencil key endpoint ka 403 body dump
3. Playwright se key fetch (Cloudflare challenge solve ho jaye to)
"""

import sys

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def resolve_target():
    """TARGET_URL (ya argv) se playlist + cookie + videoKey nikalo."""
    import os
    from urllib.parse import parse_qs, urlparse

    url = os.environ.get("TARGET_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not url:
        raise SystemExit("TARGET_URL chahiye")
    import downloader as d
    tok = parse_qs(urlparse(url).query).get("playerToken", [None])[0]
    if not tok:
        raise SystemExit("playerToken nahi mila")
    data = d.decrypt_player_token(tok)
    playlist = data["videoUrl"]
    cookie = data.get("cookie") or ""
    vid = playlist.split("/stream/")[-1].split("/")[0]
    key_url = (f"https://api.penpencil.co/v1/videos/get-hls-key"
               f"?videoKey={vid}&key=enc.key")
    # CloudFront direct URL bhi banao
    cf = None
    for c in d.cloudfront_candidates(playlist, cookie):
        if "cloudfront.net" in c:
            cf = c
    return playlist, cookie, key_url, cf


def main():
    playlist, cookie, key_url, cf = resolve_target()
    print(f"[diag] playlist: {playlist}", flush=True)
    print(f"[diag] cloudfront: {cf}", flush=True)
    print(f"[diag] key_url: {key_url}", flush=True)

    # --- 1) player host with browser headers -------------------------------
    print("\n=== 1) player host (browser headers) ===", flush=True)
    for label, hdrs in [
        ("bare", {"User-Agent": UA}),
        ("browser", {**BROWSER_HEADERS, "Referer": "https://app.studyratna.org/"}),
        ("with cookie", {**BROWSER_HEADERS, "Cookie": cookie,
                         "Referer": "https://app.studyratna.org/"}),
    ]:
        try:
            r = requests.get(playlist, headers=hdrs, timeout=30)
            print(f"[{label}] HTTP {r.status_code} len={len(r.content)}", flush=True)
            if r.status_code == 200:
                print(r.text[:600], flush=True)
        except Exception as e:
            print(f"[{label}] error {str(e)[:120]}", flush=True)

    # --- 2) key endpoint bodies -------------------------------------------
    print("\n=== 2) key endpoint ===", flush=True)
    for label, hdrs in [
        ("plain", {"User-Agent": UA}),
        ("app referer", {"User-Agent": UA, "Referer": "https://app.studyratna.org/",
                         "Origin": "https://app.studyratna.org"}),
        ("penpencil referer", {"User-Agent": UA, "Referer": "https://www.penpencil.co/",
                               "Origin": "https://www.penpencil.co"}),
    ]:
        try:
            r = requests.get(key_url, headers=hdrs, timeout=30)
            print(f"[{label}] HTTP {r.status_code} len={len(r.content)}", flush=True)
            print(f"    body[:300]: {r.content[:300]!r}", flush=True)
            print(f"    server={r.headers.get('server')} cf-ray={r.headers.get('cf-ray')}",
                  flush=True)
        except Exception as e:
            print(f"[{label}] error {str(e)[:120]}", flush=True)

    # --- 3) Playwright se key ---------------------------------------------
    print("\n=== 3) playwright key fetch ===", flush=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — skip", flush=True)
        return 0
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"])
            ctx = b.new_context(user_agent=UA, locale="en-IN")
            page = ctx.new_page()
            page.goto("https://app.studyratna.org/", timeout=60000)
            info = page.evaluate("""async (u) => {
                const r = await fetch(u, {credentials: 'include'});
                const buf = await r.arrayBuffer();
                return {status: r.status,
                        ctype: r.headers.get('content-type'),
                        bytes: Array.from(new Uint8Array(buf))};
            }""", key_url)
            print(f"[browser fetch] status={info['status']} ctype={info['ctype']} "
                  f"len={len(info['bytes'])}", flush=True)
            if info["status"] == 200 and len(info["bytes"]) in (16, 32):
                key_hex = bytes(info["bytes"]).hex()
                print(f"[+] KEY (hex) = {key_hex}", flush=True)
                with open("hls_key.txt", "w") as f:
                    f.write(key_hex)
            else:
                print(f"    bytes[:32]={bytes(info['bytes'][:32]).hex()}", flush=True)
            b.close()
    except Exception as e:
        print(f"playwright error: {str(e)[:300]}", flush=True)
    # release step isi file ko pick karta hai
    import contextlib
    return 0


if __name__ == "__main__":
    sys.exit(main())
