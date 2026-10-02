#!/usr/bin/env bash
#
# Build every region's map, walking graph and driving graph, into data/out.
#
#   pipeline/build.sh            build from the OpenStreetMap data already here
#                                (downloading it if there is none)
#   pipeline/build.sh --fresh    first fetch Geofabrik's newest, if newer
#   pipeline/build.sh --download-only   fetch the data, build nothing
#
# Needs Java 21+, uv, ~40 GB of disk and ~24 GB of RAM. Takes about an hour;
# each step says roughly how long it usually takes. Everything lands under
# data/ (gitignored): data/src downloads, data/work intermediate files,
# data/out what gets published.
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
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done
lock

SRC=data/src WORK=data/work OUT=data/out
PY=data/.venv/bin/python
mkdir -p "$SRC" "$WORK" "$OUT"
STEPS=11

# --- Tools ---------------------------------------------------------------------

step 1 $STEPS "Tools" "seconds, or a minute the first time"
command -v java >/dev/null || { echo "error: needs Java 21 or newer" >&2; exit 1; }
command -v uv >/dev/null || { echo "error: needs uv (https://docs.astral.sh/uv/)" >&2; exit 1; }
if [[ ! -x "$PY" ]]; then
  uv venv -q data/.venv
fi
# Every run, so a venv from before a package was needed gets it (quick when it has them all).
VIRTUAL_ENV=data/.venv uv pip install -q numpy scipy shapely osmium contourpy pyproj pyshp pillow tifffile
PLANETILER_VERSION=v0.10.2
[[ -f "$SRC/planetiler.jar" ]] || curl -fL --progress-bar -o "$SRC/planetiler.jar" \
  "https://github.com/onthegomap/planetiler/releases/download/$PLANETILER_VERSION/planetiler.jar"

# --- Sources -------------------------------------------------------------------

step 2 $STEPS "OpenStreetMap and height data" "a few minutes when downloading, seconds if not"
GEOFABRIK=https://download.geofabrik.de/europe
# By date: "-latest" is a redirect some proxies mangle.
NEWEST=$(curl -fsSL "$GEOFABRIK/united-kingdom.html" | grep -o 'united-kingdom-[0-9]\{6\}\.osm\.pbf' | sort -u | tail -1)
# Only a file this script downloaded and checked counts, which it marks by
# writing its name beside it: anything else (half a download, a copy put
# there by hand) is fetched afresh.
HAVE=$(cat "$SRC/united-kingdom.name" 2>/dev/null || true)
if [[ -f "$SRC/united-kingdom.osm.pbf" && -n "$HAVE" && ( "$FRESH" -eq 0 || "$HAVE" == "$NEWEST" ) ]]; then
  if [[ "$FRESH" -eq 1 ]]; then
    echo "    have $HAVE, which is Geofabrik's newest"
  else
    echo "    have ${HAVE:-the UK extract} (Geofabrik's newest is $NEWEST; --fresh to fetch it)"
  fi
else
  # Beside the old copy, which stays until the new one is whole and checked;
  # an interrupted download carries on from where it stopped.
  PART="$SRC/$NEWEST.part"
  SIZE=$(curl -fsSIL "$GEOFABRIK/$NEWEST" | grep -i '^content-length' | tail -1 | tr -dc '0-9')
  SIZE=${SIZE:-0}
  echo "    downloading $NEWEST ($((SIZE / 1000000)) MB)"
  if [[ ! -f "$PART" || $(stat -c %s "$PART") -lt $SIZE ]]; then
    curl -fL --progress-bar -C - -o "$PART" "$GEOFABRIK/$NEWEST"
  fi
  echo "    checking it"
  (cd "$SRC" && echo "$(curl -fsSL "$GEOFABRIK/$NEWEST.md5" | cut -d' ' -f1)  $NEWEST.part" | md5sum -c --quiet -)
  rm -f "$SRC/united-kingdom.name"
  mv "$PART" "$SRC/united-kingdom.osm.pbf"
  echo "$NEWEST" > "$SRC/united-kingdom.name"
  curl -fsSL -o "$SRC/index-v1.json" https://download.geofabrik.de/index-v1.json
fi
[[ -f "$SRC/index-v1.json" ]] || curl -fsSL -o "$SRC/index-v1.json" https://download.geofabrik.de/index-v1.json
if [[ ! -f "$SRC/terr50_gagg_gb.zip" ]]; then
  echo "    downloading OS Terrain 50 (160 MB)"
  curl -fL --progress-bar -o "$SRC/terr50_gagg_gb.zip.part" \
    "https://api.os.uk/downloads/v1/products/Terrain50/downloads?area=GB&format=ASCII+Grid+and+GML+%28Grid%29&redirect"
  mv "$SRC/terr50_gagg_gb.zip.part" "$SRC/terr50_gagg_gb.zip"
