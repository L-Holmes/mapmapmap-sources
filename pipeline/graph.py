#!/usr/bin/env python3
"""
The walking and driving graphs: every way a walker, or a car, can use,
split at junctions, with a cost per direction, cut into one file per
download region.

    graph.py build <osm.pbf> <dem.npy> <out.npz>
    graph.py build-driving <osm.pbf> <dem.npy> <out.npz>
    graph.py cut <graph.npz> <regions.json> <out dir>

build reads Great Britain once. cut writes <out dir>/<region>.graph for
each region (<region>.driving.graph for a driving graph): the edges within
~2 km of its outline, in the binary format the app reads (the app's
routing/Graph.kt, which documents it). Node ids are global, so graphs of
neighbouring regions join where they meet.

Walking cost is metres of flat walking: an edge's length times a factor for
the kind of way it is (1.0 for a path or a right of way, more for roads the
busier they are, more for hard or faint paths), plus Naismith's rule for
the climbing: 1 m up costs as much as 8 m along.

Driving cost is metres at motorway speed: an edge's length times
DRIVE_KMH over the speed a car makes along it (the road's usual speed,
lowered to its limit where that is lower). The file's header carries
DRIVE_KMH, which is how the app turns a cost back into a time. The way a
one-way road does not go costs infinity. Car ferries are in, at FERRY_KMH;
turn restrictions are not.

A driving graph also says what road each edge is, for the app's
directions: its number and name ("A59", "Whalley Road"), its kind (ROAD
below) and whether it is part of a roundabout. These follow the file's last
section, where an app that does not read them never looks.
"""
import json
import os
import sys
import time
from array import array

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from progress import left  # noqa: E402

CLIMB = 8.0
# The factor for each kind of way. None of these are below 1, which is what
# makes straight-line distance a safe A* heuristic in the app.
BASE = {
    "path": 1.0, "footway": 1.0, "bridleway": 1.0, "track": 1.0, "steps": 1.2,
    "pedestrian": 1.05, "cycleway": 1.15, "living_street": 1.2,
    "residential": 1.25, "service": 1.25, "unclassified": 1.3, "road": 1.3,
    "tertiary": 1.6, "tertiary_link": 1.6,
    "secondary": 2.2, "secondary_link": 2.2,
    "primary": 3.0, "primary_link": 3.0,
    "trunk": 5.0, "trunk_link": 5.0,
}
ROW = {"public_footpath", "public_bridleway", "restricted_byway", "byway_open_to_all_traffic", "public_byway", "byway"}
SAC = {"demanding_mountain_hiking": 1.2, "alpine_hiking": 2.0, "demanding_alpine_hiking": 4.0, "difficult_alpine_hiking": 6.0}
SIDEWALK = {"both", "left", "right", "yes"}

# Driving: the usual speed on each kind of road, km/h, before any limit
# lowers it. The fastest is DRIVE_KMH, so no factor is below 1 here either.
DRIVE_KMH = 110
SPEED = {
    "motorway": 110, "motorway_link": 60, "trunk": 90, "trunk_link": 50,
    "primary": 70, "primary_link": 45, "secondary": 60, "secondary_link": 40,
    "tertiary": 50, "tertiary_link": 35, "unclassified": 40, "road": 30,
    "residential": 30, "living_street": 10, "service": 15,
}
FERRY_KMH = 20
# Limits as British mapping writes them, km/h.
NATIONAL = {
    "gb:nsl_single": 96, "uk:nsl_single": 96, "national": 96,
    "gb:nsl_dual": 112, "uk:nsl_dual": 112, "gb:motorway": 112, "uk:motorway": 112,
}
NO_CARS = {"no", "private", "agricultural", "forestry", "delivery", "emergency"}
ONEWAY = {"yes", "true", "1"}
# Edge geometry is simplified this far (degrees, ~2 m) once its length and
# climb have been measured on the full detail.
SIMPLIFY = 2e-5
# The snapping grid's cell, in 1e-7 degrees: 0.01 degrees, ~1.1 km north-south.
CELL = 100_000
BUFFER = 0.02
# Networks smaller than this are dropped: a path mapped without joining
# anything else, a farmyard's tracks. Snapping to one strands the walker,
# since there is no route from it to anywhere. For driving, a network is
# the roads a car can both get to and get back from each other by (one
# strongly connected component): a one-way road into a car park whose way
# out is not mapped for cars is a network of its own, and a car snapped to
# it could never leave.
MIN_COMPONENT_EDGES = 50
MAGIC = 0x52474D4D  # "MMGR"
VERSION = 1
# A driving graph's roads: the kinds, numbered as the app's Graph.kt numbers
# them (0 for none), and the bit that marks a roundabout.
ROAD = ("", "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified", "road",
        "residential", "living_street", "service", "ferry")
