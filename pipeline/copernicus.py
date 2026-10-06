#!/usr/bin/env python3
"""
An area's heights from the Copernicus DEM, and its contours: everywhere
but Great Britain, which has OS Terrain 50 and LIDAR (pipeline/terrain.py,
pipeline/lidar.py).

    copernicus.py <area> <regions.json> <cache dir> <out dir>

The Copernicus DEM's GLO-30 is the world's heights at 1 arc-second, about
30 m (north of 50°, its cells are wider east-west: 1.5", then 2" north of
60°), in 1° tiles, free to use with credit. It is a surface model: in a
wood, the height is that of the trees' tops, near enough, not the ground
under them; on open hills, mountains and crags, which is what the relief
and the peak scores are for, it is the ground. The tiles touching the
area's regions, and 15 km round them (the peak scores look 10 km out),
are fetched once into <cache dir>, beside the list of those there are
(none for the open sea): 50 tiles for the Alps, 93 for Italy, 142 for
Norway, some 6 GB in all. They are kept: the DEM does not change.

Writes, in <out dir>, each only if it is not there:

    heights.npy, heights.grid.json
        int16 decimetres, on the area's grid (pipeline/areas.py), each cell
        bilinearly from the DEM; 0 where it has nothing (the open sea, and
        land far enough from the regions), which takes no room on disk
    contours/*.shp
        as pipeline/terrain.py draws them: every 20 m, an index contour
        every 100 m (OS Terrain 50's are every 10 m, as on OS maps; in the
        Alps and Norway 10 m would be lines a few metres apart on steep
        ground, and 20 m is what their own maps use), from the heights a
        little smoothed, without the small rings a surface model's trees
        and buildings draw
"""
import math
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool

import numpy as np
import shapely

sys.path.insert(0, os.path.dirname(__file__))
from areas import AREAS  # noqa: E402
from grid import Grid  # noqa: E402
from progress import left  # noqa: E402
from regions import outline  # noqa: E402
from terrain import contours  # noqa: E402

BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
LISTED = "tileList.txt"
REACH = 0.15  # degrees round the regions: 15 km and more
WORKERS = 8
# Contours: the Gaussian the heights are smoothed by, and the shortest ring kept, in cells.
SMOOTH, LEAST = 1.0, 8


def name(lat, lon):
    return (f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00"
            f"_{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM")


def path_of(cache, lat, lon):
    return os.path.join(cache, name(lat, lon) + ".tif")


def get(url, path):
    """Fetches url into path, whole or not at all."""
    part = path + ".part"
    with urllib.request.urlopen(url, timeout=300) as r, open(part, "wb") as f:
        while block := r.read(1 << 20):
            f.write(block)
    os.replace(part, path)


def wanted(regions_path, area, cache):
    """The DEM's tiles touching the area's regions and REACH round them, that the DEM has: (lat, lon) of each's south-west corner."""
    listed = os.path.join(cache, LISTED)
    if not os.path.exists(listed):
        get(f"{BASE}/{LISTED}", listed)
    have = {line.strip() for line in open(listed)}
    shape = outline(regions_path, area, REACH)
    shapely.prepare(shape)
    w, s, e, n = shape.bounds
    return [(lat, lon) for lat in range(math.floor(s), math.ceil(n)) for lon in range(math.floor(w), math.ceil(e))
            if name(lat, lon) in have and shape.intersects(shapely.box(lon, lat, lon + 1, lat + 1))]


def fetch(cache, lat, lon):
    """One tile into the cache; None if it came, or what went wrong the last of five tries."""
    for attempt in range(5):
        try:
            get(f"{BASE}/{name(lat, lon)}/{name(lat, lon)}.tif", path_of(cache, lat, lon))
            return None
        except (urllib.error.URLError, OSError) as ex:  # a network hiccup: try again, then leave it
            if attempt == 4:
                return str(ex)
            time.sleep(5 * 2 ** attempt)


def fetch_all(cache, tiles):
    todo = [t for t in tiles if not os.path.exists(path_of(cache, *t))]
    print(f"    {len(tiles)} tiles of the Copernicus DEM, {len(tiles) - len(todo)} already here, fetching {len(todo)}")
    failed = []
    start = time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        jobs = {pool.submit(fetch, cache, *t): t for t in todo}
        for done, f in enumerate(as_completed(jobs), 1):
            if f.result() is not None:
                failed.append((jobs[f], f.result()))
            print(f"\r    {done} of {len(todo)} tiles, {left(start, done, len(todo))} ", end="", flush=True)
    if todo:
        print()
    if failed:
        (lat, lon), why = failed[0]
        print(f"    {len(failed)} of the DEM's tiles did not come this time (the first, {lat}°N {lon}°E, said: {why}).")
        print("    Without them their land would have no heights, so the build stops here. Those that came are kept:")
        print("    run ./update-maps.sh again to carry on from here.")
        sys.exit(1)


