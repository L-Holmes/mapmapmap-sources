#!/usr/bin/env python3
"""
OS Terrain 50 -> one elevation grid for Great Britain, and contour lines.

    terrain.py <terr50_gagg_gb.zip> <out dir>

Writes:
    <out>/dem.npy         float32 heights, row 0 the southmost, 50 m cells,
                          origin at OSGB (0, 0). Sea and gaps are SEA.
    <out>/contours/*.shp  WGS84 contour lines, one file per 100 km square,
                          fields ele (m) and idx (1 on every 50 m).

The grid is what the routing graph samples for ascent; the contours are a
source for the hiking layers' Planetiler run (see pipeline/hiking/Hiking.java).
"""
import io
import os
import sys
import zipfile
from multiprocessing import Pool

import contourpy
import numpy as np
import pyproj
import shapefile
import shapely

CELL = 50
COLS = 700_000 // CELL
ROWS = 1_300_000 // CELL
SEA = -2.5
CHUNK = 100_000 // CELL
INTERVAL = 10
INDEX = 50
# Contours are simplified in OSGB metres before they are written. Well
# under a pixel at z16, and it halves what Planetiler has to read.
SIMPLIFY_M = 4.0


def read_tile(args):
    outer_path, name = args
    with zipfile.ZipFile(outer_path) as outer:
        inner = zipfile.ZipFile(io.BytesIO(outer.read(name)))
        asc = next(n for n in inner.namelist() if n.lower().endswith(".asc"))
        text = inner.read(asc).decode("ascii")
    head, values = {}, None
    lines = text.split("\n")
    for i, line in enumerate(lines[:6]):
        key, _, value = line.strip().partition(" ")
        if key.lower() in ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value"):
            head[key.lower()] = float(value)
        else:
            values = "\n".join(lines[i:])
            break
    if values is None:
        values = "\n".join(lines[len(head):])
    rows, cols = int(head["nrows"]), int(head["ncols"])
    grid = np.array(values.split(), dtype=np.float32).reshape(rows, cols)
    nodata = head.get("nodata_value")
    if nodata is not None:
        grid[grid == nodata] = SEA
    # The file's first row is its northmost; the mosaic's row 0 is the south.
    return int(head["xllcorner"]), int(head["yllcorner"]), grid[::-1]


def build_dem(zip_path, out):
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.endswith(".zip")]
    dem = np.lib.format.open_memmap(os.path.join(out, "dem.npy"), mode="w+", dtype=np.float32, shape=(ROWS, COLS))
    dem[:] = SEA
    with Pool() as pool:
        for x0, y0, grid in pool.imap_unordered(read_tile, [(zip_path, n) for n in names], chunksize=16):
            r, c = y0 // CELL, x0 // CELL
            dem[r:r + grid.shape[0], c:c + grid.shape[1]] = grid
    dem.flush()
    print(f"dem: {len(names)} tiles, max {float(np.nanmax(dem)):.1f} m")


def contour_chunk(args):
    out, ci, cj = args
    dem = np.load(os.path.join(out, "dem.npy"), mmap_mode="r")
    r0, c0 = ci * CHUNK, cj * CHUNK
    # One cell of overlap each way, so lines meet the next square's exactly.
    block = np.asarray(dem[r0:r0 + CHUNK + 1, c0:c0 + CHUNK + 1], dtype=np.float64)
    top = float(block.max())
    if top < INTERVAL:
        return 0
    gen = contourpy.contour_generator(z=block, line_type=contourpy.LineType.Separate)
    to_wgs = pyproj.Transformer.from_crs(27700, 4326, always_xy=True)
    path = os.path.join(out, "contours", f"c_{ci:02d}_{cj:02d}")
    count = 0
    with shapefile.Writer(path, shapeType=shapefile.POLYLINE) as w:
        w.field("ele", "N", size=5)
        w.field("idx", "N", size=1)
        for level in range(INTERVAL, int(top) + 1, INTERVAL):
            lines = gen.lines(level)
            if not lines:
                continue
            geoms = []
            for xy in lines:
                if len(xy) < 2:
                    continue
                e = (c0 + xy[:, 0]) * CELL + CELL / 2
                n = (r0 + xy[:, 1]) * CELL + CELL / 2
                geoms.append(shapely.linestrings(np.column_stack([e, n])))
            for g in shapely.simplify(np.array(geoms, dtype=object), SIMPLIFY_M):
                coords = shapely.get_coordinates(g)
                if len(coords) < 2:
                    continue
                lon, lat = to_wgs.transform(coords[:, 0], coords[:, 1])
                w.line([np.column_stack([lon, lat]).round(7).tolist()])
                w.record(level, 1 if level % INDEX == 0 else 0)
                count += 1
    with open(path + ".prj", "w") as f:
        f.write(pyproj.CRS.from_epsg(4326).to_wkt(pyproj.enums.WktVersion.WKT1_ESRI))
    return count


def build_contours(out):
    os.makedirs(os.path.join(out, "contours"), exist_ok=True)
    jobs = [(out, ci, cj) for ci in range(ROWS // CHUNK) for cj in range(COLS // CHUNK)]
    with Pool() as pool:
        total = sum(pool.imap_unordered(contour_chunk, jobs))
    print(f"contours: {total} lines")


def main():
    zip_path, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    build_dem(zip_path, out)
    build_contours(out)


if __name__ == "__main__":
    main()