ROUNDABOUT = 0x20


def walk(tags):
    """The walking factor, the same both ways; None if walkers cannot use it."""
    f = factor(tags)
    return None if f is None else (f, f)


def factor(tags):
    base = BASE.get(tags.get("highway"))
    if base is None or tags.get("area") == "yes" or tags.get("indoor") == "yes":
        return None
    foot = tags.get("foot")
    if foot in ("no", "private", "use_sidepath"):
        return None
    if tags.get("access") in ("no", "private") and foot not in ("yes", "designated", "permissive"):
        return None
    if tags.get("designation") in ROW:
        base = 1.0
    elif base > 1.3 and (tags.get("sidewalk") in SIDEWALK or foot == "designated"):
        base = 1.3
    base *= SAC.get(tags.get("sac_scale"), 1.0)
    if tags.get("trail_visibility") in ("bad", "horrible", "no"):
        base *= 1.5
    return base


def limit(tags):
    """The speed limit in km/h, if the way has one that can be read."""
    v = (tags.get("maxspeed") or "").strip().lower()
    if v in NATIONAL:
        return NATIONAL[v]
    try:
        return float(v[:-3]) * 1.609 if v.endswith("mph") else float(v)
    except ValueError:
        return None


def drive(tags):
    """The driving factors, forwards and backwards (infinite the way a car
    may not go); None if cars cannot use the way at all."""
    if tags.get("route") == "ferry":
        if "yes" not in (tags.get("motorcar"), tags.get("motor_vehicle")):
            return None
        f = DRIVE_KMH / FERRY_KMH
        return f, f
    highway = tags.get("highway")
    speed = SPEED.get(highway)
    if speed is None or tags.get("area") == "yes":
        return None
    if tags.get("service") in ("driveway", "emergency_access"):
        return None
    # The most particular of these that is given decides.
    car = tags.get("motorcar") or tags.get("motor_vehicle") or tags.get("vehicle")
    if car in NO_CARS or (car is None and tags.get("access") in NO_CARS):
        return None
    lim = limit(tags)
    if lim:
        speed = min(speed, lim * 0.9)
    f = DRIVE_KMH / max(speed, 5)
    oneway = tags.get("oneway")
    if oneway == "-1":
        return float("inf"), f
    if oneway in ONEWAY or (oneway != "no" and (
            tags.get("junction") in ("roundabout", "circular") or highway in ("motorway", "motorway_link"))):
        return f, float("inf")
    return f, f


def road_label(tags):
    """A road's number and name, one line each ("A59\nWhalley Road"); "" for neither."""
    ref = " / ".join(r.strip() for r in (tags.get("ref") or "").split(";") if r.strip())
    name = (tags.get("name") or "").strip()
    if not ref and not name:
        return ""
    return ref.replace("\n", " ") + "\n" + name.replace("\n", " ")


def road_kind(tags):
    """A road's kind (an index into ROAD), and ROUNDABOUT if it is part of one."""
    kind = ROAD.index("ferry") if tags.get("route") == "ferry" else ROAD.index(tags.get("highway"))
    if tags.get("junction") in ("roundabout", "circular"):
        kind |= ROUNDABOUT
    return kind


