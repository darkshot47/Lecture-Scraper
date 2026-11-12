"""
HLS AES-128 key hunter
======================
Segment encrypted hain, aur #EXT-X-KEY endpoint (api.penpencil.co) non-India
IP par CloudFront 403 deta hai. Ye script har mumkin raasta try karta hai aur
jo bhi 16-byte key mile use segment 0 decrypt karke VALIDATE karta hai
(sahi key se decrypted data 0x47 'G' TS sync se start hota hai).

Raaste:
  1. api.penpencil.co — PW ke public client headers ke saath
  2. CloudFront distribution par direct key paths
  3. player.studyratna.org proxy key paths
  4. Public CORS proxies (alag IP se fetch)

Success par hls_key.txt me key hex likh deta hai.
"""

import os
import sys
from urllib.parse import quote, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# PW ke public web-client headers (studyratna token ke image URL me client-id dikha)
PW_CLIENT_ID = "5eb393ee95fab7468a79d189"

TWITTER = {}


def validate_key(key: bytes, segment: bytes, iv: bytes) -> bool:
    """Key sahi hai? -> decrypt karke TS sync byte (0x47) check."""
    try:
        from Crypto.Cipher import AES
        head = segment[:188]
        head += b"\x00" * (-len(head) % 16)
        pt = AES.new(key, AES.MODE_CBC, iv).decrypt(head)
        return pt[:1] == b"\x47"
    except Exception:
        return False


def resolve():
    """Token se data nikalo; token na ho to VIDEO_KEY env se uuid lo."""
    import downloader as d
    from urllib.parse import parse_qs

    url = os.environ.get("TARGET_URL", "")
    tok = parse_qs(urlparse(url).query).get("playerToken", [None])[0]
    if tok:
        data = d.decrypt_player_token(tok)
        if data:
            playlist = data["videoUrl"]
            cookie = data.get("cookie") or ""
            uuid = playlist.split("/stream/")[-1].split("/")[0]
            cf = [c for c in d.cloudfront_candidates(playlist, cookie)
                  if "cloudfront.net" in c][0]
            return playlist, cookie, uuid, cf, data

    uuid = os.environ.get("VIDEO_KEY", "").strip()
    if not uuid:
        raise SystemExit("Na playerToken na VIDEO_KEY mila")
    return "", "", uuid, "", {}


def base_headers(cookie):
    return {
        "User-Agent": UA,
        "Referer": "https://app.studyratna.org/",
        "Origin": "https://app.studyratna.org",
        "Accept": "*/*",
        "Cookie": cookie,
    }


