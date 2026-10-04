#!/usr/bin/env python3
"""
Each named peak's score, how impressively it rises above the paths and
roads round it, and the place on one it does so most from: its base, the
best place to see it from.

    jut.py <osm.pbf> <heights.npy> <graph.npz> <driving.npz> <out dir>

The score starts from jut (Kai Xu's, peakjut.com): how impressively a
point P rises above a point Q is h·|sin θ|, where h is P's height above
Q's horizon (the earth's curve taken off) and θ the angle Q looks up at
it; P's jut is that at the Q that makes it most. Looking straight up a
cliff, all of h counts; from a long way off, little of it. Here it is
adjusted ("ajut"):

  - Q must be on a path or a road: any way in the walking or the driving
    graph (pipeline/graph.py), ferries aside. Somewhere a person stands,
    not the foot of a gully no one goes to.
  - The steepness that counts is the climb's, not just the straight
    line's: how far it goes for each third of its height (from where the
    ground first reaches it to where it first reaches the next), the
    lowest third counted three times, the middle twice, as the angle that
    rises a third in that weighted mean distance. A climb as steep all
    the way is its straight line's angle; one that starts at the path and
    eases off near the top, steeper; a level stretch before the climb,
    gentler. (Counted evenly, it would be the straight line's whatever
    the shape.)
  - Steepness counts for more: the jut is multiplied by 1 + 1.1 s, where
    s steps smoothly from 0 to 1 about 30° (0.1 at 25°, 0.5 at 30°, 0.9
    at 35°):

        score = jut × (1 + 1.1 / (1 + e^(-0.45 (steepness - 30°))))

P is the summit alone, for now: the highest 20 m cell within 40 m of the
peak as mapped. Q is looked for within 10 km, on the 20 m height grid
(pipeline/lidar.py: the LIDAR in England, OS Terrain 50 elsewhere), at
the middle of each cell a path or road crosses. The score, like jut, is
in metres: a cliff of 100 m, seen from its foot, scores 100 × 2.1.

Writes, in <out dir>:

    ways.npy    every 20 m cell a path or road crosses, 1 (uint8, laid out
                as heights.npy); made again when a graph is newer
    lines.shp   each scored peak's line, its summit to its base, with its
                "score" (whole metres)
    bases.shp   each base, a point, with its peak's "score"
    jut.tsv     every scored peak, with what went into its score

Peaks are OpenStreetMap's named natural=peak, hill and volcano, as the
hiking tiles' peak layer has them (pipeline/hiking/Hiking.java, which
reads lines.shp and bases.shp into its "jut" layer). A peak with no path
or road below it within reach has no score.
"""
import csv
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pyproj
import shapefile

CELL = 20
COLS, ROWS = 700_000 // CELL, 1_300_000 // CELL
EARTH = 6_371_000.0
REACH = 10_000
SUMMIT = 2  # cells either side of a peak as mapped that its summit is looked for in
STEP = CELL / 2  # along a climb, metres between the heights read
THIRDS = np.array([3.0, 2.0, 1.0])  # how much the lower, middle and upper thirds count
LIFT, MIDDLE, RATE = 1.1, 30.0, 0.45
MOST = 1 + LIFT  # the most steepness can multiply a jut by
BATCH = 256
FERRY = 16  # graph.py's ROAD.index("ferry")


# --- The score -------------------------------------------------------------------

def jut(h, d):
    """How impressively a point h metres above another's horizon, d metres from it, rises: h·sin θ."""
    h = np.maximum(h, 0.0)
    return h * h / np.maximum(np.hypot(h, d), 1e-9)


def boost(steepness):
    """What a climb's steepness (degrees) multiplies its jut by: 1 on gentle ground, 1.55 at 30°, to 2.1."""
    return 1 + LIFT / (1 + np.exp(-RATE * (steepness - MIDDLE)))


def steepness(x, rise, d, h):
    """
    How steep each of K climbs is, degrees. Climb k rises h[k] metres in
    d[k]; along it, rise[k, i] is the ground's height above where it starts
    (its horizon, so less the earth's curve), x[k, i] metres out. Past its
    end, x is d and rise is h. Each third of the height takes the distance
    from where the ground first reaches it to where it first reaches the
    next; the angle is that of a third of the height over those distances'
    mean, counted 3, 2 and 1, lowest first. (A mean of the thirds' angles
    instead would make a level stretch then a steep climb out steeper than
    an even slope of the same height and length: its steep thirds' angles
    outweigh its level one's.)
    """
    k = np.arange(len(h))
    ends = [np.zeros(len(h))]
    for third in (1, 2):
        ends.append(x[k, np.argmax(rise >= h[:, None] * third / 3, axis=1)])
    ends.append(d)
    run = np.diff(np.column_stack(ends), axis=1)
    return np.degrees(np.arctan2(h / 3 * THIRDS.sum(), run @ THIRDS))


