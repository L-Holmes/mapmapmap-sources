#!/usr/bin/env python3
"""
Tile files: merge the two Planetiler outputs, and cut regions.

    tiles.py merge <omt.mbtiles> <hiking.mbtiles> <out.mbtiles>
    tiles.py cut <merged.mbtiles> <regions.json> <out dir> [overview.mbtiles]
    tiles.py cut-as <kind> <tiles.mbtiles> <regions.json> <out dir>

merge puts both sets of layers in each tile. A vector tile is a protobuf
whose layers are a repeated field, so two tiles' bytes, decompressed and
concatenated, are one tile holding both sets of layers.

cut writes <out dir>/<region>.mbtiles for each region: from z8 up, the
tiles within ~2 km of the region's outline. Every region comes from the
same merged file, so where two overlap their tiles are identical and the
app can draw both without a seam. With an overview path, it also writes
the z0-z7 tiles, which the app ships: the low zooms, everywhere, before
anything is downloaded.

cut-as does the same with other tiles, as <out dir>/<region>.<kind>: the
relief's raster tiles (pipeline/relief.py), shade.mbtiles and slope.mbtiles.

Output files use the deduplicated layout Planetiler writes (tiles_shallow,
tiles_data and a tiles view): the sea is one tile stored once.
"""
import gzip
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

import numpy as np

OVERVIEW_MAX = 7
MAX_ZOOM = 14
BUFFER = 0.02

SCHEMA = """
CREATE TABLE metadata (name TEXT, value TEXT);
CREATE UNIQUE INDEX name ON metadata (name);
CREATE TABLE tiles_shallow (
  zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data_id INTEGER,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
) WITHOUT ROWID;
CREATE TABLE tiles_data (tile_data_id INTEGER PRIMARY KEY, tile_data BLOB);
CREATE VIEW tiles AS
  SELECT tiles_shallow.zoom_level AS zoom_level, tiles_shallow.tile_column AS tile_column,
         tiles_shallow.tile_row AS tile_row, tiles_data.tile_data AS tile_data
  FROM tiles_shallow JOIN tiles_data ON tiles_shallow.tile_data_id = tiles_data.tile_data_id;
"""


