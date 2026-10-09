#!/usr/bin/env python3
"""
How much each way is walked, read from reference heat tiles, as lines for
the hiking tiles' "walked" layer.

    walked.py <area> <bounds> <graph.npz> <driving.npz> <parks.tsv> <references dir> <out dir>

A reference is one heatmap tile (Strava's: mapmapmap's
PYTHON/get-single-tile.py fetches one), kept in <references dir>
(data/src/walked) under the name that fetches it with, which says which
tile it is: "<anything>_z11_x1010_y658_lat53.9561_lon-2.4609.png". Its
palette index is the heat, 0 for none to 255 for the most walked. Those
over <bounds> (w,s,e,n) are read; an area with none gets no lines. (The
references are part of the pipeline's revision, pipeline/common.sh, so
adding one is a new version.)

From each, the heat is scaled up so a pixel is about PIXEL metres, and the
core of each line of it found: where it is at least half the heat round it
(so a busy line is found inside the faint haze round it). Then:

- Along the ways of the walking graph (graph.py: every way walkers may
  use), each pixel of core within REACH reference pixels of a way goes to
  the nearest point of the nearest way, which takes the busiest heat it is
  given; and so does any heat within OVER reference pixels of a way, core
  or not (in a town, where the streets are closer than the reference can
  tell apart, a busy one would otherwise leave the quieter one beside it
  none). The reference places a line only to within a pixel or so (22 m at
  zoom 11) and draws it 1 to 5 of them wide, so a line of heat along a way
  covers all of it. Along each way the heat is then made to run on: gaps
  of up to GAP filled; stretches shorter than half of SMOOTH left out
  (unless they are most of a short way) and
  the rest's heat a median over SMOOTH (so a busy line crossing a quiet way
  makes no busy patch on it, and the heat does not flicker); a stretch
  that comes within END of the way's end run on to it; and a way of up to
  SHORT between two walked ones (the heat at a junction having gone to
  the others), walked as the less walked of them.
- Off them, where people walk and the map has no way: the core further
  than REACH from every way, of at least OFF_HEAT (fainter, with no way
  under it, is the haze of people wandering a field, not a line), thinned
  to its middle, with spurs of up to
  SPUR taken off, and joined on to the nearest way where it ends near one.
  Each is smoothed over ROUND (it is traced through the pixels, a stair of
  them otherwise). Left out: pieces shorter than MIN_OFF, or than ALONE where they reach no
  way (a scrap of heat on its own); and lines within BESIDE of a way for
  most of their length (the same path, its heat drawn to one side of it).

A way is a road where cars use it: where it is one of the driving graph's
(graph.py build-driving: not a track, path, footway or driveway, nor a
way cars may not use).

And from the car parks (<parks.tsv>, pipeline/parking.py: those a walk
might start from, and of them those the app shows, within WALK_M of a path
a walk would use): each walked way's car park is the one a walker would
start from, as near as can be told: the one the least far along the ways
(within PARK_REACH, any way walkers may use, from the way it is nearest),
times a little more the fewer spaces it has (size_penalty()); so one with a
bigger one nearer is never it, and a lay-by a little nearer does not win
over a car park of fifty. And a round walk from it: a walk from the car
park and back, along walked ways (and any way within ACCESS of the car
park, to get on to them), that takes in the way, of up to LOOP_MOST, with at
most REPEAT of it walked twice (out and back the same way, from the car
park). Round walks are read off the shortest ways from the car park: each
walked way not on them closes one.

<out dir>/walked.shp (with .dbf, .shx, .prj), lines, each with:
  heat    how walked, 1 to 255 in half-octave steps (1, 1.4, 2, 2.8, 4,
          ... 181, 255, rounded), the reference's heat
  mapped  1 along a way of the walking graph, following it; 0 where the
          reference has a line and the map has no way, as the heat runs
  road    1 along a way cars use
  park_m  metres on foot from its car park, to the next 100 up; 0 for none
          within PARK_REACH (a line off the ways: from the way it joins)
  loop_m  the shortest round walk from its car park that takes it in, to
          the next 100 up; 0 for none (and for every line off the ways)
pipeline/hiking/Hiking.java puts them in its "walked" layer.
"""
import glob
import math
import os
import re
import sys
import time

import numpy as np
import pyproj
import shapefile
import shapely
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize

