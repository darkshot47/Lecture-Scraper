# Lecture Scraper

StudyRatna lecture links (`?playerToken=...`) se **seedha video download** karo.

## Kyun pehle fail hota tha (asli wajah)

StudyRatna ek **Telegram mini-app** (Next.js SPA) hai:

1. Page ke HTML me video hoti hi nahi — player JS se chalta hai.
2. `?playerToken=...` ek **AES-256-CBC encrypted payload** hai
   (key = `SHA256("5d90298d9598093cf8cb4b55434de5bc")`), jiske andar
   `{type, initData, lecture, videoUrl, cookie, ...}` hota hai.
3. `videoUrl` ek **CloudFront signed HLS** (`master.m3u8`) hota hai —
   `CloudFront-Policy / Key-Pair-Id / Signature` cookie ke bina 403.
4. HLS segments **AES-128 encrypted** hote hain, aur uski key
   `https://api.penpencil.co/v1/videos/get-hls-key?...` se aati hai —
   ye endpoint **sirf India IP** se khulta hai (US/EU datacenter se 403
   CloudFront "request could not be satisfied").

Point 4 ki wajah se GitHub Actions (US runner) par video ke segments to
download ho jate hain lekin **decrypt nahi hote** — isliye .ts file
"can't play" dikhati thi.

## Solution 1 — Local (recommended, India me chalta hai) ✅

```bash
pip install requests pycryptodome
python run_local.py
```

URL paste karo -> `lecture.mp4` ban jayega (ffmpeg ho to MP4, warna .ts).

ffmpeg: Windows `winget install ffmpeg`, macOS `brew install ffmpeg`,
Linux `sudo apt install ffmpeg` (optional, sirf MP4 conversion ke liye).

## Solution 2 — GitHub Actions (automation)

Repo -> Actions -> **Lecture Downloader** -> Run workflow:

| input | matlab |
|---|---|
| `target_url` | lecture ka playerToken wala URL |
| `video_key` | (optional) 32-hex AES key — agar local run se nikali ho |
| `mode` | `download` / `detect` (key hunt) / `inspect` (JS analysis) |
| `with_browser` | real Chromium chahiye to true (slow) |

Agar AES key pehle se pata ho to workflow bhi MP4 bana kar **Release asset**
ke roop me direct download link de deta hai (zip nahi).

Key nikalne ka tareeka (India IP par): `run_local.py` chala kar terminal me
dikhi key, ya browser devtools -> Network -> `enc.key` request -> 16 bytes.

## Sabse common blocker: "Your PW Session has expired"

StudyRatna andar se Physics Wallah (PW) ka session use karta hai. Jab wo
session expire ho jata hai:

- CloudFront video URL ki cookie stale ho jati hai -> **403 Forbidden**
- `/v1/pw/playback-manifest` -> **401 "Your PW Session has expired"**
- HLS AES key endpoint bhi 403 deta hai

**Fix:** StudyRatna app me apna PW account dobara login/link karo, phir
lecture kholo aur naya `playerToken` link banao. Uske baad:

```bash
python run_local.py      # India me - sab kuch automatic
```

Script khud token decrypt karti hai, stale cookie par manifest API se fresh
URL leti hai, segments download + AES decrypt karke `lecture.mp4` deti hai.

## Notes

- Token **~2 ghante me expire** hota hai; expiry par naya link bhejo.
- Debug artifacts har run me upload hote hain (`debug-logs`), aur run log
  GitHub Release notes me bhi chala jata hai.
- `token`, `cookie` aur signature logs me **REDACTED** rehte hain.

## Files

| file | kaam |
|---|---|
| `run_local.py` | local one-command downloader (India IP) |
| `downloader.py` | workflow downloader: token decrypt -> HLS -> MP4 |
| `hls_download.py` | custom HLS downloader (cookie on every request) |
| `probe_key.py` / `probe_browser_key.py` | AES key hunt |
| `inspect_site.py` | site JS/API reverse-engineering helper |
