#!/usr/bin/env python3
"""
Each named peak's scores, how impressively it rises above where someone
can stand round it, and the place it does so most from: its base, the
best place to see it from. By where that is: a path or a road ("path"),
the sea ("sea"), or a lake, three ways, by the least size of lake counted
(see LAKE_HA): any of a hectare or more, as big as a tarn ("lake"), of
10 ha or more ("lake10"), or of 50 ha or more, lakes like Buttermere
("lake50").

    jut.py <osm.pbf> <sea polygons .zip> <regions.json> <heights.npy> <graph.npz> <driving.npz> <out dir>

The score starts from jut (Kai Xu's, peakjut.com): how impressively a
point P rises above a point Q is h·|sin θ|, where h is P's height above
Q's horizon (the earth's curve taken off) and θ the angle Q looks up at
it; P's jut is that at the Q that makes it most. Looking straight up a
cliff, all of h counts; from a long way off, little of it. Here it is
adjusted ("ajut"):

  - Q must be somewhere a person stands or floats, not the foot of a
    gully no one goes to: for "path", any way in the walking or the
    driving graph (pipeline/graph.py), ferries aside; for "sea", the
    sea's edge, as OpenStreetMap's coastline has it (sea lochs and
    estuaries are sea), at sea level; for "lake" and the bigger lakes', a
    lake's edge, at its water's height: OpenStreetMap's lakes, ponds and
    reservoirs of that size or more, not rivers, canals or the like.
    (Further out on the water is never better: the edge on the way in is
    nearer, as high, and has no flat start.)
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
the middle of each cell. The score, like jut, is in metres: a cliff of
100 m, seen from its foot, scores 100 × 2.1.

Each kind of score is ranked too: 1 for a peak whose score is the
highest within 5 km, 2 for the highest in its county (England's counties
as regions.json has them; Scotland's council areas and Wales's principal
areas, from OpenStreetMap) when no peak within 15 km of it, in any
county, is higher either, else 0. Borders run over ranges (the
Cairngorms are in three counties, Snowdonia two): a county's highest
alone would star a peak each side, the lower beside higher ones over the
border. So a county whose highest is outdone near it has no star. One
ranked 0 is "close" when it is within a tenth of the highest within
5 km.

Writes, in <out dir>:

    ways.npy     every 20 m cell a path or road crosses, 1 (uint8, laid
                 out as heights.npy); made again when a graph is newer
    water-sizes.npy  every 20 m cell of sea, 1, and of lake, 2, 3 or 4 by
                 its size (LAKE_HA's, smallest first); made again when the
                 OpenStreetMap data or the sea's polygons are newer
    counties.json  Scotland's and Wales's, from OpenStreetMap, made with it
    summits.shp  each peak's scores, a point at its summit for each kind it
                 has one of: "kind", "score" (whole metres), "rank" and
                 "close" (1 or 0); and, for the app's list of them, the
                 peak's "name", "ele" (metres: as mapped, or else its
                 summit's height here), "county" and "country" (England,
                 Scotland or Wales; both blank outside every county)
    lines.shp    a line from the summit to the base for each, with "kind"
                 and "peak_score", the summit's score
    bases.shp    each base, a point, with "kind" and "peak_score"
    jut.tsv      every score, with what went into it

Peaks are OpenStreetMap's named natural=peak, hill and volcano, as the
hiking tiles' peak layer has them (pipeline/hiking/Hiking.java, which
reads the shapefiles into its "jut" layer). A peak with nowhere of a kind
below it within reach has no score of that kind, nor one whose score
would round to 0.
"""
import csv
import json
import os
import re
import sys
import time
from multiprocessing import Pool

import numpy as np
import pyproj
import shapefile

sys.path.insert(0, os.path.dirname(__file__))
from progress import left  # noqa: E402

