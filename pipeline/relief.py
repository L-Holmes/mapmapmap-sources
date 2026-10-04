#!/usr/bin/env python3
"""
The hiking map's relief, as two sets of raster tiles: hill shading, and
steep ground.

    relief.py <heights.npy> <shade.mbtiles> <slope.mbtiles> <west,south,east,north>

First, for every 20 m cell of the height grid (pipeline/lidar.py: the
Environment Agency's LIDAR in England, OS Terrain 50 elsewhere), its slope
(Horn's 3x3 gradient) and how it is lit: from the north-west, 45° up,
heights half as much again so that lowland hills show. Kept beside the
grid as slope.npy (quarter degrees) and light.npy (-127, facing away from
the light, to 127, facing into it; flat ground 0), and made again only
when the grid is newer.

Then the tiles, each pixel taking the cells round it, bilinearly:

    shade   a soft grey shadow, to 30% opaque, the more a slope faces away
            from the light; white, to 20%, the more it faces into it.
            Zooms 8 to 12 (about 23 m a pixel).
    slope   steep ground in bands, as mapy.com shows it: peach from 25°,
            coral from 30°, pink from 38°, raspberry from 45°, each more
            opaque than the last, so the steepest stands out and gentler
            ground fades. Each band a flat colour with a crisp edge, a
            pixel soft; zooms 8 to 14 (about 5.5 m a pixel), so the edges
            stay crisp at walking zooms: at 13 they were 11 m pixels,
            blocks or blurs drawn larger. Mostly empty, so the two zooms
            more cost less than the shading's (Cumbria: 13 MB, the
            shading 14). 25° is below mapy.com's lightest band, for ground that
            is steep going without being a scramble; where the grid is
            Terrain 50's, which evens out short steep ground, it is most
            of what shows.

Flat ground is neither, so the map shows through. The app draws both
larger above their top zoom. The zooms below are their children averaged,
four pixels to one (the slope's, the steepest of the four). A tile with
nothing on it (the sea, the flat) is left out.

WebP, colour lossy, alpha exact.
"""
import hashlib
import io
import math
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pyproj
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
from progress import left  # noqa: E402
from tiles import create  # noqa: E402

CELL = 20
# The grid's sea, in its decimetres.
SEA = -25
BOTTOM = 8
SHADE_TOP, SLOPE_TOP = 12, 14
# Each worker makes the tiles under one of these, and hands them back for
# the zooms below to be made from.
JOB = 10
SIZE = 256

EXAGGERATION = 1.5
AZIMUTH, ALTITUDE = math.radians(315), math.radians(45)
SHADOW, SHADOW_COLOUR = 0.30, (0.20, 0.22, 0.28)
LIGHT = 0.20

# Slope bands: from (degrees), colour, opacity. Matched by eye, and by
# measuring a screenshot of mapy.com over Sca Fell, on the map's paper.
BANDS = [
    (25.0, (0.99, 0.72, 0.45), 0.32),
    (30.0, (0.93, 0.42, 0.42), 0.45),
    (38.0, (0.84, 0.24, 0.46), 0.62),
    (45.0, (0.70, 0.09, 0.38), 0.80),
]
# Half a band edge's width, in degrees: a pixel or so at zoom 14.
SOFT = 0.35

QUALITY = 80
# Opacity in steps of 2/255: no banding to see, and a tenth smaller.
ALPHA_STEP = 2


