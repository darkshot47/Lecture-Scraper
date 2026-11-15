# Lecture Saver (Android app)

Termux ki zaroorat khatam. Ek hi app me:

* **Dekho** — lecture app ke andar hi chalta hai (ExoPlayer, HLS + AES-128)
* **Download** — file banti hai aur seedha Gallery me `Movies/LectureSaver` me save hoti hai
* **Key sniff** — player ke asli requests se headers/cookies seekhta hai (agar key endpoint 401 de)

## Install

APK: **Actions → Build Android APK** wale run ka release, ya seedha
`https://github.com/darkshot47/Lecture-Scraper/releases/latest` (tag `apk-…`).

Phone par: APK download → "Install unknown apps" allow → install.
Release APK debug-signed hai (sideload ke liye theek, Play Store ke liye nahi).

## Kaise chalta hai

```
link paste
   │
   ├── Token.parse()  ── AES-256-CBC (iv:ct) khol ke videoUrl + cookie nikaalta hai
   │
   ├── "Dekho"     ──► PlayerActivity (ExoPlayer)
   │                    master.m3u8 + Cookie + sniffed headers
   │                    ExoPlayer khud segments laata hai aur AES key se decrypt karta hai
   │
   ├── "Download"  ──► Downloader (native, 4 workers, resumable)
   │                    master → variant → media playlist → segments
   │                    raw-NNNN.bin cache (encrypted) → key → seg-NNNN.bin
   │                    join → lecture.ts → MediaStore (Gallery)
   │
   └── "Page kholo" ► PageActivity (WebView)
                        page normal chalta hai; har request ke headers NetSniffer
                        record karta hai — Referer, Origin, Cookie, Authorization,
                        client-id… Wahi headers downloader "sniffed" variant me use karta hai.
```

### Key endpoint 401 ka jawab

`api.penpencil.co/v1/videos/get-hls-key` ko header/cookie chahiye hoti hai. Browser me
CORS ki wajah se, terminal me cookies na hone ki wajah se 401 aata tha. App me
**Key sniff** dabane se WebView ke andar player khud key maangta hai — us request ke
asli headers pakad ke downloader ko de diye jaate hain. Koi proxy nahi, koi CORS nahi.

Agar key phir bhi na mile, segments `raw-NNNN.bin` me safe rehte hain — key milte hi
file turant ban jaati hai (dobara download nahi hota).

## Build

APK GitHub Actions se banti hai (`.github/workflows/build-apk.yml`):
JDK 17 + Android SDK 34 + `gradle wrapper` (repo me `gradlew` checked-in nahi hai,
workflow khud banata hai) → `assembleDebug` + `assembleRelease` → draft release me APK.

Local build ke liye: `cd android && gradle wrapper && ./gradlew :app:assembleDebug`

## Files

| File | Kaam |
|---|---|
| `MainActivity.java` | UI: link box, Dekho / Download / Key sniff |
| `Token.java` | playerToken decrypt (URL / bare token / %3A / quoted — sab) |
| `Downloader.java` | HLS + AES-128 native download, multi-worker, resume, join |
| `NetSniffer.java` | WebView requests se headers/cookies ikattha karna |
| `PageActivity.java` | Page WebView: page ke andar download, site ka download button (blob:/data:/http), headers copy |
| `BrowserNet.java` | requests WebView ke andar bhejna (native 502 ko bypass) |
| `GalleryWriter.java` | bytes -> Gallery (MediaStore) |
| `PlayerActivity.java` | ExoPlayer se direct playback |
| `assets/inject_webview.js` | MSE/MediaRecorder capture (backup raasta) |

## Note

Sirf un lectures ke liye jinhe aap legally dekh sakte hain.
