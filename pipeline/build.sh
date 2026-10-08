#!/usr/bin/env bash
#
# Build every region's map, walking graph and driving graph, into data/out.
#
#   pipeline/build.sh            build from the OpenStreetMap data already here
#                                (downloading it if there is none)
#   pipeline/build.sh --fresh    first fetch Geofabrik's newest, if newer
#   pipeline/build.sh --download-only   fetch the data, build nothing
#
# The areas (pipeline/areas.py: Great Britain, then Italy) are built one
# after another, each start to end. One built already from the same data by
# the same pipeline (by a run that stopped in a later one) is not built
# again. Needs Java 21+, uv, ~24 GB of RAM, and ~100 GB of disk the first
# time (~40 GB after). Takes about three hours; each step says roughly how
# long it usually takes.
# Everything lands under data/ (gitignored): data/src downloads, data/work
# intermediate files, an area's in data/work/<area>, data/out what gets
# published.
#
set -euo pipefail
cd "$(dirname "$0")/.."
source pipeline/common.sh

FRESH=0
ONLY_DOWNLOAD=0
for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    --download-only) ONLY_DOWNLOAD=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done
lock

SRC=data/src WORK=data/work OUT=data/out
PY=data/.venv/bin/python
mkdir -p "$SRC" "$WORK" "$OUT"

# --- Tools ---------------------------------------------------------------------

STEPS=3
N=1
step $N $STEPS "Tools" "seconds, or a minute the first time"
command -v java >/dev/null || { echo "error: needs Java 21 or newer" >&2; exit 1; }
command -v uv >/dev/null || { echo "error: needs uv (https://docs.astral.sh/uv/)" >&2; exit 1; }
if [[ ! -x "$PY" ]]; then
  uv venv -q data/.venv
fi
# Every run, so a venv from before a package was needed gets it (quick when it has them all).
VIRTUAL_ENV=data/.venv uv pip install -q numpy scipy shapely osmium contourpy pyproj pyshp pillow tifffile imagecodecs scikit-image
PLANETILER_VERSION=v0.10.2
[[ -f "$SRC/planetiler.jar" ]] || curl -fL --progress-bar -o "$SRC/planetiler.jar" \
  "https://github.com/onthegomap/planetiler/releases/download/$PLANETILER_VERSION/planetiler.jar"
AREAS=$($PY pipeline/areas.py ids)
STEPS=$((3 + 10 * $(wc -w <<<"$AREAS") + 2))

# Great Britain's files, from before there were other areas, where its are now.
if [[ -d "$WORK/terrain" && ! -d "$WORK/gb" ]]; then
  mkdir "$WORK/gb"
  for f in terrain jut parking graph.npz driving.npz omt.mbtiles hiking.mbtiles merged.mbtiles shade.mbtiles slope.mbtiles; do
    [[ -e "$WORK/$f" ]] && mv "$WORK/$f" "$WORK/gb/"
  done
fi

# Room for it all: better told now than hours in. An area not yet built
# needs room for its heights and what is made from them.
FREE=$(df --output=avail -B1G data | tail -1 | tr -dc '0-9')
NEED=40
for area in $AREAS; do
  [[ -f "$WORK/$area/built" ]] || NEED=100
done
if (( FREE < NEED )); then
  echo "==> Stopping before building: this needs about $NEED GB free on the disk data/ is on, and there are $FREE GB."
  echo "    Free some room, then run it again."
  exit 1
fi

# --- Sources -------------------------------------------------------------------

N=$((N + 1))
step $N $STEPS "OpenStreetMap and height data" "a few minutes when downloading, seconds if not"
GEOFABRIK=https://download.geofabrik.de