CELL = 20
COLS, ROWS = 700_000 // CELL, 1_300_000 // CELL
EARTH = 6_371_000.0
REACH = 10_000
SUMMIT = 2  # cells either side of a peak as mapped that its summit is looked for in
STEP = CELL / 2  # along a climb, metres between the heights read
THIRDS = np.array([3.0, 2.0, 1.0])  # how much the lower, middle and upper thirds count
LIFT, MIDDLE, RATE = 1.1, 30.0, 0.45
BATCH = 256
FERRY = 16  # graph.py's ROAD.index("ferry")
# Lakes, by the least area counted, hectares: any as big as a tarn; big tarns and small lakes; lakes like
# Buttermere and up.
LAKE_HA = {"lake": 1, "lake10": 10, "lake50": 50}
KINDS = ("path", "sea", *LAKE_HA)
SEA, LAKE = 1, 2  # in water-sizes.npy, a lake LAKE + the index of the biggest of LAKE_HA it is as big as
# OpenStreetMap's water=* for a lake, a pond or a reservoir; none at all, mostly those too.
LAKES = {None, "lake", "pond", "reservoir", "lagoon", "oxbow", "mere", "fishpond", "lake;pond", "dew_pond"}
NEAR = 5_000  # a peak with the highest score within this is ranked 1
CLOSE = 0.9  # and one with this much of that score or more is "close"
AREA = 15_000  # the highest in its county is ranked 2 when none within this, in any county, is higher
# An OpenStreetMap height: "978", "978.4", "978 m", or in feet, "3209 ft" or "3209'" (Hiking.java's HEIGHT).
HEIGHT = re.compile(r"\s*(-?\d+(?:\.\d+)?)\s*(m|ft|feet|')?\s*")
GB = (-8.8, 49.8, 2.0, 61.0)  # west, south, east, north: where the sea's polygons are read


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


# --- Finding each peak's bases -----------------------------------------------------

