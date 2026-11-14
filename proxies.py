"""
Indian proxy pool: discovery + geo verification + health checks.

Kyun zaroori hai
----------------
GitHub runner US datacenter me chalta hai. studyratna/penpencil ka CDN India ke
bahar 403/502 deta hai (logs me saaf dikha). Isliye streaming requests ek
**India-based proxy** ke through bhejni padti hain.

Kya karta hai
-------------
1. Kai public sources se proxy list uthata hai (proxifly, monosans, proxy-list.download,
   geonode, proxyscrape, TheSpeedX, hideip.me).
2. Har proxy ko parallel verify karta hai: country == IN hona chahiye
   (ip-api.com se, request proxy ke through jaati hai, isliye rate-limit per proxy
   IP lagta hai - hamare liye theek).
3. Jo IN nikle unka latency + studyratna hosts tak pahunch (TLS/CONNECT) test karta hai.
4. Best proxies ko cache karta hai (proxy_cache.json) taaki dobara sab scan na karna pade.

Note: ye koi paid/private VPN nahi - free public proxies hain, isliye sthirta kam
hoti hai. Isi liye hum pool rakhte hain aur fail hone par agla try karte hain.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import requests

TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "12"))
CACHE = Path("proxy_cache.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Country-specific (best signal) + global lists (fallback, geo-verified anyway).
SOURCES_IN = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/IN/data.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&country=in&protocol=http&proxy_format=ipport&format=text",
    "https://openproxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&country=IN",
    "https://www.proxy-list.download/api/v1/get?type=http&country=IN",
    "https://www.proxy-list.download/api/v1/get?type=socks5&country=IN",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&country=IN",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&country=IN",
]

SOURCES_GLOBAL = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
]

GEO_ENDPOINTS = [
    "http://ip-api.com/json/?fields=countryCode,query,isp",
    "https://ipwho.is/",
    "https://ipinfo.io/json",
]

HOSTS_TO_TEST = [
    "https://app.studyratna.org/",
    "https://api-lite.studyratna.org/V1/notifications",
    "https://player.studyratna.org/",
]

LINE_RE = re.compile(r"^(?:([a-z0-9]+)://)?([0-9a-zA-Z._-]+):(\d{2,5})$")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: float = 25) -> str:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        if r.status_code != 200:
            return ""
        return r.text
    except Exception:
        return ""


def parse_proxies(text: str) -> list[str]:
    """Mixed text (plain lines, json, csv) -> ['http://ip:port', ...]."""
    out: list[str] = []
    if not text:
        return out
    stripped = text.strip()

    # geonode JSON
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            items = data.get("data") if isinstance(data, dict) else data
            for item in items or []:
                if isinstance(item, dict):
                    ip = item.get("ip") or item.get("address")
                    port = item.get("port")
                    proto = (item.get("protocols") or ["http"])
                    if isinstance(proto, list):
                        proto = proto[0] if proto else "http"
                    if ip and port:
                        scheme = "socks5" if "socks5" in str(proto).lower() else "http"
                        out.append(f"{scheme}://{ip}:{port}")
            if out:
                return out
        except Exception:
            pass

    # proxifly country file: "socks5://ip:port" / plain lines / csv with headers
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(("ip,", "host,", "proxy,")):
            continue
        piece = line.split(",")[0].strip()
        m = LINE_RE.match(piece)
        if not m:
            continue
        scheme, host, port = m.group(1) or "http", m.group(2), m.group(3)
        if scheme not in ("http", "https", "socks5", "socks4"):
            scheme = "http"
        if scheme == "https":
            scheme = "http"
        out.append(f"{scheme}://{host}:{port}")
    return out


def discover(country_only: bool = True, log=print) -> list[str]:
    """Saare sources se proxies uthao (dedup, order preserved)."""
    found: list[str] = []
    seen: set[str] = set()
    sources = list(SOURCES_IN)
    if not country_only:
        sources += SOURCES_GLOBAL
    for src in sources:
        text = _fetch(src)
        items = parse_proxies(text)
        new = [p for p in items if p not in seen]
        seen.update(new)
        found.extend(new)
        log(f"[proxy] {len(new):4d} from {src.split('/')[2]}{src[-40:]}")
    # global lists: bahut bade hote hain, random sample lo (geo-verify kharchila hai)
    if not country_only:
        random.shuffle(found)
    return found


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def proxies_for(proxy: str) -> dict:
    if proxy.startswith("socks"):
        return {"http": proxy, "https": proxy}
    return {"http": proxy, "https": proxy}


def geo_check(proxy: str, timeout: float = TIMEOUT) -> dict | None:
    """Proxy ke through country check. IN ho to details, warna None."""
    for url in GEO_ENDPOINTS:
        try:
            r = requests.get(url, proxies=proxies_for(proxy), timeout=timeout,
                             headers={"User-Agent": UA})
            if r.status_code != 200:
                continue
            if "ipwho.is" in url:
                info = r.json()
                code = str(info.get("country_code", "")).upper()
                info = {"country": code, "ip": info.get("ip"),
                        "isp": (info.get("connection") or {}).get("isp")}
            elif "ipinfo.io" in url:
                info = r.json()
                code = str(info.get("country", "")).upper()
                info = {"country": code, "ip": info.get("ip"), "isp": info.get("org")}
            else:
                info = r.json()
                code = str(info.get("countryCode", "")).upper()
            if code == "IN":
                info["country"] = "IN"
                info["proxy"] = proxy
                return info
            # IN nahi mila: agla endpoint try karo (ip-api kabhi rate-limit/false
            # negative deta hai, isliye fallback zaroori hai)
        except Exception:
            continue
    return None


def host_check(proxy: str, timeout: float = TIMEOUT) -> dict:
    """IN proxy se studyratna hosts tak pahunch test (status codes matter)."""
    result = {"proxy": proxy, "reachable": {}, "ok": False}
    for url in HOSTS_TO_TEST:
        try:
            r = requests.get(url, proxies=proxies_for(proxy), timeout=timeout,
                             headers={"User-Agent": UA}, allow_redirects=False)
            result["reachable"][url] = r.status_code
        except Exception as exc:
            result["reachable"][url] = f"ERR:{type(exc).__name__}"
    codes = [v for v in result["reachable"].values() if isinstance(v, int)]
    result["ok"] = any(v in (200, 301, 302, 401, 403, 404) for v in codes)
    return result


def validate(proxies: Iterable[str], workers: int = 64, log=print,
             limit: int | None = None) -> list[dict]:
    """IN proxies dhoondo + unki health. Latency ke hisaab se sorted."""
    pool = list(proxies)
    if limit:
        pool = pool[:limit]
    log(f"[proxy] validating {len(pool)} proxies with {workers} workers...")

    good: list[dict] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(geo_check, p): p for p in pool}
        done = 0
        for fut in as_completed(futures):
            done += 1
            if done % 100 == 0:
                log(f"[proxy]   checked {done}/{len(pool)} -> {len(good)} IN")
            info = fut.result()
            if info:
                log(f"[proxy]   IN: {info['proxy']} ({info.get('isp') or '?'})")
                good.append(info)

    if not good:
        log("[proxy] koi IN proxy nahi mila is round me.")
        return []

    # health check top candidates
    log(f"[proxy] checking studyratna reachability for {len(good)} IN proxies...")
    health: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(32, len(good))) as ex:
        for res in ex.map(host_check, [g["proxy"] for g in good]):
            health.append(res)
    by_proxy = {h["proxy"]: h for h in health}
    for g in good:
        h = by_proxy.get(g["proxy"], {})
        g["reachable"] = h.get("reachable", {})
        g["host_ok"] = h.get("ok", False)

    good.sort(key=lambda g: (not g.get("host_ok"), -len([
        v for v in g.get("reachable", {}).values() if isinstance(v, int)
        and v < 500])))
    log(f"[proxy] {len(good)} IN proxies, {sum(1 for g in good if g.get('host_ok'))} "
        f"studyratna tak pahunch rahe hain (took {time.time() - started:.0f}s)")
    return good


def save_cache(items: list[dict]) -> None:
    try:
        CACHE.write_text(json.dumps({"time": time.time(), "items": items}, indent=1),
                         encoding="utf-8")
    except Exception:
        pass


def load_cache(max_age: int = 1800) -> list[dict]:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - data.get("time", 0) < max_age:
            return data.get("items", [])
    except Exception:
        pass
    return []


def ensure_pool(min_count: int = 3, rounds: int = 3, log=print) -> list[dict]:
    """Cache se pool do; kaafi na ho to multi-round scan (har round naya sample).

    Free pool me IN proxies ~5-10% hi hote hain, isliye ek round me 3-5 milte hain.
    Kai round karne se 10-20 tak pahunch jaate hain - zyada chances stream ke liye.
    """
    cached = load_cache()
    usable = [c for c in cached if c.get("host_ok")]
    if len(usable) >= min_count:
        log(f"[proxy] cache se {len(usable)} working IN proxies")
        return usable

    good: list[dict] = []
    seen: set[str] = set()
    for r in range(1, rounds + 1):
        log(f"[proxy] === round {r}/{rounds} ===")
        found = discover(country_only=(r == 1), log=log)
        found = [p for p in found if p not in seen]
        if r > 1:
            random.shuffle(found)
            # global list bahut badi hai; har round ek naya sample
            found = found[:1500]
        seen.update(found)
        if not found:
            continue
        batch = validate(found, log=log)
        for b in batch:
            if all(b["proxy"] != g["proxy"] for g in good):
                good.append(b)
        log(f"[proxy] round {r}: +{len(batch)} -> total {len(good)} "
            f"({sum(1 for g in good if g.get('host_ok'))} reachable)")
        if len([g for g in good if g.get("host_ok")]) >= min_count + 4:
            break
    good.sort(key=lambda g: (not g.get("host_ok"),))
    save_cache(good)
    return good