def create(path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=4096;" + SCHEMA)
    return db


def metadata(db):
    return dict(db.execute("SELECT name, value FROM metadata"))


def keyed(db):
    """(z, x, y) -> data id, and the data by id, from either layout."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    if "tiles_shallow" in tables:
        return "SELECT zoom_level, tile_column, tile_row, tile_data_id FROM tiles_shallow ORDER BY 1, 2, 3", \
               "SELECT tile_data FROM tiles_data WHERE tile_data_id = ?"
    return "SELECT zoom_level, tile_column, tile_row, rowid FROM tiles ORDER BY 1, 2, 3", \
           "SELECT tile_data FROM tiles WHERE rowid = ?"


def join(pair):
    a, b = pair
    return gzip.compress(gzip.decompress(a) + gzip.decompress(b), compresslevel=6, mtime=0)


def merge(a_path, b_path, out_path):
    t = time.time()
    a, b = sqlite3.connect(a_path), sqlite3.connect(b_path)
    a_keys, a_get = keyed(a)
    b_keys, b_get = keyed(b)
    b_index = {(z, x, y): i for z, x, y, i in b.execute(b_keys)}
    out = create(out_path)
    ids = {}  # content hash -> tile_data_id
    merged_ids = {}  # (a id, b id) -> tile_data_id

    def store(data):
        h = hashlib.sha1(data).digest()
        i = ids.get(h)
        if i is None:
            i = len(ids) + 1
            ids[h] = i
            out.execute("INSERT INTO tiles_data VALUES (?, ?)", (i, data))
        return i

    batch = []

    def flush(pool):
        pairs = [(a.execute(a_get, (ai,)).fetchone()[0], b.execute(b_get, (bi,)).fetchone()[0]) for _, ai, bi in batch]
        for (key, ai, bi), data in zip(batch, pool.map(join, pairs, chunksize=64)):
            merged_ids[(ai, bi)] = store(data)
            out.execute("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)", (*key, merged_ids[(ai, bi)]))
        batch.clear()

    a_only = {}  # a id -> tile_data_id
    with Pool() as pool:
        for z, x, y, ai in a.execute(a_keys):
            bi = b_index.pop((z, x, y), None)
            if bi is None:
                if ai not in a_only:
                    a_only[ai] = store(a.execute(a_get, (ai,)).fetchone()[0])
                out.execute("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)", (z, x, y, a_only[ai]))
            elif (ai, bi) in merged_ids:
                out.execute("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)", (z, x, y, merged_ids[(ai, bi)]))
            else:
                batch.append(((z, x, y), ai, bi))
                if len(batch) >= 4096:
                    flush(pool)
        flush(pool)
    # Tiles with hiking layers and nothing from OpenMapTiles are outside the
    # base map's bounds; there are none worth keeping.
    ma, mb = metadata(a), metadata(b)
    layers = json.loads(ma["json"])["vector_layers"] + json.loads(mb["json"])["vector_layers"]
    meta = {
        "name": "mapmapmap", "format": "pbf", "type": "baselayer",
        "minzoom": "0", "maxzoom": str(MAX_ZOOM),
        "bounds": ma["bounds"], "center": ma.get("center", ""),
        "attribution": "© OpenMapTiles © OpenStreetMap contributors; contains OS data © Crown copyright and database right",
        "json": json.dumps({"vector_layers": layers}),
    }
    out.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    out.commit()
    print(f"merged: {len(ids)} distinct tiles, {len(b_index)} hiking-only dropped, {time.time() - t:.0f}s")


def tile_bounds(z, x, y):
    n = 2 ** z
    lon0 = x / n * 360 - 180
    lon1 = (x + 1) / n * 360 - 180
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon0, lat0, lon1, lat1


def tiles_touching(area, z):
    """XYZ tiles at z whose squares meet the area."""
    import shapely
    lon0, lat0, lon1, lat1 = area.bounds
    n = 2 ** z

    def tx(lon):
        return int((lon + 180) / 360 * n)

    def ty(lat):
        r = math.radians(lat)
        return int((1 - math.asinh(math.tan(r)) / math.pi) / 2 * n)

    xs = np.arange(tx(lon0), tx(lon1) + 1)
    ys = np.arange(ty(lat1), ty(lat0) + 1)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy = gx.ravel(), gy.ravel()
    west = gx / n * 360 - 180
    east = (gx + 1) / n * 360 - 180
    north = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * gy / n))))
    south = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (gy + 1) / n))))
    boxes = shapely.box(west, south, east, north)
    shapely.prepare(area)
    hit = shapely.intersects(area, boxes)
    return gx[hit], gy[hit]


def copy_tiles(src_path, out_path, keys, meta):
    """keys: rows of (z, x, tms y)."""
    out = create(out_path)
    out.execute("ATTACH DATABASE ? AS src", (src_path,))
    out.execute("CREATE TEMP TABLE want (z INTEGER, x INTEGER, y INTEGER, PRIMARY KEY (z, x, y)) WITHOUT ROWID")
    out.executemany("INSERT OR IGNORE INTO want VALUES (?, ?, ?)", keys)
    out.execute("""
        INSERT INTO tiles_shallow
        SELECT s.zoom_level, s.tile_column, s.tile_row, s.tile_data_id
        FROM want w JOIN src.tiles_shallow s
          ON s.zoom_level = w.z AND s.tile_column = w.x AND s.tile_row = w.y""")
    out.execute("""
        INSERT INTO tiles_data
        SELECT tile_data_id, tile_data FROM src.tiles_data
        WHERE tile_data_id IN (SELECT DISTINCT tile_data_id FROM tiles_shallow)""")
    out.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    out.commit()
    out.execute("DETACH DATABASE src")
    count = out.execute("SELECT count(*) FROM tiles_shallow").fetchone()[0]
    out.execute("VACUUM")
    out.close()
    return count


def cut(src_path, regions_path, out_dir, overview_path=None, kind="mbtiles"):
    from shapely.geometry import shape
    os.makedirs(out_dir, exist_ok=True)
    src = sqlite3.connect(src_path)
    base = metadata(src)
    low = [(z, x, y) for z, x, y in src.execute(
        "SELECT zoom_level, tile_column, tile_row FROM tiles_shallow WHERE zoom_level <= ?", (OVERVIEW_MAX,))]
    if overview_path:
        n = copy_tiles(src_path, overview_path, low, {**base, "maxzoom": str(OVERVIEW_MAX), "name": "overview"})
        print(f"overview: {n} tiles, {os.path.getsize(overview_path) / 1e6:.1f} MB")
    for region in json.load(open(regions_path)):
        t = time.time()
        path = os.path.join(out_dir, f"{region['id']}.{kind}")
        area = shape(region["geometry"]).buffer(BUFFER)
        if region["parent"] is None:
            keys = [(z, x, y) for z, x, y in src.execute(
                "SELECT zoom_level, tile_column, tile_row FROM tiles_shallow WHERE zoom_level > ?", (OVERVIEW_MAX,))]
        else:
            keys = []
            for z in range(OVERVIEW_MAX + 1, MAX_ZOOM + 1):
                xs, ys = tiles_touching(area, z)
                tms = (2 ** z - 1) - ys
                keys.extend(zip([z] * len(xs), xs.tolist(), tms.tolist()))
        lon0, lat0, lon1, lat1 = area.bounds
        meta = {
            **base,
            "name": region["id"],
            "minzoom": str(OVERVIEW_MAX + 1),
            "bounds": f"{lon0:.4f},{lat0:.4f},{lon1:.4f},{lat1:.4f}",
            "center": f"{(lon0 + lon1) / 2:.4f},{(lat0 + lat1) / 2:.4f},10",
        }
        n = copy_tiles(src_path, path, keys, meta)
        print(f"{region['id']}: {n} tiles, {os.path.getsize(path) / 1e6:.1f} MB in {time.time() - t:.0f}s")


def main():
    if sys.argv[1] == "merge":
        merge(*sys.argv[2:5])
    elif sys.argv[1] == "cut":
        cut(*sys.argv[2:6])
    elif sys.argv[1] == "cut-as":
        cut(*sys.argv[3:6], kind=sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
