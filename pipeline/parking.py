#!/usr/bin/env python3
"""
How far each car park is from the nearest path a walk would use, for the
app to show only those a walk might start from: not the supermarket's, the
station's or the multi-storey in town.

    parking.py <area> <osm.pbf> <out.tsv> <parks.tsv>

A path a walk would use is a path, footway, bridleway, track, steps or
cycleway that walkers may use (as the walking graph has it, graph.py),
not a pavement or a crossing, and away from the town: unpaved (grass,
dirt, gravel and the like), a track or a bridleway, a right of way that
is a bridleway or a byway, graded for hill walking or faint; or, whatever
it is made of, outside built-up land (landuse residential, commercial,
retail, industrial and the like), in a park, a wood, a nature reserve,
a common or open country, or along a canal (its towpath). A paved path
between houses is not one.

Where each piece of a path is, is read off a 50 m grid of what the land
is, over the area's height grid's ground (pipeline/areas.py): built-up, green (which wins over built-up: a park among houses is a
park), and within 75 m of a canal (which wins over both). Paths are read
every 25 m, so a distance is good to that.

<out.tsv>: each car park (amenity=parking), as its OpenStreetMap type and
id ("n123", "w456", "r789"), and the metres from it (a node; for one
mapped as an area, the middle of its outline) to the nearest point of
such a path, rounded to 10, at most MOST. pipeline/hiking/Hiking.java puts
it on each car park as "path_m".

<parks.tsv>: each car park a walk might start from, for pipeline/walked.py:
its key, longitude, latitude, spaces (as Hiking.java has them: mapped, or
worked out from its area; blank for neither) and metres to such a path.
That is, a car park Hiking.java draws (not closed to the public, not a
garage) that is not for customers only.
"""
import os
import re
import sys
import time

import numpy as np
import pyproj

sys.path.insert(0, os.path.dirname(__file__))
from areas import AREAS  # noqa: E402
from graph import factor  # noqa: E402

CELL = 50
EVERY = 25  # metres between the points a path is read at
MOST = 5000
TOWN, GREEN, CANAL = 1, 2, 3
# As Hiking.java has them: car parks the public may not use, garages, and what a space takes by the kind of car park.
CLOSED = {"private", "no", "residents", "staff", "employees", "permit", "delivery", "disabled", "emergency", "military",
          "agricultural", "forestry"}
GARAGES = {"garage_boxes", "garages", "garage", "carports", "sheds"}
STREET = {"street_side", "lane", "on_kerb", "half_on_kerb", "shoulder"}
COUNT = re.compile(r"\s*(?:~|c\.|ca\.|approx\.?)?\s*(\d+).*")
PATHS = {"path", "footway", "bridleway", "track", "steps", "cycleway"}
PAVEMENTS = {"sidewalk", "crossing", "traffic_island", "access_aisle"}
UNPAVED = {"unpaved", "ground", "dirt", "earth", "grass", "gravel", "fine_gravel", "compacted", "mud", "sand",
           "woodchips", "pebblestone", "rock", "stone", "grass_paver", "shells", "clay", "soil"}
OFF_ROAD = {"public_bridleway", "restricted_byway", "byway_open_to_all_traffic", "public_byway", "byway"}
BUILT_UP = {"residential", "commercial", "retail", "industrial", "garages", "construction", "railway", "education",
            "institutional", "depot", "port"}
GREEN_LEISURE = {"park", "nature_reserve", "common"}
GREEN_LANDUSE = {"forest", "recreation_ground", "village_green", "meadow"}
GREEN_NATURAL = {"wood", "heath", "moor", "scrub", "grassland", "fell", "wetland", "beach", "sand", "bare_rock"}


def walk_path(tags):
    """Whether a way is a path walkers may use; and if so, whether what it is says it is away from the town."""
    highway = tags.get("highway")
    if highway not in PATHS or tags.get("footway") in PAVEMENTS or factor(tags) is None:
        return None
    return (tags.get("surface") in UNPAVED or highway in ("track", "bridleway") or tags.get("designation") in OFF_ROAD
            or "sac_scale" in tags or "trail_visibility" in tags or tags.get("towpath") == "yes")


