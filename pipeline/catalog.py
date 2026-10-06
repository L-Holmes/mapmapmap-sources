#!/usr/bin/env python3
"""
The download catalogue the app reads: each region's files, sizes and hashes.

    catalog.py <regions.json> <dir of region files> <version>

Writes <dir>/catalog.json. File names are relative: the app fetches them
from wherever it found the catalogue, so the same directory works served
from a laptop or uploaded as a release.

Its "assets" are the two files the app ships inside itself, the region
outlines (app-regions.json) and the z0-7 overview (overview.mbtiles),
with their SHA-256: an app that has other ones fetches these, so regions
and the overview reach it with the maps, not only with an app release.
"""
import hashlib
import json
import os
import sys

# A region on a phone that lacks one is offered it as an update, which
# fetches just that. An app from before a kind fetches it too, and leaves it
# unused (the relief, raster tiles of hill shading and of steep ground,
# came after the rest).
KINDS = ("mbtiles", "graph", "driving.graph", "shade.mbtiles", "slope.mbtiles")
ASSETS = ("app-regions.json", "overview.mbtiles")
# What an app needs to understand these files: the tiles' layers and the
# graph's binary layout. Raise it when either changes in a way an installed
# app cannot read; apps built for an older format then leave the new files
# alone rather than download maps they would draw or route wrongly.
FORMAT = 1


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    regions_path, out_dir, version = sys.argv[1:4]
    regions = []
    for region in json.load(open(regions_path)):
        # A heading (the Alps, Italy, Norway) has nothing of its own to download.
        if not region.get("download", True):
            continue
        files = []
        for kind in KINDS:
            name = f"{region['id']}.{kind}"
            path = os.path.join(out_dir, name)
            files.append({"name": name, "size": os.path.getsize(path), "sha256": sha256(path)})
        regions.append({"id": region["id"], "files": files})
        print(region["id"], sum(f["size"] for f in files) // 1_000_000, "MB")
    # Region files of regions no longer built (an area left out, say) go:
    # what is in <dir> is what gets published.
    listed = {f["name"] for r in regions for f in r["files"]} | set(ASSETS)
    for name in sorted(os.listdir(out_dir)):
        if name.endswith((".mbtiles", ".graph")) and name not in listed:
            print(f"{name}: of no region built now, removed")
            os.remove(os.path.join(out_dir, name))
    assets = [{"name": name, "size": os.path.getsize(os.path.join(out_dir, name)),
               "sha256": sha256(os.path.join(out_dir, name))} for name in ASSETS]
    with open(os.path.join(out_dir, "catalog.json"), "w") as f:
        json.dump({"format": FORMAT, "version": version, "regions": regions, "assets": assets}, f, indent=1)


if __name__ == "__main__":
    main()
