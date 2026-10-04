#!/usr/bin/env python3
"""
A 20 m height grid for the hiking map's relief: the Environment Agency's
LIDAR Composite DTM where there is some (England, nearly all of it), OS
Terrain 50 elsewhere.

    lidar.py <regions.json> <dem.npy> <cache dir> <out.npy> [region id ...]

The LIDAR is 2 m; its WCS hands it out resampled, here at 10 m, in 10 km
squares, a request each (a few seconds; some 1,450 cover England). Each is
kept in <cache dir> as decimetres, so a build fetches only the squares it
lacks: the first, about half an hour and 6 GB; after that, nothing. A square
the service has nothing for (the sea) is kept too, empty. One that fails
five times is left for the next build, and Terrain 50 stands in. The
squares fetched are those touching the regions named (England if none).

<out>: int16 decimetres, 20 m cells, row 0 the southmost, origin OSGB
(0, 0), as pipeline/terrain.py's grid is at 50 m. A cell is the mean of the
LIDAR's 10 m cells in it, where it has any, Terrain 50 bilinearly where not;
and for 200 m either side of where one gives way to the other, a blend of
the two, so that neither England's border nor a gap in the LIDAR shows as
a step (which the relief would draw as a cliff).

Why it is worth it: at 50 m, Terrain 50 evens out the crags and gullies a
walker meets, and steep ground comes out as broad blurs; at 20 m the LIDAR
has them where they are.

Contains public sector information licensed under the Open Government
Licence v3.0 (Environment Agency).
"""
import io
import json
import logging
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pyproj
import shapely
import tifffile

sys.path.insert(0, os.path.dirname(__file__))
from progress import left  # noqa: E402
from scipy.ndimage import uniform_filter
from shapely.geometry import shape
from shapely.ops import transform

WCS = "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-2m/wcs"
COVERAGE = "09ea3b37-df3a-4e8b-ac69-fb0842227b04__Lidar_Composite_Elevation_DTM_2m"
# The coverage's extent, OSGB west, south, east, north (its DescribeCoverage).
# Asked for anything past it, the service answers 500; asked for a square
# across its edge, only the part inside.
EXTENT = (80_000, 4_000, 656_000, 665_000)
SQUARE = 10_000
FETCHED = 10
FETCHED_SCALE = 2 / FETCHED  # the service's 2 m cells to ours
PIXELS = SQUARE // FETCHED
CELL = 20
COLS, ROWS = 700_000 // CELL, 1_300_000 // CELL
TERRAIN_CELL = 50
NODATA = -32768
BLEND = 10  # cells either side: 200 m
WORKERS = 8
# Nothing in Terrain 50 above this (metres) is sea: its tiles have the sea
# at about -1.7, and where it has no tile, pipeline/terrain.py puts -2.5.
SEA = 0.0

logging.getLogger("tifffile").setLevel(logging.ERROR)


def squares(regions_path, ids):
    """The 10 km squares touching the regions, as their south-west corners."""
    to_osgb = pyproj.Transformer.from_crs(4326, 27700, always_xy=True)
    regions = {r["id"]: r for r in json.load(open(regions_path))}
    area = shapely.union_all([transform(to_osgb.transform, shape(regions[i]["geometry"])) for i in ids]).buffer(1000)
    shapely.prepare(area)
    return [(e, n) for e in range(0, 700_000, SQUARE) for n in range(0, 1_300_000, SQUARE)
            if area.intersects(shapely.box(e, n, e + SQUARE, n + SQUARE))]


def path_of(cache, e, n):
    return os.path.join(cache, f"{e // 1000:03d}_{n // 1000:04d}.npz")