def count(value):
    """A count as mapped ("40", "~40", "c. 40", "1000+"), as Hiking.java's count() reads it; None if none."""
    m = COUNT.fullmatch(value or "")
    if not m or len(m.group(1)) > 6 or int(m.group(1)) == 0:
        return None
    return int(m.group(1))


def spaces(tags, area):
    """
    A car park a walk might start from: its spaces as Hiking.java works them
    out (mapped, else from its area in m², if it has one), 0 for not known;
    or None for one that is closed, a garage, or for customers only.
    """
    kind = tags.get("parking", "surface")
    if tags.get("access", "") in CLOSED or kind in GARAGES or tags.get("access") == "customers":
        return None
    n = count(tags.get("capacity"))
    if n is None and area:
        levels = count(tags.get("parking:levels") or tags.get("building:levels"))
        per = 14 if kind in STREET else (26 / levels if levels else 6) if kind == "multi-storey" \
            else (26 / levels if levels else 12) if kind == "underground" else 24
        n = max(1, round(area / per))
    return n or 0


def area_m2(ring):
    """A ring's area, lon/lat, in square metres, near enough for a car park."""
    k = np.cos(np.radians(ring[:, 1].mean()))
    x, y = ring[:, 0] * 111_320 * k, ring[:, 1] * 110_574
    return abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))) / 2


def land(tags):
    """What an area says of the land: TOWN, GREEN, or None."""
    if tags.get("leisure") in GREEN_LEISURE or tags.get("landuse") in GREEN_LANDUSE \
            or tags.get("natural") in GREEN_NATURAL:
        return GREEN
    if tags.get("landuse") in BUILT_UP:
        return TOWN
    return None


def read(pbf):
    """
    One read of the OpenStreetMap data: the paths walkers may use, as
    (lon, lat arrays, away from the town by what it is); the canals' lines;
    built-up and green areas' outlines, with holes, as (kind, outer, holes);
    and the car parks, as (key, lon, lat, spaces), spaces as spaces() has them.
    """
    import osmium
    paths, canals, areas, parks = [], [], [], []
    t, seen = time.time(), 0
    fp = (osmium.FileProcessor(pbf)
          .with_areas(osmium.filter.KeyFilter("amenity", "landuse", "leisure", "natural"))
          .with_filter(osmium.filter.KeyFilter("highway", "waterway", "amenity", "landuse", "leisure", "natural")))
    for o in fp:
        seen += 1
        if seen % 2_000_000 == 0:
            print(f"    read {seen // 1_000_000}M of the map's paths, land and car parks, {time.time() - t:.0f}s", flush=True)
        tags = o.tags
        if o.is_node():
            if tags.get("amenity") == "parking":
                parks.append((f"n{o.id}", o.location.lon, o.location.lat, spaces(tags, None)))
            continue
        if o.is_way():
            away = walk_path(tags)
            canal = tags.get("waterway") == "canal"
            if away is None and not canal:
                continue
            try:
                xy = np.array([(n.lon, n.lat) for n in o.nodes])
            except osmium.InvalidLocationError:
                continue
            if len(xy) < 2:
                continue
            if canal:
                canals.append(xy)
            else:
                paths.append((xy, away))
            continue
        if not o.is_area():
            continue
        parking = tags.get("amenity") == "parking"
        kind = land(tags)
        if not parking and kind is None:
            continue
        for outer in o.outer_rings():
            try:
                ring = np.array([(n.lon, n.lat) for n in outer])
            except osmium.InvalidLocationError:
                continue
            if parking:
                key = f"w{o.orig_id()}" if o.from_way() else f"r{o.orig_id()}"
                parks.append((key, *ring[:-1].mean(axis=0), spaces(tags, area_m2(ring))))
                break
            holes = []
            for inner in o.inner_rings(outer):
                try:
                    holes.append(np.array([(n.lon, n.lat) for n in inner]))
                except osmium.InvalidLocationError:
                    pass
            areas.append((kind, ring, holes))
    return paths, canals, areas, parks


