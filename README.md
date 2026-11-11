# Lecture-Scraper

Lecture page se video stream nikaal kar download karne wala tool.
GitHub Actions se chalao — video `downloaded-lecture` artifact me milegi.

## Kaise chalaye (GitHub Actions)

1. GitHub repo → **Actions** tab → **Lecture Downloader** workflow
2. **Run workflow** → lecture page ka URL daalo → **Run workflow**
3. Build complete hone par **Artifacts** me se `downloaded-lecture` download karo

## Locally chalaye

```bash
pip install playwright yt-dlp
playwright install --with-deps chromium
TARGET_URL='https://lecture-page-ka-url' python downloader.py
```

## "Stream URL not found" aaye to?

Ab script fail hone par ye files banata hai (Actions me `debug-logs` artifact):

| File | Matlab |
|---|---|
| `screenshot.png` | Page kaisa dikha — login wall / error / player dikhega |
| `page.html` | Page ka full HTML |
| `network.log` | Har network request — stream URL aaya ya nahi, dikhega |

In files ko dekh kar pata chalega:
- **Login maang raha hai** → site login ke bina video nahi deti, cookies/login chahiye
- **Player dikh raha hai par stream nahi** → video DRM-protected ho sakti hai
- **YouTube/Vimeo embed hai** → ab auto-detect hota hai, yt-dlp se download hoga

## Purani problem (fix ho gayi)

Pehle sirf `.m3u8` dhundha jata tha aur Play button click nahi hota tha,
isliye stream kabhi start hi nahi hoti thi. Ab:
- m3u8, MP4, YouTube/Vimeo embeds — sab detect hote hain
- Play button auto-click + `<video>` tag se src extract hota hai
- Fail hone par build **red** (fail) dikhega, green nahi — taaki pata chale