def tile(cache, lat, lon):
    """A tile's heights, metres, row 0 its north edge; None if the DEM has none there."""
    import tifffile
    path = path_of(cache, lat, lon)
    if not os.path.exists(path):
        return None
    return tifffile.imread(path).astype(np.float32)


def padded(cache, lat, lon):
    """
    A tile with a row and a column more: its pixels are points, the first
    at its north-west corner, so the last stretch to its south and east
    edges is the next tiles' first row and column. Its own last where there
    is no next tile.
    """
    a = tile(cache, lat, lon)
    rows, cols = a.shape
    out = np.empty((rows + 1, cols + 1), np.float32)
    out[:rows, :cols] = a
    east = tile(cache, lat, lon + 1)
    out[:rows, cols] = east[:, 0] if east is not None else a[:, -1]
    south = tile(cache, lat - 1, lon)
    if south is None:
        out[rows] = out[rows - 1]
    else:
        # Further south its cells may be narrower: read it at this tile's.
        row = np.append(south[0], south[0, -1])
        out[rows] = np.interp(np.linspace(0, 1, cols + 1), np.linspace(0, 1, len(row)), row)
    return out


def fill(job):
    """Writes the grid's cells whose middles are in one tile; how many."""
    cache, heights_path, lat, lon = job
    g = Grid.of(heights_path)
    a = padded(cache, lat, lon)
    rows, cols = a.shape[0] - 1, a.shape[1] - 1
    # The tile's edge in the grid, to know which cells to look at.
    k = np.linspace(0, 1, 100)
    x, y = g.to_grid().transform(np.concatenate([lon + k, np.full(100, lon + 1), lon + 1 - k, np.full(100, lon)]),
                                 np.concatenate([np.full(100, lat), lat + k, np.full(100, lat + 1), lat + 1 - k]))
    c0, c1 = max(int((x.min() - g.x0) // g.cell) - 1, 0), min(int((x.max() - g.x0) // g.cell) + 2, g.cols)
    r0, r1 = max(int((y.min() - g.y0) // g.cell) - 1, 0), min(int((y.max() - g.y0) // g.cell) + 2, g.rows)
    if r0 >= r1 or c0 >= c1:
        return 0
    out = np.load(heights_path, mmap_mode="r+")
    to_wgs = g.to_wgs()
    xs = g.x0 + (np.arange(c0, c1) + 0.5) * g.cell
    count = 0
    for a0 in range(r0, r1, 256):
        a1 = min(a0 + 256, r1)
        px, py = np.meshgrid(xs, g.y0 + (np.arange(a0, a1) + 0.5) * g.cell)
        plon, plat = to_wgs.transform(px, py)
        inside = (np.floor(plon) == lon) & (np.floor(plat) == lat)
        if not inside.any():
            continue
        fr = np.clip((lat + 1 - plat[inside]) * rows, 0, rows)
        fc = np.clip((plon[inside] - lon) * cols, 0, cols)
        i, j = np.minimum(fr.astype(np.int64), rows - 1), np.minimum(fc.astype(np.int64), cols - 1)
        fr, fc = fr - i, fc - j
        z = (a[i, j] * (1 - fr) * (1 - fc) + a[i, j + 1] * (1 - fr) * fc
             + a[i + 1, j] * fr * (1 - fc) + a[i + 1, j + 1] * fr * fc)
        # Only these cells: the next tile's worker writes those round them.
        rr, cc = np.nonzero(inside)
        out[a0 + rr, c0 + cc] = np.clip(np.round(z * 10), -32767, 32767).astype(np.int16)
        count += len(rr)
    out.flush()
    return count


def build(area, cache, tiles, heights_path):
    g = AREAS[area].grid()
    part = heights_path[:-4] + ".part.npy"
    g.open(part, np.int16)
    g.save(part)
    start = time.time()
    cells = 0
    with Pool() as pool:
        for done, n in enumerate(pool.imap_unordered(fill, [(cache, part, *t) for t in tiles]), 1):
            cells += n
            print(f"\r    heights: {done} of {len(tiles)} tiles into the grid, {left(start, done, len(tiles))} ",
                  end="", flush=True)
    print()
    os.replace(part, heights_path)
    g.save(heights_path)
    os.remove(part[:-4] + ".grid.json")
    print(f"heights: {g.rows} x {g.cols} cells at {g.cell:.0f} m, {cells / 1e6:.0f}M from {len(tiles)} tiles,"
          f" in {time.time() - start:.0f}s")


def main():
    area, regions_path, cache, out = sys.argv[1:5]
    os.makedirs(cache, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    heights_path = os.path.join(out, "heights.npy")
    lines = os.path.join(out, "contours")
    if os.path.exists(heights_path):
        print("    have the heights")
    else:
        tiles = wanted(regions_path, area, cache)
        fetch_all(cache, tiles)
        build(area, cache, tiles, heights_path)
    if os.path.exists(lines):
        print("    have the contours")
    else:
        t = time.time()
        a = AREAS[area]
        contours(heights_path, lines + ".part", a.interval, a.index, SMOOTH, LEAST)
        os.replace(lines + ".part", lines)
        print(f"    contours in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
