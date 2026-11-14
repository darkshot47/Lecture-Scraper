"""
Self-test for the in-page playback recorder (downloader.RECORDER_JS / DUMP_JS).

Kyun: lecture download ke liye asli player chahiye, par recorder ka logic aaj hi
verify ho sakta hai. Ye test:

  1. ffmpeg se ek chhota real fragmented MP4 banata hai (testsrc, 6s).
  2. Usse local HTTP server par serve karta hai.
  3. Chromium me MSE (MediaSource + SourceBuffer.appendBuffer) ko bilkul waise hi
     feed karta hai jaise hls.js karta hai - downloader ka RECORDER_JS inject karke.
  4. Capture ko dump karke ffmpeg se remux karta hai, ffprobe se duration check.

Pass hone ka matlab: jab asli player segments append karega, humara recorder
unhe pakad kar playable MP4 bana dega.

Run: python selftest_recorder.py
"""

from __future__ import annotations

import http.server
import json
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

import downloader as dl

WORK = Path(tempfile.mkdtemp(prefix="selftest-"))
PAGE = WORK / "index.html"
FRAG = WORK / "frag.mp4"


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def make_fragment() -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg nahi mila - self-test ke liye zaroori hai")
    proc = sh([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=6",
               "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
               "-movflags", "frag_keyframe+empty_moov+default_base_moof",
               str(FRAG)])
    if proc.returncode != 0:
        sys.exit(f"ffmpeg frag banane me fail: {proc.stderr[-400:]}")
    print(f"[+] fragment: {FRAG} ({FRAG.stat().st_size} bytes)")


HTML = """<!doctype html><html><body>
<video id="v" muted playsinline></video>
<script>
window.__done = false; window.__err = null;
async function go() {
  const res = await fetch('/frag.mp4');
  const buf = await res.arrayBuffer();
  const candidates = ['video/mp4; codecs="avc1.42c01e"', 'video/mp4; codecs="avc1.42E01E"',
                      'video/mp4; codecs="avc1.64001e"', 'video/mp4'];
  const mime = candidates.find(m => window.MediaSource && MediaSource.isTypeSupported(m));
  if (!mime) { window.__err = 'no supported mp4 mime'; window.__done = true; return; }
  const ms = new MediaSource();
  const v = document.getElementById('v');
  v.src = URL.createObjectURL(ms);
  ms.addEventListener('sourceopen', async () => {
    try {
      const sb = ms.addSourceBuffer(mime);
      const total = buf.byteLength;
      const step = Math.max(4096, Math.floor(total / 4));
      for (let off = 0; off < total; off += step) {
        sb.appendBuffer(new Uint8Array(buf.slice(off, Math.min(off + step, total))));
        await new Promise(r => { const h = () => { sb.removeEventListener('updateend', h); r(); }; sb.addEventListener('updateend', h); });
      }
      await Promise.race([new Promise(r => { const h = () => { v.removeEventListener('loadeddata', h); r(); }; v.addEventListener('loadeddata', h); }), new Promise(r => setTimeout(r, 4000))]);
      v.play().catch(() => {});
      window.__done = true;
    } catch (e) { window.__err = String(e); window.__done = true; }
  });
}
go();
</script></body></html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WORK), **kw)

    def log_message(self, *a):  # quiet
        pass


def serve() -> tuple[socketserver.TCPServer, int]:
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def main() -> int:
    make_fragment()
    PAGE.write_text(HTML, encoding="utf-8")
    httpd, port = serve()
    url = f"http://127.0.0.1:{port}/index.html"

    with sync_playwright() as p:
        last = None
        for channel in ("chrome", "chrome-beta", None):
            try:
                kw = {"headless": True, "args": [
                    "--no-sandbox", "--autoplay-policy=no-user-gesture-required",
                    "--disable-dev-shm-usage"]}
                if channel:
                    kw["channel"] = channel
                browser = p.chromium.launch(**kw)
                print(f"[+] browser: {channel or 'bundled chromium'}")
                break
            except Exception as exc:
                last = exc
        else:
            sys.exit(f"koi browser launch nahi hua: {last}")
        page = browser.new_page()
        codecs = page.evaluate(
            "() => ['video/mp4; codecs=\"avc1.42c01e\"', "
            "'video/mp4; codecs=\"avc1.64001e\"'].map("
            "m => [m, !!(window.MediaSource && MediaSource.isTypeSupported(m))])"
        )
        print(f"[i] codecs: {codecs}")
        page.add_init_script(script=dl.RECORDER_JS)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("() => window.__done === true", timeout=30000)
        err = page.evaluate("() => window.__err")
        status = page.evaluate(dl.STATUS_JS)
        dumped = page.evaluate(dl.DUMP_JS)
        browser.close()
    httpd.shutdown()

    print(f"[i] page error: {err}   js errors: {errors[:3]}")
    print(f"[i] status: {json.dumps(status)}")
    print(f"[i] captured bytes: {dumped.get('mseBytes')} mime={dumped.get('mseMime')}")

    failures = []
    if not dumped.get("mseB64"):
        failures.append("appendBuffer hook ne koi data capture nahi kiya")
    else:
        raw = WORK / "capture_raw.mp4"
        raw.write_bytes(__import__("base64").b64decode(dumped["mseB64"]))
        out = WORK / "out.mp4"
        proc = sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-i", str(raw), "-c", "copy", "-movflags", "+faststart", str(out)])
        if proc.returncode != 0 or not out.exists():
            failures.append(f"remux fail: {proc.stderr[-300:]}")
        else:
            probe = sh(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(out)])
            try:
                dur = float((probe.stdout or "0").strip())
            except ValueError:
                dur = 0.0
            print(f"[+] remuxed: {out} ({out.stat().st_size} bytes, {dur:.2f}s)")
            if dur < 3:
                failures.append(f"duration bahut kam ({dur:.2f}s)")

    report = {
        "browser": "see log",
        "codecs": codecs if "codecs" in dir() else None,
        "page_error": err,
        "js_errors": errors[:3],
        "status": status,
        "captured_bytes": dumped.get("mseBytes"),
        "mime": dumped.get("mseMime"),
        "failures": failures,
    }
    Path("selftest_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n=== SELFTEST JSON ===")
    print(json.dumps(report, indent=2))

    if failures:
        print("\nSELFTEST FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("\nSELFTEST PASS - recorder + remux kaam kar rahe hain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