def get(w, s, ea, no):
    """The LIDAR in a box (OSGB metres, west, south, east, north), metres, row 0 the north, NaN where it has none."""
    url = (f"{WCS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId={COVERAGE}&format=image/tiff"
           f"&subset=E({w},{ea})&subset=N({s},{no})&SCALEFACTOR={FETCHED_SCALE}")
    want = ((no - s) // FETCHED, (ea - w) // FETCHED)
    with urllib.request.urlopen(url, timeout=300) as r:
        got = tifffile.imread(io.BytesIO(r.read())).astype(np.float32)
    if got.shape != want:
        raise ValueError(f"shape {got.shape}, not {want}")
    return got


def refused(ex):
    """Whether the service said no (500), as it does for what it holds nothing of."""
    return getattr(ex, "code", None) == 500


def quarters(w, s, ea, no, put):
    """
    A square the service refuses, asked for again a quarter at a time, each
    that comes put where it goes; how many came, or None if the service
    could not be reached (then the square is tried whole next time).
    """
    mw, mn = (w + ea) // 2, (s + no) // 2
    came = 0
    for box in ((w, s, mw, mn), (mw, s, ea, mn), (w, mn, mw, no), (mw, mn, ea, no)):
        for attempt in range(3):
            try:
                put(*box, get(*box))
                came += 1
                break
            except Exception as ex:  # noqa: BLE001 - refused, or a hiccup: try again, then leave it
                if refused(ex):
                    break
                if attempt == 2:
                    return None
                time.sleep(5)
    return came


def fetch(cache, e, n, dem):
    """
    Fetches one square into the cache; True if it is there now. The service
    refuses (500) a square inside its extent that it holds nothing of,
    which is the open sea: where Terrain 50 [dem] has only sea, a refusal
    is taken as that, and kept as an empty square, not tried again. It
    refuses some squares of land too, where it holds only part (near
    Chester, 300000,380000, every time since October 2026): those are
    asked for in quarters, and the quarters it refuses left to Terrain 50.
    Once at least one quarter has come (so it is not the service being
    down), the square is kept like that, not asked for again.
    """
    path = path_of(cache, e, n)
    if os.path.exists(path):
        return True
    sea = float(np.asarray(dem[n // TERRAIN_CELL:(n + SQUARE) // TERRAIN_CELL, e // TERRAIN_CELL:(e + SQUARE) // TERRAIN_CELL]).max()) <= SEA
    # Only the part inside the coverage is asked for; the rest is no data.
    heights = np.full((PIXELS, PIXELS), np.nan, np.float32)
    w, s, ea, no = max(e, EXTENT[0]), max(n, EXTENT[1]), min(e + SQUARE, EXTENT[2]), min(n + SQUARE, EXTENT[3])
    if w < ea and s < no:
        def put(bw, bs, be, bn, got):
            # The service's row 0 is the north.
            top, col = (n + SQUARE - bn) // FETCHED, (bw - e) // FETCHED
            heights[top:top + got.shape[0], col:col + got.shape[1]] = got

        for attempt in range(5):
            try:
                put(w, s, ea, no, get(w, s, ea, no))
                break
            except Exception as ex:  # noqa: BLE001 - a network or server hiccup: try again, then leave it
                if sea and refused(ex):
                    break
                if attempt == 4:
                    came = quarters(w, s, ea, no, put) if refused(ex) else None
                    if not came:
                        print(f"\n    {e},{n}: this 10 km square did not come from the Environment Agency this time"
                              f" (it said: {ex}); OS Terrain 50 stands in there, as it always has where there is no"
                              " LIDAR, and the square is asked for again next time")
                        return False
                    print(f"\n    {e},{n}: the Environment Agency has LIDAR for {came} of this 10 km square's 4"
                          " quarters; OS Terrain 50 stands in for the rest, as it always has where there is no LIDAR")
                    break
                time.sleep(5 * 2 ** attempt)
    bad = ~np.isfinite(heights) | (heights < -1000)
    # The grid's row 0 is the south.
    dm = np.where(bad, NODATA, np.round(np.where(bad, 0, heights) * 10)).astype(np.int16)[::-1]
    part = path[:-4] + ".part.npz"
    np.savez_compressed(part, dm=dm)
    os.replace(part, path)
    return True



def fetch_all(cache, wanted, dem):
    os.makedirs(cache, exist_ok=True)
    todo = [sq for sq in wanted if not os.path.exists(path_of(cache, *sq))]
    print(f"    {len(wanted)} squares, {len(wanted) - len(todo)} already here, fetching {len(todo)}")
    done = failed = 0
    start = time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        for f in as_completed([pool.submit(fetch, cache, e, n, dem) for e, n in todo]):
            done += 1
            failed += 0 if f.result() else 1
            print(f"\r    {done} of {len(todo)} squares, {left(start, done, len(todo))}"
                  + (f"; {failed} left to OS Terrain 50 for now" if failed else "") + " ", end="", flush=True)
    if todo:
        print()


def terrain(dem, r0, r1):
    """Terrain 50 at the 20 m cells' centres in rows r0 to r1, metres, bilinearly."""
    def axis(count, cells):
        f = ((np.arange(count) + 0.5) * CELL - TERRAIN_CELL / 2) / TERRAIN_CELL
        i = np.clip(np.floor(f).astype(np.int64), 0, cells - 2)
        return i, np.clip(f - i, 0, 1).astype(np.float32)
    ci, cf = axis(COLS, dem.shape[1])
    f = ((np.arange(r0, r1) + 0.5) * CELL - TERRAIN_CELL / 2) / TERRAIN_CELL
    ri = np.clip(np.floor(f).astype(np.int64), 0, dem.shape[0] - 2)
    rf = np.clip(f - ri, 0, 1).astype(np.float32)[:, None]
    lo = int(ri.min())
    block = np.asarray(dem[lo:int(ri.max()) + 2], dtype=np.float32)
    rows = block[ri - lo] * (1 - rf) + block[ri - lo + 1] * rf
    return rows[:, ci] * (1 - cf) + rows[:, ci + 1] * cf


def lidar(cache, have, r0, r1):
    """The LIDAR at 20 m in rows r0 to r1, metres, NaN where there is none."""
    out = np.full((r1 - r0, COLS), np.nan, np.float32)
    per = SQUARE // CELL
    for n in range(r0 * CELL // SQUARE * SQUARE, r1 * CELL, SQUARE):
        for e in range(0, 700_000, SQUARE):
            if (e, n) not in have:
                continue
            dm = np.load(path_of(cache, e, n))["dm"]
            m = np.where(dm == NODATA, np.nan, dm / np.float32(10)).astype(np.float32)
            k = CELL // FETCHED
            blocks = m.reshape(per, k, per, k)
            count = np.isfinite(blocks).sum(axis=(1, 3))
            total = np.nansum(blocks, axis=(1, 3))
            mean = np.where(count > 0, total / np.maximum(count, 1), np.nan).astype(np.float32)
            s0 = n // CELL
            a, b = max(r0, s0), min(r1, s0 + per)
            if a < b:
                out[a - r0:b - r0, e // CELL:e // CELL + per] = mean[a - s0:b - s0]
    return out


def build(dem_path, cache, out_path):
    have = set()
    for name in os.listdir(cache):
        if name.endswith(".npz") and ".part" not in name:
            e, n = name[:-4].split("_")
            have.add((int(e) * 1000, int(n) * 1000))
    dem = np.load(dem_path, mmap_mode="r")
    out = np.lib.format.open_memmap(out_path + ".part", mode="w+", dtype=np.int16, shape=(ROWS, COLS))
    band = SQUARE // CELL
    start = time.time()
    for r0 in range(0, ROWS, band):
        r1 = min(ROWS, r0 + band)
        # Rows either side, for the blend to see past the band's edges.
        a0, a1 = max(0, r0 - 2 * BLEND), min(ROWS, r1 + 2 * BLEND)
        height = terrain(dem, a0, a1)
        found = lidar(cache, have, a0, a1)
        inside = np.isfinite(found)
        if inside.any():
            weight = uniform_filter(inside.astype(np.float32), size=2 * BLEND + 1, mode="nearest")
            height = np.where(inside, weight * np.nan_to_num(found) + (1 - weight) * height, height)
        out[r0:r1] = np.clip(np.round(height[r0 - a0:r1 - a0] * 10), -32767, 32767).astype(np.int16)
        print(f"\r    grid: {r1 * CELL // 1000} of {ROWS * CELL // 1000} km north, {left(start, r1, ROWS)} ", end="", flush=True)
    print()
    out.flush()
    del out
    os.replace(out_path + ".part", out_path)
    print(f"relief heights: {ROWS} x {COLS} cells at {CELL} m, {len(have)} LIDAR squares, in {time.time() - start:.0f}s")


def main():
    regions_path, dem_path, cache, out_path = sys.argv[1:5]
    ids = sys.argv[5:] or ["england"]
    fetch_all(cache, squares(regions_path, ids), np.load(dem_path, mmap_mode="r"))
    build(dem_path, cache, out_path)


if __name__ == "__main__":
    main()
