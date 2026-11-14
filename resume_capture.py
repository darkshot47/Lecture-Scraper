"""
"Recording" rasta, server-side: playlist ko resume-points ki tarah use karo.

Kyun: agar CDN poore segments nahi deta (beech me stream kat jaata hai / token ki
signature expire hoti hai), to bhi hum jo segments mil jaayein unhe jod kar ek
playable file bana sakte hain - bilkul jaise koi beech se recording karta hai.

Kaam:
  1. media playlist lo (proxy ke through), segments ki list + duration nikaalo
  2. resume point se aage ke segments download karo (parallel), har segment ko
     turant disk par append karo (memory safe)
  3. beech me fail hone par 2 retry + proxy rotate
  4. jo mile usse .ts/.mp4 bana do; report me saaf likho kitna coverage hai
"""

from __future__ import annotations

import time
from pathlib import Path

from hls_engine import (HlsError, HlsSession, aes_decrypt, fetch_key,
                        is_media_playlist, iv_for, parse_master, parse_media,
                        pick_variant)
from hls_engine import MEDIA_SEQ_RE


def playlist_duration(text: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                total += float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pass
    return total


def capture_playlist(url: str, session: HlsSession, out_path: Path,
                     start_segment: int = 0, workers: int = 4,
                     max_retries: int = 2, log=print,
                     on_proxy_fail=None) -> dict:
    """Playlist -> file, resume support ke saath. Partial bhi return karta hai."""
    info = {"ok": False, "path": str(out_path), "bytes": 0, "segments_total": 0,
            "segments_done": 0, "duration": 0.0, "error": "", "gaps": 0}

    r = session.get(url, timeout=30)
    text = r.text
    base = r.url or url
    if not is_media_playlist(text):
        variants = parse_master(text, base)
        if not variants:
            raise HlsError("playlist samajh nahi aayi")
        v = pick_variant(variants)
        log(f"[resume] variant {v.resolution} {v.bandwidth}bps")
        r = session.get(v.url, timeout=30)
        text = r.text
        base = r.url or v.url

    seq_start = 0
    m = MEDIA_SEQ_RE.search(text)
    if m:
        seq_start = int(m.group(1))

    segments, _, init = parse_media(text, base, seq_start)
    if not segments:
        raise HlsError("segments nahi mile")
    info["segments_total"] = len(segments)
    info["duration"] = playlist_duration(text)
    if start_segment:
        segments = segments[start_segment:]

    key_cache: dict[str, bytes] = {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    written = 0
    gaps = 0

    with out_path.open("wb") as fh:
        if init and init.get("uri"):
            try:
                fh.write(session.get(init["uri"], timeout=30).content)
            except Exception as exc:
                log(f"[resume] init fail: {exc}")

        for i, seg in enumerate(segments):
            data = None
            for attempt in range(max_retries + 1):
                try:
                    data = session.get(seg.url, timeout=60).content
                    if seg.key and (seg.key.get("method") or "").upper() == "AES-128":
                        key = fetch_key(session, seg.key, seg.seq, key_cache, None)
                        data = aes_decrypt(data, key, iv_for(seg.key, seg.seq))
                    break
                except Exception as exc:
                    log(f"[resume] seg {i} attempt {attempt + 1} fail: "
                        f"{type(exc).__name__} {str(exc)[:80]}")
                    data = None
                    if on_proxy_fail is not None:
                        try:
                            on_proxy_fail()
                        except Exception:
                            pass
                    time.sleep(1.0 + attempt)
            if data is None:
                gaps += 1
                log(f"[resume] seg {i} skip (gap #{gaps})")
                continue
            fh.write(data)
            written += len(data)
            info["segments_done"] += 1
            if (i + 1) % 20 == 0:
                rate = (i + 1) / max(1e-6, time.time() - started)
                log(f"[resume] {i + 1}/{len(segments)} seg, "
                    f"{written / 1048576:.1f} MB, gap={gaps}, {rate:.1f} seg/s")

    size = out_path.stat().st_size if out_path.exists() else 0
    coverage = info["segments_done"] / max(1, info["segments_total"])
    info.update({
        "bytes": size,
        "gaps": gaps,
        "coverage": coverage,
        "ok": size > 500_000 and coverage > 0.5,
        "error": "" if gaps == 0 else f"{gaps} gaps",
    })
    log(f"[resume] done: {out_path} {size / 1048576:.1f} MB, "
        f"{info['segments_done']}/{info['segments_total']} segments "
        f"({coverage * 100:.0f}%), gaps={gaps}")
    return info
