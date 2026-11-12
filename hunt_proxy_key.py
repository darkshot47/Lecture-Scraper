"""
India proxy se HLS key hunt
===========================
api.penpencil.co non-India IP par CloudFront 403 deta hai. GitHub se free
proxy lists laa kar India ke proxies se key fetch karne ki koshish.

Key milne par hls_key.txt (hex) save hoti hai.
"""

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PROXY_LISTS = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
]


def load_proxies():
    proxies = []
    for url in PROXY_LISTS:
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": UA})
            if r.status_code != 200:
                print(f"[*] list {url.split('/')[-2]}: HTTP {r.status_code}", flush=True)
                continue
            found = re.findall(r"(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}", r.text)
            print(f"[*] {url.split('/')[-2]}: {len(found)} proxies", flush=True)
            proxies += found
        except Exception as e:
            print(f"[*] list fail: {str(e)[:80]}", flush=True)
    seen, out = set(), []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            out.append(p)
    print(f"[*] total unique proxies: {len(out)}", flush=True)
    return out[:400]


def try_proxy(proxy, key_url):
    px = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    try:
        r = requests.get(key_url, proxies=px, timeout=8,
                         headers={"User-Agent": UA}, verify=False)
    except Exception:
        return None
    if r.status_code == 200 and len(r.content) in (16, 32):
        return (proxy, r.content)
    return None


def main():
    uuid = os.environ.get("VIDEO_KEY", "").strip()
    if not uuid:
        print("[-] VIDEO_KEY chahiye", flush=True)
        return 2
    key_url = (f"https://api.penpencil.co/v1/videos/get-hls-key"
               f"?videoKey={uuid}&key=enc.key")
    print(f"[*] key url: {key_url}", flush=True)

    try:
        requests.packages.urllib3.disable_warnings()
    except Exception:
        pass

    proxies = load_proxies()
    hits = []
    with ThreadPoolExecutor(max_workers=40) as pool:
        for res in pool.map(lambda p: try_proxy(p, key_url), proxies):
            if res:
                proxy, key = res
                print(f"[+] HIT via {proxy}: {key.hex()}", flush=True)
                hits.append((proxy, key))

    if hits:
        with open("hls_key.txt", "w") as f:
            for _, k in hits:
                f.write(k.hex() + "\n")
        print(f"\n[+] {len(hits)} key candidate mili (uppercase/hex validate karo)",
              flush=True)
        return 0
    print("\n[-] Kisi proxy se key nahi mili.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
