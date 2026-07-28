#!/usr/bin/env bash
# Re-download the front-end assets that used to come from CDNs.
#
#   scripts/vendor_assets.sh        (needs internet; writes static/vendor/)
#
# WHY THESE ARE IN THE REPO
# The tutor serves students from a Jetson over its own WiFi hotspot, with no
# internet. Anything fetched from fonts.googleapis.com or cdn.jsdelivr.net just
# fails there. For the fonts that was cosmetic; for the scripts it was fatal —
# templates/tutoring/chat_tutor.html calls marked.parse() unguarded, so a
# missing `marked` threw a ReferenceError and no message rendered at all.
#
# Run this when a version below changes, then commit the result.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/static/vendor"

KATEX_VERSION="0.16.38"
DOMPURIFY_VERSION="3.1.6"
FONTS_URL="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap"
# Google Fonts serves woff2 only to browsers; a curl UA gets legacy ttf.
BROWSER_UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

mkdir -p "$OUT"/{js,css,css/fonts,fonts}

echo "==> scripts"
curl -fsS -o "$OUT/js/marked.min.js"  "https://cdn.jsdelivr.net/npm/marked/marked.min.js"
curl -fsS -o "$OUT/js/purify.min.js"  "https://cdn.jsdelivr.net/npm/dompurify@${DOMPURIFY_VERSION}/dist/purify.min.js"
curl -fsS -o "$OUT/js/katex.min.js"   "https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}/dist/katex.min.js"
curl -fsS -o "$OUT/js/katex-auto-render.min.js" \
    "https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}/dist/contrib/auto-render.min.js"

echo "==> katex css + fonts"
curl -fsS -o "$OUT/css/katex.min.css" "https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}/dist/katex.min.css"
# katex.min.css references fonts/ relative to itself, so they must live beside it.
grep -oE 'fonts/KaTeX_[A-Za-z0-9_-]+\.(woff2|woff|ttf)' "$OUT/css/katex.min.css" \
    | sort -u \
    | while read -r f; do
        curl -fsS -o "$OUT/css/$f" "https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}/dist/$f"
    done
echo "    $(ls "$OUT/css/fonts" | wc -l) font files"

echo "==> google fonts"
curl -fsS -A "$BROWSER_UA" "$FONTS_URL" -o "$OUT/css/google-fonts.css"
grep -oE 'https://fonts\.gstatic\.com[^)]+\.woff2' "$OUT/css/google-fonts.css" \
    | sort -u \
    | while read -r u; do
        curl -fsS -o "$OUT/fonts/$(basename "$u")" "$u"
    done
# Rewrite absolute gstatic URLs to the local copies (../fonts/ is relative to css/).
sed -i -E 's#https://fonts\.gstatic\.com[^)]*/([^/)]+\.woff2)#../fonts/\1#g' \
    "$OUT/css/google-fonts.css"
echo "    $(ls "$OUT/fonts" | wc -l) font files"

remaining=$(grep -rlE 'https?://(fonts\.(googleapis|gstatic)|cdn\.jsdelivr)' "$OUT" 2>/dev/null | wc -l)
[ "$remaining" -eq 0 ] || { echo "ERROR: $remaining vendored file(s) still reference a CDN" >&2; exit 1; }
echo "==> done — no external references remain in static/vendor/"
