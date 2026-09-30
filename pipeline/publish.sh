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
# so every release holds every region under the same names, and the newest
# is the one apps see. The catalogue's version is the OSM data's date; an
# installed region older than it shows as having an update.
#
# The release before stays, so a download begun from it can finish; older
# ones are deleted. Publishing the same data twice replaces its release,
# and an upload that stopped partway picks up where it left off.
#
# GitHub's limit is 2 GiB per file. All of Great Britain's tiles are just
# under it; this refuses to upload anything over.
#
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=${1:-L-Holmes/mapmapmap-sources}
source pipeline/common.sh
lock
OUT=data/out
KEEP=2
command -v gh >/dev/null || { echo "error: needs the GitHub CLI (gh); see README.md" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh is not logged in; run: gh auth login" >&2; exit 1; }
[[ -f "$OUT/catalog.json" ]] || { echo "error: no $OUT/catalog.json; run pipeline/build.sh" >&2; exit 1; }

VERSION=$(python3 -c "import json; print(json.load(open('$OUT/catalog.json'))['version'])")
TAG="maps-$VERSION"
LIMIT=$((2 * 1024 * 1024 * 1024))
# The regions, and the two files the app ships and refreshes from here: the
# region outlines and the low-zoom overview (which *.mbtiles includes).
FILES=("$OUT/catalog.json" "$OUT"/*.mbtiles "$OUT"/*.graph "$OUT/app-regions.json")
for f in "${FILES[@]}"; do
  size=$(stat -c %s "$f")
  if (( size >= LIMIT )); then
    echo "error: $f is $size bytes, over GitHub's 2 GiB limit" >&2
    exit 1
  fi
done

# A draft of this release is an upload that stopped partway: carry on from
# where it got to. A published one is being replaced.
UPLOADED=""
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  if [[ "$(gh release view "$TAG" --repo "$REPO" --json isDraft --jq .isDraft)" == "true" ]]; then
    echo "==> Resuming the unfinished upload of $TAG"
    UPLOADED=$(gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[] | "\(.name) \(.size)"')
  else
    echo "==> $TAG exists; replacing it"
    gh release delete "$TAG" --repo "$REPO" --yes --cleanup-tag
  fi
fi

echo "==> Release $TAG on $REPO: ${#FILES[@]} files, $(du -shc "${FILES[@]}" | tail -1 | cut -f1)"
if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  # Made as a draft and only published once every file is up, so no app
  # ever sees a catalogue whose files are still uploading.
  NOTES="Map data for Great Britain from OpenStreetMap, $VERSION, and OS Terrain 50.

Each region is two files: \`<region>.mbtiles\` (vector map tiles) and \`<region>.graph\` (the walking graph the app routes on). \`catalog.json\` lists them with their sizes and SHA-256. \`overview.mbtiles\` and \`app-regions.json\` are what the app ships inside itself.

© OpenStreetMap contributors, available under the Open Database Licence. Contains OS data © Crown copyright and database right."
  gh release create "$TAG" --repo "$REPO" --draft --title "Maps $VERSION" --notes "$NOTES"
fi

# What is left, with its size, to say how far along the upload is.
TODO=()
LEFT=0
for f in "${FILES[@]}"; do
  size=$(stat -c %s "$f")
  grep -qx "$(basename "$f") $size" <<<"$UPLOADED" && continue
  TODO+=("$f")
  LEFT=$((LEFT + size))
done
echo "    ${#TODO[@]} files to upload, $((LEFT / 1000000)) MB"
DONE=0
BEGAN=$SECONDS
for i in "${!TODO[@]}"; do
  f=${TODO[$i]}
  size=$(stat -c %s "$f")
  took=$((SECONDS - BEGAN))
  if (( DONE > 0 && took > 0 )); then
    eta=$(( (LEFT - DONE) * took / DONE / 60 ))
    rate="$((DONE / took / 1000)) kB/s, about $eta min left"
  else
    rate="measuring speed"
  fi
  printf '    [%d/%d] %s, %d MB  (%d of %d MB done, %s)\n' $((i + 1)) ${#TODO[@]} "$(basename "$f")" \
    $((size / 1000000)) $((DONE / 1000000)) $((LEFT / 1000000)) "$rate"
  gh release upload "$TAG" "$f" --repo "$REPO" --clobber
  DONE=$((DONE + size))
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
