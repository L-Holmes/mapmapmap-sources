#!/usr/bin/env bash
#
# Update the maps everyone downloads.
#
#   ./update-maps.sh           finish an interrupted upload; otherwise, if
#                              OpenStreetMap has newer data than what is
#                              published, or the pipeline has changed since,
#                              rebuild every region and publish it
#   ./update-maps.sh --force   rebuild and publish even if nothing is newer
#
# Run it by hand whenever the maps should catch up with OpenStreetMap
# (monthly is plenty). A full run is about two hours of building, then
# uploading ~12 GB (an hour at 3 MB/s, four on a slow line); it says what it is doing and how long each
# part usually takes. No app release is needed: apps see the new data the
# next time they check, and offer each region's update. Needs Java 21+, uv
# and a logged-in GitHub CLI; see README.md.
#
set -euo pipefail
cd "$(dirname "$0")"
source pipeline/common.sh

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done
lock

command -v gh >/dev/null || { echo "error: needs the GitHub CLI (gh); see README.md" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: the GitHub CLI is not logged in; run: gh auth login" >&2; exit 1; }

# An upload that stopped partway is finished first: its release is a draft,
# invisible to the app, until every file is up.
BUILT=$(python3 -c "import json; print(json.load(open('data/out/catalog.json'))['version'])" 2>/dev/null || true)
DRAFT=$(gh release list --repo "$REPO" --json tagName,isDraft --jq '.[] | select(.isDraft) | .tagName' | head -1)
if [[ -n "$DRAFT" && "$DRAFT" == "maps-$BUILT" ]]; then
  echo "==> An upload of $DRAFT stopped partway; finishing it"
  pipeline/publish.sh
  # That is this run's job done: apps have the maps now. Anything newer
  # waits for the next run, rather than hours more building on the back of it.
  echo
  echo "==> Maps updated in $(elapsed). Run this again to check for anything newer."
  exit 0
fi

# Is there anything newer to build? A version is the map data's date and
# the pipeline's revision (pipeline/common.sh): either changing is new.
echo "==> Checking for newer map data, or a changed pipeline"
PUBLISHED=$(curl -fsL "$RELEASES/latest/download/catalog.json" | python3 -c "import json, sys; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo none)
NEWEST=$(curl -fsSL https://download.geofabrik.de/europe/united-kingdom.html \
  | grep -o 'united-kingdom-[0-9]\{6\}\.osm\.pbf' | sort -u | tail -1 | sed -E 's/.*-([0-9]{2})([0-9]{2})([0-9]{2})\..*/20\1-\2-\3/')
WANT="$NEWEST.$(revision)"
echo "    published: $PUBLISHED; newest data and this pipeline would be: $WANT"
if [[ "$PUBLISHED" == "$WANT" && "$FORCE" -eq 0 ]]; then
  echo "==> The published maps are already from the newest data and this pipeline. Nothing to do (--force rebuilds anyway)."
  exit 0
fi

pipeline/build.sh --fresh
pipeline/publish.sh
echo
echo "==> Maps updated in $(elapsed)"