def elevation(dem, lat_e7, lon_e7):
    """Heights in decimetres, bilinear from the OS Terrain 50 grid."""
    import pyproj
    to_osgb = pyproj.Transformer.from_crs(4326, 27700, always_xy=True)
    e, n = to_osgb.transform(lon_e7 / 1e7, lat_e7 / 1e7)
    c = np.clip((e - 25.0) / 50.0, 0, dem.shape[1] - 1.001)
    r = np.clip((n - 25.0) / 50.0, 0, dem.shape[0] - 1.001)
    c0, r0 = c.astype(np.int64), r.astype(np.int64)
    fc, fr = c - c0, r - r0
    z = (dem[r0, c0] * (1 - fc) * (1 - fr) + dem[r0, c0 + 1] * fc * (1 - fr)
         + dem[r0 + 1, c0] * (1 - fc) * fr + dem[r0 + 1, c0 + 1] * fc * fr)
    return np.round(np.maximum(z, 0) * 10).astype(np.int16)


def seg_lengths(lat, lon):
    """Metres between consecutive points."""
    la = np.radians(lat / 1e7)
    lo = np.radians(lon / 1e7)
    dla = np.diff(la)
    dlo = np.diff(lo) * np.cos((la[1:] + la[:-1]) / 2)
    return 6371008.8 * np.hypot(dla, dlo)


def morton(lat, lon):
    def spread(v):
        v = v.astype(np.uint64) & 0xFFFF
        v = (v | (v << 8)) & 0x00FF00FF
        v = (v | (v << 4)) & 0x0F0F0F0F
        v = (v | (v << 2)) & 0x33333333
        v = (v | (v << 1)) & 0x55555555
        return v
    y = ((lat.astype(np.int64) + 900_000_000) >> 15)
    x = ((lon.astype(np.int64) + 1_800_000_000) >> 16)
    return spread(x) | (spread(y) << np.uint64(1))