# fetch <path>: Geofabrik's extract (europe/alps) into $SRC/<name>.osm.pbf, if
# it has none or (--fresh) Geofabrik has a newer one.
fetch() {
  local path=$1 stem=${1##*/} newest have part size
  # By date: "-latest" is a redirect some proxies mangle.
  newest=$(curl -fsSL "$GEOFABRIK/$path.html" | grep -o "$stem-[0-9]\{6\}\.osm\.pbf" | sort -u | tail -1)
  # Only a file this script downloaded and checked counts, which it marks by
  # writing its name beside it: anything else (half a download, a copy put
  # there by hand) is fetched afresh.
  have=$(cat "$SRC/$stem.name" 2>/dev/null || true)
  if [[ -f "$SRC/$stem.osm.pbf" && -n "$have" && ( "$FRESH" -eq 0 || "$have" == "$newest" ) ]]; then
    if [[ "$FRESH" -eq 1 ]]; then
      echo "    have $have, which is Geofabrik's newest"
    else
      echo "    have $have (Geofabrik's newest is $newest; --fresh to fetch it)"
    fi
    return
  fi
  # Beside the old copy, which stays until the new one is whole and checked;
  # an interrupted download carries on from where it stopped.
  part="$SRC/$newest.part"
  size=$(curl -fsSIL "$GEOFABRIK/${path%/*}/$newest" | grep -i '^content-length' | tail -1 | tr -dc '0-9')
  size=${size:-0}
  echo "    downloading $newest ($((size / 1000000)) MB)"
  if [[ ! -f "$part" || $(stat -c %s "$part") -lt $size ]]; then
    curl -fL --progress-bar -C - -o "$part" "$GEOFABRIK/${path%/*}/$newest"
  fi
  echo "    checking it"
  (cd "$SRC" && echo "$(curl -fsSL "$GEOFABRIK/${path%/*}/$newest.md5" | cut -d' ' -f1)  $newest.part" | md5sum -c --quiet -)
  rm -f "$SRC/$stem.name"
  mv "$part" "$SRC/$stem.osm.pbf"
  echo "$newest" > "$SRC/$stem.name"
  # The regions' outlines, as new as the data.
  curl -fsSL -o "$SRC/index-v1.json" "$GEOFABRIK/index-v1.json"
}
for area in $AREAS; do
  eval "$($PY pipeline/areas.py shell "$area")"
  fetch "$EXTRACT"
done
[[ -f "$SRC/index-v1.json" ]] || curl -fsSL -o "$SRC/index-v1.json" "$GEOFABRIK/index-v1.json"
# The sea, as OpenStreetMap's coastline has it: Planetiler's for the base
# map, and the peak scores' (before Planetiler would fetch it).
if [[ ! -f "$SRC/sources/water-polygons-split-3857.zip" ]]; then
  echo "    downloading the sea's polygons (900 MB)"
  mkdir -p "$SRC/sources"
  curl -fL --progress-bar -o "$SRC/sources/water-polygons-split-3857.zip.part" \
    https://osmdata.openstreetmap.de/download/water-polygons-split-3857.zip
  mv "$SRC/sources/water-polygons-split-3857.zip.part" "$SRC/sources/water-polygons-split-3857.zip"
fi
if [[ ! -f "$SRC/terr50_gagg_gb.zip" ]]; then
  echo "    downloading OS Terrain 50 (160 MB)"
  curl -fL --progress-bar -o "$SRC/terr50_gagg_gb.zip.part" \
    "https://api.os.uk/downloads/v1/products/Terrain50/downloads?area=GB&format=ASCII+Grid+and+GML+%28Grid%29&redirect"
  mv "$SRC/terr50_gagg_gb.zip.part" "$SRC/terr50_gagg_gb.zip"
fi
# (The Copernicus DEM, for the other areas' heights, is fetched as each is built.)

# The release's version: Great Britain's OSM data's own date, and the
# pipeline's revision. (Geofabrik makes every extract each day: the others'
# are of the same day's data, give or take.)
DATA=$($PY -c "
import osmium, sys
r = osmium.io.Reader(sys.argv[1], osmium.osm.osm_entity_bits.NOTHING)
print(r.header().get('osmosis_replication_timestamp')[:10])
r.close()" "$SRC/united-kingdom.osm.pbf")
VERSION="$DATA.$(revision)"
echo "    map data from $DATA; this build is version $VERSION"
if [[ "$ONLY_DOWNLOAD" -eq 1 ]]; then
  echo "==> Downloaded; built nothing (--download-only)"
  exit 0
fi

# --- Build ---------------------------------------------------------------------

N=$((N + 1))
step $N $STEPS "Regions" "seconds"
# The full outlines for cutting, and simplified ones the app ships to tell
# which region you are in with no signal (published as app-regions.json).
$PY pipeline/regions.py "$SRC/index-v1.json" "$WORK/regions.json" "$OUT/app-regions.json"

GB_SIZE=$(stat -c %s "$SRC/united-kingdom.osm.pbf")

# usually <minutes>: how long a step usually takes in this area, from the
# minutes it takes in Great Britain, by the size of the area's extract.
usually() {
  local m=$(( ($1 * $(stat -c %s "$PBF") + GB_SIZE / 2) / GB_SIZE ))
  if (( m < 2 )); then echo "a minute or two"
  elif (( m < 90 )); then echo "about $m minutes"
  else echo "about $(( (m + 30) / 60 )) hours"
  fi
}

# next <what> <how long>: the next step, of this area.
next() {
  N=$((N + 1))
  step $N $STEPS "${AREA_NAME^}: $1" "$2"
}

build_area() {
  local area=$1
  eval "$($PY pipeline/areas.py shell "$area")"
  W=$WORK/$area
  PBF=$SRC/$STEM.osm.pbf
  mkdir -p "$W"
  # What it is built from: this pipeline, and the extract by its date.
  local stamp
  stamp="$(revision) $(cat "$SRC/$STEM.name")"
  if [[ "$(cat "$W/built" 2>/dev/null)" == "$stamp" && -f "$W/overview.mbtiles" ]]; then
    N=$((N + 10))
    printf '\n==> [%s/%s] %s: built already, from %s by this pipeline (in a run that stopped later on): kept\n' \
      "$N" "$STEPS" "${AREA_NAME^}" "$(cat "$SRC/$STEM.name")"
    return
  fi
  rm -f "$W/built"

  local heights=$W/terrain/heights.npy graph_heights
  if [[ "$HEIGHTS" == gb ]]; then
    next "heights and contours" "a minute; 1 more the first time, half an hour more fetching England's LIDAR (6 GB)"
    if [[ -f "$W/terrain/dem.npy" ]]; then
      echo "    have OS Terrain 50's heights and contours"
    else
      $PY pipeline/terrain.py "$SRC/terr50_gagg_gb.zip" "$W/terrain"
    fi
    # 20 m heights for the relief: the Environment Agency's LIDAR in England, Terrain 50 elsewhere.
    $PY -u pipeline/lidar.py "$WORK/regions.json" "$W/terrain/dem.npy" "$SRC/lidar" "$heights"
    # The graphs' climbs are Terrain 50's, as they always have been.
    graph_heights=$W/terrain/dem.npy
  else
    next "heights and contours, from the Copernicus DEM" "seconds; the first time, 10 to 40 minutes, fetching the DEM (some GB)"
    $PY -u pipeline/copernicus.py "$area" "$WORK/regions.json" "$SRC/copernicus" "$W/terrain"
    graph_heights=$heights
  fi

  # The relief and the graphs come before the tiles: the peak scores, which
  # go in the tiles, are worked out from the relief's heights and the graphs' ways.
  next "relief: hill shading and steep ground" "seconds when the heights are as they were; else $(usually 20), more in the mountains"
  if [[ "$W/shade.mbtiles" -nt "$heights" && "$W/slope.mbtiles" -nt "$heights" \
        && "$W/shade.mbtiles" -nt pipeline/relief.py && "$W/slope.mbtiles" -nt pipeline/relief.py ]]; then
    echo "    have them: the heights are as they were when they were made"
  else
    $PY -u pipeline/relief.py "$heights" "$W/shade.mbtiles" "$W/slope.mbtiles" "$BOUNDS" "$WORK/regions.json" "$area"
  fi
  $PY pipeline/tiles.py cut-as shade.mbtiles "$W/shade.mbtiles" "$WORK/regions.json" "$area" "$OUT"
  $PY pipeline/tiles.py cut-as slope.mbtiles "$W/slope.mbtiles" "$WORK/regions.json" "$area" "$OUT"

  next "walking graph" "$(usually 20)"
  $PY -u pipeline/graph.py build "$PBF" "$graph_heights" "$W/graph.npz"
  $PY -u pipeline/graph.py cut "$W/graph.npz" "$WORK/regions.json" "$area" "$OUT"

  next "driving graph" "$(usually 15)"
  $PY -u pipeline/graph.py build-driving "$PBF" "$graph_heights" "$W/driving.npz"
  $PY -u pipeline/graph.py cut "$W/driving.npz" "$WORK/regions.json" "$area" "$OUT"

  next "peak scores: how each peak rises above the paths, roads, sea and lakes (three sizes) round it" "$(usually 12)"
  $PY -u pipeline/jut.py "$area" "$PBF" "$SRC/sources/water-polygons-split-3857.zip" "$WORK/regions.json" \
    "$SRC/index-v1.json" "$heights" "$W/graph.npz" "$W/driving.npz" "$W/jut"

  next "car parks: how far each is from a path a walk would use" "$(usually 10)"
  mkdir -p "$W/parking"
  $PY -u pipeline/parking.py "$area" "$PBF" "$W/parking/near.tsv"

  next "how much the ways are walked, where there are reference heat tiles ($SRC/walked)" "seconds"
  $PY -u pipeline/walked.py "$area" "$BOUNDS" "$W/graph.npz" "$W/driving.npz" "$SRC/walked" "$W/walked"

  # Outside Great Britain, tiles only round the regions: the extract's
  # bounds hold a lot of sea and other countries, all empty tiles.
  local polygon=()
  if [[ "$HEIGHTS" != gb ]]; then
    $PY pipeline/areas.py poly "$area" "$WORK/regions.json" "$W/area.poly"
    polygon=(--polygon="$W/area.poly")
  fi

  next "base map tiles (Planetiler, OpenMapTiles)" "$(usually 18)"
  echo "    Planetiler's own log follows, with its own progress and time left. Its few WAR lines about"
  echo "    boundaries it cannot close are neighbours' borders that run past the map's edge: expected."
  java -Xmx12g -jar "$SRC/planetiler.jar" --osm-path="$PBF" \
    --output="$W/omt.mbtiles" --download --download-dir="$SRC/sources" --tmpdir="$WORK/tmp" --force \
    --languages=en --exclude-layers=housenumber --maxzoom=14 --bounds="$BOUNDS" "${polygon[@]}"

  next "hiking tiles: paths, rights of way, routes, contours, car parks, peak scores, how walked" "$(usually 3)"
  echo "    Planetiler's own log follows; its \"data errors:\" list at the end is usually empty: expected."
  java -Xmx12g -cp "$SRC/planetiler.jar" pipeline/hiking/Hiking.java --osm-path="$PBF" \
    --contours="$W/terrain/contours" --jut="$W/jut" --parking="$W/parking/near.tsv" --walked="$W/walked" \
    --output="$W/hiking.mbtiles" \
    --tmpdir="$WORK/tmp" --force --maxzoom=14 --bounds="$BOUNDS" "${polygon[@]}"

  next "merge and cut tiles by region" "$(usually 2)"
  $PY pipeline/tiles.py merge "$W/omt.mbtiles" "$W/hiking.mbtiles" "$W/merged.mbtiles" "© OpenMapTiles © OpenStreetMap contributors; $CREDIT"
  # Made afresh every run: room kept for what is not.
  rm -f "$W/omt.mbtiles" "$W/hiking.mbtiles"
  # Its z0-7 too, for the overview the app ships.
  $PY pipeline/tiles.py cut "$W/merged.mbtiles" "$WORK/regions.json" "$area" "$OUT" "$W/overview.mbtiles"
  echo "$stamp" > "$W/built"
}

for area in $AREAS; do
  build_area "$area"
done

N=$((N + 1))
step $N $STEPS "Overview: every area's zooms 0 to 7, which the app ships" "seconds"
overviews=()
for area in $AREAS; do overviews+=("$WORK/$area/overview.mbtiles"); done
$PY pipeline/tiles.py overview "$OUT/overview.mbtiles" "${overviews[@]}"

N=$((N + 1))
step $N $STEPS "Catalogue: sizes and checksums" "a minute or two"
$PY pipeline/catalog.py "$WORK/regions.json" "$OUT" "$VERSION"

echo
echo "==> Built: $(du -sh "$OUT" | cut -f1) in $OUT, version $VERSION (map data from $DATA), in $(elapsed)"
