#!/usr/bin/env bash
#
# Publish data/out as a GitHub release, for the app to download from.
#
#   pipeline/publish.sh [owner/repo]      (default L-Holmes/mapmapmap-sources)
#
# Needs the GitHub CLI (gh), logged in, and a PUBLIC repository: the app
# downloads with no account, so the files must be fetchable by anyone. The
# app is built pointing at
#
#   https://github.com/<owner/repo>/releases/latest/download/
#
# (mapsUrl in gradle.properties), so every release holds every region under
# the same names, and the newest is the one apps see. The catalogue's
# version is the OSM data's date; an installed region older than it shows
# as having an update.
#
# The release before stays, so a download begun from it can finish; older
# ones are deleted. Publishing the same data twice replaces its release.
#
# GitHub's limit is 2 GiB per file. All of Great Britain's tiles are just
# under it; this refuses to upload anything over.
#
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=${1:-L-Holmes/mapmapmap-sources}
OUT=data/out
KEEP=2
command -v gh >/dev/null || { echo "error: needs the GitHub CLI (gh); see README.md" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh is not logged in; run: gh auth login" >&2; exit 1; }
[[ -f "$OUT/catalog.json" ]] || { echo "error: no $OUT/catalog.json; run pipeline/build.sh" >&2; exit 1; }

VERSION=$(python3 -c "import json; print(json.load(open('$OUT/catalog.json'))['version'])")
TAG="maps-$VERSION"
LIMIT=$((2 * 1024 * 1024 * 1024))
# The regions, and the two files the app ships and refreshes from here:
# the region outlines and the low-zoom overview.
FILES=("$OUT/catalog.json" "$OUT"/*.mbtiles "$OUT"/*.graph "$OUT/app-regions.json")
for f in "${FILES[@]}"; do
  size=$(stat -c %s "$f")
  if (( size >= LIMIT )); then
    echo "error: $f is $size bytes, over GitHub's 2 GiB limit" >&2
    exit 1
  fi
done

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "==> $TAG exists; replacing it"
  gh release delete "$TAG" --repo "$REPO" --yes --cleanup-tag
fi

echo "==> Release $TAG on $REPO: ${#FILES[@]} files, $(du -shc "${FILES[@]}" | tail -1 | cut -f1)"
# Made as a draft and only published once every file is up, so no app ever
# sees a catalogue whose files are still uploading.
gh release create "$TAG" --repo "$REPO" --draft --title "Maps $VERSION" --notes-file - <<EOF
Map data for Great Britain from OpenStreetMap, $VERSION, and OS Terrain 50.

Each region is two files: \`<region>.mbtiles\` (vector map tiles) and
\`<region>.graph\` (the walking graph the app routes on). \`catalog.json\`
lists them with their sizes and SHA-256.

© OpenStreetMap contributors, available under the Open Database Licence.
Contains OS data © Crown copyright and database right.
EOF
for f in "${FILES[@]}"; do
  echo "    $(basename "$f")"
  gh release upload "$TAG" "$f" --repo "$REPO" --clobber
done
gh release edit "$TAG" --repo "$REPO" --draft=false --latest

echo "==> Keeping the newest $KEEP releases"
gh release list --repo "$REPO" --limit 100 --json tagName,createdAt --jq 'sort_by(.createdAt) | reverse | .[].tagName' \
  | tail -n +$((KEEP + 1)) \
  | while read -r old; do
      echo "    deleting $old"
      gh release delete "$old" --repo "$REPO" --yes --cleanup-tag
    done

echo "==> Published: https://github.com/$REPO/releases/latest/download/catalog.json"