def build(pbf, dem_path, out, driving=False):
    import osmium
    t = time.time()
    refs, xs, ys = array("q"), array("i"), array("i")
    starts, forward, backward = array("q", [0]), array("f"), array("f")
    # Driving: each way's road, its label an index into labels.
    labels = {"": 0}
    way_label, way_kind = array("i"), array("b")
    keys = ("highway", "route") if driving else ("highway",)
    profile = drive if driving else walk
    climb = 0.0 if driving else CLIMB
    fp = osmium.FileProcessor(pbf).with_locations().with_filter(osmium.filter.KeyFilter(*keys))
    seen = 0
    for w in fp:
        if not w.is_way():
            continue
        seen += 1
        if seen % 1_000_000 == 0:
            print(f"    read {seen // 1_000_000}M roads and paths, {time.time() - t:.0f}s", flush=True)
        f = profile(w.tags)
        if f is None:
            continue
        nodes = w.nodes
        if len(nodes) < 2:
            continue
        try:
            for nd in nodes:
                xs.append(nd.x)
                ys.append(nd.y)
                refs.append(nd.ref)
        except osmium.InvalidLocationError:
            # A way reaching outside the extract: drop what was added of it.
            del refs[starts[-1]:], xs[starts[-1]:], ys[starts[-1]:]
            continue
        starts.append(len(refs))
        forward.append(f[0])
        backward.append(f[1])
        if driving:
            way_label.append(labels.setdefault(road_label(w.tags), len(labels)))
            way_kind.append(road_kind(w.tags))
    refs = np.frombuffer(refs, dtype=np.int64)
    lon = np.frombuffer(xs, dtype=np.int32)
    lat = np.frombuffer(ys, dtype=np.int32)
    starts = np.frombuffer(starts, dtype=np.int64)
    forward = np.frombuffer(forward, dtype=np.float32)
    backward = np.frombuffer(backward, dtype=np.float32)
    print(f"read {len(forward)} ways, {len(refs)} refs in {time.time() - t:.0f}s")

    # Junctions: nodes shared by two ways or used twice, and every way's ends.
    uniq, inv, counts = np.unique(refs, return_inverse=True, return_counts=True)
    junction = counts[inv] >= 2
    junction[starts[:-1]] = True
    junction[starts[1:] - 1] = True
    j = np.flatnonzero(junction)
    way_of = np.searchsorted(starts, j, side="right") - 1
    pair = way_of[:-1] == way_of[1:]
    a, b = j[:-1][pair], j[1:][pair]
    factor_f = forward[way_of[:-1][pair]]
    factor_b = backward[way_of[:-1][pair]]
    print(f"{len(a)} edges")

    # Each edge's points, full detail, concatenated.
    n_pts = b - a + 1
    offs = np.zeros(len(a) + 1, dtype=np.int64)
    np.cumsum(n_pts, out=offs[1:])
    idx = np.arange(offs[-1]) - np.repeat(offs[:-1] - a, n_pts)
    g_lat, g_lon = lat[idx], lon[idx]

    dem = np.load(dem_path, mmap_mode="r")
    g_ele = elevation(dem, g_lat, g_lon).astype(np.float32) / 10
    seg = seg_lengths(g_lat, g_lon)
    rise = np.diff(g_ele)
    # Segments that run from one edge into the next are not segments.
    last = offs[1:-1] - 1
    seg[last] = 0
    rise[last] = 0
    seg_start = offs[:-1]
    # reduceat needs a start per edge; every edge has at least one segment.
    length = np.add.reduceat(np.append(seg, 0), seg_start)
    up = np.add.reduceat(np.append(np.maximum(rise, 0), 0), seg_start)
    down = np.add.reduceat(np.append(np.maximum(-rise, 0), 0), seg_start)
    length = length.astype(np.float32)
    # Infinity times a zero length would be no number at all.
    with np.errstate(invalid="ignore"):
        cost_f = np.where(np.isinf(factor_f), np.inf, length * factor_f + climb * up).astype(np.float32)
        cost_b = np.where(np.isinf(factor_b), np.inf, length * factor_b + climb * down).astype(np.float32)

    # Nodes, numbered along a Morton curve so neighbours sit together in the
    # file, which is what the app's memory-mapped reads want.
    node_ref, first = np.unique(refs[j], return_index=True)
    node_lat, node_lon = lat[j][first], lon[j][first]
    order = np.argsort(morton(node_lat, node_lon), kind="stable")
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    node_lat, node_lon = node_lat[order], node_lon[order]
    u = rank[np.searchsorted(node_ref, refs[a])].astype(np.int32)
    v = rank[np.searchsorted(node_ref, refs[b])].astype(np.int32)

    # Simplified geometry, ends kept exactly.
    import shapely
    lines = shapely.linestrings(np.column_stack([g_lon / 1e7, g_lat / 1e7]), indices=np.repeat(np.arange(len(a)), n_pts))
    lines = shapely.simplify(lines, SIMPLIFY, preserve_topology=False)
    # A way whose points all sit in one place simplifies to nothing; it
    # keeps its two ends.
    broken = np.flatnonzero(shapely.get_num_coordinates(lines) < 2)
    if len(broken):
        ends = np.stack([
            np.column_stack([g_lon[offs[broken]], g_lat[offs[broken]]]),
            np.column_stack([g_lon[offs[broken + 1] - 1], g_lat[offs[broken + 1] - 1]]),
        ], axis=1) / 1e7
        lines[broken] = shapely.linestrings(ends)
    coords, which = shapely.get_coordinates(lines, return_index=True)
    s_lat = np.round(coords[:, 1] * 1e7).astype(np.int32)
    s_lon = np.round(coords[:, 0] * 1e7).astype(np.int32)
    s_offs = np.zeros(len(a) + 1, dtype=np.int64)
    np.cumsum(np.bincount(which, minlength=len(a)), out=s_offs[1:])
    s_ele = elevation(dem, s_lat, s_lon)
    print(f"{len(node_lat)} nodes, {len(g_lat)} -> {len(s_lat)} points in {time.time() - t:.0f}s")

    roads = {}
    if driving:
        way_of_edge = way_of[:-1][pair]
        text = [label.encode() for label in sorted(labels, key=labels.get)]
        label_start = np.zeros(len(text) + 1, dtype=np.int64)
        np.cumsum([len(t) for t in text], out=label_start[1:])
        roads = dict(
            label=np.frombuffer(way_label, dtype=np.int32)[way_of_edge],
            kind=np.frombuffer(way_kind, dtype=np.int8)[way_of_edge],
            label_bytes=np.frombuffer(b"".join(text), dtype=np.uint8),
            label_start=label_start,
        )
        print(f"{len(text)} road names and numbers")

    np.savez(
        out,
        node_lat=node_lat, node_lon=node_lon,
        u=u, v=v, length=length, cost_f=cost_f, cost_b=cost_b,
        geom=s_offs, lat=s_lat, lon=s_lon, ele=s_ele,
        speed=np.int32(DRIVE_KMH if driving else 0),
        **roads,
    )