def derive(dem_path):
    """slope.npy and light.npy beside the grid, made again only if it is newer."""
    folder = os.path.dirname(dem_path)
    slope_path, light_path = os.path.join(folder, "slope.npy"), os.path.join(folder, "light.npy")
    if all(os.path.exists(p) and os.path.getmtime(p) >= os.path.getmtime(dem_path) for p in (slope_path, light_path)):
        return slope_path, light_path
    t = time.time()
    dem = np.load(dem_path, mmap_mode="r")
    rows, cols = dem.shape
    slope = np.lib.format.open_memmap(slope_path + ".part", mode="w+", dtype=np.uint8, shape=dem.shape)
    light = np.lib.format.open_memmap(light_path + ".part", mode="w+", dtype=np.int8, shape=dem.shape)
    lx = math.sin(AZIMUTH) * math.cos(ALTITUDE)
    ly = math.cos(AZIMUTH) * math.cos(ALTITUDE)
    lz = math.sin(ALTITUDE)
    chunk = 1000
    for r0 in range(0, rows, chunk):
        r1 = min(rows, r0 + chunk)
        # A row of overlap each way, the edges of the grid repeated.
        a, b = max(0, r0 - 1), min(rows, r1 + 1)
        z = np.asarray(dem[a:b], dtype=np.float32) / 10
        z = np.pad(z, ((1 if r0 == 0 else 0, 1 if r1 == rows else 0), (1, 1)), mode="edge")
        # Row 0 is the south: up a row is north.
        nw, n, ne = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
        w, e = z[1:-1, :-2], z[1:-1, 2:]
        sw, s, se = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
        dx = ((ne + 2 * e + se) - (nw + 2 * w + sw)) / (8 * CELL)
        dy = ((nw + 2 * n + ne) - (sw + 2 * s + se)) / (8 * CELL)
        slope[r0:r1] = np.clip(np.degrees(np.arctan(np.hypot(dx, dy))) * 4 + 0.5, 0, 255).astype(np.uint8)
        nx, ny = -EXAGGERATION * dx, -EXAGGERATION * dy
        lit = (nx * lx + ny * ly + lz) / np.sqrt(nx * nx + ny * ny + 1)
        shade = np.where(lit >= lz, (lit - lz) / (1 - lz), (lit - lz) / lz)
        light[r0:r1] = np.clip(np.round(shade * 127), -127, 127).astype(np.int8)
    slope.flush()
    light.flush()
    del slope, light
    os.replace(slope_path + ".part", slope_path)
    os.replace(light_path + ".part", light_path)
    print(f"slope and light: {rows} x {cols} cells in {time.time() - t:.0f}s")
    return slope_path, light_path


GRIDS = {}


def grids(slope_path, light_path, dem_path):
    if not GRIDS:
        GRIDS["slope"] = np.load(slope_path, mmap_mode="r")
        GRIDS["light"] = np.load(light_path, mmap_mode="r")
        GRIDS["dem"] = np.load(dem_path, mmap_mode="r")
        GRIDS["osgb"] = pyproj.Transformer.from_crs(4326, 27700, always_xy=True)
    return GRIDS


def bilinear(grid, r, c):
    """grid at fractional rows r and columns c (cell centres at whole numbers); 0 outside."""
    rows, cols = grid.shape
    r0 = np.floor(r).astype(np.int64)
    c0 = np.floor(c).astype(np.int64)
    fr, fc = r - r0, c - c0
    inside = (r0 >= 0) & (c0 >= 0) & (r0 + 1 < rows) & (c0 + 1 < cols)
    if not inside.any():
        return np.zeros(r.shape, np.float32)
    lo_r, hi_r = int(r0[inside].min()), int(r0[inside].max()) + 2
    lo_c, hi_c = int(c0[inside].min()), int(c0[inside].max()) + 2
    block = np.asarray(grid[lo_r:hi_r, lo_c:hi_c], dtype=np.float32)
    rr = np.where(inside, r0 - lo_r, 0)
    cc = np.where(inside, c0 - lo_c, 0)
    v = (block[rr, cc] * (1 - fr) * (1 - fc) + block[rr, cc + 1] * (1 - fr) * fc
         + block[rr + 1, cc] * fr * (1 - fc) + block[rr + 1, cc + 1] * fr * fc)
    return np.where(inside, v, 0).astype(np.float32)


def cells(g, z, x, y):
    """Tile z, x, y's pixel centres as fractional grid rows and columns, or None if it is all sea."""
    n = 2 ** z
    u = (np.arange(SIZE) + 0.5) / SIZE
    lon = (x + u) / n * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + u) / n))))
    lon, lat = np.meshgrid(lon, lat)
    e, north = g["osgb"].transform(lon, lat)
    c = (e - CELL / 2) / CELL
    r = (north - CELL / 2) / CELL
    dem = g["dem"]
    r0, r1 = int(max(0, r.min())), int(min(dem.shape[0], r.max() + 2))
    c0, c1 = int(max(0, c.min())), int(min(dem.shape[1], c.max() + 2))
    if r0 >= r1 or c0 >= c1 or float(np.asarray(dem[r0:r1, c0:c1]).max()) <= SEA:
        return None
    return r, c