def pw_headers(cookie):
    h = base_headers(cookie)
    h.update({
        "client-id": PW_CLIENT_ID,
        "client-type": "WEB",
        "client-version": "1.0",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    return h


def hunt_key_only(s, cookie, uuid):
    """Bina playlist ke key endpoints test karo (cookie expire ho jaye to)."""
    key_api = (f"https://api.penpencil.co/v1/videos/get-hls-key"
               f"?videoKey={uuid}&key=enc.key")
    enc = quote(key_api, safe="")
    tests = [
        ("penpencil + PW headers", key_api, pw_headers("")),
        ("penpencil plain", key_api, {"User-Agent": UA}),
        ("allorigins", f"https://api.allorigins.win/raw?url={enc}",
         {"User-Agent": UA}),
        ("codetabs", f"https://api.codetabs.com/v1/proxy?quest={enc}",
         {"User-Agent": UA}),
        ("corsproxy.io", f"https://corsproxy.io/?{enc}", {"User-Agent": UA}),
        ("thingproxy", f"https://thingproxy.freeboard.io/fetch/{key_api}",
         {"User-Agent": UA}),
        ("jina", f"https://r.jina.ai/{key_api}", {"User-Agent": UA}),
        ("whateverorigin", f"http://www.whateverorigin.org/get?url={enc}",
         {"User-Agent": UA}),
        ("textance", f"https://api.cors.lol/?url={enc}", {"User-Agent": UA}),
    ]
    for label, url, hdrs in tests:
        try:
            r = requests.get(url, headers=hdrs, timeout=30)
        except Exception as e:
            print(f"[*] {label}: error {str(e)[:80]}", flush=True)
            continue
        body = r.content
        print(f"[*] {label}: HTTP {r.status_code} len={len(body)} "
              f"ctype={r.headers.get('content-type')} server={r.headers.get('server')}",
              flush=True)
        if r.status_code == 200:
            print(f"    body[:200]: {body[:200]!r}", flush=True)
            if len(body) in (16, 32):
                print(f"[+] KEY raw: {body.hex()}", flush=True)
                with open("hls_key.txt", "w") as f:
                    f.write(body.hex())
                return 0
    return 1


def main():
    playlist, cookie, uuid, cf, data = resolve()
    print(f"[*] uuid={uuid}", flush=True)
    print(f"[*] cloudfront={cf}", flush=True)

    s = requests.Session()
    segment, iv = b"", bytes(16)

    if not cf:
        print("[*] Playlist URL nahi (cookie expired / token nahi) — "
              "sirf key endpoints test honge, validation skip.", flush=True)
        return hunt_key_only(s, cookie, uuid)

    # audio/video playlist -> segments
    master = s.get(cf, headers=base_headers(cookie), timeout=30).text
    variant = None
    for ln in master.splitlines():
        if ln and not ln.startswith("#"):
            variant = ln.strip()
            break
    media_url = cf.rsplit("/", 1)[0] + "/" + variant
    media = s.get(media_url, headers=base_headers(cookie), timeout=30).text
    segs, iv_hex = [], None
    for ln in media.splitlines():
        if ln.startswith("#EXT-X-KEY") and "IV=" in ln:
            iv_hex = ln.split("IV=")[1].split(",")[0].strip('"')
        elif ln and not ln.startswith("#"):
            segs.append(ln.strip())
    seg_url = media_url.rsplit("/", 1)[0] + "/" + segs[0]
    segment = s.get(seg_url, headers=base_headers(cookie), timeout=30).content
    iv = bytes.fromhex((iv_hex or "").replace("0x", "")) if iv_hex else bytes(16)
    print(f"[*] probe segment: {len(segment)} bytes, iv={iv.hex()}", flush=True)
    print(f"[*] segment head: {segment[:16].hex()}", flush=True)

    key_api = (f"https://api.penpencil.co/v1/videos/get-hls-key"
               f"?videoKey={uuid}&key=enc.key")

    candidates = []  # (label, url, headers, post_process)

    # 1) penpencil API with PW client headers
    candidates += [
        ("penpencil + PW headers", key_api, pw_headers(cookie), None),
        ("penpencil + app headers", key_api, base_headers(cookie), None),
        ("penpencil no cookie", key_api,
         {k: v for k, v in pw_headers(cookie).items()
          if k.lower() not in ("cookie", "origin")}, None),
    ]

    # 2) CloudFront direct
    for path in (f"/{uuid}/enc.key", f"/{uuid}/key", f"/{uuid}/hls/enc.key",
                 f"/{uuid}/hls/720/enc.key", f"/{uuid}/720/enc.key"):
        candidates.append((f"cloudfront {path}", f"https://{cf.split('/')[2]}{path}",
                           base_headers(cookie), None))

    # 3) player proxy
    for path in (f"/stream/{uuid}/enc.key", f"/stream/{uuid}/key",
                 f"/stream/{uuid}/hls/720/enc.key"):
        candidates.append((f"player {path}", f"https://player.studyratna.org{path}",
                           base_headers(cookie), None))

    # 4) public proxies (alag egress IP)
    enc = quote(key_api, safe="")
    proxies = [
        ("allorigins", f"https://api.allorigins.win/raw?url={enc}"),
        ("codetabs", f"https://api.codetabs.com/v1/proxy?quest={enc}"),
        ("corsproxy.io", f"https://corsproxy.io/?{enc}"),
        ("thingproxy", f"https://thingproxy.freeboard.io/fetch/{key_api}"),
        ("jina", f"https://r.jina.ai/{key_api}"),
    ]
    for label, purl in proxies:
        candidates.append((f"proxy:{label}", purl,
                           {"User-Agent": UA, "Accept": "*/*"}, "proxy"))

    found = None
    for label, url, hdrs, kind in candidates:
        try:
            r = requests.get(url, headers=hdrs, timeout=30,
                             allow_redirects=True)
        except Exception as e:
            print(f"[*] {label}: error {str(e)[:80]}", flush=True)
            continue

        body = r.content
        print(f"[*] {label}: HTTP {r.status_code} len={len(body)} "
              f"ctype={r.headers.get('content-type')}", flush=True)

        if r.status_code != 200:
            continue

        key = None
        if len(body) in (16, 32):
            key = body
        else:
            # hex text? (32 chars)
            txt = body.strip().decode("utf-8", "replace")
            if len(txt) == 32 and all(c in "0123456789abcdefABCDEF" for c in txt):
                key = bytes.fromhex(txt)
        if not key:
            continue

        ok = validate_key(key, segment, iv)
        print(f"    -> key candidate {key.hex()} valid={ok}", flush=True)
        if ok:
            found = key
            print(f"\n[+] KEY MIL GAYI via {label}: {key.hex()}", flush=True)
            with open("hls_key.txt", "w") as f:
                f.write(key.hex())
            break

    if not found:
        print("\n[-] Koi bhi raaste se valid key nahi mili.", flush=True)
        print("    Ye endpoint non-India IP par geo-blocked lagta hai.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
