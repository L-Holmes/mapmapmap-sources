#!/usr/bin/env python3
"""
How much each way is walked, read from Strava's heatmap, as lines for the
hiking tiles' "walked" layer. The method, end to end, is mapmapmap's
STRAVA_METHOD.md.

    walked.py <area> <bounds> <graph.npz> <driving.npz> <parks.tsv> <strava.mbtiles> <work dir>

<strava.mbtiles> (data/src/walked/strava-run.mbtiles; mapmapmap's
PYTHON/fetch-strava.py fetches it): Strava's run heatmap, zoom 11 tiles,
each a paletted PNG whose palette index is the heat, 0 for none to 255 for
the most walked. The tiles over <bounds> (w,s,e,n) are read; an area with
none gets no lines. (The file is part of the pipeline's revision,
pipeline/common.sh, so new tiles are a new version.)

Two passes. First each tile, WORKERS at a time, with MARGIN reference
pixels of its neighbours' heat round it so that nothing stops at its edge,
keeping only what is inside it. The heat is scaled up so a pixel is about
PIXEL metres, and the core of each line of it found: where it is at least
half the heat round it (so a busy line is found inside the faint haze round
it). Then:

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
  (unless they are most of a short way) and the rest's heat a median over
  SMOOTH (so a busy line crossing a quiet way makes no busy patch on it,
  and the heat does not flicker); a stretch that comes within END of the
  way's end run on to it; and a way of up to SHORT between two walked ones
  (the heat at a junction having gone to the others), walked as the less
  walked of them.
- Off them, where people walk and the map has no way: the core further
  than REACH from every way, of at least OFF_HEAT (fainter, with no way
  under it, is the haze of people wandering a field, not a line), thinned
  to its middle, with spurs of up to SPUR taken off, and joined on to the
  nearest way where it ends near one. Each is smoothed over ROUND (it is
  traced through the pixels, a stair of them otherwise). Left out: pieces
  shorter than MIN_OFF, or than ALONE where they reach no way (a scrap of
  heat on its own); and lines within BESIDE of a way for most of their
  length (the same path, its heat drawn to one side of it).
- A way is a road where cars use it: where it is one of the driving
  graph's (graph.py build-driving: not a track, path, footway or driveway,
  nor a way cars may not use).

Then the whole area at once, from the car parks (<parks.tsv>,
pipeline/parking.py: those a walk might start from, and of them those the
app shows, within WALK_M of a path a walk would use). Each walked way's car
park is the one a walker would start from, as near as can be told: the
nearest along the ways (any way walkers may use, within PARK_REACH),
counting a small one as further than it is, SIZE_EXTRA over the square root
of its spaces (size_extra()); so one with a bigger one nearer is never it,
and a lay-by a little nearer does not win over a car park of fifty. And
round walks from it: walks from the car park and back, along walked ways
(WALKED_SHARE or more of them walked; and any way within ACCESS of a car
park, to get on to them), of up to LOOP_MOST, with at most REPEAT of it
walked twice (out and back the same way, from the car park). They are read
off the shortest ways from the car park: each walked way not on them closes
one. For each walked way, the shortest that takes it in; the shortest of
those coming back along popular ways (of POPULAR heat or more, those drawn
with a lighter middle: every way of the walk but those on its way out to
this one, and those within ACCESS of a car park); the shortest of which
OFF_ROAD or more is off the roads; and the shortest that is both.

<work dir>/walked.gpkg: a GeoPackage (SQLite), table "walked", the lines,
each with:
  heat        how walked, 1 to 255 in half-octave steps (1, 1.4, 2, 2.8, 4,
              ... 181, 255, rounded), the reference's heat
  mapped      1 along a way of the walking graph, following it; 0 where the
              reference has a line and the map has no way, as the heat runs
  road        1 along a way cars use
  park_m      metres on foot from its car park, to the next PARK_STEP up (the
              app asks for whole kilometres); 0 for none within PARK_REACH (a
              line off the ways: from the way it joins)
  loop_m      the shortest round walk from its car park that takes it in, to
              the next LOOP_STEP up (the app asks for 4, 6, 8 or 10 km); 0 for
              none (and for every line off the ways). So stepped, lines along
              one way mostly have the same, and are one line in the tiles.
  loop_pop_m  the same, coming back along popular ways
  loop_off_m  the same, OFF_ROAD or more of it off the roads
  loop_po_m   the same, both
  tile_x, tile_y  the zoom 11 tile it was read from
<work dir>/walked-tiles.sqlite: each tile's lines as made, by what they
were made from (its and its neighbours' heat, the graphs, this script), so
that a build that stops carries on from where it was.
pipeline/hiking/Hiking.java reads walked.gpkg into its "walked" layer.
"""
import glob
import hashlib
import io
import math
import os
import pickle
import sqlite3
import struct
import sys
import time
import zlib

import numpy as np
import pyproj
import shapely
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize

sys.path.insert(0, os.path.dirname(__file__))
from progress import left  # noqa: E402