def over(rgb, alpha, colour, a):
    """Paints colour at opacity a (per pixel) over premultiplied rgb, alpha."""
    return rgb * (1 - a)[..., None] + a[..., None] * np.array(colour, np.float32), a + alpha * (1 - a)


def shade(g, x, y):
    """Shade tile SHADE_TOP/x/y: premultiplied RGBA floats, or None if there is nothing on it."""
    at = cells(g, SHADE_TOP, x, y)
    if at is None:
        return None
    light = bilinear(g["light"], *at) / 127
    rgb = np.zeros((SIZE, SIZE, 3), np.float32)
    alpha = np.zeros((SIZE, SIZE), np.float32)
    rgb, alpha = over(rgb, alpha, SHADOW_COLOUR, SHADOW * np.clip(-light, 0, 1))
    rgb, alpha = over(rgb, alpha, (1.0, 1.0, 1.0), LIGHT * np.clip(light, 0, 1))
    return finish(rgb, alpha)


def slope(g, x, y):
    """Slope tile SLOPE_TOP/x/y, the same."""
    at = cells(g, SLOPE_TOP, x, y)
    if at is None:
        return None
    degrees = bilinear(g["slope"], *at) / 4
    if degrees.max() < BANDS[0][0] - SOFT:
        return None
    # How far into each band a pixel is, 0 to 1 across its soft edge; the
    # band it is in drawn alone, a steeper one's edge over it.
    reach = [np.clip((degrees - start + SOFT) / (2 * SOFT), 0, 1) for start, _, _ in BANDS]
    rgb = np.zeros((SIZE, SIZE, 3), np.float32)
    alpha = np.zeros((SIZE, SIZE), np.float32)
    for i, (_, colour, opacity) in enumerate(BANDS):
        a = reach[i] * opacity
        # Replace, not stack: blend from the band below to this one.
        rgb = rgb * (1 - reach[i])[..., None] + a[..., None] * np.array(colour, np.float32)
        alpha = alpha * (1 - reach[i]) + a
    return finish(rgb, alpha)


def finish(rgb, alpha):
    if alpha.max() < 1 / 255:
        return None
    return np.dstack([rgb, alpha])


def encode(tile):
    a = tile[..., 3:]
    rgb = np.where(a > 0, tile[..., :3] / np.maximum(a, 1e-6), 0)
    pixels = np.clip(np.dstack([rgb, a]) * 255 + 0.5, 0, 255).astype(np.uint8)
    pixels[..., 3] = np.minimum(np.round(pixels[..., 3] / ALPHA_STEP) * ALPHA_STEP, 255).astype(np.uint8)
    out = io.BytesIO()
    # method 6 is a few per cent smaller and a hundred times slower.
    Image.fromarray(pixels, "RGBA").save(out, "WEBP", quality=QUALITY, alpha_quality=100, method=4)
    return out.getvalue()


def shrink(kids, steepest=False):
    """
    Four tiles (top left, top right, bottom left, bottom right; None for
    empty) as their parent, or None: each pixel the four under it averaged,
    or, [steepest], the most opaque of them, which for the slope is the
    steepest. Averaged, a crag a pixel wide fades to nothing a zoom or two
    out; this way steep ground still shows with the whole Lake District on
    the screen.
    """
    if all(k is None for k in kids):
        return None
    blank = np.zeros((SIZE, SIZE, 4), np.float32)
    k = [blank if t is None else t.astype(np.float32) for t in kids]
    big = np.vstack([np.hstack(k[:2]), np.hstack(k[2:])]).reshape(SIZE, 2, SIZE, 2, 4)
    if steepest:
        four = big.transpose(0, 2, 1, 3, 4).reshape(SIZE, SIZE, 4, 4)
        pick = four[..., 3].argmax(axis=2)[..., None, None]
        t = np.take_along_axis(four, pick, axis=2)[:, :, 0, :]
    else:
        t = big.mean(axis=(1, 3))
    return t if t[..., 3].max() >= 1 / 255 else None


# Each kind: how a tile is made, its top zoom, and whether the zooms below keep the steepest pixel.
KINDS = {"shade": (shade, SHADE_TOP, False), "slope": (slope, SLOPE_TOP, True)}


