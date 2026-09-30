#!/usr/bin/env python3
"""
The download regions, from Geofabrik's index: Great Britain, its three
countries, and England's counties.

    regions.py <index-v1.json> <out regions.json> <out app asset>

regions.json is the pipeline's copy: full outlines, for cutting tiles and
graphs. The app asset is the same regions with outlines simplified to a few
hundred metres, enough to tell which region a position is in, offline.

Northern Ireland is not in Geofabrik's United Kingdom extract, so the top
region is Great Britain, not the UK.
"""
import json
import sys

import shapely
from shapely.geometry import mapping, shape

TOP = "great-britain"
COUNTRIES = ["england", "scotland", "wales"]
# Degrees. About 300 m north-south, less east-west: plenty for "which
# county am I in", and it keeps the asset small.
APP_TOLERANCE = 0.003


def main():
    index_path, out_path, asset_path = sys.argv[1:4]
    features = {f["properties"]["id"]: f for f in json.load(open(index_path))["features"]}
    regions = []
    countries = []
    for country in COUNTRIES:
        f = features[country]
        geom = shape(f["geometry"])
        countries.append(geom)
        regions.append({"id": country, "name": f["properties"]["name"], "parent": TOP, "geometry": geom})
        children = sorted(
            (c for c in features.values() if c["properties"].get("parent") == country),
            key=lambda c: c["properties"]["name"],
        )
        for c in children:
            regions.append({
                "id": c["properties"]["id"],
                "name": c["properties"]["name"],
                "parent": country,
                "geometry": shape(c["geometry"]),
            })
    regions.insert(0, {"id": TOP, "name": "Great Britain", "parent": None, "geometry": shapely.union_all(countries)})

    with open(out_path, "w") as f:
        json.dump([{**r, "geometry": mapping(r["geometry"])} for r in regions], f)

    def small(geom):
        simple = shapely.simplify(geom, APP_TOLERANCE, preserve_topology=True)
        # Rounded to 4 places, about 10 m, which is all this precision needs.
        return json.loads(json.dumps(mapping(simple)), parse_float=lambda s: round(float(s), 4))

    with open(asset_path, "w") as f:
        json.dump(
            [{"id": r["id"], "name": r["name"], "parent": r["parent"], "geometry": small(r["geometry"])} for r in regions],
            f,
            separators=(",", ":"),
        )
    print(f"{len(regions)} regions")


if __name__ == "__main__":
    main()
