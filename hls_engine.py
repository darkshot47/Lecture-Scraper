"""
Chhota par poora HLS downloader - poora traffic Indian proxy ke through.

yt-dlp kyun kaafi nahi tha
--------------------------
yt-dlp proxy use kar sakta hai, par jab playlist/segments proxy se fail hote hain
to uska retry/fragment logic opaque hota hai aur key/session cookies handle karna
mushkil. Isliye yahan apna engine hai:

  1. master.m3u8  -> variant chuno (bandwidth ke hisaab se, 720p preferred)
  2. variant m3u8 -> segments + EXT-X-KEY (AES-128) + EXT-X-MAP init
  3. key URL      -> proxy ke through fetch (India IP + session cookies)
  4. segments     -> parallel download, AES-128 decrypt (pycryptodome)
  5. output       -> fMP4 (.mp4) ya MPEG-TS (.ts) concatenate

Sab kuch `session` (proxy+cookies) se hota hai. Koi DRM bypass nahi: jo key
player khud maangta hai, wahi app ke cookies ke saath maangi jaati hai.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BANDWIDTH_RE = re.compile(r"#EXT-X-STREAM-INF:(.*)")
ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
KEY_RE = re.compile(r"#EXT-X-KEY:(.*)")
MAP_RE = re.compile(r"#EXT-X-MAP:(.*)")
MEDIA_SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")


class HlsError(Exception):
    pass


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""


@dataclass
class Segment:
    url: str
    seq: int
    key: dict | None = None
    init: dict | None = None


@dataclass
class FetchStats:
    ok: int = 0
    failed: int = 0
    bytes: int = 0
    proxy: str = ""
    hosts: set = field(default_factory=set)


def attrs(text: str) -> dict:
    out = {}
    for k, v in ATTR_RE.findall(text or ""):
        out[k.strip().upper()] = v.strip().strip('"')
    return out


class HlsSession:
    """proxy + cookies ke saath ek requests session."""

    def __init__(self, proxy: str | None = None, cookies: str | None = None,
                 referer: str = "https://app.studyratna.org/"):
        self.proxy = proxy
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": referer,
            "Origin": referer.rstrip("/"),
            "Accept": "*/*",
        })
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        if cookies:
            for part in cookies.split(";"):
                if "=" in part:
                    k, _, v = part.partition("=")
                    self.s.cookies.set(k.strip(), v.strip())

    def get(self, url: str, timeout: float = 30, retries: int = 2) -> requests.Response:
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.s.get(url, timeout=timeout, allow_redirects=True)
                if r.status_code == 200:
                    return r
                last = HlsError(f"HTTP {r.status_code} {url[:110]}")
                if r.status_code in (401, 403, 404):
                    break
            except Exception as exc:
                last = exc
            time.sleep(0.6 * (attempt + 1))
        raise HlsError(str(last))


# ---------------------------------------------------------------------------
# Playlist parsing
# ---------------------------------------------------------------------------

def parse_master(text: str, base: str) -> list[Variant]:
    variants: list[Variant] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        m = BANDWIDTH_RE.match(line)
        if not m:
            continue
        a = attrs(m.group(1))
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not nxt or nxt.startswith("#"):
            continue
        variants.append(Variant(
            url=urljoin(base, nxt),
            bandwidth=int(a.get("BANDWIDTH") or 0),
            resolution=a.get("RESOLUTION", ""),
            codecs=a.get("CODECS", ""),
        ))
    return variants


def pick_variant(variants: list[Variant], prefer: str = "720") -> Variant:
    if not variants:
        raise HlsError("master me koi variant nahi mila")
    def score(v: Variant) -> tuple:
        res = v.resolution or ""
        height = 0
        m = re.search(r"x(\d+)$", res)
        if m:
            height = int(m.group(1))
        # 720p ke qareeb best, usse zyada bhi theek (proxy bandwidth ke liye 720 prefer)
        closeness = 0 if height == int(prefer) else (1 if abs(height - int(prefer)) <= 180 else 2)
        return (closeness, -v.bandwidth)
    return sorted(variants, key=score)[0]


def parse_media(text: str, base: str, media_sequence: int = 0
                ) -> tuple[list[Segment], dict | None, dict | None]:
    segments: list[Segment] = []
    current_key: dict | None = None
    current_map: dict | None = None
    seq = media_sequence
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-KEY:"):
            a = attrs(line.split(":", 1)[1])
            method = (a.get("METHOD") or "NONE").upper()
            if method == "NONE":
                current_key = None
            else:
                current_key = {
                    "method": method,
                    "uri": urljoin(base, a.get("URI", "")) if a.get("URI") else "",
                    "iv": a.get("IV", ""),
                    "keyformat": a.get("KEYFORMAT", "identity"),
                }
            continue
        if line.startswith("#EXT-X-MAP:"):
            a = attrs(line.split(":", 1)[1])
            if a.get("URI"):
                current_map = {"uri": urljoin(base, a["URI"]),
                               "bytestart": a.get("BYTERANGE", "")}
            continue
        if line.startswith("#"):
            continue
        segments.append(Segment(url=urljoin(base, line), seq=seq,
                                key=current_key, init=current_map))
        seq += 1
    return segments, current_key, current_map


def is_media_playlist(text: str) -> bool:
    return "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text


# ---------------------------------------------------------------------------
# AES-128 decrypt
# ---------------------------------------------------------------------------

def aes_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError as exc:  # pragma: no cover
        raise HlsError("pycryptodome chahiye: pip install pycryptodome") from exc
    if len(key) != 16:
        raise HlsError(f"AES key length galat: {len(key)}")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plain = cipher.decrypt(data + (b"\0" * (-len(data) % 16)))
    try:
        return unpad(plain, 16)
    except ValueError:
        return plain  # kuch encoders padding nahi karte; raw rakho


def iv_for(key_info: dict, seq: int) -> bytes:
    iv = (key_info or {}).get("iv")
    if iv:
        iv = iv.strip()
        if iv.lower().startswith("0x"):
            iv = iv[2:]
        try:
            return bytes.fromhex(iv).rjust(16, b"\0")[:16]
        except ValueError:
            pass
    # RFC 8216: IV na ho to sequence number hi IV hota hai
    return int(seq).to_bytes(16, "big")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def fetch_key(session: HlsSession, key_info: dict, seq: int, cache: dict,
              stats: FetchStats) -> bytes:
    uri = key_info.get("uri")
    if not uri:
        raise HlsError("EXT-X-KEY me URI nahi")
    if uri in cache:
        return cache[uri]
    r = session.get(uri, timeout=20)
    key = r.content
    cache[uri] = key
    stats.hosts.add(uri.split("/")[2])
    return key


def fetch_segment(session: HlsSession, seg: Segment, key_cache: dict,
                  stats: FetchStats) -> bytes:
    r = session.get(seg.url, timeout=45)
    data = r.content
    stats.bytes += len(data)
    if seg.key and (seg.key.get("method") or "").upper() == "AES-128":
        key = fetch_key(session, seg.key, seg.seq, key_cache, stats)
        data = aes_decrypt(data, key, iv_for(seg.key, seg.seq))
    return data


def download_hls(url: str, session: HlsSession, out_base: str = "downloaded_lecture",
                 workers: int = 4, log=print, max_segments: int | None = None,
                 expect_min_bytes: int = 200_000) -> dict:
    """master ya media playlist -> file. Return: {ok, path, bytes, segments, error}."""
    stats = FetchStats(proxy=session.proxy or "direct")
    result = {"ok": False, "path": None, "bytes": 0, "segments": 0,
              "error": "", "variant": "", "kind": ""}

    log(f"[hls] playlist fetch: {url[:120]}")
    r = session.get(url, timeout=30)
    text = r.text
    base = r.url or url

    variants: list[Variant] = []
    if not is_media_playlist(text):
        variants = parse_master(text, base)
        log(f"[hls] {len(variants)} variants: "
            + ", ".join(f"{v.resolution or '?'}@{v.bandwidth // 1000}k" for v in variants[:6]))
        if not variants:
            # kabhi master ke andar direct media playlist hoti hai
            raise HlsError("playlist parse nahi hui (na master na media)")
        v = pick_variant(variants)
        result["variant"] = f"{v.resolution or '?'} ({v.bandwidth} bps)"
        log(f"[hls] chosen: {v.resolution} {v.bandwidth}bps -> {v.url[:120]}")
        r = session.get(v.url, timeout=30)
        text = r.text
        base = r.url or v.url

    media_seq = 0
    m = MEDIA_SEQ_RE.search(text)
    if m:
        media_seq = int(m.group(1))
    segments, _, init = parse_media(text, base, media_seq)
    if not segments:
        raise HlsError("media playlist me segments nahi mile")
    if max_segments:
        segments = segments[:max_segments]
    log(f"[hls] {len(segments)} segments | key={'YES' if segments[0].key else 'NO'} "
        f"| init={'YES' if init else 'NO'} | seq={media_seq}")

    kind = "mp4" if (init or ".m4s" in segments[0].url or "fmp4" in text.lower()) else "ts"
    result["kind"] = kind
    out = Path(f"{out_base}.{kind}")

    key_cache: dict[str, bytes] = {}
    chunks: dict[int, bytes] = {}
    failed: list[int] = []

    def work(index: int, seg: Segment) -> tuple[int, bytes | None]:
        try:
            return index, fetch_segment(session, seg, key_cache, stats)
        except Exception as exc:
            log(f"[hls] segment {index} fail: {type(exc).__name__}: {str(exc)[:90]}")
            return index, None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, i, s) for i, s in enumerate(segments)]
        done = 0
        for fut in as_completed(futures):
            idx, data = fut.result()
            done += 1
            if data is None:
                failed.append(idx)
            else:
                chunks[idx] = data
            if done % 25 == 0 or done == len(segments):
                got = sum(len(c) for c in chunks.values())
                log(f"[hls]   {done}/{len(segments)} segments, "
                    f"{got / 1048576:.1f} MB, fail={len(failed)}, "
                    f"{done / max(1e-6, time.time() - started):.1f} seg/s")

    if not chunks:
        result["error"] = "koi bhi segment download nahi hua"
        result["stats"] = stats
        return result

    with out.open("wb") as fh:
        if init and init.get("uri"):
            try:
                init_data = session.get(init["uri"], timeout=30).content
                fh.write(init_data)
                log(f"[hls] init segment {len(init_data)} bytes")
            except Exception as exc:
                log(f"[hls] init fail: {exc}")
        for i in sorted(chunks):
            fh.write(chunks[i])

    size = out.stat().st_size
    result.update({
        "ok": size >= expect_min_bytes and len(failed) == 0,
        "path": str(out),
        "bytes": size,
        "segments": len(chunks),
        "failed": len(failed),
        "error": ("" if not failed else f"{len(failed)} segments fail hue"),
        "stats": stats,
    })
    log(f"[hls] likha: {out} ({size / 1048576:.1f} MB, {len(chunks)} segments, "
        f"fail={len(failed)}, proxy={stats.proxy})")

    # partial par bhi kaam chalta hai - par honest flag
    if size < expect_min_bytes:
        result["ok"] = False
        result["error"] = f"output bahut chhoti ({size} bytes) - stream block ho raha hai"
    return result