fi

# The OSM data's own date is the release's version.
VERSION=$($PY -c "
import osmium, sys
r = osmium.io.Reader(sys.argv[1], osmium.osm.osm_entity_bits.NOTHING)
print(r.header().get('osmosis_replication_timestamp')[:10])
r.close()" "$SRC/united-kingdom.osm.pbf")
echo "    map data from $VERSION"
if [[ "$ONLY_DOWNLOAD" -eq 1 ]]; then
  echo "==> Downloaded; built nothing (--download-only)"
  exit 0
fi

# --- Build ---------------------------------------------------------------------

step 3 $STEPS "Regions" "seconds"
# The full outlines for cutting, and simplified ones the app ships to tell
# which region you are in with no signal (published as app-regions.json).
$PY pipeline/regions.py "$SRC/index-v1.json" "$WORK/regions.json" "$OUT/app-regions.json"

step 4 $STEPS "Heights and contours" "1 minute the first time, then kept"
if [[ -f "$WORK/terrain/dem.npy" ]]; then
  echo "    have them"
else
  $PY pipeline/terrain.py "$SRC/terr50_gagg_gb.zip" "$WORK/terrain"
fi

# Great Britain, St Kilda to Shetland: the extract's own bounds reach far
# out into the Atlantic, which would be a lot of empty sea tiles.
BOUNDS=-8.8,49.8,2.0,61.0

step 5 $STEPS "Base map tiles (Planetiler, OpenMapTiles)" "15 to 20 minutes"
java -Xmx12g -jar "$SRC/planetiler.jar" --osm-path="$SRC/united-kingdom.osm.pbf" \
  --output="$WORK/omt.mbtiles" --download --download-dir="$SRC/sources" --tmpdir="$WORK/tmp" --force \
  --languages=en --exclude-layers=housenumber --maxzoom=14 --bounds=$BOUNDS

step 6 $STEPS "Hiking tiles: paths, rights of way, routes, contours" "2 to 3 minutes"
java -Xmx12g -cp "$SRC/planetiler.jar" pipeline/hiking/Hiking.java --osm-path="$SRC/united-kingdom.osm.pbf" \
  --contours="$WORK/terrain/contours" --output="$WORK/hiking.mbtiles" --tmpdir="$WORK/tmp" --force \
  --maxzoom=14 --bounds=$BOUNDS

step 7 $STEPS "Merge and cut tiles by region" "1 to 2 minutes"
$PY pipeline/tiles.py merge "$WORK/omt.mbtiles" "$WORK/hiking.mbtiles" "$WORK/merged.mbtiles"
# The z0-7 overview the app ships, published beside the regions.
$PY pipeline/tiles.py cut "$WORK/merged.mbtiles" "$WORK/regions.json" "$OUT" "$OUT/overview.mbtiles"

step 8 $STEPS "Relief: hill shading and steep ground" "20 minutes; half an hour more the first time, fetching England's LIDAR (6 GB)"
# 20 m heights: the Environment Agency's LIDAR in England, Terrain 50 elsewhere.
$PY -u pipeline/lidar.py "$WORK/regions.json" "$WORK/terrain/dem.npy" "$SRC/lidar" "$WORK/terrain/heights.npy"
$PY -u pipeline/relief.py "$WORK/terrain/heights.npy" "$WORK/shade.mbtiles" "$WORK/slope.mbtiles" "$BOUNDS"
$PY pipeline/tiles.py cut-as shade.mbtiles "$WORK/shade.mbtiles" "$WORK/regions.json" "$OUT"
$PY pipeline/tiles.py cut-as slope.mbtiles "$WORK/slope.mbtiles" "$WORK/regions.json" "$OUT"

step 9 $STEPS "Walking graph" "about 20 minutes"
$PY -u pipeline/graph.py build "$SRC/united-kingdom.osm.pbf" "$WORK/terrain/dem.npy" "$WORK/graph.npz"
$PY -u pipeline/graph.py cut "$WORK/graph.npz" "$WORK/regions.json" "$OUT"

step 10 $STEPS "Driving graph" "about 15 minutes"
$PY -u pipeline/graph.py build-driving "$SRC/united-kingdom.osm.pbf" "$WORK/terrain/dem.npy" "$WORK/driving.npz"
$PY -u pipeline/graph.py cut "$WORK/driving.npz" "$WORK/regions.json" "$OUT"

step 11 $STEPS "Catalogue: sizes and checksums" "under a minute"
$PY pipeline/catalog.py "$WORK/regions.json" "$OUT" "$VERSION"

echo
echo "==> Built: $(du -sh "$OUT" | cut -f1) in $OUT, map data from $VERSION, in $(elapsed)"