PIXEL = 2.0  # metres: the heat is read at about this
REACH = 1.5  # reference pixels: how far a line of heat may be from the way it is put on
MIN_REACH = 5.0  # metres: and at least this
OVER = 0.5  # reference pixels: heat this near a way is its own, the core of a line or not
STEP = 1.0  # metres between the points a way is read at
GAP = 50.0  # metres: a way's gap in its heat this long or less is filled
END = 60.0  # metres: and its heat run on to its end from this near
SMOOTH = 51.0  # metres: a way's heat is the median over this much of it
SHORT = 120.0  # metres: a way this long or less between two walked ones is walked
SPUR = 20.0  # metres: spurs off a line off the ways this long or less are taken off
MIN_OFF = 60.0  # metres: lines off the ways shorter than this are left out
ALONE = 150.0  # metres: and those reaching no way, shorter than this
OFF_HEAT = 2.0  # the least heat a line off the ways is made from
ROUND = 14.0  # metres: a line off the ways is the running mean of its points over this
BESIDE = 55.0  # metres: a line off the ways within this of one for most of its length is that way's heat
ROAD_NEAR = 3.0  # metres: a walking way this near a driving one, for most of its length, is a road
PARK_REACH = 5000.0  # metres: the furthest along the ways a car park is looked for (the most the app asks for)
LOOP_MOST = 5000.0  # metres: the longest round walk looked for
REPEAT = 0.4  # the most of a round walk walked twice
ACCESS = 300.0  # metres along any way from a car park to the walked ones, for a round walk
PARK_SNAP = 250.0  # metres: a car park further than this from every way is not one a walk starts from
WALK_M = 500  # metres: car parks further than this from a path a walk would use are not on the map (tools/styles.py)
WALKED_SHARE = 0.6  # a way walked for this much of it or more is walked, for round walks
EARTH = 40075016.686  # metres round the equator
NAME = re.compile(r"_z(\d+)_x(\d+)_y(\d+)_lat[-\d.]+_lon[-\d.]+\.png$")
NEIGHBOURS = ((0, 1), (1, 0), (1, 1), (1, -1))


def tile_to_lat_lon(x, y, zoom):
    """Latitude and longitude of points in tile coordinates (arrays; may be fractional)."""
    n = 2.0 ** zoom
    return np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * y / n)))), x / n * 360.0 - 180.0


def references(folder, bounds):
    """(path, z, x, y) of each reference over bounds (w, s, e, n)."""
    w, s, e, n = bounds
    out = []
    for path in sorted(glob.glob(os.path.join(folder, "*.png"))):
        m = NAME.search(os.path.basename(path))
        if not m:
            continue
        z, x, y = map(int, m.groups())
        north, west = tile_to_lat_lon(x, y, z)
        south, east = tile_to_lat_lon(x + 1, y + 1, z)
        if west < e and east > w and south < n and north > s:
            out.append((path, z, x, y))
    return out


def level(heat):
    """Heat in half-octave steps, 1 to 255; 0 stays 0."""
    with np.errstate(divide="ignore"):
        q = np.where(heat > 0, np.round(2 ** (np.round(np.log2(np.maximum(heat, 1)) * 2) / 2)), 0)
    return np.minimum(q, 255).astype(np.int32)


def edges_near(graph, west, south, east, north):
    """The graph's ways (edge numbers) reaching into the box."""
    lat, lon = graph["lat"], graph["lon"]
    inside = np.flatnonzero((lon >= west * 1e7) & (lon <= east * 1e7) & (lat >= south * 1e7) & (lat <= north * 1e7))
    return np.unique(np.searchsorted(graph["geom"], inside, side="right") - 1)


def ways_near(graph, west, south, east, north):
    """The graph's ways reaching into the box: each its edge number, ends (node numbers), longitudes and latitudes."""
    lat, lon, starts = graph["lat"], graph["lon"], graph["geom"]
    return [(int(e), int(graph["u"][e]), int(graph["v"][e]), lon[starts[e]:starts[e + 1]] / 1e7, lat[starts[e]:starts[e + 1]] / 1e7)
            for e in edges_near(graph, west, south, east, north)]


def size_penalty(spaces):
    """How much further a car park seems for its size: a lay-by of 4 half as far again, a car park of 100 a tenth."""
    return 1 + 1.5 / math.sqrt(spaces if spaces else 10)