def score(h, d, steep):
    return jut(h, d) * boost(steep)


# --- Finding each peak's base ------------------------------------------------------

def climbs(z, r0, c0, qe, qn, e, n, zq, d, h):
    """The steepness of the climbs from points (qe, qn) to the summit at (e, n), over heights z from (r0, c0)."""
    count = np.maximum(np.ceil(d / STEP), 1)
    t = np.minimum(np.arange(1, int(count.max()) + 1)[None, :] / count[:, None], 1.0)
    x = t * d[:, None]
    rows = np.clip(((qn[:, None] + t * (n - qn)[:, None]) // CELL).astype(np.int64) - r0, 0, z.shape[0] - 1)
    cols = np.clip(((qe[:, None] + t * (e - qe)[:, None]) // CELL).astype(np.int64) - c0, 0, z.shape[1] - 1)
    rise = np.where(t >= 1, h[:, None], z[rows, cols] - zq[:, None] - x * x / (2 * EARTH))
    return steepness(x, rise, d, h)


HEIGHTS = WAYS = None


def opened(heights_path, ways_path):
    global HEIGHTS, WAYS
    HEIGHTS = np.load(heights_path, mmap_mode="r")
    WAYS = np.load(ways_path, mmap_mode="r")


def base(peak):
    """
    A peak's best base: (score, jut, steepness, angle, height above it,
    distance, base easting, northing, summit height), or None. Every
    path and road cell in reach is a candidate; the climb is read only for
    those whose jut, at the most steepness could make of it, would still
    beat the best so far, the highest jut first.
    """
    e, n = peak
    r, c = int(n // CELL), int(e // CELL)
    if not (SUMMIT <= r < ROWS - SUMMIT and SUMMIT <= c < COLS - SUMMIT):
        return None
    zp = float(HEIGHTS[r - SUMMIT:r + SUMMIT + 1, c - SUMMIT:c + SUMMIT + 1].max()) / 10
    if zp <= 0:
        return None
    w = REACH // CELL
    r0, c0 = max(r - w, 0), max(c - w, 0)
    r1, c1 = min(r + w + 1, ROWS), min(c + w + 1, COLS)
    rr, cc = np.nonzero(np.asarray(WAYS[r0:r1, c0:c1]))
    if not len(rr):
        return None
    z = np.asarray(HEIGHTS[r0:r1, c0:c1], dtype=np.float32) / 10
    qe, qn = (c0 + cc + 0.5) * CELL, (r0 + rr + 0.5) * CELL
    zq = z[rr, cc].astype(np.float64)
    d = np.hypot(qe - e, qn - n)
    h = zp - zq - d * d / (2 * EARTH)
    j = jut(h, d)
    near = np.flatnonzero((d <= REACH) & (h > 0))
    order = near[np.argsort(-j[near], kind="stable")]
    best, found = 0.0, None
    for start in range(0, len(order), BATCH):
        b = order[start:start + BATCH]
        b = b[MOST * j[b] > best]
        if not len(b):
            break
        steep = climbs(z, r0, c0, qe[b], qn[b], e, n, zq[b], d[b], h[b])
        s = j[b] * boost(steep)
        k = int(np.argmax(s))
        if s[k] > best:
            best = float(s[k])
            q = b[k]
            found = (best, float(j[q]), float(steep[k]), float(np.degrees(np.arctan2(h[q], d[q]))),
                     float(h[q]), float(d[q]), float(qe[q]), float(qn[q]), zp)
    return found


# --- Inputs and outputs ----------------------------------------------------------

def rasterize(graphs, out_path):
    """Every 20 m cell a way in the graphs crosses, ferries aside, as 1s in a grid like heights.npy."""
    t = time.time()
    to_osgb = pyproj.Transformer.from_crs(4326, 27700, always_xy=True)
    ways = np.lib.format.open_memmap(out_path + ".part", mode="w+", dtype=np.uint8, shape=(ROWS, COLS))
    cells = 0
    for path in graphs:
        g = np.load(path, mmap_mode="r")
        geom = np.asarray(g["geom"])
        ferry = (np.asarray(g["kind"]) & 0x1F) == FERRY if "kind" in g.files else np.zeros(len(geom) - 1, bool)
        chunk = 2_000_000
        for a in range(0, len(geom) - 1, chunk):
            b = min(a + chunk, len(geom) - 1)
            p0, p1 = geom[a], geom[b]
            e, n = to_osgb.transform(np.asarray(g["lon"][p0:p1]) / 1e7, np.asarray(g["lat"][p0:p1]) / 1e7)
            edge = np.repeat(np.arange(a, b), np.diff(geom[a:b + 1]))
            seg = np.flatnonzero((edge[:-1] == edge[1:]) & ~ferry[edge[:-1]])
            # Each segment read every half cell, both ends included.
            de, dn = e[seg + 1] - e[seg], n[seg + 1] - n[seg]
            steps = np.ceil(np.hypot(de, dn) / (CELL / 2)).astype(np.int64) + 1
            first = np.repeat(np.cumsum(steps) - steps, steps)
            f = (np.arange(steps.sum()) - first) / np.repeat(np.maximum(steps - 1, 1), steps)
            s = np.repeat(seg, steps)
            col = ((e[s] + f * np.repeat(de, steps)) // CELL).astype(np.int64)
            row = ((n[s] + f * np.repeat(dn, steps)) // CELL).astype(np.int64)
            inside = (row >= 0) & (row < ROWS) & (col >= 0) & (col < COLS)
            ways[row[inside], col[inside]] = 1
            cells += int(inside.sum())
        print(f"    {os.path.basename(path)}: read every way in {time.time() - t:.0f}s", flush=True)
    ways.flush()
    del ways
    os.replace(out_path + ".part", out_path)
    print(f"    paths and roads: {cells / 1e6:.0f}M points read into the grid in {time.time() - t:.0f}s")


def peaks(pbf):
    """OpenStreetMap's named peaks, hills and volcanoes: (id, lon, lat, name)."""
    import osmium
    found = []
    fp = osmium.FileProcessor(pbf, osmium.osm.NODE).with_filter(
        osmium.filter.TagFilter(("natural", "peak"), ("natural", "hill"), ("natural", "volcano")))
    for node in fp:
        name = node.tags.get("name")
        if name and node.location.valid():
            found.append((node.id, node.location.lon, node.location.lat, name))
    return found


def write(out, found, results):
    to_wgs = pyproj.Transformer.from_crs(27700, 4326, always_xy=True)
    lines = shapefile.Writer(os.path.join(out, "lines"), shapeType=shapefile.POLYLINE)
    bases = shapefile.Writer(os.path.join(out, "bases"), shapeType=shapefile.POINT)
    for w in (lines, bases):
        w.field("score", "N", size=6)
    rows = []
    for (osm_id, lon, lat, name), r in zip(found, results):
        if r is None:
            continue
        s, j, steep, angle, h, d, qe, qn, zp = r
        blon, blat = to_wgs.transform(qe, qn)
        lines.line([[[round(lon, 7), round(lat, 7)], [round(blon, 7), round(blat, 7)]]])
        lines.record(round(s))
        bases.point(round(blon, 7), round(blat, 7))
        bases.record(round(s))
        rows.append((name, osm_id, round(lat, 6), round(lon, 6), round(zp), round(s), round(j), round(steep, 1),
                     round(angle, 1), round(h), round(d), round(blat, 6), round(blon, 6)))
    lines.close()
    bases.close()
    prj = pyproj.CRS.from_epsg(4326).to_wkt(pyproj.enums.WktVersion.WKT1_ESRI)
    for stem in ("lines", "bases"):
        with open(os.path.join(out, stem + ".prj"), "w") as f:
            f.write(prj)
    rows.sort(key=lambda row: -row[5])
    with open(os.path.join(out, "jut.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(("name", "osm_id", "lat", "lon", "summit_m", "score", "jut", "steepness", "angle", "height_m",
                    "distance_m", "base_lat", "base_lon"))
        w.writerows(rows)
    return rows


def main():
    pbf, heights_path, walking, driving, out = sys.argv[1:6]
    os.makedirs(out, exist_ok=True)
    t = time.time()
    ways_path = os.path.join(out, "ways.npy")
    newest = max(os.path.getmtime(p) for p in (walking, driving))
    if os.path.exists(ways_path) and os.path.getmtime(ways_path) > newest:
        print("    have the paths and roads' grid")
    else:
        rasterize([walking, driving], ways_path)
    found = peaks(pbf)
    print(f"    {len(found)} named peaks, read in {time.time() - t:.0f}s", flush=True)
    to_osgb = pyproj.Transformer.from_crs(4326, 27700, always_xy=True)
    e, n = to_osgb.transform(np.array([p[1] for p in found]), np.array([p[2] for p in found]))
    # Neighbours together, so a worker's reads of the grids overlap.
    order = np.lexsort((e // REACH, n // REACH))
    results = [None] * len(found)
    with Pool(initializer=opened, initargs=(heights_path, ways_path)) as pool:
        done = 0
        for i, r in zip(order, pool.imap(base, [(e[i], n[i]) for i in order], chunksize=16)):
            results[i] = r
            done += 1
            if done % 2000 == 0:
                print(f"\r    {done}/{len(found)} peaks, {time.time() - t:.0f}s", end="", flush=True)
    print()
    rows = write(out, found, results)
    print(f"jut: {len(rows)} of {len(found)} peaks scored in {time.time() - t:.0f}s; highest: "
          + ", ".join(f"{r[0]} {r[5]}" for r in rows[:5]))


if __name__ == "__main__":
    main()
