#!/usr/bin/env bash
#
# Build every region's map and walking graph from scratch, into data/out.
#
#   pipeline/build.sh            download what is missing, then build
#   pipeline/build.sh --fresh    fetch the newest OpenStreetMap data first
#
# Needs Java 21+, uv, ~40 GB of disk and ~24 GB of RAM. Takes about an hour.
# Everything lands under data/ (gitignored): data/src for downloads,
# data/work for intermediate files, data/out for what gets published.
#
set -euo pipefail
cd "$(dirname "$0")/.."

FRESH=0
for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

SRC=data/src WORK=data/work OUT=data/out
PY=data/.venv/bin/python
mkdir -p "$SRC" "$WORK" "$OUT"

# --- Tools ---------------------------------------------------------------------

command -v java >/dev/null || { echo "error: needs Java 21 or newer" >&2; exit 1; }
command -v uv >/dev/null || { echo "error: needs uv (https://docs.astral.sh/uv/)" >&2; exit 1; }
if [[ ! -x "$PY" ]]; then
  uv venv -q data/.venv
  VIRTUAL_ENV=data/.venv uv pip install -q numpy scipy shapely osmium contourpy pyproj pyshp
fi
PLANETILER_VERSION=v0.10.2
[[ -f "$SRC/planetiler.jar" ]] || curl -fsSL -o "$SRC/planetiler.jar" \
  "https://github.com/onthegomap/planetiler/releases/download/$PLANETILER_VERSION/planetiler.jar"

# --- Sources -------------------------------------------------------------------

if [[ "$FRESH" -eq 1 ]]; then
  rm -f "$SRC/united-kingdom.osm.pbf" "$SRC/index-v1.json"
fi
[[ -f "$SRC/index-v1.json" ]] || curl -fsSL -o "$SRC/index-v1.json" https://download.geofabrik.de/index-v1.json
if [[ ! -f "$SRC/united-kingdom.osm.pbf" ]]; then
  echo "==> Downloading the newest OpenStreetMap data for the UK"
  # Asked for by date: "-latest" is a redirect some proxies mangle.
  DATED=$(curl -fsSL https://download.geofabrik.de/europe/united-kingdom.html \
    | grep -o 'united-kingdom-[0-9]\{6\}\.osm\.pbf' | sort | tail -1)
  curl -fsSL -o "$SRC/united-kingdom.osm.pbf" "https://download.geofabrik.de/europe/$DATED"
  (cd "$SRC" && echo "$(curl -fsSL "https://download.geofabrik.de/europe/$DATED.md5" | cut -d' ' -f1)  united-kingdom.osm.pbf" | md5sum -c -)
fi
[[ -f "$SRC/terr50_gagg_gb.zip" ]] || curl -fsSL -o "$SRC/terr50_gagg_gb.zip" \
  "https://api.os.uk/downloads/v1/products/Terrain50/downloads?area=GB&format=ASCII+Grid+and+GML+%28Grid%29&redirect"

# The OSM data's own date is the release's version.
VERSION=$($PY -c "
import osmium, sys
r = osmium.io.Reader(sys.argv[1], osmium.osm.osm_entity_bits.NOTHING)
print(r.header().get('osmosis_replication_timestamp')[:10])
r.close()" "$SRC/united-kingdom.osm.pbf")
echo "==> Version $VERSION"

# --- Build ---------------------------------------------------------------------

echo "==> Regions"
# The full outlines for cutting, and simplified ones the app ships to tell
# which region you are in with no signal (published as app-regions.json).
$PY pipeline/regions.py "$SRC/index-v1.json" "$WORK/regions.json" "$OUT/app-regions.json"

echo "==> Terrain"
[[ -f "$WORK/terrain/dem.npy" ]] || $PY pipeline/terrain.py "$SRC/terr50_gagg_gb.zip" "$WORK/terrain"

# Great Britain, St Kilda to Shetland: the extract's own bounds reach far
# out into the Atlantic, which would be a lot of empty sea tiles.
BOUNDS=-8.8,49.8,2.0,61.0

echo "==> Base map tiles"
java -Xmx12g -jar "$SRC/planetiler.jar" --osm-path="$SRC/united-kingdom.osm.pbf" \
  --output="$WORK/omt.mbtiles" --download --download-dir="$SRC/sources" --tmpdir="$WORK/tmp" --force \
  --languages=en --exclude-layers=housenumber --maxzoom=14 --bounds=$BOUNDS

echo "==> Hiking tiles"
java -Xmx12g -cp "$SRC/planetiler.jar" pipeline/hiking/Hiking.java --osm-path="$SRC/united-kingdom.osm.pbf" \
  --contours="$WORK/terrain/contours" --output="$WORK/hiking.mbtiles" --tmpdir="$WORK/tmp" --force \
  --maxzoom=14 --bounds=$BOUNDS

echo "==> Merge and cut tiles"
$PY pipeline/tiles.py merge "$WORK/omt.mbtiles" "$WORK/hiking.mbtiles" "$WORK/merged.mbtiles"
# The z0-7 overview the app ships, published beside the regions.
$PY pipeline/tiles.py cut "$WORK/merged.mbtiles" "$WORK/regions.json" "$OUT" "$OUT/overview.mbtiles"

echo "==> Walking graph"
$PY pipeline/graph.py build "$SRC/united-kingdom.osm.pbf" "$WORK/terrain/dem.npy" "$WORK/graph.npz"
$PY pipeline/graph.py cut "$WORK/graph.npz" "$WORK/regions.json" "$OUT"

echo "==> Catalogue"
$PY pipeline/catalog.py "$WORK/regions.json" "$OUT" "$VERSION"

echo "==> Done: $(du -sh "$OUT" | cut -f1) in $OUT, version $VERSION"
