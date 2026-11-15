"""
Fast, progress-visible HLS downloader (India proxy ke liye).

Purana engine `download_hls` har segment par default 45s timeout leta hai; free
proxy par ye 100 segments x 45s = 75 min tak hang kar sakta hai (aur log kuch
nahi dikhata). Yahan:

  * chhota connect/read timeout (8s) + 1 retry -> fail fast
  * parallel workers (default 8)
  * har 10 segments par ek progress line (EXACT counts)
  * pehle 5 segment sequential "preflight" -> proxy sach me data de raha hai ya nahi
  * retry: fail hue segments ko sirf ek baar dobara (alag pool se)
  * jo mile unse file banao (partial bhi), coverage report ke saath

Output: (path, stats dict). Caller decide karta hai success/partial.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


from hls_engine import (HlsSession, aes_decrypt, is_media_playlist, iv_for,
                        parse_master, parse_media, pick_variant)

UA_HDRS = {}


def _preflight(session: HlsSession, segments, log) -> int:
    """Pehle kuch segments sequential try karo - proxy ka asli test."""
    ok = 0
    for seg in segments[:5]:
        try:
            r = session.get(seg.url, timeout=10)
            if r.status_code == 200 and len(r.content) > 1000:
                ok += 1
            else:
                log(f"[fast] preflight seg -> HTTP {r.status_code} len={len(r.content)}")
        except Exception as exc:
            log(f"[fast] preflight seg fail: {type(exc).__name__}: {str(exc)[:70]}")
    log(f"[fast] preflight: {ok}/5 segments OK")
    return ok


def download(url: str, session: HlsSession, out_base: str = "downloaded_lecture",
             workers: int = 8, log=print, timeout: int = 10,
             min_bytes: int = 500_000, max_seconds: int = 1800) -> dict:
    started = time.time()
    res = {"ok": False, "path": None, "bytes": 0, "segments_total": 0,
           "segments_ok": 0, "segments_fail": 0, "kind": "", "variant": "",
           "error": "", "preflight": 0, "proxy": session.proxy or "direct",
           "seconds": 0.0}

    log(f"[fast] playlist: {url[:110]} (proxy={session.proxy or 'direct'})")
    r = session.get(url, timeout=20)
    text, base = r.text, (r.url or url)
    if not is_media_playlist(text):
        variants = parse_master(text, base)
        if not variants:
            res["error"] = "playlist parse fail"
            return res
        v = pick_variant(variants)
        res["variant"] = f"{v.resolution}@{v.bandwidth // 1000}k"
        log(f"[fast] variant: {res['variant']}")
        r = session.get(v.url, timeout=20)
        text, base = r.text, (r.url or v.url)

    segments, _, init = parse_media(text, base)
    if not segments:
        res["error"] = "segments nahi mile"
        return res
    res["segments_total"] = len(segments)
    res["kind"] = "mp4" if (init or ".m4s" in segments[0].url) else "ts"
    log(f"[fast] {len(segments)} segments, kind={res['kind']}, "
        f"key={'YES' if segments[0].key else 'NO'}")

    res["preflight"] = _preflight(session, segments, log)
    if res["preflight"] == 0:
        res["error"] = "preflight fail - proxy data nahi de raha"
        res["seconds"] = round(time.time() - started, 1)
        return res

    key_cache: dict[str, bytes] = {}
    got: dict[int, bytes] = {}
    failed: list[int] = []

    def fetch(i_seg):
        i, seg = i_seg
        for attempt in (1, 2):
            try:
                rr = session.get(seg.url, timeout=timeout if attempt == 1 else 20)
                data = rr.content
                if seg.key and (seg.key.get("method") or "").upper() == "AES-128":
                    uri = seg.key.get("uri")
                    if uri not in key_cache:
                        key_cache[uri] = session.get(uri, timeout=15).content
                    data = aes_decrypt(data, key_cache[uri], iv_for(seg.key, seg.seq))
                return i, data
            except Exception:
                if attempt == 2:
                    return i, None
        return i, None

    last_log = 0.0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(fetch, (i, s)) for i, s in enumerate(segments)]
        for done, fut in enumerate(as_completed(futs), 1):
            i, data = fut.result()
            if data is None:
                failed.append(i)
            else:
                got[i] = data
            now = time.time()
            if done % 10 == 0 or done == len(futs) or now - last_log > 30:
                last_log = now
                mb = sum(len(d) for d in got.values()) / 1048576
                rate = done / max(1e-6, now - started)
                log(f"[fast] {done}/{len(futs)} seg | {mb:.1f} MB | fail={len(failed)} "
                    f"| {rate:.1f} seg/s | {round(now - started)}s")
            if now - started > max_seconds:
                log(f"[fast] time limit {max_seconds}s - jo mila usse bana rahe hain")
                break

    # retry failed once, sequentially with longer timeout
    if failed:
        log(f"[fast] {len(failed)} segments retry…")
        still: list[int] = []
        for i in failed:
            i2, data = fetch((i, segments[i]))
            if data is None:
                still.append(i)
            else:
                got[i2] = data
        failed = still
        log(f"[fast] retry ke baad bhi fail: {len(failed)}")

    out = Path(f"{out_base}.{res['kind']}")
    with out.open("wb") as fh:
        if init and init.get("uri"):
            try:
                fh.write(session.get(init["uri"], timeout=20).content)
            except Exception:
                pass
        for i in sorted(got):
            fh.write(got[i])

    size = out.stat().st_size if out.exists() else 0
    res.update({
        "path": str(out), "bytes": size,
        "segments_ok": len(got), "segments_fail": len(failed),
        "seconds": round(time.time() - started, 1),
    })
    coverage = len(got) / max(1, len(segments))
    res["coverage"] = round(coverage, 3)
    res["ok"] = size >= min_bytes and coverage >= 0.9
    res["error"] = "" if res["ok"] else (
        f"{len(failed)} segments fail, coverage {coverage * 100:.0f}%")
    log(f"[fast] DONE {out} {size / 1048576:.1f} MB | {len(got)}/{len(segments)} seg "
        f"| fail={len(failed)} | {res['seconds']}s | ok={res['ok']}")
    return res
