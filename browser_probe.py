"""
Browser probe: asli Chrome me lecture kholo aur poora sach likho.

Kyun: user ke phone par lecture CHAL raha hai, par hamare datacenter IP se
player 502 deta hai. Matlab player ka behaviour IP/headers par depend karta hai.
Yahan hum 4 scenarios chalate hain aur har network response capture karte hain:

  A. bundled/direct Chrome (runner IP)
  B. Chrome + India proxy (agar proxy pool mile)
  C. Chrome + mobile UA + India proxy
  D. Chrome + token se nikala hua CloudFront cookie manually set karke

Har case me: kaunse host 200/403/502 de rahe hain, kaunsa stream URL player
maangta hai, aur kya koi segment actually download hua.

Output: browser_probe.json + console log (draft release me jaata hai).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from base64 import b64decode
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import proxies

WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")

HOSTS_OF_INTEREST = ("studyratna.org", "penpencil.co", "pw.live", "cloudfront.net")


def decrypt_token(url: str) -> dict:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    tok = unquote((parse_qs(urlparse(url).query).get("playerToken") or [""])[0])
    iv, ct = tok.split(":", 1)
    key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
    return json.loads(unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv))
                            .decrypt(bytes.fromhex(ct)), 16).decode())


def cf_host(cookie: str, policy_b64: str = "") -> str:
    pol = re.search(r"CloudFront-Policy=([^;]+)", cookie or "")
    if not pol:
        return ""
    v = pol.group(1).replace("-", "+").replace("_", "/")
    v += "=" * (-len(v) % 4)
    try:
        policy, _ = json.JSONDecoder().raw_decode(b64decode(v).decode())
        m = re.search(r"https://([^/]+)/", policy["Statement"][0]["Resource"])
        return m.group(1) if m else ""
    except Exception:
        return ""


async def probe_case(name: str, url: str, ua: str, proxy: str | None,
                     cookies: list[dict], log_path: str) -> dict:
    from playwright.async_api import async_playwright

    result = {"case": name, "ua": "mobile" if "Mobile" in ua else "desktop",
              "proxy": proxy or "direct", "assets": [], "streams": [], "errors": []}

    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--autoplay-policy=no-user-gesture-required",
                       "--disable-dev-shm-usage"]
        browser = None
        for channel in ("chrome", None):
            try:
                kwargs = {"headless": True, "args": launch_args}
                if channel:
                    kwargs["channel"] = channel
                if proxy:
                    kwargs["proxy"] = {"server": proxy}
                browser = await p.chromium.launch(**kwargs)
                result["browser"] = channel or "bundled"
                break
            except Exception as exc:
                result["errors"].append(f"launch {channel or 'bundled'}: {exc}")
        if browser is None:
            return result

        ctx = await browser.new_context(user_agent=ua, viewport={"width": 1280, "height": 800},
                                        locale="en-IN", timezone_id="Asia/Kolkata")
        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception as exc:
                result["errors"].append(f"add_cookies: {exc}")
        page = await ctx.new_page()
        reqs: list[str] = []

        def on_response(resp):
            u = resp.url
            if not any(h in u for h in HOSTS_OF_INTEREST):
                return
            status = resp.status
            if status >= 400 or any(x in u for x in (".m3u8", ".ts", "keys", "manifest")):
                entry = f"{status} {u[:160]}"
                result["assets"].append(entry)
                print(f"  [{name}] {entry}", flush=True)
            if ".m3u8" in u and status == 200:
                result["streams"].append(u)

        page.on("response", on_response)
        page.on("request", lambda r: reqs.append(r.url))

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            result["errors"].append(f"goto: {exc}")

        # player ko play karo (3 baar try)
        for _ in range(3):
            try:
                await page.evaluate("""() => {
                    document.querySelectorAll('video').forEach(v => {
                        v.muted = true; const p = v.play();
                        if (p && p.catch) p.catch(()=>{});
                    });
                }""")
                for sel in ("button[aria-label*='play' i]", ".vjs-big-play-button",
                            "[class*='play-button' i]", "video"):
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            await loc.click(timeout=1200, force=True)
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            await asyncio.sleep(4)

        try:
            result["page_text"] = (await page.inner_text("body", timeout=5000))[:600]
        except Exception:
            result["page_text"] = ""
        try:
            await page.screenshot(path=f"shot-{name}.png", timeout=15000)
        except Exception:
            pass

        result["requests_count"] = len(reqs)
        result["stream_requests"] = [u[:160] for u in reqs
                                     if any(x in u for x in (".m3u8", "/hls/", "get-hls-key"))][:20]
        await ctx.close()
        await browser.close()

    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, indent=1) + "\n")
    return result


async def main() -> int:
    url = os.environ.get("TARGET_URL", "") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not url:
        print("TARGET_URL nahi mila")
        return 2

    print(f"[i] time: {datetime.now(timezone.utc).isoformat()}")
    info = {}
    try:
        info = decrypt_token(url)
        print(f"[i] lecture: {info.get('videoUrl')}")
    except Exception as exc:
        print(f"[i] token decrypt skip: {exc}")

    # token se cookies banao (CloudFront + koi bhi)
    cookies: list[dict] = []
    raw_cookie = info.get("cookie") or ""
    host = cf_host(raw_cookie)
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if host:
            cookies.append({"name": k.strip(), "value": v.strip(),
                            "domain": host, "path": "/", "secure": True,
                            "sameSite": "None"})

    print("[i] proxy pool dhoond rahe hain…")
    pool = proxies.ensure_pool(min_count=2, rounds=2)
    print(f"[i] proxies: {len(pool)}")
    best = [p["proxy"] for p in pool if p.get("host_ok")][:2]

    cases = [("direct-desktop", DESKTOP_UA, None, [])]
    if best:
        cases.append(("proxy-desktop", DESKTOP_UA, best[0], []))
        cases.append(("proxy-mobile", MOBILE_UA, best[0], []))
    if cookies:
        cases.append(("proxy-mobile-cf", MOBILE_UA, best[0] if best else None, cookies))

    results = []
    for name, ua, proxy, ck in cases:
        print(f"\n=== case: {name} (proxy={proxy}) ===", flush=True)
        try:
            results.append(await probe_case(name, url, ua, proxy, ck, "browser_probe.jsonl"))
        except Exception as exc:
            print(f"  case fail: {exc}")

    summary = {
        "time": datetime.now(timezone.utc).isoformat(),
        "token_created": dict(p.split("=", 1) for p in unquote(info.get("initData", "")).split("&")
                              if "=" in p).get("auth_date"),
        "video_url": info.get("videoUrl"),
        "cases": [{k: v for k, v in r.items() if k != "page_text"} for r in results],
    }
    open("browser_probe.json", "w", encoding="utf-8").write(json.dumps(summary, indent=1))

    print("\n===== SUMMARY =====")
    for r in results:
        ok = len([a for a in r["assets"] if a.startswith("200")])
        bad = len([a for a in r["assets"] if not a.startswith("200")])
        print(f"{r['case']:20s} proxy={str(r['proxy'])[:22]:22s} "
              f"200s={ok} errors={bad} streams={len(r['streams'])}")
        for a in r["assets"][:6]:
            print(f"    {a}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
