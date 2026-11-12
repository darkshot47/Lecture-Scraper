"""
page_recorder.js se console-paste karne layak code banata hai.

DevTools console me paste karte waqt kuch browsers newline par pehli line hi
execute karke baaki chhod dete hain, isliye hum pura script ek single line me
(minified-ish) wrap kar dete hain. Output: console_paste.txt (+ clipboard copy).

Run: python build_console.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "page_recorder.js"
OUT = ROOT / "console_paste.txt"
HTML = ROOT / "page_recorder_snippet.txt"


def main() -> int:
    src = SRC.read_text(encoding="utf-8")
    # 1) single-line version: comments hata kar whitespace collapse
    no_comments = []
    in_block = False
    for line in src.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        no_comments.append(line)
    body = "\n".join(no_comments)
    one_line = " ".join(body.split())

    OUT.write_text(
        "/* StudyRatna lecture recorder — DevTools Console me paste karein.\n"
        "   Poora ek hi line hai taaki console sab execute kare. */\n"
        + one_line + "\n",
        encoding="utf-8",
    )
    HTML.write_text(
        '<!-- Bookmarklet: is link ko bookmark bana kar lecture page par click karein -->\n'
        '<a href="javascript:' + one_line.replace('"', "%22") + '">Lecture Save</a>\n',
        encoding="utf-8",
    )

    size = len(one_line)
    print(f"[+] {OUT} ({size} chars, one line)")
    print(f"[+] {HTML} (bookmarklet)")

    # Node se syntax validate karo (agar available ho)
    try:
        proc = subprocess.run(
            ["node", "--check", str(SRC)], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            print("[!] page_recorder.js syntax error:")
            print(proc.stderr[:600])
            return 1
        print("[+] page_recorder.js syntax OK (node --check)")
    except FileNotFoundError:
        print("[i] node nahi mila - syntax check skip")

    # optional: clipboard (CI me nahi hoga, local par help karta hai)
    if "--clip" in sys.argv:
        for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["clip"]):
            try:
                subprocess.run(cmd, input=one_line.encode(), check=True, timeout=15)
                print("[+] copied to clipboard")
                break
            except Exception:
                continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