Z = 11  # the reference tiles' zoom
SIZE = 512  # their pixels across
MARGIN = 32  # reference pixels of the neighbours' heat read round a tile (~720 m at zoom 11)
PIXEL = 4.0  # metres: the heat is read at about this (finer is no truer, the reference being 22 m, and much slower)
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
SIZE_EXTRA = 1500.0  # metres: a car park counts as this over the square root of its spaces further than it is
LOOP_MOST = 10000.0  # metres: the longest round walk looked for (the most the app asks for)
PARK_STEP = 1000  # metres: a car park's distance, to the next of these up (Walked.NEAR, the app's)
LOOP_STEP = 2000  # metres: a round walk's, likewise (Walked.LOOPS)
POPULAR = 128  # the least heat of a popular way, for coming back along them: drawn with a lighter middle (tools/styles.py)
OFF_ROAD = 0.8  # the least of a round walk off the roads, for those that keep off them
REPEAT = 0.4  # the most of a round walk walked twice
ACCESS = 300.0  # metres along any way from a car park to the walked ones, for a round walk
PARK_SNAP = 250.0  # metres: a car park further than this from every way is not one a walk starts from
WALK_M = 500  # metres: car parks further than this from a path a walk would use are not on the map (tools/styles.py)
WALKED_SHARE = 0.6  # a way walked for this much of it or more is walked, for round walks
CELL = (0.2, 0.3)  # degrees of latitude and longitude: round walks are looked for this much of the area at a time
WORKER_GB = 1.6  # memory a tile takes to read, for how many are read at once
EARTH = 40075016.686  # metres round the equator
NEIGHBOURS = ((0, 1), (1, 0), (1, 1), (1, -1))
LOOPS = ("loop_m", "loop_pop_m", "loop_off_m", "loop_po_m")  # any round walk; popular back; off road; both
FIELDS = ("heat", "mapped", "road", "park_m") + LOOPS + ("tile_x", "tile_y")

# Read by the tile workers, set before they start (they share them, forked).
WALKING = DRIVING = STORE = None


def tile_to_lat_lon(x, y, zoom):
    """Latitude and longitude of points in tile coordinates (arrays; may be fractional)."""
    n = 2.0 ** zoom
    return np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * y / n)))), x / n * 360.0 - 180.0


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


def runs(values):
    """(start, end) of each stretch of equal, non-zero values."""
    cut = np.flatnonzero(np.diff(values)) + 1
    return [(a, b) for a, b in zip(np.r_[0, cut], np.r_[cut, len(values)]) if values[a]]


# --- The reference tiles ------------------------------------------------------

def open_store(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def tiles_over(store, bounds):
    """(x, y) of each reference tile over bounds (w, s, e, n)."""
    w, s, e, n = bounds
    out = []
    for x, row in store.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (Z,)):
        y = 2 ** Z - 1 - row
        north, west = tile_to_lat_lon(x, y, Z)
        south, east = tile_to_lat_lon(x + 1, y + 1, Z)
        if west < e and east > w and south < n and north > s:
            out.append((x, y))
    return sorted(out)


