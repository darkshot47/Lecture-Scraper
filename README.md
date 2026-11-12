# Lecture-Scraper

Apne **apne authorized account** ka lecture (StudyRatna / Telegram Mini App) browser
session se nikaal kar MP4 me save karne ka GitHub Actions workflow.

> ⚠️ Sirf un lectures ke liye jinhe aap legally access kar sakte hain. Kisi ka bhi
> account/session token, DRM ya paywall bypass karne ke liye ise use na karein.

## Kya badla (v1 se v2)

Purana code 10 second ruk kar bas *pehli* `.m3u8`/`manifest` request pakadta tha aur
yt-dlp ko bilkul wahi URL de deta tha. StudyRatna jaisi SPA me isse kabhi video nahi
bani — aur failure ke bawajood workflow `success` dikhta rehta tha.

| Purana problem (logs se) | v2 me fix |
|---|---|
| `manifest.json` (PWA file) pakad li jaati thi | exact `.m3u8`/`.mpd`/media match + scoring |
| player ko play nahi karaya jata tha | autoplay + play-button clicks, 45s tak wait |
| sirf pehla URL | best (master) playlist chuna jata hai |
| cookies/headers yt-dlp ko nahi milte | Playwright context cookies + Referer/Origin/UA |
| `ffmpeg: NOT FOUND` | workflow ffmpeg install karta hai, warna warning |
| fail hone par exit 0 → green run | proper exit codes, MP4 na bane to run **fail** |
| token logs me | token/cookie/log-in URLs redact |
| artifacts/debug nahi | `debug-logs` artifact: network, candidates, page text, screenshot |

## Kaise chalayein

1. Telegram app me lecture kholen → "Open in Browser" → address bar se poora URL
   copy karein (`https://app.studyratna.org/?playerToken=...`).
   Token ~2 ghante me expire hota hai, isliye run se **turant pehle** banayein.
2. GitHub → **Actions** → **Download Lecture** → **Run workflow**:
   - `target_url`: wahi fresh link
   - `wait_seconds`: default `45` (slow network par `90`)
3. Run ke baad **Artifacts → downloaded-lecture**.

Exit codes: `2` URL missing, `3` session/token expired, `4` stream URL nahi mili,
`5` download fail (403/502/geo).

## Agar run fail ho

- `debug/candidates.txt` — kaun se stream URLs mile
- `debug/network.log` — poora request list (redacted)
- `debug/page_text.txt` + `screenshot.png` — site ne kya dikhaya

403/502 ka matlab aksar: **token/cookie expire** ya **India ke bahar se block**.
Aise case me log ek **India (apne ghar ke network)** machine par chalayein:

```bash
pip install -r requirements.txt && python -m playwright install chromium
TARGET_URL="https://app.studyratna.org/?playerToken=..." python downloader.py
```

## Kya ye nahi karta

Reverse-engineered AES keys, forged CloudFront cookies, session replay, HLS
decryption-key extraction, DRM/paywall ya geo-restriction bypass — ye sab jaan
bujh kar shaamil nahi kiya gaya. Access ke liye aapka apna valid session chahiye.

## Diagnostic: asli 403 ka karan (fresh token ke saath bhi)

`playerToken` **naya** ho sakta hai par uske andar ka CloudFront signed URL
purana ho sakta hai. Real example (token `auth_date` = 12:08 UTC):

```
Token decrypt OK | lecture: Alight Motion Mobile Video Editing 01 ...
CloudFront signed URL ki limit : 2026-11-11T18:34:49Z   <-- kal expire
Token banaya gaya              : 2026-11-12T12:08:41Z   <-- abhi
=> CloudFront resource: https://d1d34p8vz63oiq.cloudfront.net/<videoKey>/*
```

Matlab: token fresh hai, magar usme embedded signed cookie already expire hai.
Isliye `player.studyratna.org/.../master.m3u8` 403/502 deta hai. Ye **code ka
bug nahi** — server ne token me stale signature daala hai.

Solution (v3 me built-in): app ka apna `playback-manifest` refresh endpoint call
hota hai jo **fresh signed URL + cookie** deta hai. Us endpoint ko kaam karne ke
liye PW (Physics Wallah) login valid hona chahiye:

- `401 Your PW Session has expired. Please log in again.` aaye to app me
  PW account **dobara link/login** karo, phir naya lecture link banao.
- App me lecture **play karke** confirm karo: agar app me bhi nahi chalta to
  lecture/session server-side hi kharab hai, koi downloader usse nahi la sakta.

## Chalane ke raste (jo actually kaam karte hain)

1. **GitHub UI (recommended)** — Actions → *Download Lecture* → *Run workflow* →
   `target_url` me fresh link. (Workflow `main` par hona chahiye; is branch ka PR
   merge karne ke baad button dikhega.)
2. **repository_dispatch** (token Git me commit kiye bina):

   ```bash
   gh api -X POST repos/<owner>/<repo>/dispatches \
     -f event_type=download-lecture \
     -f "client_payload[url]=https://app.studyratna.org/?playerToken=..."
   ```

3. Har run ka result ek **draft release** (`run-<id>`) aur `run_report.md` me
   milta hai: token diagnostics, probes, stream candidates, yt-dlp output.
