#!/usr/bin/env python3
"""
Tile files: merge the two Planetiler outputs, and cut regions.

    tiles.py merge <omt.mbtiles> <hiking.mbtiles> <out.mbtiles> [attribution]
    tiles.py cut <merged.mbtiles> <regions.json> <area> <out dir> [overview.mbtiles]
    tiles.py cut-as <kind> <tiles.mbtiles> <regions.json> <area> <out dir>
    tiles.py overview <out.mbtiles> <an area's overview.mbtiles> ...

merge puts both sets of layers in each tile. A vector tile is a protobuf
whose layers are a repeated field, so two tiles' bytes, decompressed and
concatenated, are one tile holding both sets of layers.

cut writes <out dir>/<region>.mbtiles for each of an area's regions
(pipeline/areas.py): from z8 up, the tiles within ~2 km of the region's
outline. Every region of an area comes from the same merged file, so where
two overlap their tiles are identical and the app can draw both without a
seam. With an overview path, it also writes the area's z0-z7 tiles.

cut-as does the same with other tiles, as <out dir>/<region>.<kind>: the
relief's raster tiles (pipeline/relief.py), shade.mbtiles and slope.mbtiles.

overview makes the z0-z7 tiles the app ships, the low zooms everywhere
before anything is downloaded, from each area's: a tile two areas have
(the world, at z0) is both in one, each layer's features from both, and
those both have alike (Natural Earth's coasts and borders, made from the
same data) once.

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

sys.path.insert(0, os.path.dirname(__file__))
from progress import left  # noqa: E402

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


def merge(a_path, b_path, out_path, attribution=None):
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
        "attribution": attribution or "© OpenMapTiles © OpenStreetMap contributors; contains OS data © Crown copyright and database right",
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


def cut(src_path, regions_path, area, out_dir, overview_path=None, kind="mbtiles"):
    from shapely.geometry import shape
    from regions import of
    os.makedirs(out_dir, exist_ok=True)
    src = sqlite3.connect(src_path)
    base = metadata(src)
    low = [(z, x, y) for z, x, y in src.execute(
        "SELECT zoom_level, tile_column, tile_row FROM tiles_shallow WHERE zoom_level <= ?", (OVERVIEW_MAX,))]
    if overview_path:
        n = copy_tiles(src_path, overview_path, low, {**base, "maxzoom": str(OVERVIEW_MAX), "name": "overview"})
        print(f"overview: {n} tiles, {os.path.getsize(overview_path) / 1e6:.1f} MB")
    regions = of(regions_path, area)
    began = time.time()
    for i, region in enumerate(regions, 1):
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
        print(f"{region['id']}: {n} tiles, {os.path.getsize(path) / 1e6:.1f} MB in {time.time() - t:.0f}s"
              f"  ({i} of {len(regions)} regions, {left(began, i, len(regions))})", flush=True)


# --- Vector tiles, read and written: as much of the format as overview needs ---------

def varint(buf, i):
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, i
        shift += 7


def fields(buf):
    """A protobuf message's fields, in order: (number, value), the value an int or, length-delimited, bytes."""
    i, out = 0, []
    while i < len(buf):
        key, i = varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = varint(buf, i)
        elif wire == 2:
            n, i = varint(buf, i)
            value, i = bytes(buf[i:i + n]), i + n
        elif wire == 1:
            value, i = bytes(buf[i:i + 8]), i + 8
        elif wire == 5:
            value, i = bytes(buf[i:i + 4]), i + 4
        else:
            raise ValueError(f"wire type {wire}")
        out.append((number, value))
    return out


def put_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def put(number, value):
    """A field: an int as a varint, bytes length-delimited."""
    if isinstance(value, int):
        return put_varint(number << 3) + put_varint(value)
    return put_varint(number << 3 | 2) + put_varint(len(value)) + value


def packed(values):
    return b"".join(put_varint(v) for v in values)


def unpacked(buf):
    i, out = 0, []
    while i < len(buf):
        v, i = varint(buf, i)
        out.append(v)
    return out


