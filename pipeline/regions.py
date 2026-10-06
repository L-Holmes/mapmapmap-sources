#!/usr/bin/env python3
"""
The download regions, from Geofabrik's index, each in the area it is cut
from (pipeline/areas.py), those of the areas built:

    Great Britain, its three countries, and England's counties
    Italy, in Geofabrik's five parts

and of the areas defined but not built, none:

    the Alps, by country: the Austrian, French (with Monaco), German,
        Italian and Swiss (with Liechtenstein) Alps, each the Alps' part
        of it, and all of Slovenia
    Norway, in its five parts (landsdeler), as Geofabrik has them

    regions.py <index-v1.json> <out regions.json> <out app asset>

regions.json is the pipeline's copy: full outlines, for cutting tiles and
graphs, and each region's "area". The app asset is the same regions with
outlines simplified to a few hundred metres, enough to tell which region a
position is in, offline.

Italy (and the Alps and Norway) are only headings: each whole would be over
GitHub's 2 GiB a file, and more than a phone wants. Such a region is
"download": false, and nothing is cut for it. Great Britain is all of its
regions in one, as it always has been.

Northern Ireland is not in Geofabrik's United Kingdom extract, so the top
region is Great Britain, not the UK.
"""
import json
import os
import sys

import shapely
from shapely.geometry import mapping, shape

sys.path.insert(0, os.path.dirname(__file__))
from areas import AREAS  # noqa: E402

TOP = "great-britain"
COUNTRIES = ["england", "scotland", "wales"]
# Each the Alps' part of the countries named, by Geofabrik's outlines, which
# run a few km past each border, as the counties' do.
ALPS = [
    ("alps-austria", "Austrian Alps", ["austria"]),
    ("alps-france", "French Alps", ["france", "monaco"]),
    ("alps-germany", "German Alps", ["germany"]),
    ("alps-italy", "Italian Alps", ["italy"]),
    ("alps-switzerland", "Swiss Alps", ["switzerland", "liechtenstein"]),
]
ITALY = [
    ("italy-nord-ovest", "North-West Italy", "nord-ovest"),
    ("italy-nord-est", "North-East Italy", "nord-est"),
    ("italy-centro", "Central Italy", "centro"),
    ("italy-sud", "Southern Italy", "sud"),
    ("italy-isole", "Sicily and Sardinia", "isole"),
]
NORWAY = [
    ("norway-nord-norge", "Northern Norway", "nord-norge"),
    ("norway-trondelag", "Trøndelag", "trondelag"),
    ("norway-vestlandet", "Western Norway", "vestlandet"),
    ("norway-ostlandet", "Eastern Norway", "ostlandet"),
    ("norway-sorlandet", "Southern Norway", "sorlandet"),
]
# Degrees. About 300 m north-south, less east-west: plenty for "which
# county am I in", and it keeps the asset small.
APP_TOLERANCE = 0.003


def main():
    index_path, out_path, asset_path = sys.argv[1:4]
    features = {f["properties"]["id"]: f for f in json.load(open(index_path))["features"]}

    def outline(*ids):
        return shapely.union_all([shape(features[i]["geometry"]) for i in ids])

    def region(id, name, parent, area, geometry, download=True):
        return {"id": id, "name": name, "parent": parent, "area": area, "download": download, "geometry": geometry}

    regions = []
    countries = []
    for country in COUNTRIES:
        f = features[country]
        geom = shape(f["geometry"])
        countries.append(geom)
        regions.append(region(country, f["properties"]["name"], TOP, "gb", geom))
        children = sorted(
            (c for c in features.values() if c["properties"].get("parent") == country),
            key=lambda c: c["properties"]["name"],
        )
        for c in children:
            regions.append(region(c["properties"]["id"], c["properties"]["name"], country, "gb", shape(c["geometry"])))
    regions.insert(0, region(TOP, "Great Britain", None, "gb", shapely.union_all(countries)))

    alps = outline("alps")
    regions.append(region("alps", "The Alps", None, "alps", shapely.union_all([alps, outline("slovenia")]), download=False))
    parts = [region(id, name, "alps", "alps", shapely.intersection(alps, outline(*within))) for id, name, within in ALPS]
    parts.append(region("slovenia", "Slovenia", "alps", "slovenia", outline("slovenia")))
    regions += sorted(parts, key=lambda r: r["name"])

    regions.append(region("italy", "Italy", None, "italy", outline("italy"), download=False))
    regions += [region(id, name, "italy", "italy", outline(part)) for id, name, part in ITALY]

    regions.append(region("norway", "Norway", None, "norway", outline(*[part for _, _, part in NORWAY]), download=False))
    regions += [region(id, name, "norway", "norway", outline(part)) for id, name, part in NORWAY]

    # Only the areas built.
    regions = [r for r in regions if r["area"] in AREAS]

    with open(out_path, "w") as f:
        json.dump([{**r, "geometry": mapping(r["geometry"])} for r in regions], f)

    def small(geom):
        simple = shapely.simplify(geom, APP_TOLERANCE, preserve_topology=True)
        # Rounded to 4 places, about 10 m, which is all this precision needs.
        return json.loads(json.dumps(mapping(simple)), parse_float=lambda s: round(float(s), 4))

    with open(asset_path, "w") as f:
        json.dump(
            [{"id": r["id"], "name": r["name"], "parent": r["parent"], **({} if r["download"] else {"download": False}),
              "geometry": small(r["geometry"])} for r in regions],
            f,
            separators=(",", ":"),
        )
    print(f"{len(regions)} regions, {sum(r['download'] for r in regions)} to download")


def of(regions_path, area):
    """The regions cut from an area: those to download, in it."""
    return [r for r in json.load(open(regions_path)) if r.get("area", "gb") == area and r.get("download", True)]


def outline(regions_path, area, buffer=0.0):
    """All of an area's regions to download, as one shape, [buffer] degrees round."""
    return shapely.union_all([shape(r["geometry"]) for r in of(regions_path, area)]).buffer(buffer)


if __name__ == "__main__":
    main()
