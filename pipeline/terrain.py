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
contours() draws them from any grid (pipeline/grid.py): pipeline/copernicus.py
draws the other areas' with it.
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

sys.path.insert(0, os.path.dirname(__file__))
from grid import Grid  # noqa: E402

CELL = 50
COLS = 700_000 // CELL
ROWS = 1_300_000 // CELL
SEA = -2.5
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
    grid_path, out, ci, cj, interval, index, smooth, least = args
    g = Grid.of(grid_path)
    dem = np.load(grid_path, mmap_mode="r")
    chunk = round(100_000 / g.cell)
    r0, c0 = ci * chunk, cj * chunk
    # One cell of overlap each way, so lines meet the next square's exactly;
    # smoothed, with more round it, so they are smoothed the same either side.
    m = 3 * int(np.ceil(smooth)) if smooth else 0
    a0, b0 = max(r0 - m, 0), max(c0 - m, 0)
    block = np.asarray(dem[a0:r0 + chunk + 1 + m, b0:c0 + chunk + 1 + m], dtype=np.float64) * g.scale
    if smooth:
        from scipy.ndimage import gaussian_filter
        block = gaussian_filter(block, smooth, mode="nearest")
    block = block[r0 - a0:r0 - a0 + chunk + 1, c0 - b0:c0 - b0 + chunk + 1]
    if not block.size:
        return 0
    top = float(block.max())
    if top < interval:
        return 0
    gen = contourpy.contour_generator(z=block, line_type=contourpy.LineType.Separate)
    to_wgs = g.to_wgs()
    path = os.path.join(out, f"c_{ci:02d}_{cj:02d}")
    count = 0
    with shapefile.Writer(path, shapeType=shapefile.POLYLINE) as w:
        w.field("ele", "N", size=5)
        w.field("idx", "N", size=1)
        for level in range(interval, int(top) + 1, interval):
            lines = gen.lines(level)
            if not lines:
                continue
            geoms = []
            for xy in lines:
                if len(xy) < 2:
                    continue
                # A ring this short is a bump in the heights, not a hill.
                if least and np.allclose(xy[0], xy[-1]) and np.hypot(*np.diff(xy, axis=0).T).sum() < least:
                    continue
                e = g.x0 + (c0 + xy[:, 0]) * g.cell + g.cell / 2
                n = g.y0 + (r0 + xy[:, 1]) * g.cell + g.cell / 2
                geoms.append(shapely.linestrings(np.column_stack([e, n])))
            for line in shapely.simplify(np.array(geoms, dtype=object), SIMPLIFY_M):
                coords = shapely.get_coordinates(line)
                if len(coords) < 2:
                    continue
                lon, lat = to_wgs.transform(coords[:, 0], coords[:, 1])
                w.line([np.column_stack([lon, lat]).round(7).tolist()])
                w.record(level, 1 if level % index == 0 else 0)
                count += 1
    with open(path + ".prj", "w") as f:
        f.write(pyproj.CRS.from_epsg(4326).to_wkt(pyproj.enums.WktVersion.WKT1_ESRI))
    return count


def contours(grid_path, out, interval=INTERVAL, index=INDEX, smooth=0.0, least=0):
    """
    Contour lines from a grid into <out>, one shapefile per 100 km square:
    every [interval] metres, [index] marking every so many. [smooth]: the
    heights first blurred by a Gaussian of that many cells; [least]: rings
    shorter than so many cells left out.
    """
    os.makedirs(out, exist_ok=True)
    g = Grid.of(grid_path)
    chunk = round(100_000 / g.cell)
    jobs = [(grid_path, out, ci, cj, interval, index, smooth, least)
            for ci in range(-(-g.rows // chunk)) for cj in range(-(-g.cols // chunk))]
    with Pool() as pool:
        total = sum(pool.imap_unordered(contour_chunk, jobs))
    print(f"contours: {total} lines")


def main():
    zip_path, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    build_dem(zip_path, out)
    contours(os.path.join(out, "dem.npy"), os.path.join(out, "contours"))


if __name__ == "__main__":
    main()