def pyramid(job):
    """For zoom JOB tile x, y: each kind's tiles under it, (z, x, y, webp) for each with something on it, and its own."""
    paths, x, y = job
    g = grids(*paths)
    out = {}
    for kind, (render, top, steepest) in KINDS.items():
        tiles = []

        def tile(z, tx, ty):
            if z == top:
                t = render(g, tx, ty)
            else:
                t = shrink([tile(z + 1, 2 * tx + dx, 2 * ty + dy) for dy in (0, 1) for dx in (0, 1)], steepest)
            if t is not None:
                tiles.append((z, tx, ty, encode(t)))
            return t

        t = tile(JOB, x, y)
        out[kind] = (tiles, None if t is None else t.astype(np.float16))
    return x, y, out


class Output:
    """An mbtiles file being written, its tiles stored once however often they repeat."""

    def __init__(self, path, kind, top, bounds):
        self.path, self.kind, self.top, self.bounds = path, kind, top, bounds
        self.db = create(path + ".part")
        self.ids = {}
        self.count = self.size = 0

    def store(self, z, x, y, data):
        h = hashlib.sha1(data).digest()
        if h not in self.ids:
            self.ids[h] = len(self.ids) + 1
            self.db.execute("INSERT INTO tiles_data VALUES (?, ?)", (self.ids[h], data))
            self.size += len(data)
        self.db.execute("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)", (z, x, (2 ** z - 1) - y, self.ids[h]))
        self.count += 1

    def close(self):
        what = "Hill shading" if self.kind == "shade" else "Steep ground: from 25°, 30°, 38° and 45°"
        meta = {
            "name": self.kind, "format": "webp", "type": "overlay", "version": "1",
            "description": f"{what}, from OS Terrain 50",
            "attribution": "Contains OS data © Crown copyright and database right",
            "minzoom": str(BOTTOM), "maxzoom": str(self.top), "bounds": self.bounds,
        }
        self.db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
        self.db.commit()
        self.db.close()
        os.replace(self.path + ".part", self.path)
        print(f"{self.kind}: {self.count} tiles, {os.path.getsize(self.path) / 1e6:.0f} MB")


def main():
    heights_path, shade_path, slope_path, bounds = sys.argv[1:5]
    west, south, east, north = map(float, bounds.split(","))
    start = time.time()
    paths = (*derive(heights_path), heights_path)
    n = 2 ** JOB
    x0, x1 = int((west + 180) / 360 * n), int((east + 180) / 360 * n)
    y0 = int((1 - math.asinh(math.tan(math.radians(north))) / math.pi) / 2 * n)
    y1 = int((1 - math.asinh(math.tan(math.radians(south))) / math.pi) / 2 * n)
    jobs = [(paths, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    outputs = {"shade": Output(shade_path, "shade", SHADE_TOP, bounds), "slope": Output(slope_path, "slope", SLOPE_TOP, bounds)}
    levels = {kind: {} for kind in outputs}
    began = time.time()
    with Pool() as pool:
        for done, (x, y, kinds) in enumerate(pool.imap_unordered(pyramid, jobs), 1):
            for kind, (tiles, t) in kinds.items():
                for tile in tiles:
                    outputs[kind].store(*tile)
                if t is not None:
                    levels[kind][(x, y)] = t
            print(f"\r    {done}/{len(jobs)} zoom {JOB} blocks, "
                  + ", ".join(f"{k} {o.count} tiles {o.size / 1e6:.0f} MB" for k, o in outputs.items())
                  + f", {left(began, done, len(jobs))} ", end="", flush=True)
    print()
    # The zooms below the workers', from theirs.
    for kind, level in levels.items():
        for z in range(JOB - 1, BOTTOM - 1, -1):
            parents = {(x // 2, y // 2) for x, y in level}
            level = {
                (x, y): t for x, y in parents
                if (t := shrink([level.get((2 * x + dx, 2 * y + dy)) for dy in (0, 1) for dx in (0, 1)], KINDS[kind][2])) is not None
            }
            for (x, y), t in level.items():
                outputs[kind].store(z, x, y, encode(t))
    for o in outputs.values():
        o.close()
    print(f"relief in {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
