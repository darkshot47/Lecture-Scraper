#!/usr/bin/env bash
# Termux shortcut: hamesha latest download.mjs leke chalao
#   bash get-and-download.sh "<lecture link>"
#   bash get-and-download.sh "$(termux-clipboard-get)"
cd "$(dirname "$0")" || exit 1

BASE="https://raw.githubusercontent.com/darkshot47/Lecture-Scraper/main/local"
echo "  update ho raha hai…"
ok=1
for f in download.mjs keyprobe.mjs; do
  if curl -fsSL "$BASE/$f" -o "$f.new"; then
    mv "$f.new" "$f"
  else
    rm -f "$f.new"; ok=0
  fi
done
[ "$ok" = 1 ] && echo "  ✓ latest version" || echo "  (update fail — jo file hai usse chala rahe hain)"

# key ka pata lagane wala mode
if [ "$1" = "probe" ] || [ "$1" = "key" ]; then
  shift
  exec node keyprobe.mjs "$@"
fi

if [ -z "$1" ]; then
  echo ""
  echo '  Istemal: bash get-and-download.sh "https://app.studyratna.org/?playerToken=..."'
  echo '  ya     : bash get-and-download.sh "$(termux-clipboard-get)"'
  echo '  key check: bash get-and-download.sh probe "…link…"'
  echo ""
  exit 1
fi

exec node download.mjs "$@"
