#!/usr/bin/env python3
"""
How far each car park is from the nearest path a walk would use, for the
app to show only those a walk might start from: not the supermarket's, the
station's or the multi-storey in town.

    parking.py <osm.pbf> <out.tsv>

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
is: built-up, green (which wins over built-up: a park among houses is a
park), and within 75 m of a canal (which wins over both). Paths are read
every 25 m, so a distance is good to that.

<out.tsv>: each car park (amenity=parking), as its OpenStreetMap type and
id ("n123", "w456", "r789"), and the metres from it (a node; for one
mapped as an area, the middle of its outline) to the nearest point of
such a path, rounded to 10, at most MOST. pipeline/hiking/Hiking.java puts
it on each car park as "path_m".
"""
import os
import sys
import time

import numpy as np
import pyproj

sys.path.insert(0, os.path.dirname(__file__))
from graph import factor  # noqa: E402

CELL = 50
COLS, ROWS = 700_000 // CELL, 1_300_000 // CELL
EVERY = 25  # metres between the points a path is read at
MOST = 5000
TOWN, GREEN, CANAL = 1, 2, 3
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
    and the car parks, as (key, lon, lat).
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
                parks.append((f"n{o.id}", o.location.lon, o.location.lat))
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
                parks.append((key, *ring[:-1].mean(axis=0)))
                break
            holes = []
            for inner in o.inner_rings(outer):
                try:
                    holes.append(np.array([(n.lon, n.lat) for n in inner]))
                except osmium.InvalidLocationError:
                    pass
            areas.append((kind, ring, holes))
    return paths, canals, areas, parks


def grid_of(areas, canals, to_osgb):
    """The land, as TOWN, GREEN or CANAL, in 50 m cells, row 0 the southmost, as the other grids are at 20 m."""
    from PIL import Image, ImageDraw
    Image.MAX_IMAGE_PIXELS = None
    image = Image.new("L", (COLS, ROWS), 0)
    draw = ImageDraw.Draw(image)

    def cells(xy):
        e, n = to_osgb(xy[:, 0], xy[:, 1])
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


def walk_points(paths, grid, to_osgb):
    """Every path a walk would use, read every EVERY metres, as OSGB eastings and northings."""
    out = []
    for xy, away in paths:
        e, n = to_osgb(xy[:, 0], xy[:, 1])
        de, dn = np.diff(e), np.diff(n)
        steps = np.ceil(np.hypot(de, dn) / EVERY).astype(np.int64) + 1
        seg = np.repeat(np.arange(len(de)), steps)
        f = (np.arange(steps.sum()) - np.repeat(np.cumsum(steps) - steps, steps)) / np.repeat(np.maximum(steps - 1, 1), steps)
        pe, pn = e[seg] + f * de[seg], n[seg] + f * dn[seg]
        if not away:
            r = np.clip((pn // CELL).astype(np.int64), 0, ROWS - 1)
            c = np.clip((pe // CELL).astype(np.int64), 0, COLS - 1)
            keep = grid[r, c] != TOWN
            pe, pn = pe[keep], pn[keep]
        if len(pe):
            out.append(np.column_stack([pe, pn]))
    return np.concatenate(out)


def main():
    from scipy.spatial import cKDTree
    pbf, out = sys.argv[1:3]
    t = time.time()
    to_osgb = pyproj.Transformer.from_crs(4326, 27700, always_xy=True).transform
    paths, canals, areas, parks = read(pbf)
    print(f"    {len(paths)} paths, {len(canals)} canals, {len(areas)} built-up and green areas, {len(parks)} car parks"
          f" read in {time.time() - t:.0f}s", flush=True)
    grid = grid_of(areas, canals, to_osgb)
    del areas, canals
    points = walk_points(paths, grid, to_osgb)
    del grid, paths
    print(f"    {len(points) / 1e6:.0f}M points along paths a walk would use, in {time.time() - t:.0f}s", flush=True)
    e, n = to_osgb(np.array([p[1] for p in parks]), np.array([p[2] for p in parks]))
    d, _ = cKDTree(points).query(np.column_stack([e, n]), distance_upper_bound=MOST)
    metres = np.minimum(np.round(np.nan_to_num(d, posinf=MOST) / 10) * 10, MOST).astype(int)
    with open(out + ".part", "w") as f:
        for (key, _, _), m in zip(parks, metres):
            f.write(f"{key}\t{m}\n")
    os.replace(out + ".part", out)
    near = int((metres <= 500).sum())
    print(f"parking: {len(parks)} car parks, {near} ({100 * near / max(len(parks), 1):.0f}%) within 500 m of a path"
          f" a walk would use, in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