def read_parks(path):
    """The car parks on the map a walk might start from: (lon, lat, spaces) each, spaces 0 for not known."""
    out = []
    with open(path) as f:
        for row in f:
            key, lon, lat, spaces, path_m = row.rstrip("\n").split("\t")
            if int(path_m) <= WALK_M:
                out.append((float(lon), float(lat), int(spaces) if spaces else 0))
    return out


def from_car_parks(walking, parks, box, ways, walked):
    """
    For each of [ways] (the tile's, as ways_near() gives them, each with the
    share of it [walked]): its car park's metres, and the shortest round walk
    from that car park taking it in, both by way index, for those that have
    them. See the module's notes.
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra
    west, south, east, north = box
    k = math.cos(math.radians((north + south) / 2))
    pad_lat, pad_lon = PARK_REACH / 110_574, PARK_REACH / (111_320 * k)
    big = edges_near(walking, west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat)
    nodes, local = np.unique(np.r_[walking["u"][big], walking["v"][big]], return_inverse=True)
    a, b, length = local[:len(big)], local[len(big):], np.maximum(walking["length"][big].astype(float), 0.01)

    def graph(edges):
        """The ways [edges] (indices into big) as a graph of their nodes, the shortest of any two between the same ends;
        and which edge joins any two nodes."""
        order = edges[np.argsort(length[edges], kind="stable")]
        pair = {}
        for i in order:
            if a[i] != b[i]:
                pair.setdefault((min(a[i], b[i]), max(a[i], b[i])), i)
        keep = np.array(list(pair.values()), dtype=int)
        m = csr_matrix((np.r_[length[keep], length[keep]], (np.r_[a[keep], b[keep]], np.r_[b[keep], a[keep]])),
                       shape=(len(nodes), len(nodes)))
        return m, pair

    # The car parks, each on the way's end nearest it.
    x = lambda lon, lat: np.column_stack([np.asarray(lon) * 111_320 * k, np.asarray(lat) * 110_574])
    near = cKDTree(x(walking["node_lon"][nodes] / 1e7, walking["node_lat"][nodes] / 1e7))
    inside = [p for p in parks if west - pad_lon <= p[0] <= east + pad_lon and south - pad_lat <= p[1] <= north + pad_lat]
    park_node, park_access, penalty = [], [], []
    for lon, lat, spaces in inside:
        d, j = near.query(x(lon, lat)[0])
        if d <= PARK_SNAP:
            park_node.append(j)
            park_access.append(d)
            penalty.append(size_penalty(spaces))
    park_m, loop_m = {}, {}
    if not park_node:
        return park_m, loop_m
    access, penalty = np.array(park_access), np.array(penalty)
    everything, _ = graph(np.arange(len(big)))
    far = dijkstra(everything, indices=park_node, limit=PARK_REACH)

    # Each way's car park: the least far, for its size.
    where = {e: i for i, e in enumerate(big)}
    index = [where[w[0]] for w in ways]
    ref = {}
    for i, e in enumerate(index):
        if walked[i] <= 0:
            continue
        d = access + np.minimum(far[:, a[e]], far[:, b[e]])
        c = int(np.argmin(d * penalty))
        if d[c] <= PARK_REACH:
            ref[i] = c
            park_m[i] = d[c]

    # Round walks from each of those car parks.
    on = {e for i, e in enumerate(index) if walked[i] >= WALKED_SHARE}
    for c in sorted(set(ref.values())):
        near_park = np.flatnonzero(np.maximum(far[c, a], far[c, b]) + access[c] <= ACCESS)
        edges = np.array(sorted(on | set(near_park.tolist())), dtype=int)
        m, pair = graph(edges)
        d, pred = dijkstra(m, indices=park_node[c], return_predecessors=True, limit=LOOP_MOST / 2)
        tree = {pair[(min(n, p), max(n, p))] for n, p in enumerate(pred) if p >= 0}
        best = {}
        for e in edges:
            if e in tree or a[e] == b[e] or not (np.isfinite(d[a[e]]) and np.isfinite(d[b[e]])):
                continue
            walk = d[a[e]] + length[e] + d[b[e]] + 2 * access[c]
            if walk > LOOP_MOST:
                continue
            up, n = set(), a[e]
            while n >= 0:
                up.add(n)
                n = pred[n]
            meet = b[e]
            while meet not in up:
                meet = pred[meet]
            if 2 * (d[meet] + access[c]) > REPEAT * walk:
                continue
            loop = [e]
            for n in (a[e], b[e]):
                while n != meet:
                    loop.append(pair[(min(n, pred[n]), max(n, pred[n]))])
                    n = pred[n]
            for f in loop:
                best[f] = min(best.get(f, np.inf), walk)
        for i, e in enumerate(index):
            if ref.get(i) == c and e in best:
                loop_m[i] = best[e]
    return park_m, loop_m


def along(line, step):
    """Points every [step] (or a little less) along a line, its ends included."""
    n = max(2, math.ceil(shapely.length(line) / step) + 1)
    return shapely.get_coordinates(shapely.line_interpolate_point(line, np.linspace(0, 1, n), normalized=True))


def runs(values):
    """(start, end) of each stretch of equal, non-zero values."""
    cut = np.flatnonzero(np.diff(values)) + 1
    return [(a, b) for a, b in zip(np.r_[0, cut], np.r_[cut, len(values)]) if values[a]]


def tile_lines(path, z, x, y, walking, driving, parks):
    """A reference's lines: (longitudes and latitudes, heat, mapped, road, park_m, loop_m) each."""
    image = Image.open(path)
    if image.mode != "P":
        raise SystemExit(f"{path}: not a heat tile as fetched (its palette index is the heat)")
    heat = np.array(image).astype(np.float32)
    size = heat.shape[1]
    lat0, _ = tile_to_lat_lon(x + 0.5, y + 0.5, z)
    m_per_ref_px = EARTH * math.cos(math.radians(lat0)) / 2 ** z / size
    up = max(1, round(m_per_ref_px / PIXEL))
    mpp = m_per_ref_px / up  # metres a pixel of the scaled-up heat, the frame everything here is in
    reach = max(MIN_REACH, REACH * m_per_ref_px) / mpp
    fine = ndimage.zoom(heat, up, order=1, grid_mode=True, mode="nearest")
    core = (fine >= 0.5) & (fine >= 0.5 * ndimage.maximum_filter(fine, size=2 * up + 1))

    def px(lon, lat):
        n = 2.0 ** z
        tx = (np.asarray(lon) + 180.0) / 360.0 * n
        ty = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n
        return np.column_stack([(tx - x) * size * up, (ty - y) * size * up])

    def degrees(p):
        la, lo = tile_to_lat_lon(x + p[:, 0] / (size * up), y + p[:, 1] / (size * up), z)
        return np.column_stack([lo, la])

    north, west = tile_to_lat_lon(x, y, z)
    south, east = tile_to_lat_lon(x + 1, y + 1, z)
    pad = (reach * mpp + END) / 111_000
    box = (west - pad * 2, south - pad, east + pad * 2, north + pad)

    # The ways, read every STEP metres; the driving graph's, to tell the roads.
    ways = ways_near(walking, *box)
    lines = [shapely.linestrings(px(lo, la)) for _, _, _, lo, la in ways]
    points = [along(line, STEP / mpp) for line in lines]
    owner = np.repeat(np.arange(len(ways)), [len(p) for p in points])
    points = np.concatenate(points) if points else np.zeros((0, 2))
    roads = [along(shapely.linestrings(px(lo, la)), 2 * STEP / mpp) for _, _, _, lo, la in ways_near(driving, *box)]
    road = np.zeros(len(ways), bool)
    if roads and len(points):
        near_road = cKDTree(np.concatenate(roads)).query(points, distance_upper_bound=ROAD_NEAR / mpp)[0] < np.inf
        road = np.bincount(owner, near_road, len(ways)) / np.bincount(owner, minlength=len(ways)) >= 0.6

    # The core's pixels, each to the nearest point of a way within reach.
    rows, cols = np.nonzero(core)
    taken = np.zeros(len(points), np.float32)
    if len(points):
        tree = cKDTree(points)
        distance, nearest = tree.query(np.column_stack([cols + 0.5, rows + 0.5]), distance_upper_bound=reach, workers=-1)
        ok = np.isfinite(distance)
        np.maximum.at(taken, nearest[ok], fine[rows[ok], cols[ok]])
        rows, cols = np.nonzero((fine >= 0.5) & ~core)
        distance, nearest = tree.query(np.column_stack([cols + 0.5, rows + 0.5]), distance_upper_bound=OVER * up, workers=-1)
        ok = np.isfinite(distance)
        np.maximum.at(taken, nearest[ok], fine[rows[ok], cols[ok]])

    # Along each way: gaps filled, short stretches out, the heat smoothed, run on to its ends.
    gap, end, smooth = round(GAP / STEP), round(END / STEP), round(SMOOTH / STEP) | 1
    starts = np.searchsorted(owner, np.arange(len(ways) + 1))
    heats = []
    for i in range(len(ways)):
        h = taken[starts[i]:starts[i + 1]]
        if h.any():
            # Its ends as they are: padded with nothing, a closing would wear them away.
            h = ndimage.grey_closing(h, size=gap, mode="nearest")
            lit = h > 0
            out = np.zeros_like(h)
            edges = np.flatnonzero(np.diff(np.r_[0, lit.astype(np.int8), 0]))
            for a, b in zip(edges[::2], edges[1::2]):
                if b - a >= smooth // 2 or b - a >= 0.8 * len(h):
                    out[a:b] = ndimage.median_filter(h[a:b], size=min(smooth, (b - a) | 1), mode="mirror")
            h = out
            lit = np.flatnonzero(h)
            if lit.size:
                if lit[0] <= end:
                    h[:lit[0]] = h[lit[0]]
                if len(h) - 1 - lit[-1] <= end:
                    h[lit[-1] + 1:] = h[lit[-1]]
        heats.append(h)
    # A short way between walked ones: the heat at the junction went to them.
    ends = {}
    for (_, u, v, _, _), h in zip(ways, heats):
        for node, value in ((u, h[0]), (v, h[-1])):
            if value:
                ends[node] = max(ends.get(node, 0), value)
    for i, (_, u, v, _, _) in enumerate(ways):
        if not heats[i].any() and u in ends and v in ends and shapely.length(lines[i]) * mpp <= SHORT:
            heats[i][:] = min(ends[u], ends[v])

    # From the car parks: how far, and the shortest round walk.
    walked = [float(np.mean(h > 0)) for h in heats]
    park_m, loop_m = from_car_parks(walking, parks, box, ways, walked)

    def hundreds(m):
        return int(math.ceil(m / 100) * 100) if m is not None else 0

    out = []  # in pixels, until the end
    for i, h in enumerate(heats):
        q = level(h)
        at = points[starts[i]:starts[i + 1]]
        for a, b in runs(q):
            # On to the next stretch's first point, so that they meet.
            stretch = at[a:min(b + 1, len(at))]
            if len(stretch) >= 2:
                out.append((shapely.simplify(shapely.linestrings(stretch), 0.5 / mpp), int(q[a]), 1, int(road[i]),
                            hundreds(park_m.get(i)), hundreds(loop_m.get(i))))
    along_ways = len(out)

    # Off the ways: the core beyond reach of them, thinned, spurs off, short pieces out.
    reached = np.zeros(core.shape, bool)
    if len(points):
        cells = np.floor(points).astype(int)
        cells = cells[(cells[:, 0] >= 0) & (cells[:, 1] >= 0) & (cells[:, 0] < core.shape[1]) & (cells[:, 1] < core.shape[0])]
        reached[cells[:, 1], cells[:, 0]] = True
    away = ndimage.distance_transform_edt(~reached) if reached.any() else np.full(core.shape, np.inf)
    off = skeletonize(core & (away > reach) & (fine >= OFF_HEAT))
    ring = np.ones((3, 3), int)
    ring[1, 1] = 0
    for _ in range(round(SPUR / mpp)):
        off &= ~(off & (ndimage.convolve(off.astype(int), ring, mode="constant") <= 1))
    join = reach + (SPUR + 10) / mpp  # an end this near a way is joined on to it
    pieces, n = ndimage.label(off, structure=np.ones((3, 3)))
    length = np.bincount(pieces.ravel(), minlength=n + 1) * mpp
    joined = np.zeros(n + 1, bool)
    joined[np.unique(pieces[off & (away <= join)])] = True
    keep = (length >= MIN_OFF) & (joined | (length >= ALONE))
    keep[0] = False
    off &= keep[pieces]
    rows, cols = np.nonzero(off)
    index = {(r, c): k for k, (r, c) in enumerate(zip(rows, cols))}
    pairs = []
    for k, (r, c) in enumerate(zip(rows, cols)):
        for dr, dc in NEIGHBOURS:
            j = index.get((r + dr, c + dc))
            # A diagonal step is left out where the way round its corner is there already.
            if j is not None and not (dr and dc and ((r, c + dc) in index or (r + dr, c) in index)):
                pairs.append((k, j))
    if pairs:
        pairs = np.array(pairs)
        centres = np.column_stack([cols + 0.5, rows + 0.5])
        merged = shapely.line_merge(shapely.multilinestrings(
            shapely.linestrings(centres[pairs.ravel()], indices=np.repeat(np.arange(len(pairs)), 2))))
        for line in shapely.get_parts(merged):
            p = shapely.get_coordinates(line)
            r, c = np.floor(p[:, 1]).astype(int), np.floor(p[:, 0]).astype(int)
            if np.mean(away[r, c] * mpp <= BESIDE) >= 0.8:
                continue
            h = ndimage.median_filter(fine[r, c], size=min(round(SMOOTH / 2 / PIXEL) | 1, len(p) | 1), mode="mirror")
            q = level(h)
            # Round, its ends where they were, for the joins.
            p[1:-1] = ndimage.uniform_filter1d(p, size=min(round(ROUND / mpp) | 1, len(p) | 1), axis=0, mode="nearest")[1:-1]
            # Joined on to a way it ends near: as far from a car park as the nearer of them.
            joins, metres = [None, None], []
            for e, end in enumerate((0, -1)):
                if away[r[end], c[end]] <= join:
                    k = tree.query(p[end])[1]
                    joins[e] = points[k]
                    if owner[k] in park_m:
                        metres.append(park_m[owner[k]])
            far = hundreds(min(metres)) if metres else 0
            stretches = runs(q)
            for k, (a, b) in enumerate(stretches):
                stretch = p[a:min(b + 1, len(p))]
                if k == 0 and joins[0] is not None:
                    stretch = np.vstack([joins[0], stretch])
                if k == len(stretches) - 1 and joins[1] is not None:
                    stretch = np.vstack([stretch, joins[1]])
                if len(stretch) >= 2:
                    out.append((shapely.simplify(shapely.linestrings(stretch), 0.25), int(q[a]), 0, 0, far, 0))

    print(f"    {os.path.basename(path)}: {along_ways} lines along ways ({int(road.sum())} of {len(ways)} ways near it roads), "
          f"{len(out) - along_ways} off them; {len(park_m)} walked ways within {PARK_REACH / 1000:.0f} km of a car park, "
          f"{len(loop_m)} on a round walk from it of {LOOP_MOST / 1000:.0f} km or less", flush=True)
    return [(degrees(shapely.get_coordinates(line)), *rest) for line, *rest in out]