def png(store, x, y):
    row = store.execute("SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                        (Z, x, 2 ** Z - 1 - y)).fetchone()
    return row[0] if row else None


def heat_of(data):
    image = Image.open(io.BytesIO(data))
    if image.mode != "P":
        raise SystemExit("A reference tile is not as fetched: its palette index is not the heat")
    return np.array(image).astype(np.float32)


def mosaic(store, x, y):
    """The tile's heat with MARGIN pixels of its neighbours' round it (none where there is no neighbour)."""
    out = np.zeros((SIZE + 2 * MARGIN, SIZE + 2 * MARGIN), np.float32)
    parts = {-1: (SIZE - MARGIN, SIZE, 0), 0: (0, SIZE, MARGIN), 1: (0, MARGIN, MARGIN + SIZE)}
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            data = png(store, x + dx, y + dy)
            if data is None:
                continue
            ry0, ry1, oy = parts[dy]
            rx0, rx1, ox = parts[dx]
            out[oy:oy + ry1 - ry0, ox:ox + rx1 - rx0] = heat_of(data)[ry0:ry1, rx0:rx1]
    return out


def made_from(store, x, y, stamp):
    """What a tile's lines are made from, as one key: its and its neighbours' heat, the graphs, this script."""
    h = hashlib.sha1(stamp.encode())
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            h.update(png(store, x + dx, y + dy) or b"-")
    return h.hexdigest()


# --- Each tile ----------------------------------------------------------------

def tile(xy):
    """
    One reference tile's lines, inside it: (x, y, result), result being
    {"runs": [(edge, heat, wkb)], "ways": (edges, points, heated, popular, road),
    "off": [(heat, wkb, joined edges)]}, wkb in longitude and latitude; the
    ways' counts are of their points inside the tile.
    """
    global STORE_DB
    if "STORE_DB" not in globals():
        STORE_DB = open_store(STORE)
    x, y = xy
    walking, driving = WALKING, DRIVING
    heat = mosaic(STORE_DB, x, y)
    lat0, _ = tile_to_lat_lon(x + 0.5, y + 0.5, Z)
    m_per_ref_px = EARTH * math.cos(math.radians(lat0)) / 2 ** Z / SIZE
    up = max(1, round(m_per_ref_px / PIXEL))
    mpp = m_per_ref_px / up  # metres a pixel of the scaled-up heat, the frame everything here is in
    reach = max(MIN_REACH, REACH * m_per_ref_px) / mpp
    fine = ndimage.zoom(heat, up, order=1, grid_mode=True, mode="nearest")
    core = (fine >= 0.5) & (fine >= 0.5 * ndimage.maximum_filter(fine, size=2 * up + 1))
    inner = (MARGIN * up, MARGIN * up, (MARGIN + SIZE) * up, (MARGIN + SIZE) * up)  # the tile itself, in pixels

    def px(lon, lat):
        n = 2.0 ** Z
        tx = (np.asarray(lon) + 180.0) / 360.0 * n
        ty = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n
        return np.column_stack([((tx - x) * SIZE + MARGIN) * up, ((ty - y) * SIZE + MARGIN) * up])

    def inside(lines):
        """Lines in pixels: their parts inside the tile, as WKB in longitude and latitude, and which line each is of."""
        if not lines:
            return [], np.zeros(0, int)
        lines = np.asarray(lines, dtype=object)
        bounds = shapely.bounds(lines)
        crosses = (bounds[:, 0] < inner[0]) | (bounds[:, 1] < inner[1]) | (bounds[:, 2] > inner[2]) | (bounds[:, 3] > inner[3])
        lines[crosses] = shapely.clip_by_rect(lines[crosses], *inner)
        parts, which = shapely.get_parts(lines, return_index=True)
        good = (shapely.get_type_id(parts) == 1) & (shapely.get_num_coordinates(parts) >= 2)
        parts, which = parts[good], which[good]

        def to_degrees(p):
            la, lo = tile_to_lat_lon(x + (p[:, 0] / up - MARGIN) / SIZE, y + (p[:, 1] / up - MARGIN) / SIZE, Z)
            return np.column_stack([lo, la])
        return list(shapely.to_wkb(shapely.transform(parts, to_degrees))), which

    m = MARGIN / SIZE
    north, west = tile_to_lat_lon(x - m, y - m, Z)
    south, east = tile_to_lat_lon(x + 1 + m, y + 1 + m, Z)
    pad = (reach * mpp + END) / 111_000
    box = (west - pad * 2, south - pad, east + pad * 2, north + pad)

    def read_along(found, step):
        """Ways as ways_near() gives them: as lines in pixels, their lengths, and points every [step] along them, with
        which way each point is of."""
        if not found:
            return np.zeros(0, dtype=object), np.zeros(0), np.zeros((0, 2)), np.zeros(0, int)
        counts = np.array([len(w[3]) for w in found])
        xy = px(np.concatenate([w[3] for w in found]), np.concatenate([w[4] for w in found]))
        lines_ = shapely.linestrings(xy, indices=np.repeat(np.arange(len(found)), counts))
        lengths_ = shapely.length(lines_)
        n_ = np.maximum(2, np.ceil(lengths_ / step).astype(int) + 1)
        owner_ = np.repeat(np.arange(len(found)), n_)
        frac = (np.arange(n_.sum()) - np.repeat(np.cumsum(n_) - n_, n_)) / np.repeat(n_ - 1, n_)
        return lines_, lengths_, shapely.get_coordinates(shapely.line_interpolate_point(lines_[owner_], frac, normalized=True)), owner_

    # The ways, read every STEP metres; the driving graph's, to tell the roads.
    ways = ways_near(walking, *box)
    lines, lengths, points, owner = read_along(ways, STEP / mpp)
    road = np.zeros(len(ways), bool)
    roads = read_along(ways_near(driving, *box), 2 * STEP / mpp)[2]
    if len(roads) and len(points):
        near_road = cKDTree(roads).query(points, distance_upper_bound=ROAD_NEAR / mpp)[0] < np.inf
        road = np.bincount(owner, near_road, len(ways)) / np.bincount(owner, minlength=len(ways)) >= 0.6

    # How far from the ways, in pixels, read on a grid COARSE times coarser (near enough for distances of REACH).
    coarse = 4
    reached = np.zeros((core.shape[0] // coarse + 1, core.shape[1] // coarse + 1), bool)
    if len(points):
        cells = np.floor(points / coarse).astype(int)
        cells = cells[(cells[:, 0] >= 0) & (cells[:, 1] >= 0) & (cells[:, 0] < reached.shape[1]) & (cells[:, 1] < reached.shape[0])]
        reached[cells[:, 1], cells[:, 0]] = True
    far_small = ndimage.distance_transform_edt(~reached).astype(np.float32) * coarse if reached.any() \
        else np.full(reached.shape, np.inf, np.float32)

    def away(r, c):
        return far_small[r // coarse, c // coarse]

    # The core's pixels, each to the nearest point of a way within reach; and any heat near enough a way.
    taken = np.zeros(len(points), np.float32)
    tree = None
    if len(points):
        tree = cKDTree(points)
        for mask, within_ in ((core, reach), ((fine >= 0.5) & ~core, OVER * up)):
            rows, cols = np.nonzero(mask)
            near_ = away(rows, cols) <= within_ + 2 * coarse
            rows, cols = rows[near_], cols[near_]
            distance, nearest = tree.query(np.column_stack([cols + 0.5, rows + 0.5]), distance_upper_bound=within_)
            ok = np.isfinite(distance)
            # The most heat each point is given: sorted by point, the greatest of each.
            who, value = nearest[ok], fine[rows[ok], cols[ok]]
            if len(who):
                order = np.argsort(who, kind="stable")
                who, value = who[order], value[order]
                first = np.r_[0, np.flatnonzero(np.diff(who)) + 1]
                taken[who[first]] = np.maximum(taken[who[first]], np.maximum.reduceat(value, first))

    # Along each way: gaps filled, short stretches out, the heat smoothed, run on to its ends.
    gap, end, smooth = round(GAP / STEP), round(END / STEP), round(SMOOTH / STEP) | 1
    starts = np.searchsorted(owner, np.arange(len(ways) + 1))
    heats = []
    for i in range(len(ways)):
        h = taken[starts[i]:starts[i + 1]].copy()
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
        if not heats[i].any() and u in ends and v in ends and lengths[i] * mpp <= SHORT:
            heats[i][:] = min(ends[u], ends[v])

    # What of each way is inside the tile, walked, and popular: the area's pass adds the tiles up.
    within = ((points[:, 0] >= inner[0]) & (points[:, 0] < inner[2]) & (points[:, 1] >= inner[1]) & (points[:, 1] < inner[3])) \
        if len(points) else np.zeros(0, bool)
    flat = np.concatenate(heats) if heats else np.zeros(0)
    steps = level(flat)
    count = lambda mask: np.bincount(owner, mask & within, len(ways)).astype(np.int32)
    seen = count(np.ones(len(flat), bool))
    keep = np.flatnonzero(seen > 0)
    result = {
        "ways": (np.array([w[0] for w in ways], np.int64)[keep], seen[keep], count(flat > 0)[keep],
                 count(steps >= POPULAR)[keep], road[keep]),
        "runs": [],
        "off": [],
    }
    # Each run of one step along a way, a line: on to the next run's first point, so that they meet.
    if len(steps):
        change = np.r_[True, (steps[1:] != steps[:-1]) | (owner[1:] != owner[:-1])]
        first = np.flatnonzero(change)
        last = np.r_[first[1:], len(steps)]  # one past the end
        last = np.where((last < len(steps)) & (owner[np.minimum(last, len(steps) - 1)] == owner[first]), last + 1, last)
        good = (steps[first] > 0) & (last - first >= 2)
        first, last = first[good], last[good]
        n_ = last - first
        idx = np.repeat(first, n_) + (np.arange(n_.sum()) - np.repeat(np.cumsum(n_) - n_, n_))
        made = shapely.simplify(shapely.linestrings(points[idx], indices=np.repeat(np.arange(len(first)), n_)), 0.5 / mpp)
        edge_ids = np.array([w[0] for w in ways], np.int64)
        parts, which = inside(list(made))
        result["runs"] = [(int(edge_ids[owner[first[j]]]), int(steps[first[j]]), part) for part, j in zip(parts, which)]

    # Off the ways: the core beyond reach of them, thinned, spurs off, short pieces out.
    rows, cols = np.nonzero(core & (fine >= OFF_HEAT))
    beyond = away(rows, cols) > reach
    candidates = np.zeros(core.shape, bool)
    candidates[rows[beyond], cols[beyond]] = True
    off = skeletonize(candidates)
    del candidates
    # Spurs off: the ends taken off, a pixel a time, SPUR's worth.
    rows, cols = np.nonzero(off)
    padded = np.pad(off, 1)
    rows, cols = rows + 1, cols + 1
    around = [(dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc]
    for _ in range(round(SPUR / mpp)):
        count = sum(padded[rows + dr, cols + dc].astype(np.uint8) for dr, dc in around)
        end_ = count <= 1
        if not end_.any():
            break
        padded[rows[end_], cols[end_]] = False
        rows, cols = rows[~end_], cols[~end_]
    off = padded[1:-1, 1:-1]
    rows, cols = rows - 1, cols - 1
    join = reach + (SPUR + 10) / mpp  # an end this near a way is joined on to it
    pieces, n = ndimage.label(off, structure=np.ones((3, 3)))
    label = pieces[rows, cols]
    length = np.bincount(label, minlength=n + 1) * mpp
    joined = np.zeros(n + 1, bool)
    joined[np.unique(label[away(rows, cols) <= join])] = True
    keep = (length >= MIN_OFF) & (joined | (length >= ALONE))
    keep[0] = False
    rows, cols = rows[keep[label]], cols[keep[label]]
    del pieces
    # Each pixel to its neighbours, a diagonal left out where the way round its corner is there already.
    width = off.shape[1] + 2
    flat_index = np.sort((rows + 1) * width + (cols + 1))
    rows, cols = flat_index // width - 1, flat_index % width - 1

    def find(r, c):
        key = (r + 1) * width + (c + 1)
        at_ = np.minimum(np.searchsorted(flat_index, key), len(flat_index) - 1)
        return np.where(flat_index[at_] == key, at_, -1)
    pairs = []
    for dr, dc in NEIGHBOURS:
        j = find(rows + dr, cols + dc)
        ok = j >= 0
        if dr and dc:
            ok &= (find(rows, cols + dc) < 0) & (find(rows + dr, cols) < 0)
        pairs.append(np.column_stack([np.flatnonzero(ok), j[ok]]))
    pairs = np.concatenate(pairs) if pairs else np.zeros((0, 2), int)
    off_lines, off_about = [], []
    if len(pairs):
        centres = np.column_stack([cols + 0.5, rows + 0.5])
        merged = shapely.line_merge(shapely.multilinestrings(
            shapely.linestrings(centres[pairs.ravel()], indices=np.repeat(np.arange(len(pairs)), 2))))
        for line in shapely.get_parts(merged):
            p = shapely.get_coordinates(line)
            r, c = np.floor(p[:, 1]).astype(int), np.floor(p[:, 0]).astype(int)
            if np.mean(away(r, c) * mpp <= BESIDE) >= 0.8:
                continue
            h = ndimage.median_filter(fine[r, c], size=min(round(SMOOTH / 2 / PIXEL) | 1, len(p) | 1), mode="mirror")
            q = level(h)
            # Round, its ends where they were, for the joins.
            p[1:-1] = ndimage.uniform_filter1d(p, size=min(round(ROUND / mpp) | 1, len(p) | 1), axis=0, mode="nearest")[1:-1]
            # Joined on to a way it ends near: as far from a car park as the nearer of them.
            joins, via = [None, None], []
            for e, end_ in enumerate((0, -1)):
                if tree is not None and away(r[end_], c[end_]) <= join:
                    k = tree.query(p[end_])[1]
                    joins[e] = points[k]
                    via.append(ways[owner[k]][0])
            stretches = runs(q)
            for k, (a, b) in enumerate(stretches):
                stretch = p[a:min(b + 1, len(p))]
                if k == 0 and joins[0] is not None:
                    stretch = np.vstack([joins[0], stretch])
                if k == len(stretches) - 1 and joins[1] is not None:
                    stretch = np.vstack([stretch, joins[1]])
                if len(stretch) >= 2:
                    off_lines.append(shapely.linestrings(stretch))
                    off_about.append((int(q[a]), tuple(via)))
    parts, which = inside(list(shapely.simplify(np.asarray(off_lines, dtype=object), 0.25)) if off_lines else [])
    result["off"] = [(off_about[j][0], part, off_about[j][1]) for part, j in zip(parts, which)]
    return x, y, result


def read_tiles(store_path, todo, cache, keys):
    """Each of [todo]'s lines, WORKERS at a time, into [cache] as each is done."""
    import multiprocessing
    available = 0
    with open("/proc/meminfo") as f:
        for row in f:
            if row.startswith("MemAvailable:"):
                available = int(row.split()[1]) / 1e6
    workers = max(1, min((os.cpu_count() or 2) - 2, int(available / WORKER_GB)))
    print(f"    reading {len(todo)} tiles, {workers} at a time", flush=True)
    t, done = time.time(), 0
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        for x, y, result in pool.imap_unordered(tile, todo, chunksize=1):
            cache.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                          (x, y, keys[(x, y)], zlib.compress(pickle.dumps(result), 3)))
            cache.commit()
            done += 1
            if done % 20 == 0 or done == len(todo):
                print(f"    {done}/{len(todo)} tiles read, {left(t, done, len(todo))}", flush=True)


# --- The whole area: car parks and round walks ----------------------------------

def size_extra(spaces):
    """How much further a car park seems for its size: a lay-by of 4, 750 m; a car park of 100, 150 m."""
    return SIZE_EXTRA / math.sqrt(spaces if spaces else 10)


def read_parks(path, box):
    """The car parks on the map a walk might start from, in the box: (lon, lat, spaces) each, spaces 0 for not known."""
    west, south, east, north = box
    out = []
    with open(path) as f:
        for row in f:
            key, lon, lat, spaces, path_m = row.rstrip("\n").split("\t")
            lon, lat = float(lon), float(lat)
            if int(path_m) <= WALK_M and west <= lon <= east and south <= lat <= north:
                out.append((lon, lat, int(spaces) if spaces else 0))
    return out


def shortest_pairs(u, v, length):
    """Of ways joining the same two nodes, the shortest: their indices."""
    lo, hi = np.minimum(u, v), np.maximum(u, v)
    order = np.lexsort((length, hi, lo))
    key = lo[order].astype(np.int64) * (int(max(u.max(), v.max())) + 1) + hi[order]
    first = np.r_[True, key[1:] != key[:-1]]
    keep = order[first]
    return keep[u[keep] != v[keep]]


def csr(u, v, length, n):
    from scipy.sparse import csr_matrix
    return csr_matrix((np.r_[length, length], (np.r_[u, v], np.r_[v, u])), shape=(n, n))


def car_parks_and_loops(walking, parks_path, box, ways):
    """
    From the tiles' [ways] (edge -> (points, heated, popular, road), added
    up over the tiles): each walked way's car park's metres, and for each of
    LOOPS the shortest round walk from it, by edge. See the module's notes.
    """
    from scipy.sparse.csgraph import dijkstra
    t = time.time()
    west, south, east, north = box
    k = math.cos(math.radians((north + south) / 2))
    pad_lat, pad_lon = PARK_REACH * 1.2 / 110_574, PARK_REACH * 1.2 / (111_320 * k)
    region = (west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat)

    # The walking graph round the tiles, numbered afresh.
    nlat, nlon = walking["node_lat"] / 1e7, walking["node_lon"] / 1e7
    u_all, v_all = walking["u"], walking["v"]
    near = (nlon >= region[0]) & (nlon <= region[2]) & (nlat >= region[1]) & (nlat <= region[3])
    edges = np.flatnonzero(near[u_all] & near[v_all])
    nodes, local = np.unique(np.r_[u_all[edges], v_all[edges]], return_inverse=True)
    a, b = local[:len(edges)], local[len(edges):]
    length = np.maximum(walking["length"][edges].astype(float), 0.01)
    n = len(nodes)

    def where(numbers):
        """Indices into edges of edge [numbers], -1 for those not round the tiles."""
        numbers = np.asarray(numbers, np.int64)
        i = np.minimum(np.searchsorted(edges, numbers), len(edges) - 1)
        return np.where(edges[i] == numbers, i, -1)

    # The car parks, each on the way's end nearest it, and the nearest of them (for its size) to everywhere.
    parks = read_parks(parks_path, region)
    xy = lambda lon, lat: np.column_stack([np.asarray(lon) * 111_320 * k, np.asarray(lat) * 110_574])
    snap_d, snap = cKDTree(xy(nlon[nodes], nlat[nodes])).query(xy([p[0] for p in parks], [p[1] for p in parks])) \
        if parks else (np.zeros(0), np.zeros(0, int))
    ok = snap_d <= PARK_SNAP
    park_node, access = snap[ok], snap_d[ok]
    extra = np.array([size_extra(p[2]) for p, good in zip(parks, ok) if good])
    print(f"    {len(park_node)} car parks round the tiles, on {len(edges)} ways ({time.time() - t:.0f}s)", flush=True)
    park_m, loops = {}, {kind: {} for kind in LOOPS}
    if not len(park_node):
        return park_m, loops
    keep = shortest_pairs(a, b, length)
    p = len(park_node)
    virtual = n + np.arange(p)
    graph = csr(np.r_[a[keep], virtual], np.r_[b[keep], park_node], np.r_[length[keep], access + extra], n + p)
    dist, _, source = dijkstra(graph, indices=virtual, min_only=True, return_predecessors=True,
                               limit=PARK_REACH + extra.max() + PARK_SNAP)
    dist, source = dist[:n], source[:n]
    reached = source >= 0
    park_of = np.where(reached, source - n, -1)
    # On foot, the car park's own way to its way included; less the extra for its size.
    true = np.where(reached, dist - extra[np.maximum(park_of, 0)], np.inf)
    print(f"    each way's nearest car park, for its size ({time.time() - t:.0f}s)", flush=True)

    # Each walked way's car park: its nearer end's (for its size).
    walked_edges = np.array([e for e, (_, heated, _, _) in ways.items() if heated > 0], dtype=np.int64)
    idx = where(walked_edges)
    walked_edges, idx = walked_edges[idx >= 0], idx[idx >= 0]
    if len(idx):
        ea, eb = a[idx], b[idx]
        nearer = np.where(dist[ea] <= dist[eb], ea, eb)
        ref = park_of[nearer]
        metres = true[nearer]
        good = (ref >= 0) & (metres <= PARK_REACH)
        park_m = {int(e): float(m) for e, m in zip(walked_edges[good], metres[good])}
        ref_of = {int(e): int(r) for e, r in zip(walked_edges[good], ref[good])}
    else:
        ref_of = {}

    # The ways round walks may use: walked ones, and any near a car park.
    numbers = np.fromiter(ways.keys(), np.int64, len(ways))
    points, heated, popular, is_road = (np.array(v) for v in zip(*ways.values())) if ways else (np.zeros(0),) * 4
    share = heated / np.maximum(points, 1)
    i = where(numbers)
    good = i >= 0
    walked = np.zeros(len(edges), bool)
    liked = np.zeros(len(edges), bool)
    offroad = np.zeros(len(edges), bool)
    walked[i[good]] = share[good] >= WALKED_SHARE
    liked[i[good]] = (popular[good] / np.maximum(heated[good], 1) >= 0.5) & (share[good] >= WALKED_SHARE)
    offroad[i[good]] = ~is_road[good].astype(bool)
    by_park = np.maximum(true[a], true[b]) <= ACCESS
    usable = walked | by_park
    liked |= by_park
    print(f"    {int(walked.sum())} walked ways for round walks, {len(set(ref_of.values()))} car parks to walk from "
          f"({time.time() - t:.0f}s)", flush=True)

    # Round walks: the area a cell at a time, each car park's on the ways round it.
    refs = {}
    for e, r in ref_of.items():
        refs.setdefault(r, []).append(e)
    cell_of = {r: (int(nlat[nodes[park_node[r]]] // CELL[0]), int(nlon[nodes[park_node[r]]] // CELL[1])) for r in refs}
    cells = {}
    for r, c in cell_of.items():
        cells.setdefault(c, []).append(r)
    reach_lat, reach_lon = LOOP_MOST / 2 / 110_574 * 1.1, LOOP_MOST / 2 / (111_320 * k) * 1.1
    lat_a, lon_a = nlat[nodes[a]], nlon[nodes[a]]
    started = time.time()
    for done, ((cy, cx), members) in enumerate(cells.items()):
        s, w = cy * CELL[0] - reach_lat, cx * CELL[1] - reach_lon
        inside = usable & (lat_a >= s) & (lat_a <= s + CELL[0] + 2 * reach_lat) & (lon_a >= w) & (lon_a <= w + CELL[1] + 2 * reach_lon)
        sub = np.flatnonzero(inside)
        if not len(sub):
            continue
        sub_nodes, sub_local = np.unique(np.r_[a[sub], b[sub]], return_inverse=True)
        sa, sb = sub_local[:len(sub)], sub_local[len(sub):]
        chosen = sub[shortest_pairs(sa, sb, length[sub])]
        local_of = {int(g): i for i, g in enumerate(sub_nodes)}
        ca, cb = sub_local[:len(sub)][np.searchsorted(sub, chosen)], sub_local[len(sub):][np.searchsorted(sub, chosen)]
        m = csr(ca, cb, length[chosen], len(sub_nodes))
        pair_key = np.minimum(ca, cb).astype(np.int64) * len(sub_nodes) + np.maximum(ca, cb)
        order = np.argsort(pair_key)
        pair_key, pair_edge = pair_key[order], chosen[order]
        for r in members:
            start = local_of.get(int(park_node[r]))
            if start is None:
                continue
            found = round_walks(m, start, access[r], pair_key, pair_edge, sa, sb, sub, length, liked, by_park, offroad,
                                where(refs[r]))
            for i, values in found.items():
                for kind, value in zip(LOOPS, values):
                    if value < np.inf:
                        loops[kind][int(edges[i])] = value
        if (done + 1) % 25 == 0 or done + 1 == len(cells):
            print(f"    round walks: {done + 1}/{len(cells)} parts of the area, {left(started, done + 1, len(cells))}", flush=True)
    return park_m, loops


def round_walks(m, start, access, pair_key, pair_edge, sa, sb, sub, length, liked, by_park, offroad, wanted):
    """
    The round walks from one car park (at node [start] of the graph [m],
    [access] metres from it; [sa], [sb] the ends of its ways [sub], indices
    into the area's ways): for each of the [wanted] ways (indices into the
    area's ways), the shortest of each of LOOPS that takes it in, by index.
    """
    from scipy.sparse.csgraph import dijkstra
    n = m.shape[0]
    d_all, pred_all = dijkstra(m, indices=start, return_predecessors=True, limit=LOOP_MOST / 2)
    # From here on, only the nodes reached, numbered afresh.
    reached = np.flatnonzero(np.isfinite(d_all))
    pos = np.full(n, -1, np.int64)
    pos[reached] = np.arange(len(reached))
    r = len(reached)
    d, pred = d_all[reached], pred_all[reached]
    has = pred >= 0
    above = np.where(has, pred, reached)
    parent = pos[above]
    # Each node's way to its parent on the shortest ways (an index into the area's ways).
    key = np.minimum(reached, above).astype(np.int64) * n + np.maximum(reached, above)
    tree_way = np.full(r, -1, np.int64)
    tree_way[has] = pair_edge[np.searchsorted(pair_key, key[has])]
    tw = np.maximum(tree_way, 0)
    # Up the tree, by doubling: depth; of the ways to the car park, how many not popular, how long off the roads.
    depth, nonpop = has.astype(np.int64), (has & ~(liked[tw] | by_park[tw])).astype(np.int64)
    offlen, jump = np.where(has, offroad[tw] * length[tw], 0.0), parent.copy()
    lift = [parent]
    for _ in range(int(np.ceil(np.log2(max(r, 2)))) + 1):
        depth, nonpop, offlen = depth + depth[jump], nonpop + nonpop[jump], offlen + offlen[jump]
        jump = jump[jump]
        lift.append(jump)
    levels = len(lift)

    def up(nodes, steps):
        for bit in range(levels):
            go = (steps >> bit) & 1 == 1
            if go.any():
                nodes = np.where(go, lift[bit][nodes], nodes)
        return nodes

    def lca(p, q):
        dp, dq = depth[p], depth[q]
        p, q = up(p, np.maximum(dp - dq, 0)), up(q, np.maximum(dq - dp, 0))
        for bit in range(levels - 1, -1, -1):
            differ = lift[bit][p] != lift[bit][q]
            p, q = np.where(differ, lift[bit][p], p), np.where(differ, lift[bit][q], q)
        return np.where(p == q, p, parent[p])

    # Each way not on the tree, with both ends reached, closes a round walk.
    on_tree = np.isin(sub, tree_way[has])
    pa, pb = pos[sa], pos[sb]
    closing = np.flatnonzero(~on_tree & (pa >= 0) & (pb >= 0) & (sa != sb))
    out = {}
    if not len(closing):
        return out
    ca, cb, ce = pa[closing], pb[closing], sub[closing]
    walk = d[ca] + length[ce] + d[cb] + 2 * access
    meet = lca(ca, cb)
    fine = (walk <= LOOP_MOST) & (2 * (d[meet] + access) <= REPEAT * walk)
    ca, cb, ce, walk, meet = ca[fine], cb[fine], ce[fine], walk[fine], meet[fine]
    if not len(ce):
        return out
    # Off the roads: both sides and the closing way once, the way out to where they meet twice.
    off_ok = (offlen[ca] + offlen[cb] + offroad[ce] * length[ce]) / np.maximum(walk - 2 * access, 1) >= OFF_ROAD
    e_liked = liked[ce] | by_park[ce]
    pop_a = (nonpop[cb] == 0) & e_liked  # out along the a side, back along the b side
    pop_b = (nonpop[ca] == 0) & e_liked
    pop_e = (nonpop[ca] == 0) | (nonpop[cb] == 0)

    def best(on, pop):
        """The shortest of each kind for each row of [on] (loops its columns), [pop] its loops coming back popular."""
        return np.stack([np.where(on & ok, walk, np.inf).min(axis=1)
                         for ok in (np.ones(on.shape, bool), pop, np.broadcast_to(off_ok, on.shape), pop & off_ok)], axis=1)

    wanted = np.asarray(wanted, np.int64)
    # Wanted ways on the tree: on a loop if their lower end leads down to one of its ends, below where they meet.
    nodes_with = np.flatnonzero(has)
    by_way = np.argsort(tree_way[nodes_with])
    sorted_ways = tree_way[nodes_with][by_way]
    at = np.minimum(np.searchsorted(sorted_ways, wanted), max(len(sorted_ways) - 1, 0))
    on_it = (len(sorted_ways) > 0) & (sorted_ways[at] == wanted) if len(sorted_ways) else np.zeros(len(wanted), bool)
    rows = np.flatnonzero(on_it)
    if len(rows):
        x = nodes_with[by_way[at[rows]]][:, None]
        dx = depth[x]

        def leads(to):
            steps = depth[to][None, :] - dx
            shape = steps.shape
            top = up(np.broadcast_to(to[None, :], shape).ravel(), np.maximum(steps, 0).ravel()).reshape(shape)
            return (steps >= 0) & (top == x)
        below = dx > depth[meet][None, :]
        side_a, side_b = leads(ca) & below, leads(cb) & below
        values = np.minimum(best(side_a, np.broadcast_to(pop_a, side_a.shape)), best(side_b, np.broadcast_to(pop_b, side_b.shape)))
        for row, v in zip(rows, values):
            out[int(wanted[row])] = v
    # Wanted ways that close loops themselves.
    position = {int(e): j for j, e in enumerate(ce)}
    for w in wanted:
        j = position.get(int(w))
        if j is not None:
            v = np.array([walk[j], walk[j] if pop_e[j] else np.inf, walk[j] if off_ok[j] else np.inf,
                          walk[j] if pop_e[j] and off_ok[j] else np.inf])
            out[int(w)] = np.minimum(out.get(int(w), np.full(4, np.inf)), v)
    return out


# --- The GeoPackage ----------------------------------------------------------

def write_gpkg(path, rows):
    """[rows] (wkb in longitude and latitude, then FIELDS) into a GeoPackage's table "walked"."""
    part = path + ".part"
    if os.path.exists(part):
        os.remove(part)
    db = sqlite3.connect(part)
    wkt = pyproj.CRS.from_epsg(4326).to_wkt(pyproj.enums.WktVersion.WKT1_GDAL)
    db.executescript(f"""
        PRAGMA application_id = 1196444487;
        PRAGMA user_version = 10300;
        CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
            description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, PRIMARY KEY (table_name, column_name));
        CREATE TABLE walked (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING,
            {", ".join(f"{f} INTEGER" for f in FIELDS)});
    """)
    db.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)", [
        ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
        ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
        ("WGS 84 geodetic", 4326, "EPSG", 4326, wkt, "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid"),
    ])
    lo = [math.inf, math.inf, -math.inf, -math.inf]
    batch = []
    insert = f"INSERT INTO walked (geom, {', '.join(FIELDS)}) VALUES ({', '.join('?' * (len(FIELDS) + 1))})"
    count = 0
    for wkb, *values in rows:
        g = shapely.from_wkb(wkb)
        x0, y0, x1, y1 = shapely.bounds(g)
        lo = [min(lo[0], x0), min(lo[1], y0), max(lo[2], x1), max(lo[3], y1)]
        # The GeoPackage header: magic, version, flags (little-endian, an envelope of x then y), the SRS, the envelope.
        blob = b"GP\x00\x03" + struct.pack("<i4d", 4326, x0, x1, y0, y1) + shapely.to_wkb(g, byte_order=1)
        batch.append((blob, *values))
        count += 1
        if len(batch) >= 10_000:
            db.executemany(insert, batch)
            batch.clear()
    if batch:
        db.executemany(insert, batch)
    if count == 0:
        lo = [0.0, 0.0, 0.0, 0.0]
    db.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, min_x, min_y, max_x, max_y, srs_id) "
               "VALUES ('walked', 'features', 'walked', ?, ?, ?, ?, 4326)", lo)
    db.execute("INSERT INTO gpkg_geometry_columns VALUES ('walked', 'geom', 'LINESTRING', 4326, 0, 0)")
    db.commit()
    db.close()
    os.replace(part, path)
    return count


# --- The step ----------------------------------------------------------------

def main():
    global WALKING, DRIVING, STORE
    area, bounds, walking_path, driving_path, parks_path, store_path, out = sys.argv[1:8]
    t = time.time()
    os.makedirs(out, exist_ok=True)
    # Lines as an earlier pipeline wrote them, before the GeoPackage.
    for old in glob.glob(os.path.join(out, "walked.s??")) + glob.glob(os.path.join(out, "walked.[dpq]??")):
        os.remove(old)
    bounds = [float(v) for v in bounds.split(",")]
    gpkg = os.path.join(out, "walked.gpkg")
    tiles = tiles_over(open_store(store_path), bounds) if os.path.exists(store_path) else []
    if not tiles:
        print(f"    no Strava tiles over {area} ({store_path}): no lines")
        write_gpkg(gpkg, [])
        return

    keys = ("lat", "lon", "geom", "u", "v")
    with np.load(walking_path) as w, np.load(driving_path) as d:
        WALKING = {k: w[k] for k in keys + ("length", "node_lat", "node_lon")}
        DRIVING = {k: d[k] for k in keys}
    STORE = store_path
    store = open_store(store_path)
    code = open(__file__, "rb").read()
    stamp = hashlib.sha1(code + b"".join(f"{os.stat(p).st_size}:{os.stat(p).st_mtime_ns}".encode()
                                         for p in (walking_path, driving_path))).hexdigest()
    made = {xy: made_from(store, *xy, stamp) for xy in tiles}
    cache = sqlite3.connect(os.path.join(out, "walked-tiles.sqlite"))
    cache.execute("CREATE TABLE IF NOT EXISTS tiles (x INTEGER, y INTEGER, made_from TEXT, result BLOB, PRIMARY KEY (x, y))")
    have = {(x, y) for x, y, key in cache.execute("SELECT x, y, made_from FROM tiles") if made.get((x, y)) == key}
    todo = [xy for xy in tiles if xy not in have]
    print(f"    {len(tiles)} Strava tiles over {area}; {len(have)} read already, from the same heat, graphs and script", flush=True)
    if todo:
        read_tiles(store_path, todo, cache, made)

    # The tiles' ways, added up: points, walked, popular, road.
    ways = {}
    for x, y in tiles:
        result = pickle.loads(zlib.decompress(cache.execute("SELECT result FROM tiles WHERE x = ? AND y = ?", (x, y)).fetchone()[0]))
        for e, points, heated, popular, road in zip(*result["ways"]):
            p, h, q, r = ways.get(int(e), (0, 0, 0, False))
            ways[int(e)] = (p + int(points), h + int(heated), q + int(popular), r or bool(road))
    print(f"    {len(tiles)} tiles read: {sum(1 for v in ways.values() if v[1])} ways walked ({time.time() - t:.0f}s)", flush=True)
    north, west = tile_to_lat_lon(min(x for x, _ in tiles), min(y for _, y in tiles), Z)
    south, east = tile_to_lat_lon(max(x for x, _ in tiles) + 1, max(y for _, y in tiles) + 1, Z)
    park_m, loops = car_parks_and_loops(WALKING, parks_path, (west, south, east, north), ways)

    def up_to(m, step):
        return int(math.ceil(m / step) * step) if m is not None else 0

    def rows():
        for x, y in tiles:
            result = pickle.loads(zlib.decompress(cache.execute("SELECT result FROM tiles WHERE x = ? AND y = ?", (x, y)).fetchone()[0]))
            for e, heat, wkb in result["runs"]:
                yield (wkb, heat, 1, int(ways.get(e, (0, 0, 0, False))[3]), up_to(park_m.get(e), PARK_STEP),
                       *(up_to(loops[kind].get(e), LOOP_STEP) for kind in LOOPS), x, y)
            for heat, wkb, via in result["off"]:
                metres = [park_m[e] for e in via if e in park_m]
                yield (wkb, heat, 0, 0, up_to(min(metres), PARK_STEP) if metres else 0, *(0 for _ in LOOPS), x, y)
    count = write_gpkg(gpkg, rows())
    print(f"    {count} lines from {len(tiles)} tiles; {len(park_m)} walked ways within {PARK_REACH / 1000:.0f} km of a car park, "
          f"{len(loops['loop_m'])} on a round walk from it of {LOOP_MOST / 1000:.0f} km or less (back along popular ways "
          f"{len(loops['loop_pop_m'])}, {OFF_ROAD:.0%} off the roads {len(loops['loop_off_m'])}, both {len(loops['loop_po_m'])}); "
          f"in {(time.time() - t) / 60:.0f} min")


if __name__ == "__main__":
    main()
