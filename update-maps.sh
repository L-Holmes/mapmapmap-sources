#!/usr/bin/env bash
#
# Update the maps everyone downloads.
#
#   ./update-maps.sh           finish an interrupted upload, or carry on an
#                              interrupted build; otherwise, if
#                              OpenStreetMap has newer data than what is
#                              published, or the pipeline has changed since,
#                              rebuild every region and publish it
#   ./update-maps.sh --force   rebuild and publish even if nothing is newer
#
# Run it by hand whenever the maps should catch up with OpenStreetMap
# (monthly is plenty). A full run is about three hours of building (Great
# Britain, then Italy), then uploading ~18 GB (under two hours at 3 MB/s,
# more on a slow line); it says what it is doing and how long each
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
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Publishes data/out as a GitHub release, for the app to download from:
# every release holds every region under the same names, and apps read the
# newest (releases/latest/download/). It is made as a draft and published
# once every file is up, so no app sees a catalogue whose files are still
# uploading; a draft of it is an upload that stopped partway, carried on
# from where it got to. The release before stays, so a download begun from
# it can finish; older ones are deleted. GitHub's limit is 2 GiB a file.
publish() {
  OUT=data/out
  KEEP=2
  PARALLEL=4
  VERSION=$(python3 -c "import json; print(json.load(open('$OUT/catalog.json'))['version'])")
  TAG="maps-$VERSION"
  LIMIT=$((2 * 1024 * 1024 * 1024))
  # The catalogue, the regions' files it lists, and the two files the app
  # ships and refreshes from here, the region outlines and the low-zoom
  # overview: just those, whatever else is in data/out.
  mapfile -t FILES < <(python3 -c "
import json, sys
out = sys.argv[1]
c = json.load(open(out + '/catalog.json'))
print(out + '/catalog.json')
for name in [f['name'] for r in c['regions'] for f in r['files']] + [f['name'] for f in c.get('assets', [])]:
    print(out + '/' + name)" "$OUT")
  for f in "${FILES[@]}"; do
    size=$(stat -c %s "$f")
    if (( size >= LIMIT )); then
      echo "error: $f is $size bytes, over GitHub's 2 GiB limit" >&2
      exit 1
    fi
  done

  # A draft of this release is an upload that stopped partway: carry on from
  # where it got to. A published one is being replaced. IS_DRAFT is "true",
  # "false", or empty for no such release. It is read off the list so that a
  # dropped connection stops the run, rather than passing for "no release"
  # and starting a second draft of it.
  IS_DRAFT=$(gh release list --repo "$REPO" --json tagName,isDraft --jq ".[] | select(.tagName == \"$TAG\") | .isDraft")
  UPLOADED=""
  if [[ "$IS_DRAFT" == "true" ]]; then
    echo "==> Resuming the unfinished upload of $TAG"
    UPLOADED=$(gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[] | select(.state == "uploaded") | "\(.name) \(.size)"')
  elif [[ "$IS_DRAFT" == "false" ]]; then
    echo "==> $TAG exists; replacing it"
    gh release delete "$TAG" --repo "$REPO" --yes --cleanup-tag
  fi

  echo "==> Release $TAG on $REPO: ${#FILES[@]} files, $(du -shc "${FILES[@]}" | tail -1 | cut -f1)"
  if [[ "$IS_DRAFT" != "true" ]]; then
    # Made as a draft and only published once every file is up, so no app
    # ever sees a catalogue whose files are still uploading.
    NOTES="Map data for Great Britain and Italy, from OpenStreetMap; heights from OS Terrain 50 and the Environment Agency's LIDAR in Great Britain, and the Copernicus DEM in Italy; version $VERSION (Great Britain's data's date, and the pipeline's revision).

Each region is five files: \`<region>.mbtiles\` (vector map tiles), \`<region>.graph\` (the walking graph the app routes on), \`<region>.driving.graph\` (the driving graph), and \`<region>.shade.mbtiles\` and \`<region>.slope.mbtiles\` (raster tiles of hill shading, and of steep ground). \`catalog.json\` lists them with their sizes and SHA-256. \`overview.mbtiles\` and \`app-regions.json\` are what the app ships inside itself.

© OpenStreetMap contributors, available under the Open Database Licence. Contains OS data © Crown copyright and database right. Contains Environment Agency LIDAR data © Environment Agency copyright and/or database right, under the Open Government Licence v3.0. Heights in Italy produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved."
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
  echo "    ${#TODO[@]} files to upload, $((LEFT / 1000000)) MB, $PARALLEL at a time"
  # Several at once: GitHub gives one upload a fraction of what the line can
  # do. The next starts as soon as any finishes. Over hours of uploading a
  # connection is bound to drop now and then, so a file that fails is tried
  # again, five tries in all; one that still fails stops the run (run it
  # again to carry on from there). Stopping for any reason, Ctrl-C too, stops
  # the uploads under way, rather than leaving them running behind the prompt.
  DONE=0                 # bytes in the files finished
  BEGAN=$SECONDS
  SAID=$SECONDS          # when it last said how far along it is
  LOGS=$(mktemp -d)      # what each file's latest try said
  declare -A RUNNING=()  # upload's pid -> its file
  trap 'for j in $(jobs -p); do kill $j $(pgrep -P $j) 2>/dev/null || true; done; rm -rf "$LOGS"' EXIT
  trap 'echo; echo "==> Stopped. What is up so far stays up: run ./update-maps.sh again to carry on."; exit 130' INT TERM HUP
  upload() {
    local name try
    name=$(basename "$1")
    for try in 1 2 3 4 5; do
      gh release upload "$TAG" "$1" --repo "$REPO" --clobber >"$LOGS/$name" 2>&1 && return 0
      (( try < 5 )) || return 1
      echo "    $name failed ($(tail -1 "$LOGS/$name")); trying again in $try min"
      sleep $((try * 60))
    done
  }
  # How far along it is: the files finished, and as far as each upload under
  # way has read into its file.
  progress() {
    local sent=$DONE pid p fd pos took
    for pid in "${!RUNNING[@]}"; do
      for p in $(pgrep -P "$pid"); do
        for fd in /proc/$p/fd/*; do
          [[ "$(readlink "$fd" 2>/dev/null)" == "${RUNNING[$pid]}" ]] || continue
          pos=$(awk '/^pos:/ {print $2}' "/proc/$p/fdinfo/${fd##*/}" 2>/dev/null || true)
          sent=$((sent + ${pos:-0}))
        done
      done
    done
    took=$((SECONDS - BEGAN))
    printf '%d of %d MB done, ' $((sent / 1000000)) $((LEFT / 1000000))
    if (( sent > 0 && took > 0 )); then
      printf '%d kB/s, about %d min left' $((sent / took / 1000)) $(( (LEFT - sent) * took / sent / 60 ))
    else
      printf 'measuring speed'
    fi
  }
  # Waits for an upload to end, saying every minute meanwhile how it is
  # going: the biggest file takes the best part of an hour.
  finish_one() {
    local pid f names
    while :; do
      for pid in "${!RUNNING[@]}"; do
        kill -0 "$pid" 2>/dev/null && continue
        f=${RUNNING[$pid]}
        unset "RUNNING[$pid]"
        if ! wait "$pid"; then
          echo "error: $(basename "$f") would not upload (five tries, over ten minutes); the last said:" >&2
          echo "       $(tail -1 "$LOGS/$(basename "$f")")" >&2
          echo "       What is up so far stays up: run ./update-maps.sh again to carry on from here." >&2
          exit 1
        fi
        DONE=$((DONE + $(stat -c %s "$f")))
        return
      done
      sleep 5
      if (( SECONDS - SAID >= 60 )); then
        names=""
        for pid in "${!RUNNING[@]}"; do names="${names:+$names, }$(basename "${RUNNING[$pid]}")"; done
        echo "    ... $(progress); uploading $names"
        SAID=$SECONDS
      fi
    done
  }
  for i in "${!TODO[@]}"; do
    f=${TODO[$i]}
    while (( ${#RUNNING[@]} >= PARALLEL )); do finish_one; done
    printf '    [%d/%d] %s, %d MB  (%s)\n' $((i + 1)) ${#TODO[@]} "$(basename "$f")" \
      $(( $(stat -c %s "$f") / 1000000 )) "$(progress)"
    SAID=$SECONDS
    upload "$f" &
    RUNNING[$!]=$(realpath "$f")
  done
  while (( ${#RUNNING[@]} > 0 )); do finish_one; done
  gh release edit "$TAG" --repo "$REPO" --draft=false --latest

  echo "==> Keeping the newest $KEEP releases"
  # By when each was published: their creation times are their tags', which
  # can be the same for two.
  gh release list --repo "$REPO" --limit 100 --json tagName,publishedAt,isDraft \
    --jq '[.[] | select(.isDraft | not)] | sort_by(.publishedAt) | reverse | .[].tagName' \
    | tail -n +$((KEEP + 1)) \
    | while read -r old; do
        echo "    deleting $old"
        gh release delete "$old" --repo "$REPO" --yes --cleanup-tag
      done

  echo "==> Published: https://github.com/$REPO/releases/latest/download/catalog.json"
}

lock

command -v gh >/dev/null || { echo "error: needs the GitHub CLI (gh); see README.md" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: the GitHub CLI is not logged in; run: gh auth login" >&2; exit 1; }

# An upload that stopped partway is finished first: its release is a draft,
# invisible to the app, until every file is up.
BUILT=$(python3 -c "import json; print(json.load(open('data/out/catalog.json'))['version'])" 2>/dev/null || true)
DRAFT=$(gh release list --repo "$REPO" --json tagName,isDraft --jq '.[] | select(.isDraft) | .tagName' | head -1)
if [[ -n "$DRAFT" && "$DRAFT" == "maps-$BUILT" && "$BUILT" == *".$(revision)" ]]; then
  echo "==> An upload of $DRAFT stopped partway; finishing it"
  publish
  # That is this run's job done: apps have the maps now. Anything newer
  # waits for the next run, rather than hours more building on the back of it.
  echo
  echo "==> Maps updated in $(elapsed). Run this again to check for anything newer."
  exit 0
fi

# Any other draft is an upload left unfinished of maps this pipeline no
# longer makes (it has changed since): it goes, rather than ever being
# finished, or counted among the releases kept.
for old in $(gh release list --repo "$REPO" --json tagName,isDraft --jq '.[] | select(.isDraft) | .tagName'); do
  echo "==> Dropping $old: an upload left unfinished, of maps this pipeline no longer makes"
  gh release delete "$old" --repo "$REPO" --yes --cleanup-tag
done

# Temporary files on the data disk, not in /tmp, which may be memory (a
# tmpfs): the peak scores unpack the sea's polygons there, 1.3 GB, and a
# worker ended with its job leaves its copy behind. Emptied every run.
export TMPDIR="$PWD/data/tmp"
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR"

# A build that stopped partway (some of its areas built, from the data here,
# by this pipeline; or all of them, but not its catalogue) carries on with
# that data: fetching newer, which Geofabrik has every day, would start
# every area over.
DONE=0
AREAS=0
UNFINISHED=0
for area in $(data/.venv/bin/python pipeline/areas.py ids 2>/dev/null); do
  AREAS=$((AREAS + 1))
  eval "$(data/.venv/bin/python pipeline/areas.py shell "$area")"
  if [[ "$(cat "data/work/$area/built" 2>/dev/null)" == "$(revision) $(cat "data/src/$STEM.name" 2>/dev/null)" ]]; then
    DONE=$((DONE + 1))
    [[ "data/work/$area/built" -nt data/out/catalog.json ]] && UNFINISHED=1
  fi
done
if (( DONE > 0 && (DONE < AREAS || UNFINISHED) )); then
  echo "==> A build stopped partway, with $DONE of its $AREAS areas built: carrying on from there, with the same map data"
  pipeline/build.sh
  publish
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
publish
echo
echo "==> Maps updated in $(elapsed)"
