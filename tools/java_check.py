"""Java source ka lightweight sanity check (brace balance, string strip)."""
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else \
    "android/app/src/main/java/org/studyratna/saver/MainActivity.java"
src = open(path, encoding="utf-8").read()
clean = re.sub(r'"(\\.|[^"\\])*"', '""', src)
clean = re.sub(r"'(\\.|[^'\\])*'", "''", clean)
clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.S)
clean = re.sub(r"//[^\n]*", "", clean)

depth = 0
for i, ch in enumerate(clean):
    if ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth < 0:
            print("EXTRA CLOSING BRACE at line", clean[:i].count("\n") + 1)
            sys.exit(1)
parens = clean.count("(") - clean.count(")")
brackets = clean.count("[") - clean.count("]")
print(f"paths: {path}")
print(f"brace depth: {depth} | paren delta: {parens} | bracket delta: {brackets}")
if depth or parens or brackets:
    sys.exit(1)
print("Java structural check OK")