def cut(npz_path, regions_path, out_dir):
    import shapely
    from shapely.geometry import shape
    os.makedirs(out_dir, exist_ok=True)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    g = np.load(npz_path)
    u, v, geom = g["u"], g["v"], g["geom"]
    lat, lon = g["lat"], g["lon"]
    n_nodes = len(g["node_lat"])

    speed = int(g["speed"]) if "speed" in g else 0
    if speed:
        # Each way an edge can be driven, as an arc.
        fwd = np.isfinite(g["cost_f"])
        back = np.isfinite(g["cost_b"])
        tail = np.concatenate([u[fwd], v[back]])
        head = np.concatenate([v[fwd], u[back]])
        _, label = connected_components(
            coo_matrix((np.ones(len(tail)), (tail, head)), shape=(n_nodes, n_nodes)), directed=True, connection="strong")
        inside = label[u] == label[v]
    else:
        _, label = connected_components(coo_matrix((np.ones(len(u)), (u, v)), shape=(n_nodes, n_nodes)), directed=False)
        inside = np.ones(len(u), dtype=bool)
    size = np.bincount(label[u][inside], minlength=label.max() + 1)
    kept = np.flatnonzero(inside & (size[label[u]] >= MIN_COMPONENT_EDGES))
    print(f"{len(u) - len(kept)} edges in small networks dropped")

    degree = np.bincount(np.concatenate([u[kept], v[kept]]), minlength=n_nodes)
    n_pts = np.diff(geom)[kept]
    starts = geom[kept]
    offs = np.zeros(len(kept) + 1, dtype=np.int64)
    np.cumsum(n_pts, out=offs[1:])
    idx = np.arange(offs[-1]) - np.repeat(offs[:-1] - starts, n_pts)
    lines = shapely.linestrings(np.column_stack([lon[idx] / 1e7, lat[idx] / 1e7]), indices=np.repeat(np.arange(len(kept)), n_pts))
    tree = shapely.STRtree(lines)
    suffix = ".driving.graph" if speed else ".graph"
    regions = json.load(open(regions_path))
    began = time.time()
    for i, region in enumerate(regions, 1):
        t = time.time()
        if region["parent"] is None:
            sel = kept
        else:
            area = shape(region["geometry"]).buffer(BUFFER)
            sel = kept[np.sort(tree.query(area, predicate="intersects"))]
        path = os.path.join(out_dir, region["id"] + suffix)
        write_region(path, g, sel, degree, speed)
        print(f"{region['id']}: {len(sel)} edges, {os.path.getsize(path) / 1e6:.1f} MB in {time.time() - t:.0f}s"
              f"  ({i} of {len(regions)} regions, {left(began, i, len(regions))})", flush=True)


