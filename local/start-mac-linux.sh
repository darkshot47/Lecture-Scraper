#!/usr/bin/env bash
# Lecture -> MP4 local relay
cd "$(dirname "$0")"

if ! command -v node >/dev/null 2>&1; then
  echo
  echo "  Node.js nahi mila. Install karein: https://nodejs.org  (LTS)"
  echo
  exit 1
fi

echo
echo "  Local relay shuru ho raha hai..."
echo "  Browser khud khulega. Band karne ke liye Ctrl+C."
echo

( sleep 1; if command -v xdg-open >/dev/null; then xdg-open http://localhost:8787
  elif command -v open >/dev/null; then open http://localhost:8787; fi ) &

exec node localrelay.mjs
