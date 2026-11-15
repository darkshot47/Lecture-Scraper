# 📱 Termux par chalane ka tarika (Android)

**Sabse aasan**: har baar bas 2 command.

## Pehli baar (ek hi baar)

```bash
pkg update -y && pkg install nodejs-lts ffmpeg curl -y
mkdir -p ~/lr && cd ~/lr && \
B=https://raw.githubusercontent.com/darkshot47/Lecture-Scraper/main/local && \
curl -fsSLO $B/download.mjs && curl -fsSLO $B/localrelay.mjs && \
curl -fsSLO $B/app.js && curl -fsSLO $B/index.html && \
curl -fsSLO $B/style.css && curl -fsSLO $B/mux.min.js && ls -la
```

## Download (browser ki zaroorat nahi)

```bash
cd ~/lr
node download.mjs "https://app.studyratna.org/?playerToken=..."
```

16:41 lecture = **106 segments**, ~6-10 minute me `.mp4` ban jayega.

### Options

```bash
node download.mjs "<link>" -o Lecture.mp4     # naam
node download.mjs "<link>" -w 8               # 8 parallel (tez)
node download.mjs "<link>" -q 1080            # quality
node download.mjs --resume                    # adhoora continue (session band ho gaya tha?)
node download.mjs --fresh                     # cache saaf karke shuru se
```

## 🔁 Session band ho gaya? — resume

Har segment alag save hota hai: `~/lr/.lecture-cache/seg-0000.bin`

```bash
cd ~/lr && node download.mjs --resume
```

Sirf **missing** segments aayenge. 50 segment ho gaye the to baaki 56 aayenge.

## File kahan hai

- `~/lr/lecture.mp4` (Termux ke andar)
- Bahar nikalne ke liye: `termux-setup-storage` (Allow karein), phir

```bash
cp ~/lr/lecture.mp4 /sdcard/Download/
```

Ab Gallery / VLC me khul jayega.

## GUI wala tarika (browser me progress)

```bash
cd ~/lr && node localrelay.mjs
```

Chrome me: `http://localhost:8787`

**Termux session band mat karein** — background me chalta rahe. Android process
maar de to: Settings → Apps → Termux → Battery → **Unrestricted**.

## Dhyan rakhein

| Baat | Kya karein |
|---|---|
| 5597 min purana token | **Naya link** lein (StudyRatna se dobara kholein). Purana token chal sakta hai par playlist/key band ho sakti hai |
| `key HTTP 403/404` | Key endpoint block → WiFi ↔ mobile data badal ke dekhein |
| `player ne 502 diya` | Us network par player band → network badal ke dekhein |
| 3-4 segment fail | `--resume` dobara chalayein, sirf missing aayenge |
| ffmpeg nahi | `pkg install ffmpeg -y` (MP4 ke liye). Bina ffmpeg `.ts` banta hai — VLC me chalta hai |
