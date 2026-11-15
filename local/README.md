# Lecture → MP4 (local app)

**Kyun local:** player `player.studyratna.org` Cloudflare ke peeche hai aur response
sirf apne origin (`app.studyratna.org`) ko padhne deta hai (CORS). Browser ka `fetch`
kisi bhi doosre origin se `Failed to fetch` deta hai — chahe IP kitna bhi sahi ho.
Server-side request par CORS lagta hi nahi, aur ye app **aapke PC** par chalti hai,
isliye IP bhi aapka hi rehta hai.

## Chalane ka tarika

**Windows** — `start-windows.bat` par **double-click**.
**Mac / Linux** — terminal me:

```bash
./start-mac-linux.sh
```

Ya seedha:

```bash
node localrelay.mjs      # phir http://localhost:8787
```

Bas. Koi `npm install` nahi, koi dependency nahi — sirf Node.js 18+ chahiye
(<https://nodejs.org>, LTS).

## Istemal

1. Browser khulega: <http://localhost:8787>
2. Lecture link (`https://app.studyratna.org/?playerToken=...`) paste karein
3. **1 · Token check** → token sahi?
4. **2 · Player test** → `master -> 200` aana chahiye, phir plan ban jayega
5. **3 · MP4 download** → progress bar → save dialog → file

Poora `16:41` lecture ~100 segments / ~40 MB, 2-4 minute me aa jaata hai.

## Kya andar hota hai

| Step | Kahan | Kaise |
|---|---|---|
| token decrypt | aapka PC | AES-256-CBC, wahi seed (`localrelay.mjs`) |
| playlist + segments | aapka PC | `fetch` server-side — referer/origin/cookie set hote hain |
| AES-128 decrypt | aapka PC | `crypto.createDecipheriv`, seq-number IV, padding fallback |
| TS → MP4 | aapka browser | mux.js (local cache, CDN fallback) |
| save | aapka device | File System Access API → button download |

Token aapke PC se bahar nahi jaata. Kuch bhi upload nahi hota.

## Agar player test fail ho

`?action=selftest` ka JSON dikhega (`master -> 502`, `403` etc.).
- **502 / 403** → us network par player band hai → mobile hotspot / VPN try karein
- **`ERR` / `fetch failed`** → internet ya DNS issue

Playlist manually dekhne ke liye UI me **"Playlist directly check karo"** dabayein —
wahan `#EXTM3U` text dikhna chahiye.

## Files

```
localrelay.mjs      Node HTTP server + token decrypt + HLS + AES (koi dependency nahi)
index.html/app.js   UI (browser me mux.js + save)
style.css           styling
start-windows.bat   Windows double-click launcher
start-mac-linux.sh  Mac/Linux launcher
mux.min.js          (optional) pehli baar khud cache ho jaata hai
```

## Test (jise main ne chalaya)

ffmpeg se asli H.264+AAC HLS banaya, segments AES-128 (sequence IV) encrypt kiye,
phir `localrelay.mjs` ko usi playlist par chalaya:

```
inspect   PASS   (auth age, videoUrl)
selftest  master=200 hls720=200
plan      PASS   4 segments, 1280x720, aes=true
chunk     1360 KB  failed=0  done=true
mux.js    → 1260 KB MP4
ffmpeg    h264 1280x720 + aac, 25.45s, boxes ftyp/moov/moof/mdat
```
