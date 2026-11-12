"""
curl_cffi probe
===============
Plain `requests` ko Cloudflare 502 deta hai (TLS/HTTP2 fingerprint).
curl_cffi asli Chrome ka TLS/JA3 fingerprint deta hai — usually block
bypass ho jata hai. Yahan playlist/segment/key teeno test hote hain.
"""

import os
import sys

UUID = "bfa6e07b-8ad3-4308-8aa3-5441a6e61eca"
BASE = f"https://player.studyratna.org/stream/{UUID}"
HDRS = {
    "Referer": "https://app.studyratna.org/",
    "Origin": "https://app.studyratna.org",
    "Accept": "*/*",
    "Accept-Language": "en-IN,en;q=0.9",
}


def main():
    try:
        from curl_cffi import requests as cr
    except ImportError:
        print("[-] curl_cffi missing")
        return 2

    print(f"[*] curl_cffi {getattr(cr, '__version__', '')}", flush=True)
    urls = [
        ("master", f"{BASE}/master.m3u8"),
        ("key", "https://player.studyratna.org/?url="
                "https%3A%2F%2Fapi.penpencil.co%2Fv1%2Fvideos%2Fget-hls-key"
                f"%3FvideoKey%3D{UUID}%26key%3Denc.key"),
    ]
    best = None
    for imp in ["chrome124", "chrome120", "chrome110", "safari17_0",
                "chrome"]:
        for http2 in (True,):
            for name, url in urls[:1]:
                try:
                    r = cr.get(url, headers=HDRS, impersonate=imp, timeout=30)
                    body = r.content
                    tag = f"{imp}/h2={http2}"
                    print(f"[{r.status_code}] len={len(body)} {tag} {name}",
                          flush=True)
                    if r.status_code == 200 and b"#EXTM3U" in body:
                        print(body[:600].decode("utf-8", "replace"),
                              flush=True)
                        best = (imp, http2, body)
                        break
                except Exception as e:
                    print(f"[err] {imp} h2={http2}: {str(e)[:70]}", flush=True)
            if best:
                break
        if best:
            break

    if not best:
        print("[-] koi impersonation kaam nahi kiya", flush=True)
        return 1

    imp, http2, master = best
    print(f"[+] WORKING: {imp} http2={http2}", flush=True)
    with open("master.m3u8", "wb") as f:
        f.write(master)

    # variant + seg + key
    import re
    lines = master.decode("utf-8", "replace").splitlines()
    var = None
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            var = lines[i + 1].strip()
            break
    if var:
        vurl = var if var.startswith("http") else f"{BASE}/{var}"
        r = cr.get(vurl, headers=HDRS, impersonate=imp, timeout=30)
        print(f"[{r.status_code}] variant len={len(r.content)}", flush=True)
        if r.status_code == 200:
            with open("media.m3u8", "wb") as f:
                f.write(r.content)
            txt = r.content.decode("utf-8", "replace")
            segs = [l.strip() for l in txt.splitlines()
                    if l and not l.startswith("#")]
            if segs:
                surl = segs[0]
                if not surl.startswith("http"):
                    surl = vurl.rsplit("/", 1)[0] + "/" + surl
                r = cr.get(surl, headers=HDRS, impersonate=imp, timeout=60)
                print(f"[{r.status_code}] seg0 len={len(r.content)} "
                      f"first={r.content[:1]!r}", flush=True)

    for name, url in urls[1:]:
        r = cr.get(url, headers=HDRS, impersonate=imp, timeout=30)
        print(f"[{r.status_code}] len={len(r.content)} {name}", flush=True)
        if r.status_code == 200 and len(r.content) in (16, 32):
            print(f"[+++] KEY {r.content.hex()}", flush=True)
            with open("hls_key.txt", "w") as f:
                f.write(r.content.hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