class Layer:
    """A vector tile's layer, gathering features from tiles, each feature once."""

    def __init__(self, name, version, extent):
        self.name, self.version, self.extent = name, version, extent
        self.keys, self.values = {}, {}
        self.features, self.seen = [], set()

    def add(self, buf):
        """Adds a layer's features; False if they cannot be (another extent), so they are left out."""
        keys, values, features = [], [], []
        extent = 4096
        for number, value in fields(buf):
            if number == 3:
                keys.append(value)
            elif number == 4:
                values.append(value)
            elif number == 2:
                features.append(value)
            elif number == 5:
                extent = value
        if extent != self.extent:
            return False
        for feature in features:
            fid, tags, kind, geometry = None, [], 0, b""
            for number, value in fields(feature):
                if number == 1:
                    fid = value
                elif number == 2:
                    tags += unpacked(value) if isinstance(value, bytes) else [value]
                elif number == 3:
                    kind = value
                elif number == 4:
                    geometry = value if isinstance(value, bytes) else put_varint(value)
            pairs = tuple((keys[tags[k]], values[tags[k + 1]]) for k in range(0, len(tags) - 1, 2))
            same = (kind, geometry, pairs)
            if same in self.seen:
                continue
            self.seen.add(same)
            remapped = []
            for key, value in pairs:
                remapped += [self.keys.setdefault(key, len(self.keys)), self.values.setdefault(value, len(self.values))]
            self.features.append((fid, remapped, kind, geometry))
        return True

    def encode(self):
        out = put(15, self.version) + put(1, self.name)
        for fid, tags, kind, geometry in self.features:
            body = (put(1, fid) if fid is not None else b"") + put(2, packed(tags)) + put(3, kind) + put(4, geometry)
            out += put(2, body)
        for key in self.keys:
            out += put(3, key)
        for value in self.values:
            out += put(4, value)
        return out + put(5, self.extent)


def combine(tiles):
    """Several gzipped vector tiles of the same place as one: each layer's features from all, alike ones once."""
    layers = {}
    for data in tiles:
        for number, layer in fields(gzip.decompress(data)):
            if number != 3:
                continue
            head = {n: v for n, v in fields(layer) if n in (1, 5, 15)}
            name = head[1]
            if name not in layers:
                layers[name] = Layer(name, head.get(15, 2), head.get(5, 4096))
            layers[name].add(layer)
    return gzip.compress(b"".join(put(3, layer.encode()) for layer in layers.values()), compresslevel=6, mtime=0)


def overview(out_path, *paths):
    """The app's z0-z7 tiles: each area's, combined where two have the same tile."""
    t = time.time()
    found = {}  # (z, x, y) -> each area's tile there
    metas = []
    for path in paths:
        db = sqlite3.connect(path)
        metas.append(metadata(db))
        keys, get = keyed(db)
        for z, x, y, i in db.execute(keys):
            found.setdefault((z, x, y), []).append(db.execute(get, (i,)).fetchone()[0])
        db.close()
    out = create(out_path)
    ids = {}
    shared = 0
    for key, tiles in sorted(found.items()):
        if len(set(tiles)) > 1:
            data = combine(tiles)
            shared += 1
        else:
            data = tiles[0]
        h = hashlib.sha1(data).digest()
        if h not in ids:
            ids[h] = len(ids) + 1
            out.execute("INSERT INTO tiles_data VALUES (?, ?)", (ids[h], data))
        out.execute("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)", (*key, ids[h]))
    # All of the areas' bounds: the map asks for no tile outside them.
    bounds = [list(map(float, m["bounds"].split(","))) for m in metas if m.get("bounds")]
    meta = {**metas[0], "name": "overview",
            "bounds": ",".join(f"{f(b[k] for b in bounds):.4f}" for k, f in enumerate((min, min, max, max)))}
    out.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    out.commit()
    out.execute("VACUUM")
    out.close()
    print(f"overview: {len(found)} tiles, {shared} of them in two areas or more, "
          f"{os.path.getsize(out_path) / 1e6:.1f} MB in {time.time() - t:.0f}s")


def main():
    if sys.argv[1] == "merge":
        merge(*sys.argv[2:6])
    elif sys.argv[1] == "cut":
        cut(*sys.argv[2:7])
    elif sys.argv[1] == "cut-as":
        cut(*sys.argv[3:7], kind=sys.argv[2])
    elif sys.argv[1] == "overview":
        overview(*sys.argv[2:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
