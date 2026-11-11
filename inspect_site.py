"""
StudyRatna reverse-engineering inspector (debug only).

Targeted dumps:
  1. decryptForWeb / decrypt module scope (AES key chahiye)
  2. playback-manifest call sites (params order)
  3. playerToken handling effect (kya hota hai decrypt ke baad)
  4. saare 32/64-char hex literals (possible keys)
"""

import re
import sys
import urllib.request

BASE = "https://app.studyratna.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TARGETS = [
    ("decryptForWeb", 6000, 1200),
    ("playback-manifest", 3000, 3000),
    ("playerToken", 800, 2500),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": BASE + "/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    out = []
    html = fetch(BASE + "/")
    chunks = sorted(set(re.findall(r"/_next/static/chunks/[A-Za-z0-9~._-]+\.js", html)))
    sources = {"PAGE_HTML": html}
    for c in chunks:
        try:
            sources[c] = fetch(BASE + c)
        except Exception as e:
            out.append(f"[-] chunk fail {c}: {e}")
    out.append(f"[+] {len(sources)} sources, "
               f"{sum(len(v) for v in sources.values())} chars total")

    # 1-3: targeted context dumps
    for pat, before, after in TARGETS:
        out.append(f"\n\n########## DUMP: {pat} ##########")
        shown = 0
        for name, src in sources.items():
            for m in re.finditer(re.escape(pat), src):
                if shown >= 4:
                    break
                s = max(0, m.start() - before)
                e = min(len(src), m.end() + after)
                out.append(f"\n--- [{name}] @{m.start()} ---\n{src[s:e]}\n")
                shown += 1
            if shown >= 4:
                break

    # 4: hex literals (possible AES keys)
    out.append("\n\n########## HEX LITERALS ##########")
    seen = set()
    for name, src in sources.items():
        for m in re.finditer(r"""["']([0-9a-fA-F]{32}|[0-9a-fA-F]{64})["']""", src):
            val = m.group(1)
            if val in seen:
                continue
            seen.add(val)
            s = max(0, m.start() - 150)
            out.append(f"\n[{name} @{m.start()}] {len(val)} chars: {val}\n"
                       f"  ctx: ...{src[s:m.end() + 150]}...")
            if len(seen) >= 25:
                break
        if len(seen) >= 25:
            break

    text = "\n".join(out)
    print(text[:80000])
    with open("inspect_report.md", "w", encoding="utf-8") as f:
        f.write(text[:80000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
