"""
MSE capture
===========
App ka player hls.js se video chalata hai. hls.js AES-128 decrypt karke
media `SourceBuffer.appendBuffer()` me deta hai — yaani decrypted data
page ke andar milta hai. Hum wahin se capture karte hain.

AES key ki zaroorat nahi. Output: downloaded_lecture.mp4
"""

import asyncio
import base64
import os
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN','en']});
window.chrome = window.chrome || {runtime: {}};
window.__mseChunks = [];
window.__mseMime = null;
(function () {
  const SB = window.SourceBuffer && window.SourceBuffer.prototype;
  if (SB && SB.appendBuffer) {
    const orig = SB.appendBuffer;
    SB.appendBuffer = function (buf) {
      try {
        const u8 = new Uint8Array(buf.buffer ? buf.buffer.slice(
          buf.byteOffset, buf.byteOffset + buf.byteLength) : buf);
        window.__mseChunks.push(u8);
      } catch (e) {}
      return orig.call(this, buf);
    };
  }
  const MS = window.MediaSource && window.MediaSource.prototype;
  if (MS && MS.addSourceBuffer) {
    const origAdd = MS.addSourceBuffer;
    MS.addSourceBuffer = function (m) {
      window.__mseMime = m;
      return origAdd.call(this, m);
    };
  }
})();
"""

DUMP_JS = """(maxN) => {
  const a = window.__mseChunks.splice(0, maxN);
  let total = 0; for (const u of a) total += u.length;
  const out = new Uint8Array(total); let off = 0;
  for (const u of a) { out.set(u, off); off += u.length; }
  let s = '';
  const CH = 8192;
  for (let i = 0; i < out.length; i += CH)
    s += String.fromCharCode.apply(null, out.subarray(i, i + CH));
  return {n: a.length, data: btoa(s), total: total};
}"""

VIDEO_JS = """() => {
  const out = [];
  const walk = (root) => {
    root.querySelectorAll('video').forEach(v => out.push(v));
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
  };
  walk(document);
  if (!out.length) return {found: false};
  const v = out[0];
  let be = 0;
  try { if (v.buffered.length) be = v.buffered.end(v.buffered.length - 1); } catch (e) {}
  return {found: true, duration: v.duration || 0, current: v.currentTime || 0,
          bufferedEnd: be, mime: window.__mseMime,
          chunks: window.__mseChunks.length};
}"""


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


async def run_attempt(browser, target, outdir, attempt):
    ctx = await browser.new_context(user_agent=UA, locale="en-IN",
                                    viewport={"width": 1366, "height": 768})
    await ctx.add_init_script(STEALTH_JS)

    async def guard(route):
        u = route.request.url
        rt = route.request.resource_type
        if "disable-devtool" in u:
            return await route.abort()
        if rt == "document" and urlparse(u).netloc in (
                "studyratna.org", "www.studyratna.org"):
            return await route.fulfill(status=200, content_type="text/html",
                                       body="<html><body>ok</body></html>")
        return await route.continue_()

    await ctx.route("**/*", guard)
    page = await ctx.new_page()

    def on_resp(r):
        u = r.url
        if ("player.studyratna.org" in u) or ("get-hls-key" in u):
            print(f"    [{r.status}] {u[:100]}", flush=True)

    page.on("response", on_resp)

    try:
        await page.goto(target, timeout=90000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"    goto: {str(e)[:100]}", flush=True)

    # video + duration ka intezaar
    info = {}
    for i in range(20):
        await page.wait_for_timeout(3000)
        try:
            info = await page.evaluate(VIDEO_JS)
        except Exception:
            info = {}
        if info.get("found") and (info.get("duration") or 0) > 1:
            print(f"    video ready: dur={info['duration']:.0f}s "
                  f"mime={info.get('mime')}", flush=True)
            break
        if i % 3 == 0:
            print(f"    {3*(i+1)}s dur={info.get('duration')} "
                  f"found={info.get('found')}", flush=True)

    dur = (info.get("duration") or 0)
    if dur <= 1:
        print("    playlist nahi chali — is attempt me kuch nahi", flush=True)
        await ctx.close()
        return 0, ""

    try:
        await page.evaluate("""() => {
          const walk=(r)=>{r.querySelectorAll('video').forEach(v=>{v.muted=true;
            v.play().catch(()=>{});});r.querySelectorAll('*').forEach(e=>{
            if(e.shadowRoot) walk(e.shadowRoot);});};walk(document);
        }""")
    except Exception:
        pass

    os.makedirs(outdir, exist_ok=True)
    total = 0
    part = 0
    mime = info.get("mime") or ""
    last_be, stall = 0.0, 0

    for step in range(1200):
        await page.wait_for_timeout(2500)
        # chunks dump
        while True:
            try:
                d = await page.evaluate(DUMP_JS, 400)
            except Exception:
                break
            if not d or not d.get("n"):
                break
            with open(os.path.join(outdir, f"p{attempt}_{part:05d}.bin"),
                      "wb") as f:
                f.write(base64.b64decode(d["data"]))
            part += 1
            total += d["total"]
            if part % 20 == 0:
                print(f"    captured {total/1048576:.1f} MB "
                      f"({part} parts)", flush=True)

        try:
            info = await page.evaluate(VIDEO_JS)
        except Exception:
            info = None
        if not info:
            continue
        be = info.get("bufferedEnd") or 0
        mime = info.get("mime") or mime
        if be >= dur - 1:
            print(f"    poora buffer aa gaya ({be:.0f}/{dur:.0f}s)", flush=True)
            break
        if be > last_be + 0.5:
            last_be, stall = be, 0
        else:
            stall += 1
        # buffer aage badhane ke liye currentTime ko buffer ke end ke paas le jao
        try:
            await page.evaluate(
                "() => { const walk=(r)=>{r.querySelectorAll('video')"
                ".forEach(v=>{try{if(v.buffered.length){v.currentTime="
                "Math.max(0,v.buffered.end(v.buffered.length-1)-1);}}catch(e){}});"
                "r.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)walk(e.shadowRoot);})};"
                "walk(document);}")
        except Exception:
            pass
        if stall >= 40 and be < 10:
            print("    no progress — abort", flush=True)
            break
        if stall >= 80:
            print(f"    stall at {be:.0f}s — abort", flush=True)
            break

    # bache hue chunks
    while True:
        try:
            d = await page.evaluate(DUMP_JS, 400)
        except Exception:
            break
        if not d or not d.get("n"):
            break
        with open(os.path.join(outdir, f"p{attempt}_{part:05d}.bin"), "wb") as f:
            f.write(base64.b64decode(d["data"]))
        part += 1
        total += d["total"]

    await ctx.close()
    print(f"    total captured: {total/1048576:.1f} MB | mime={mime}", flush=True)
    return total, mime


def assemble(outdir, mime):
    parts = sorted(os.listdir(outdir))
    if not parts:
        return None
    is_ts = b"mp2t" in (mime or "").encode() or "mp2t" in (mime or "")
    ext = ".ts" if is_ts else ".mp4"
    raw = "mse_joined" + ext
    print(f"[*] joining {len(parts)} parts -> {raw} (mime={mime})", flush=True)
    with open(raw, "wb") as out:
        for p in parts:
            with open(os.path.join(outdir, p), "rb") as f:
                shutil.copyfileobj(f, out)
    size = os.path.getsize(raw) / 1048576
    print(f"[+] {raw} ({size:.1f} MB)", flush=True)
    if size < 1:
        return None

    exe = ffmpeg_exe()
    if not exe:
        print("[-] ffmpeg nahi mila", flush=True)
        return raw
    final = "downloaded_lecture.mp4"
    cmd = [exe, "-y", "-loglevel", "error", "-fflags", "+genpts",
           "-i", raw, "-c", "copy", "-movflags", "+faststart", final]
    try:
        subprocess.run(cmd, check=True, timeout=900)
        print(f"[+] MP4: {os.path.getsize(final)/1048576:.1f} MB", flush=True)
        return final
    except Exception as e:
        print(f"[-] ffmpeg fail: {str(e)[:150]}", flush=True)
        return raw


async def main():
    from playwright.async_api import async_playwright

    target = os.environ.get("TARGET_URL", "").strip()
    if not target or "playerToken" not in target:
        print("[-] TARGET_URL chahiye (playerToken wala)")
        return 2

    outdir = "mse_capture"
    shutil.rmtree(outdir, ignore_errors=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required"])
        best = (0, "")
        for attempt in range(3):
            print(f"[*] attempt {attempt+1}/3", flush=True)
            total, mime = await run_attempt(browser, target, outdir, attempt)
            if total > best[0]:
                best = (total, mime)
            if total > 5 * 1048576:
                break
        await browser.close()

    print(f"[=] best capture: {best[0]/1048576:.1f} MB", flush=True)
    if best[0] < 512 * 1024:
        print("[-] capture bahut chhota — player chala hi nahi", flush=True)
        return 1
    res = assemble(outdir, best[1])
    return 0 if res else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