def grid_of(areas, canals, to_local, rows, cols):
    """The land, as TOWN, GREEN or CANAL, in 50 m cells, row 0 the southmost, as the height grid is in its."""
    from PIL import Image, ImageDraw
    Image.MAX_IMAGE_PIXELS = None
    image = Image.new("L", (cols, rows), 0)
    draw = ImageDraw.Draw(image)

    def cells(xy):
        e, n = to_local(xy[:, 0], xy[:, 1])
        return list(zip(e / CELL, n / CELL))

    # Built-up first, then green over it, then the canals over both.
    for kind in (TOWN, GREEN):
        for k, ring, holes in areas:
            if k != kind or len(ring) < 3:
                continue
            draw.polygon(cells(ring), fill=kind)
            for hole in holes:
                if len(hole) >= 3:
                    draw.polygon(cells(hole), fill=0)
    for xy in canals:
        draw.line(cells(xy), fill=CANAL, width=3)
    return np.asarray(image)


def walk_points(paths, grid, to_local):
    """Every path a walk would use, read every EVERY metres, as metres east and north on the grid."""
    rows, cols = grid.shape
    out = []
    for xy, away in paths:
        e, n = to_local(xy[:, 0], xy[:, 1])
        de, dn = np.diff(e), np.diff(n)
        steps = np.ceil(np.hypot(de, dn) / EVERY).astype(np.int64) + 1
        seg = np.repeat(np.arange(len(de)), steps)
        f = (np.arange(steps.sum()) - np.repeat(np.cumsum(steps) - steps, steps)) / np.repeat(np.maximum(steps - 1, 1), steps)
        pe, pn = e[seg] + f * de[seg], n[seg] + f * dn[seg]
        if not away:
            r = np.clip((pn // CELL).astype(np.int64), 0, rows - 1)
            c = np.clip((pe // CELL).astype(np.int64), 0, cols - 1)
            keep = grid[r, c] != TOWN
            pe, pn = pe[keep], pn[keep]
        if len(pe):
            out.append(np.column_stack([pe, pn]))
    return np.concatenate(out)


def main():
    from scipy.spatial import cKDTree
    area, pbf, out, parks_out = sys.argv[1:5]
    t = time.time()
    g = AREAS[area].grid(CELL)
    to_grid = pyproj.Transformer.from_crs("EPSG:4326", g.crs, always_xy=True).transform

    def to_local(lon, lat):
        x, y = to_grid(lon, lat)
        return np.asarray(x) - g.x0, np.asarray(y) - g.y0
    paths, canals, areas, parks = read(pbf)
    print(f"    {len(paths)} paths, {len(canals)} canals, {len(areas)} built-up and green areas, {len(parks)} car parks"
          f" read in {time.time() - t:.0f}s", flush=True)
    grid = grid_of(areas, canals, to_local, g.rows, g.cols)
    del areas, canals
    points = walk_points(paths, grid, to_local)
    del grid, paths
    print(f"    {len(points) / 1e6:.0f}M points along paths a walk would use, in {time.time() - t:.0f}s", flush=True)
    e, n = to_local(np.array([p[1] for p in parks]), np.array([p[2] for p in parks]))
    d, _ = cKDTree(points).query(np.column_stack([e, n]), distance_upper_bound=MOST)
    metres = np.minimum(np.round(np.nan_to_num(d, posinf=MOST) / 10) * 10, MOST).astype(int)
    with open(out + ".part", "w") as f:
        for (key, _, _, _), m in zip(parks, metres):
            f.write(f"{key}\t{m}\n")
    os.replace(out + ".part", out)
    with open(parks_out + ".part", "w") as f:
        for (key, lon, lat, n), m in zip(parks, metres):
            if n is not None:
                f.write(f"{key}\t{lon:.7f}\t{lat:.7f}\t{n or ''}\t{m}\n")
    os.replace(parks_out + ".part", parks_out)
    near = int((metres <= 500).sum())
    print(f"parking: {len(parks)} car parks, {near} ({100 * near / max(len(parks), 1):.0f}%) within 500 m of a path"
          f" a walk would use, in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
