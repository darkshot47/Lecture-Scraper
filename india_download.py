"""
India-proxy workflow runner.

Flow (har step report me likha jaata hai):
  1. Token decrypt        -> lecture, ids, videoUrl, embedded cookie + expiry
  2. India proxy pool     -> geo-verify (IN) + studyratna reachability
  3. Manifest refresh     -> app ka apna endpoint, fresh signed URL + cookie
  4. HLS via India proxy  -> master -> variant -> segments -> MP4 (AES-128 in-house)
  5. Resume/partial       -> jo segments mile unse bhi playable file
  6. Video na bane to     -> honest FAIL + poora report (jhoothi success nahi)

Bas! Koi browser/WebView/device ki zaroorat nahi - poora kaam runner par.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from base64 import b64decode
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import proxies
import resume_capture
from hls_engine import HlsSession, download_hls

WEB_TOKEN_KEY_SEED = "5d90298d9598093cf8cb4b55434de5bc"
API_KEY_SEED = "939567f60cc4728ee81fc28fae7458af"
MANIFEST_API = "https://api-lite.studyratna.org/v1/pw/playback-manifest"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEBUG = Path("debug")
REPORT = Path("run_report.md")
NOTES: list[str] = []
START = time.monotonic()


# ---------------------------------------------------------------------------
# report helpers
# ---------------------------------------------------------------------------

def redact(text: object) -> str:
    s = str(text)
    s = re.sub(r"(playerToken=)[^&\s\"'<>]+", r"\1<REDACTED>", s, flags=re.I)
    for name in ("cookie", "authorization", "x-init-data", "signature",
                 "cloudfront-signature", "cloudfront-policy"):
        s = re.sub(rf"(\b{re.escape(name)}\s*[:=]\s*)([^\r\n,;]+)", r"\1<REDACTED>",
                   s, flags=re.I)
    s = re.sub(r"\b[0-9a-f]{64,}(?::[0-9a-f]{64,})?\b", "<REDACTED>", s, flags=re.I)
    return s


def log(msg: object) -> None:
    print(f"[{time.monotonic() - START:6.1f}s] {redact(msg)}", flush=True)


def note(msg: object) -> None:
    line = redact(msg)
    NOTES.append(line)
    log(line)


def write_report(status: str, extra: str = "") -> None:
    lines = [
        "# India-proxy run report", "",
        f"- result: **{status}**",
        f"- time: {datetime.now(timezone.utc).isoformat()}",
        f"- target host: {urlparse(os.environ.get('TARGET_URL', '')).netloc or '(none)'}",
        "", "## Findings", "",
    ]
    lines += [f"- {n}" for n in NOTES] or ["- (none)"]
    if extra:
        lines += ["", "## Output", "", "```", redact(extra)[:4000], "```"]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n===== REPORT START =====", flush=True)
    print(REPORT.read_text(encoding="utf-8"), flush=True)
    print("===== REPORT END =====", flush=True)


def ts(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def decrypt_token(url: str) -> dict:
    try:
        token = unquote((parse_qs(urlparse(url).query).get("playerToken") or [""])[0])
        if ":" not in token:
            note("URL me playerToken nahi mila.")
            return {}
        iv_hex, ct_hex = token.split(":", 1)
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        key = hashlib.sha256(WEB_TOKEN_KEY_SEED.encode()).digest()
        pt = unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(
            bytes.fromhex(ct_hex)), 16)
        data = json.loads(pt.decode("utf-8", "replace"))
        lecture = data.get("lecture") or {}
        topic = (lecture.get("topic")
                 or (lecture.get("videoDetails") or {}).get("name") or "(unknown)")
        note(f"Token OK | lecture: {topic}")
        note(f"IDs: batch={data.get('batchId')} subject={data.get('subjectId')} "
             f"schedule={data.get('scheduleId')}")
        if data.get("videoUrl"):
            note(f"Stream URL: {data['videoUrl']}")
        cookie = data.get("cookie") or ""
        pol = re.search(r"CloudFront-Policy=([^;]+)", cookie)
        if pol:
            v = pol.group(1).replace("-", "+").replace("_", "/")
            v += "=" * (-len(v) % 4)
            policy, _ = json.JSONDecoder().raw_decode(b64decode(v).decode("utf-8", "replace"))
            cond = policy["Statement"][0].get("Condition", {})
            less = cond.get("DateLessThan", {}).get("AWS:EpochTime")
            note(f"Embedded CloudFront signature valid till: {ts(less)}")
            if less and float(less) < time.time():
                note("!! Embedded signature EXPIRED -> manifest refresh zaroori hai.")
        return data
    except Exception as exc:
        note(f"Token decrypt fail: {type(exc).__name__}: {exc}")
        return {}


def decrypt_api(enc: str, init_data: str) -> dict | None:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        key = hashlib.sha256((API_KEY_SEED + (init_data or "")[:50]).encode()).digest()
        iv_hex, ct_hex = enc.split(":", 1)
        pt = unpad(AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(
            bytes.fromhex(ct_hex)), 16)
        return json.loads(pt.decode("utf-8", "replace"))
    except Exception as exc:
        note(f"API response decrypt fail: {type(exc).__name__}: {exc}")
        return None


def manifest_refresh(info: dict, proxy: str | None) -> tuple[str | None, str | None, str]:
    """App ka apna playback-manifest - fresh signed URL + cookie. Proxy ke through."""
    init_data = info.get("initData") or ""
    batch, subject, schedule = info.get("batchId"), info.get("subjectId"), info.get("scheduleId")
    if not (init_data and batch and subject and schedule):
        note("Manifest refresh skip (ids/initData adhoora).")
        return None, None, ""
    url = f"{MANIFEST_API}/{batch}/{subject}/{schedule}"
    variants = {
        "authorization-tma": {"Authorization": f"tma {init_data}"},
        "x-init-data": {"x-init-data": init_data},
        "both": {"Authorization": f"tma {init_data}", "x-init-data": init_data},
    }
    handlers = []
    if proxy:
        handlers.append(proxy)
    handlers.append(None)
    for px in handlers:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px} if px else {}))
        for label, extra in variants.items():
            headers = {"User-Agent": UA, "Referer": "https://app.studyratna.org/",
                       "Origin": "https://app.studyratna.org", "Accept": "application/json"}
            headers.update(extra)
            req = urllib.request.Request(url, headers=headers)
            try:
                with opener.open(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
                body = exc.read().decode("utf-8", "replace")
            except Exception as exc:
                note(f"manifest[{label}] via {px or 'direct'} ERR {type(exc).__name__}")
                continue
            note(f"manifest[{label}] via {px or 'direct'} HTTP {code} :: {body[:160]}")
            if code != 200:
                continue
            try:
                payload = json.loads(body)
            except Exception:
                continue
            if payload.get("encrypted"):
                payload = decrypt_api(payload["encrypted"], init_data) or {}
            data = payload.get("data") or payload
            if data.get("videoUrl"):
                note(f"Fresh URL mila (manifest): {data['videoUrl']}")
                return data["videoUrl"], data.get("cookie"), label
    return None, None, ""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_sessions(pool: list[dict], cookie: str, max_tries: int = 6,
                   log=log) -> list[HlsSession]:
    sessions: list[HlsSession] = []
    if os.environ.get("NO_PROXY_MODE"):
        sessions.append(HlsSession(None, cookie))
        return sessions
    good = [p for p in pool if p.get("host_ok")] or pool
    for item in good[:max_tries]:
        px = item["proxy"]
        log(f"[geo] proxy: {px} ({item.get('isp') or '?'})")
        sessions.append(HlsSession(px, cookie))
    sessions.append(HlsSession(None, cookie))  # last resort: direct
    return sessions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("TARGET_URL", ""))
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--min-mb", type=float, default=0.5)
    args = ap.parse_args()

    DEBUG.mkdir(exist_ok=True)
    if not args.url:
        note("TARGET_URL khali hai (workflow me URL bhejein).")
        write_report("NO-INPUT")
        return 2
    if args.no_proxy:
        os.environ["NO_PROXY_MODE"] = "1"
        note("NO_PROXY_MODE: sirf direct try hoga (debug ke liye).")

    info = decrypt_token(args.url)

    # 1) proxies (Indian IP)
    pool: list[dict] = []
    if not args.no_proxy:
        note("Indian proxy pool dhoond rahe hain (geo-verify ke saath)…")
        try:
            pool = proxies.ensure_pool(min_count=3, log=log)
        except Exception as exc:
            note(f"Proxy scan fail: {type(exc).__name__}: {exc}")
        if pool:
            note(f"IN proxies ready: {len(pool)} "
                 f"({sum(1 for p in pool if p.get('host_ok'))} studyratna-reachable)")
            for p in pool[:5]:
                note(f"  - {p['proxy']} isp={p.get('isp')} reach={p.get('reachable')}")
        else:
            note("!! koi IN proxy nahi mila - direct try karenge (403 expected).")

    cookie = info.get("cookie") or ""
    streams: list[tuple[str, dict, str]] = []
    token_url = info.get("videoUrl")
    if token_url:
        streams.append((token_url, info, "token"))

    # 2) manifest refresh via best IN proxy
    best_proxy = (pool[0]["proxy"] if pool else None)
    fresh_url, fresh_cookie, how = manifest_refresh(info, best_proxy)
    if fresh_url:
        if fresh_cookie:
            cookie = fresh_cookie
        streams.insert(0, (fresh_url, info, f"manifest:{how}"))

    if not streams:
        note("Token me videoUrl nahi aur manifest refresh bhi fail - rukna pad raha hai.")
        write_report("FAIL", "no stream URL available")
        return 4

    # 3) HLS via proxies, best pehle
    expect = int(args.min_mb * 1024 * 1024)
    attempts: list[str] = []
    for url, meta, label in streams:
        note(f"stream [{label}] {url}")
        for session in build_sessions(pool, cookie):
            tag = f"{label} via {session.proxy or 'direct'}"
            log(f"[hls] try: {tag}")
            try:
                res = download_hls(url, session, out_base="downloaded_lecture",
                                   workers=int(os.environ.get("HLS_WORKERS", "4")),
                                   log=log, expect_min_bytes=expect)
            except Exception as exc:
                note(f"[{tag}] HLS error: {type(exc).__name__}: {str(exc)[:160]}")
                attempts.append(f"{tag}: {type(exc).__name__}")
                continue
            attempts.append(f"{tag}: {res.get('bytes')} bytes "
                            f"({res.get('segments')} seg, err={res.get('error') or '-'})")
            if res.get("ok"):
                note(f"SUCCESS via {tag}: {res['bytes'] / 1048576:.1f} MB "
                     f"[{res.get('variant')}]")
                write_report("SUCCESS", json.dumps(
                    {k: v for k, v in res.items() if k != "stats"},
                    default=str, indent=1))
                return 0
            note(f"[{tag}] fail: {res.get('error')}")

    # 4) partial/resume capture: jo mila usse bhi file banao
    note("Poora download nahi hua - ab resume/partial capture try kar rahe hain "
         "(jo segments milte hain unse playable file).")
    for url, meta, label in streams:
        for session in build_sessions(pool, cookie, max_tries=3):
            try:
                part = resume_capture.capture_playlist(
                    url, session, Path("downloaded_lecture.ts"),
                    workers=2, log=log)
            except Exception as exc:
                note(f"[resume {session.proxy or 'direct'}] "
                     f"{type(exc).__name__}: {str(exc)[:120]}")
                continue
            if part.get("ok"):
                note(f"PARTIAL SUCCESS: {part['bytes'] / 1048576:.1f} MB, "
                     f"{part['segments_done']}/{part['segments_total']} segments")
                write_report("SUCCESS(partial)", json.dumps(part, default=str, indent=1))
                return 0

    note("Saare raste fail. Neeche per-attempt detail hai.")
    write_report("FAIL", "\n".join(attempts[-25:]))
    return 5


if __name__ == "__main__":
    sys.exit(main())
