#!/usr/bin/env python3
"""vercel.json ka region badalne ke liye (workflow fallback ke liye)."""
import json, sys
reg = sys.argv[1]
d = json.load(open("vercel.json"))
d["regions"] = [reg]
json.dump(d, open("vercel.json", "w"), indent=2)
print("regions ->", reg)