def climbs(z, r0, c0, qe, qn, e, n, zq, d, h):
    """The steepness of the climbs from points (qe, qn) to the summit at (e, n), over heights z from (r0, c0)."""
    count = np.maximum(np.ceil(d / STEP), 1)
    t = np.minimum(np.arange(1, int(count.max()) + 1)[None, :] / count[:, None], 1.0)
    x = t * d[:, None]
    rows = np.clip(((qn[:, None] + t * (n - qn)[:, None]) // CELL).astype(np.int64) - r0, 0, z.shape[0] - 1)
    cols = np.clip(((qe[:, None] + t * (e - qe)[:, None]) // CELL).astype(np.int64) - c0, 0, z.shape[1] - 1)
    rise = np.where(t >= 1, h[:, None], z[rows, cols] - zq[:, None] - x * x / (2 * EARTH))
    return steepness(x, rise, d, h)


def search(z, r0, c0, rr, cc, zq, e, n, zp):
    """
    The best base among the cells (rr, cc) of heights z from (r0, c0), where
    one stands at zq, for the summit at (e, n), zp high: (score, jut,
    steepness, angle, height above it, distance, base easting, northing,
    summit height), or None. The climb is read only for those whose score
    could still beat the best so far, the likeliest first: at most its jut
    times what the steepest climb it could be makes of it, which is all of
    its height in the last third of the way, atan(2h / d) (see steepness()).
    """
    qe, qn = (c0 + cc + 0.5) * CELL, (r0 + rr + 0.5) * CELL
    d = np.hypot(qe - e, qn - n)
    h = zp - zq - d * d / (2 * EARTH)
    j = jut(h, d)
    near = np.flatnonzero((d <= REACH) & (h > 0))
    most = j[near] * boost(np.degrees(np.arctan2(2 * h[near], d[near])))
    sort = np.argsort(-most, kind="stable")
    order, most = near[sort], most[sort]
    best, found = 0.0, None
    for start in range(0, len(order), BATCH):
        b = order[start:start + BATCH][most[start:start + BATCH] > best]
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


HEIGHTS = WAYS = WATER = None


def opened(heights_path, ways_path, water_path):
    global HEIGHTS, WAYS, WATER
    HEIGHTS = np.load(heights_path, mmap_mode="r")
    WAYS = np.load(ways_path, mmap_mode="r")
    WATER = np.load(water_path, mmap_mode="r")


def bases(peak):
    """
    A peak's best base of each kind, as search() has it: {kind: base}, the
    kinds it has one of. Paths and roads: every cell one crosses, at the
    ground's height. The sea and lakes: the cells at their edge, the sea's
    at sea level and a lake's at its water's (the lowest of its cells round
    each, as the edge's own are partly shore); for each size of lake, the
    lakes that size or bigger.
    """
    from scipy.ndimage import binary_erosion, minimum_filter
    e, n = peak
    r, c = int(n // CELL), int(e // CELL)
    if not (SUMMIT <= r < ROWS - SUMMIT and SUMMIT <= c < COLS - SUMMIT):
        return {}
    zp = float(HEIGHTS[r - SUMMIT:r + SUMMIT + 1, c - SUMMIT:c + SUMMIT + 1].max()) / 10
    if zp <= 0:
        return {}
    w = REACH // CELL
    r0, c0 = max(r - w, 0), max(c - w, 0)
    r1, c1 = min(r + w + 1, ROWS), min(c + w + 1, COLS)
    z = np.asarray(HEIGHTS[r0:r1, c0:c1], dtype=np.float32) / 10
    found = {}
    rr, cc = np.nonzero(np.asarray(WAYS[r0:r1, c0:c1]))
    if len(rr):
        found["path"] = search(z, r0, c0, rr, cc, z[rr, cc].astype(np.float64), e, n, zp)
    water = np.asarray(WATER[r0:r1, c0:c1])
    masks = [("sea", water == SEA)] + [(kind, water >= LAKE + i) for i, kind in enumerate(LAKE_HA)]
    for kind, mask in masks:
        if not mask.any():
            continue
        # Past the window's edge counts as more of it, not as its edge.
        rr, cc = np.nonzero(mask & ~binary_erosion(mask, border_value=1))
        if kind == "sea":
            zq = np.zeros(len(rr))
        else:
            zq = minimum_filter(np.where(mask, z, np.inf), size=3)[rr, cc].astype(np.float64)
        found[kind] = search(z, r0, c0, rr, cc, zq, e, n, zp)
    # A score under a metre is nothing to see: it would show as 0.
    return {kind: b for kind, b in found.items() if b is not None and b[0] >= 0.5}


# --- Ranks -------------------------------------------------------------------------

def ranks(where, scores, county):
    """
    Each score's rank: 2 for the highest in its county (county[i], -1 for
    none) when none within AREA of it, in any county, is higher; 1 for the
    highest within NEAR of it; else 0. Where a tie, the first. And whether
    each ranked 0 is close: CLOSE of the highest within NEAR of it or more.
    where: the peaks' OSGB eastings and northings.
    """
    from scipy.spatial import cKDTree
    tree = cKDTree(where)

    def highest(i, near):
        return all(scores[i] > scores[j] or (scores[i] == scores[j] and i < j) for j in near if j != i)

    rank = np.zeros(len(scores), dtype=np.int64)
    close = np.zeros(len(scores), dtype=bool)
    for i, near in enumerate(tree.query_ball_point(where, NEAR)):
        if highest(i, near):
            rank[i] = 1
        else:
            close[i] = scores[i] >= CLOSE * max(scores[j] for j in near)
    for c in np.unique(county[county >= 0]):
        within = np.flatnonzero(county == c)
        best = within[np.argmax(scores[within])]
        if highest(best, tree.query_ball_point(where[best], AREA)):
            rank[best] = 2
    return rank, close & (rank == 0)


def counties_of(lon, lat, regions_path, counties_path):
    """
    Which county each point is in, an index into the names returned, -1 for
    none; and the names, and each one's country.
    """
    import shapely
    from shapely.geometry import shape
    names, countries, geoms = [], [], []
    for region in json.load(open(regions_path)):
        if region["parent"] == "england":
            names.append(region["name"])
            countries.append("England")
            geoms.append(shape(region["geometry"]))
    for area in json.load(open(counties_path)):
        names.append(area["name"])
        countries.append(area["country"])
        geoms.append(shapely.from_wkb(bytes.fromhex(area["wkb"])))
    within, which = shapely.STRtree(geoms).query(shapely.points(lon, lat), predicate="within")
    county = np.full(len(lon), -1)
    # The first where two overlap.
    county[within[::-1]] = which[::-1]
    return county, names, countries


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


def areas(pbf):
    """
    From OpenStreetMap, in one read: its lakes as big as the least of
    LAKE_HA or bigger, as OSGB polygons; and Scotland's council areas and
    Wales's principal areas (admin_level 6, GSS codes S12 and W06), as
    {"name", "country", "wkb"} in WGS84.
    """
    import osmium
    import shapely
    from shapely.ops import transform
    to_osgb = pyproj.Transformer.from_crs(4326, 27700, always_xy=True).transform
    wkb = osmium.geom.WKBFactory()
    lakes, counties = [], []
    t, seen = time.time(), 0
    fp = (osmium.FileProcessor(pbf)
          .with_areas(osmium.filter.TagFilter(("natural", "water"), ("landuse", "reservoir"), ("boundary", "administrative")))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA))
          .with_filter(osmium.filter.TagFilter(("natural", "water"), ("landuse", "reservoir"), ("admin_level", "6"))))
    for area in fp:
        seen += 1
        if seen % 50_000 == 0:
            print(f"    read {seen // 1000}k lakes and boundaries, {time.time() - t:.0f}s", flush=True)
        tags = area.tags
        try:
            geom = shapely.from_wkb(wkb.create_multipolygon(area))
        except Exception:  # noqa: BLE001 - a multipolygon too broken to make
            continue
        if tags.get("boundary") == "administrative":
            country = {"S12": "Scotland", "W06": "Wales"}.get((tags.get("ref:gss") or "")[:3])
            if country and tags.get("name"):
                counties.append({"name": tags["name"], "country": country, "wkb": shapely.to_wkb(geom, hex=True)})
            continue
        if tags.get("water") not in LAKES or tags.get("waterway") or tags.get("tidal") == "yes":
            continue
        lake = transform(to_osgb, geom)
        if lake.area >= min(LAKE_HA.values()) * 10_000:
            lakes.append(lake)
    return lakes, counties


def rings(polygons):
    """Each polygon's outer rings and its holes, in grid cells: (outer, holes) pairs."""
    import shapely
    for polygon in shapely.get_parts(polygons):
        yield (np.asarray(polygon.exterior.coords) / CELL,
               [np.asarray(hole.coords) / CELL for hole in polygon.interiors])


def water_grid(sea_zip, lakes, out_path):
    """
    The sea, 1, and the lakes, LAKE and up by size, in a grid like
    heights.npy, by drawing each polygon's cells: the lakes smallest first,
    so that where two overlap, the bigger's cells are the bigger's.
    """
    from PIL import Image, ImageDraw
    import shapely
    t = time.time()
    Image.MAX_IMAGE_PIXELS = None
    image = Image.new("L", (COLS, ROWS), 0)
    draw = ImageDraw.Draw(image)
    to_osgb = pyproj.Transformer.from_crs(3857, 27700, always_xy=True)
    west, south = pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform(GB[0], GB[1])
    east, north = pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform(GB[2], GB[3])
    # The sea's polygons, as OpenStreetMap's coastline has it: in a shapefile,
    # outer rings run clockwise and holes (islands) the other way.
    sea = []
    for shape in shapefile.Reader(sea_zip).iterShapes(bbox=[west, south, east, north]):
        xy = np.asarray(shape.points)
        e, n = to_osgb.transform(xy[:, 0], xy[:, 1])
        parts = list(shape.parts) + [len(xy)]
        outer, holes = [], []
        for a, b in zip(parts[:-1], parts[1:]):
            ring = np.column_stack([e[a:b], n[a:b]]) / CELL
            area = np.sum(ring[:-1, 0] * ring[1:, 1] - ring[1:, 0] * ring[:-1, 1])
            (outer if area < 0 else holes).append(ring)
        sea.append((outer, holes))
    for outer, holes in sea:
        for ring in outer:
            draw.polygon(list(map(tuple, ring)), fill=SEA)
        for ring in holes:
            draw.polygon(list(map(tuple, ring)), fill=0)
    least = np.array(list(LAKE_HA.values())) * 10_000
    for lake in sorted(lakes, key=lambda lake: lake.area):
        size = LAKE + int(np.searchsorted(least, lake.area, side="right")) - 1
        for outer, holes in rings(lake):
            draw.polygon(list(map(tuple, outer)), fill=size)
            for ring in holes:
                draw.polygon(list(map(tuple, ring)), fill=0)
    grid = np.lib.format.open_memmap(out_path + ".part", mode="w+", dtype=np.uint8, shape=(ROWS, COLS))
    band = 5000
    for r in range(0, ROWS, band):
        grid[r:r + band] = np.asarray(image.crop((0, r, COLS, min(r + band, ROWS))))
    grid.flush()
    del grid, image
    os.replace(out_path + ".part", out_path)
    print(f"    sea and {len(lakes)} lakes drawn into the grid in {time.time() - t:.0f}s")


def water_and_counties(pbf, sea_zip, water_path, counties_path):
    t = time.time()
    lakes, counties = areas(pbf)
    print(f"    {len(lakes)} lakes and {len(counties)} Scottish and Welsh counties read in {time.time() - t:.0f}s",
          flush=True)
    with open(counties_path + ".part", "w") as f:
        json.dump(counties, f)
    water_grid(sea_zip, lakes, water_path)
    os.replace(counties_path + ".part", counties_path)


def metres(ele):
    """An OpenStreetMap height, in whole metres, or None: as Hiking.java's metres() reads it."""
    m = HEIGHT.fullmatch(ele or "")
    if not m:
        return None
    return round(float(m.group(1)) * (0.3048 if m.group(2) not in (None, "m") else 1))


def peaks(pbf):
    """OpenStreetMap's named peaks, hills and volcanoes: (id, lon, lat, name, height as mapped or None)."""
    import osmium
    found = []
    fp = osmium.FileProcessor(pbf, osmium.osm.NODE).with_filter(
        osmium.filter.TagFilter(("natural", "peak"), ("natural", "hill"), ("natural", "volcano")))
    for node in fp:
        name = node.tags.get("name")
        if name and node.location.valid():
            found.append((node.id, node.location.lon, node.location.lat, name, metres(node.tags.get("ele"))))
    return found


def write(out, found, scored, rank, close, county, names, countries):
    """
    The shapefiles and jut.tsv: scored, (peak index, kind, base) for each
    score; rank and close, each one's (see ranks()).
    """
    to_wgs = pyproj.Transformer.from_crs(27700, 4326, always_xy=True)
    summits = shapefile.Writer(os.path.join(out, "summits"), shapeType=shapefile.POINT)
    lines = shapefile.Writer(os.path.join(out, "lines"), shapeType=shapefile.POLYLINE)
    ends = shapefile.Writer(os.path.join(out, "bases"), shapeType=shapefile.POINT)
    for w in (summits, lines, ends):
        w.field("kind", "C", size=8)
    summits.field("score", "N", size=6)
    summits.field("rank", "N", size=1)
    summits.field("close", "N", size=1)
    summits.field("name", "C", size=254)
    summits.field("ele", "N", size=5)
    summits.field("county", "C", size=100)
    summits.field("country", "C", size=8)
    for w in (lines, ends):
        w.field("peak_score", "N", size=6)
    rows = []
    for (i, kind, b), r, c in zip(scored, rank, close):
        osm_id, lon, lat, name, ele = found[i]
        s, j, steep, angle, h, d, qe, qn, zp = b
        blon, blat = to_wgs.transform(qe, qn)
        where = (names[county[i]], countries[county[i]]) if county[i] >= 0 else ("", "")
        summits.point(round(lon, 7), round(lat, 7))
        summits.record(kind, round(s), int(r), int(c), name, ele if ele is not None else round(zp), *where)
        lines.line([[[round(lon, 7), round(lat, 7)], [round(blon, 7), round(blat, 7)]]])
        lines.record(kind, round(s))
        ends.point(round(blon, 7), round(blat, 7))
        ends.record(kind, round(s))
        rows.append((kind, name, osm_id, *where, int(r), int(c), round(s), round(j),
                     round(steep, 1), round(angle, 1), round(h), round(d), round(zp), "" if ele is None else ele,
                     round(lat, 6), round(lon, 6), round(blat, 6), round(blon, 6)))
    for w in (summits, lines, ends):
        w.close()
    prj = pyproj.CRS.from_epsg(4326).to_wkt(pyproj.enums.WktVersion.WKT1_ESRI)
    for stem in ("summits", "lines", "bases"):
        with open(os.path.join(out, stem + ".prj"), "w") as f:
            f.write(prj)
        # Planetiler reads the names in the encoding this says, Latin-1 without it: Sgùrr, not SgÃ¹rr.
        with open(os.path.join(out, stem + ".cpg"), "w") as f:
            f.write("UTF-8")
    rows.sort(key=lambda row: (KINDS.index(row[0]), -row[7]))
    with open(os.path.join(out, "jut.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(("kind", "name", "osm_id", "county", "country", "rank", "close", "score", "jut", "steepness",
                    "angle", "height_m", "distance_m", "summit_m", "ele", "lat", "lon", "base_lat", "base_lon"))
        w.writerows(rows)
    return rows


def newer(path, *sources):
    return os.path.exists(path) and os.path.getmtime(path) > max(os.path.getmtime(s) for s in sources)


def main():
    pbf, sea_zip, regions_path, heights_path, walking, driving, out = sys.argv[1:8]
    os.makedirs(out, exist_ok=True)
    t = time.time()
    ways_path = os.path.join(out, "ways.npy")
    water_path = os.path.join(out, "water-sizes.npy")
    counties_path = os.path.join(out, "counties.json")
    if newer(ways_path, walking, driving):
        print("    have the paths and roads' grid")
    else:
        rasterize([walking, driving], ways_path)
    # The grid from before lakes were told apart by size, 2 GB of nothing now: water-sizes.npy is made in its
    # place, and the counties again with it, now with their countries.
    stale = os.path.join(out, "water.npy")
    if os.path.exists(stale):
        os.remove(stale)
    if newer(water_path, pbf, sea_zip) and newer(counties_path, pbf):
        print("    have the sea and lakes' grid, and the counties")
    else:
        # In a process of its own, so that what it takes (8 GB) is given
        # back when it ends, not carried into every worker below.
        with Pool(1) as one:
            one.apply(water_and_counties, (pbf, sea_zip, water_path, counties_path))
    found = peaks(pbf)
    print(f"    {len(found)} named peaks, read in {time.time() - t:.0f}s", flush=True)
    lon, lat = np.array([p[1] for p in found]), np.array([p[2] for p in found])
    e, n = pyproj.Transformer.from_crs(4326, 27700, always_xy=True).transform(lon, lat)
    # Neighbours together, so a worker's reads of the grids overlap.
    order = np.lexsort((e // REACH, n // REACH))
    results = [{}] * len(found)
    began = time.time()
    with Pool(initializer=opened, initargs=(heights_path, ways_path, water_path)) as pool:
        done = 0
        for i, r in zip(order, pool.imap(bases, [(e[i], n[i]) for i in order], chunksize=16)):
            results[i] = r
            done += 1
            if done % 2000 == 0:
                print(f"\r    {done} of {len(found)} peaks, {left(began, done, len(found))} ", end="", flush=True)
    print()
    county, names, countries = counties_of(lon, lat, regions_path, counties_path)
    scored, rank, close = [], [], []
    for kind in KINDS:
        have = [i for i, r in enumerate(results) if kind in r]
        if not have:
            continue
        idx = np.array(have, dtype=np.int64)
        scored += [(i, kind, results[i][kind]) for i in have]
        r, c = ranks(np.column_stack([e[idx], n[idx]]), np.array([results[i][kind][0] for i in have]), county[idx])
        rank += list(r)
        close += list(c)
    rows = write(out, found, scored, rank, close, county, names, countries)
    for kind in KINDS:
        best = [r for r in rows if r[0] == kind]
        print(f"jut: {len(best)} {kind} scores; highest: " + ", ".join(f"{r[1]} {r[7]}" for r in best[:4]))
    print(f"jut: done in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
