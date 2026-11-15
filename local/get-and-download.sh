#!/usr/bin/env bash
# Termux shortcut: hamesha latest download.mjs leke chalao
#   bash get-and-download.sh "<lecture link>"
#   bash get-and-download.sh "$(termux-clipboard-get)"
cd "$(dirname "$0")" || exit 1

RAW="https://raw.githubusercontent.com/darkshot47/Lecture-Scraper/main/local/download.mjs"
echo "  download.mjs update ho raha hai…"
if curl -fsSL "$RAW" -o download.mjs.new; then
  mv download.mjs.new download.mjs
  echo "  ✓ latest version"
else
  rm -f download.mjs.new
  echo "  (update fail — jo file hai usse chala rahe hain)"
fi

if [ -z "$1" ]; then
  echo ""
  echo '  Istemal: bash get-and-download.sh "https://app.studyratna.org/?playerToken=..."'
  echo '  ya     : bash get-and-download.sh "$(termux-clipboard-get)"'
  echo ""
  exit 1
fi

exec node download.mjs "$@"