def main():
    area, bounds, walking_path, driving_path, parks_path, folder, out = sys.argv[1:8]
    t = time.time()
    os.makedirs(out, exist_ok=True)
    for old in glob.glob(os.path.join(out, "walked.*")):
        os.remove(old)
    found = references(folder, [float(v) for v in bounds.split(",")])
    if not found:
        print(f"    no references over {area} in {folder}: no lines")
        return
    keys = ("lat", "lon", "geom", "u", "v")
    with np.load(walking_path) as w, np.load(driving_path) as d:
        walking = {k: w[k] for k in keys + ("length", "node_lat", "node_lon")}
        driving = {k: d[k] for k in keys}
    parks = read_parks(parks_path)
    lines = []
    for path, z, x, y in found:
        lines += tile_lines(path, z, x, y, walking, driving, parks)
    w = shapefile.Writer(os.path.join(out, "walked"), shapeType=shapefile.POLYLINE)
    w.field("heat", "N", size=3)
    w.field("mapped", "N", size=1)
    w.field("road", "N", size=1)
    w.field("park_m", "N", size=5)
    w.field("loop_m", "N", size=5)
    for xy, *record in lines:
        w.line([np.round(xy, 7).tolist()])
        w.record(*record)
    w.close()
    with open(os.path.join(out, "walked.prj"), "w") as f:
        f.write(pyproj.CRS.from_epsg(4326).to_wkt(pyproj.enums.WktVersion.WKT1_ESRI))
    print(f"    {len(lines)} lines from {len(found)} references in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
