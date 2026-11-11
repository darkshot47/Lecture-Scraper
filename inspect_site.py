"""
StudyRatna site inspector (debug only).
Page + saare JS chunks download karke playerToken / decryption / video URL
se related code greps karta hai, taaki pata chale player kaise chalta hai.
"""

import re
import sys
import urllib.request

BASE = "https://app.studyratna.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PATTERNS = [
    "playerToken",
    "api-lite",
    "decrypt",
    "createDecipheriv",
    "AES",
    "CryptoJS",
    "m3u8",
    "hls",
    "videoUrl",
    "streamUrl",
    "video_url",
    "lecture",
    "TOKEN_KEY",
    "secret",
    "atob",
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": BASE + "/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    out = []
    html = fetch(BASE + "/")
    out.append(f"[+] HTML fetched: {len(html)} chars")

    chunks = sorted(set(re.findall(r"/_next/static/chunks/[A-Za-z0-9~._-]+\.js", html)))
    out.append(f"[+] JS chunks found: {len(chunks)}")

    sources = {}
    for c in chunks:
        try:
            sources[c] = fetch(BASE + c)
        except Exception as e:
            out.append(f"[-] chunk fail {c}: {e}")

    # HTML me bhi dhoondho
    sources["PAGE_HTML"] = html
    out.append(f"[+] downloaded {len(sources)} sources, "
               f"total {sum(len(v) for v in sources.values())} chars")

    for pat in PATTERNS:
        out.append(f"\n===== PATTERN: {pat} =====")
        hits = 0
        seen = set()
        for name, src in sources.items():
            for m in re.finditer(re.escape(pat), src, re.IGNORECASE):
                s = max(0, m.start() - 250)
                snippet = src[s:m.end() + 350].replace("\n", " ")
                key = snippet[:120]
                if key in seen:
                    continue
                seen.add(key)
                hits += 1
                out.append(f"[{name} @{m.start()}]\n...{snippet}...\n")
                if hits >= 8:
                    break
            if hits >= 8:
                break
        if hits == 0:
            out.append("(no match)")

    text = "\n".join(out)
    print(text[:60000])
    with open("inspect_report.md", "w", encoding="utf-8") as f:
        f.write(text[:60000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