def write_region(path, g, sel, degree, speed):
    u_all, v_all = g["u"], g["v"]
    geom = g["geom"]
    nodes = np.unique(np.concatenate([u_all[sel], v_all[sel]]))
    u = np.searchsorted(nodes, u_all[sel]).astype(np.int32)
    v = np.searchsorted(nodes, v_all[sel]).astype(np.int32)
    n, e = len(nodes), len(sel)

    local_degree = np.bincount(np.concatenate([u, v]), minlength=n)
    border = np.flatnonzero(local_degree < degree[nodes]).astype(np.int32)

    # Adjacency: each edge once from each end, e from u and ~e from v.
    at = np.concatenate([u, v])
    ref = np.concatenate([np.arange(e, dtype=np.int32), ~np.arange(e, dtype=np.int32)])
    order = np.argsort(at, kind="stable")
    adj = ref[order]
    adj_start = np.zeros(n + 1, dtype=np.int32)
    np.cumsum(np.bincount(at, minlength=n), out=adj_start[1:])

    # Geometry of the chosen edges.
    counts = (geom[sel + 1] - geom[sel]).astype(np.int64)
    offs = np.zeros(e + 1, dtype=np.int64)
    np.cumsum(counts, out=offs[1:])
    idx = np.arange(offs[-1]) - np.repeat(offs[:-1] - geom[sel], counts)
    lat, lon, ele = g["lat"][idx], g["lon"][idx], g["ele"][idx]

    # The snapping grid: every cell each segment's box touches lists the edge.
    min_lat = int(lat.min() // CELL * CELL)
    min_lon = int(lon.min() // CELL * CELL)
    rows = int((lat.max() - min_lat) // CELL + 1)
    cols = int((lon.max() - min_lon) // CELL + 1)
    edge_of_pt = np.repeat(np.arange(e, dtype=np.int64), counts)
    same = edge_of_pt[:-1] == edge_of_pt[1:]
    s_edge = edge_of_pt[:-1][same]
    r0 = (np.minimum(lat[:-1], lat[1:])[same] - min_lat) // CELL
    r1 = (np.maximum(lat[:-1], lat[1:])[same] - min_lat) // CELL
    c0 = (np.minimum(lon[:-1], lon[1:])[same] - min_lon) // CELL
    c1 = (np.maximum(lon[:-1], lon[1:])[same] - min_lon) // CELL
    nr, nc = r1 - r0 + 1, c1 - c0 + 1
    k = (nr * nc).astype(np.int64)
    rep_edge = np.repeat(s_edge, k)
    within = np.arange(k.sum()) - np.repeat(np.cumsum(k) - k, k)
    rr = np.repeat(r0, k) + within // np.repeat(nc, k)
    cc = np.repeat(c0, k) + within % np.repeat(nc, k)
    cell = rr.astype(np.int64) * cols + cc
    key = np.unique(cell * e + rep_edge)
    cell, cell_edge = key // e, (key % e).astype(np.int32)
    grid_start = np.zeros(rows * cols + 1, dtype=np.int32)
    np.cumsum(np.bincount(cell, minlength=rows * cols), out=grid_start[1:])

    header = np.zeros(16, dtype="<i4")
    header[:13] = [MAGIC, VERSION, n, e, len(lat), len(border), cols, rows, min_lat, min_lon, CELL, len(cell_edge), speed]

    # The roads, with the labels this region's edges use, renumbered: label
    # 0, no number and no name, stays 0.
    roads = ()
    if "label" in g.files:
        used, local = np.unique(np.concatenate([[0], g["label"][sel]]), return_inverse=True)
        road = local[1:].astype(np.int64) | (g["kind"][sel].astype(np.int64) << 24)
        label_start = g["label_start"]
        lengths = label_start[used + 1] - label_start[used]
        starts = np.zeros(len(used) + 1, dtype=np.int64)
        np.cumsum(lengths, out=starts[1:])
        text = g["label_bytes"][np.arange(starts[-1]) - np.repeat(starts[:-1] - label_start[used], lengths)]
        header[13:16] = [1, len(used), len(text)]
        roads = (road.astype("<i4"), starts.astype("<i4"), text.astype(np.uint8), np.zeros(-len(text) % 4, dtype=np.uint8))
    with open(path, "wb") as f:
        for part in (
            header,
            nodes.astype("<i4"), g["node_lat"][nodes].astype("<i4"), g["node_lon"][nodes].astype("<i4"),
            adj_start.astype("<i4"), adj.astype("<i4"),
            u.astype("<i4"), v.astype("<i4"),
            g["length"][sel].astype("<f4"), g["cost_f"][sel].astype("<f4"), g["cost_b"][sel].astype("<f4"),
            offs.astype("<i4"),
            lat.astype("<i4"), lon.astype("<i4"),
            ele.astype("<i2"), np.zeros(len(ele) % 2, dtype="<i2"),
            border.astype("<i4"),
            grid_start.astype("<i4"), cell_edge.astype("<i4"),
            *roads,
        ):
            f.write(part.tobytes())


def main():
    if sys.argv[1] == "build":
        build(*sys.argv[2:5])
    elif sys.argv[1] == "build-driving":
        build(*sys.argv[2:5], driving=True)
    elif sys.argv[1] == "cut":
        cut(*sys.argv[2:5])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
