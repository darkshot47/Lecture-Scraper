# Lecture-Scraper

StudyRatna (Telegram Mini App) me chalti hui lecture ko **apne device par record
karke** MP4 me save karne ke tools. GitHub Actions workflow bhi hai, par wo sirf
tab chalta hai jab server US datacenter IP ko allow kare — aam taur par nahi.

> Sirf un lectures ke liye jinhe aap legally dekh sakte hain. Ye tools aapke
> browser me **already play ho rahi** cheez ko save karte hain — koi key/signature
> forge, DRM/paywall ya geo-bypass nahi karte.

---

## Site ka behaviour (jo aapne bataya, aur code se confirm hua)

Aapke detail ke mutabik: StudyRatna third-party app hai, aapne koi PW account
login nahi kiya, lecture aapke token link se **chal raha hai**, par site ka
"Download" button video ko **gallery me nahi** bhejta.

Iska karan site ke JS me saaf milta hai:

- site ka download button `<a href="downloadable_video_url">Download Video (.webm)`
  hai — yani file **`.webm`** format me aati hai
- Android gallery / photos app `.webm` ko index nahi karti (sirf mp4/mkv dekhti
  hai) → "download hua par gallery me nahi" wali feeling
- player `master.m3u8` → `hls/720/main.m3u8` → `000.ts, 001.ts…` +
  `POST /V1/pw/keys/hls/<videoKey>` (app ka apna legit key endpoint) chalta hai,
  yani jab lecture **chal rahi** hoti hai tab media page ke andar maujood hota hai

Matlab: player check karne ki zaroorat nahi — **jab aap dekh sakte hain, tab
recording pakki hai.** Bas recording aapke device par honi chahiye.

---

## Tarika 0 (best): Android app — token URL → Gallery

`.github/workflows/build-apk.yml` se APK banta hai (verified build: JDK 17 +
Android SDK 34, debug + release APK, draft release me attached).

**Install:** Actions → *Build Android APK* → Run workflow → run khatam hone par
draft release `apk-<id>` me `lecture-saver-1.0.apk` download → phone par
install ("Install unknown apps" allow karna hoga).

**Use:**
1. App kholein, token link paste karein (`https://app.studyratna.org/?playerToken=...`).
2. **Open** → lecture play karein.
3. Neeche status dikhega: `capture: 12.3 MB (mp4) | rec: 8.1 MB (mp4) | 240s / 1001s`.
4. **Save to Gallery** → file seedha
   `Movies/LectureSaver/lecture-YYYYMMDD-HHmmss.mp4` me jaati hai —
   **Gallery/Photos me turant dikhti hai**. Koi "download folder me pada hai par
   gallery me nahi" wali dikkat nahi.

Kaise kaam karta hai (technical):

| Layer | Kya karta hai |
|---|---|
| `WebView` | user ka token link kholta hai (site ka apna player chalta hai) |
| `inject_webview.js` | `SourceBuffer.appendBuffer` hook + MediaRecorder; decrypted media capture |
| `AndroidBridge` | capture ko 2 MB slices me Java tak bhejta hai (memory-safe) |
| `MainActivity` | `MediaStore` me `Movies/LectureSaver` par stream likhta hai, `IS_PENDING` ke saath |

Save format: **`.mp4`** (fMP4 capture) — isliye Gallery index karti hai. Sirf HLS
segments (`.ts`) mile to `.ts` bhi Gallery me chala jaata hai; ffmpeg se mp4:
`ffmpeg -i file.ts -c copy lecture.mp4`.

App sirf wahi save karta hai jo aap dekh sakte hain — koi login/DRM/geo bypass
nahi, aur koi data bahar nahi jaata.

---

## Tarika 1 (recommended): browser me recorder paste karein

```bash
python build_console.py        # console_paste.txt banata hai (ek line, paste-ready)
```

1. Lecture **laptop/PC ke Chrome** me kholen: Telegram → lecture → ⋮ →
   "Open in browser" → address copy karke Chrome me paste.
2. Lecture **play** karein (zaroori — data tabhi aata hai).
3. `F12` → **Console** → `console_paste.txt` ka poora content paste → Enter.
4. Right-bottom panel aayega: `MSE: 12.3 MB (mp4)`, `Recorder: 8.1 MB (mp4)`.
5. `Save MSE` (best quality, lossless) ya `Save Rec` dabayein.
   Lecture end hote hi khud bhi save ho jaata hai.

Output:
- **Save MSE** → `.mp4` (fMP4) ya `.ts` (HLS segments) — ffmpeg se mp4 me convert
  kar sakte hain: `ffmpeg -i file.ts -c copy lecture.mp4`
- **Save Rec** → `.mp4` (Chrome MP4 MediaRecorder support karta hai) warna `.webm`

### Android par

DevTools console mobile me nahi khulta. Do raste:
- **Aasan:** laptop par lecture kholen (Telegram Web / link copy) aur wahi recorder
  chalayein; ya
- USB debugging + laptop Chrome me `chrome://inspect` → phone ka page → Console me
  paste.

`page_recorder_snippet.txt` me bookmarklet bhi hai (Chrome mobile me bookmark ka URL
edit karke paste kar sakte hain).

---

## Tarika 2: kuch bhi install kiye bina (screen record)

Controls wale player par long-press karke "Download / Save" nahi milta to:

- **Android:** notification shade se **Screen Recorder** chalu karein → lecture
  play karein → recording stop → gallery me mp4.
- **PC:** OBS / Xbox Game Bar (`Win+G`) se window capture.

Isme quality re-encode hoti hai, par 100% kaam karta hai aur koi setup nahi.

---

## Tarika 3: GitHub Actions (jab server allow kare)

Workflow: `.github/workflows/download.yml` → `target_url` me fresh link.

Kaam kaise karta hai: real Chrome me lecture kholta hai, player ko play karata
hai, `appendBuffer` se media capture karta hai (in-page recording), ffmpeg se MP4
banata hai; backup me yt-dlp + manifest-refresh bhi try karta hai.

**Seema:** GitHub runner US datacenter se chalta hai. Logs me `master.m3u8` par
`403`/`502` aata hai jab:
- token ke andar ka CloudFront signature expire ho (verified: `DateLessThan`
  token banane se ~17h purana tha), ya
- content India-only ho.

Us waqt runner par kuch nahi ho sakta — **Tarika 1/2** hi chalega. Runner ke
`run_report.md` / draft release me diagnostics milte hain (`codec`, `probe`,
`recorder mode`, capture bytes).

---

## Files

| File | Kaam |
|---|---|
| `page_recorder.js` | In-page recorder (MSE hook + MediaRecorder), save panel |
| `console_paste.txt` | Wahi recorder, ek line me — DevTools me paste karne ke liye |
| `build_console.py` | `page_recorder.js` → `console_paste.txt` + bookmarklet |
| `tools/test_page_recorder.js` | jsdom test: fake MSE/MediaRecorder se capture+save verify |
| `selftest_recorder.py` | CI test: asli fragment ko MSE me feed karke capture+remux verify |
| `downloader.py` | GitHub Actions runner ke liye downloader + recorder |
| `.github/workflows/download.yml` | Workflow (chrome install, self-tests, draft release) |

Tests:

```bash
node tools/test_page_recorder.js      # npm install jsdom
python selftest_recorder.py          # ffmpeg + playwright chahiye
```
