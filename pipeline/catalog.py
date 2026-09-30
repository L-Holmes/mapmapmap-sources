#!/usr/bin/env python3
"""
The download catalogue the app reads: each region's files, sizes and hashes.

    catalog.py <regions.json> <dir of region files> <version>

Writes <dir>/catalog.json. File names are relative: the app fetches them
from wherever it found the catalogue, so the same directory works served
from a laptop or uploaded as a release.
"""
import hashlib
import json
import os
import sys

KINDS = ("mbtiles", "graph")
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
        files = []
        for kind in KINDS:
            name = f"{region['id']}.{kind}"
            path = os.path.join(out_dir, name)
            files.append({"name": name, "size": os.path.getsize(path), "sha256": sha256(path)})
        regions.append({"id": region["id"], "files": files})
        print(region["id"], sum(f["size"] for f in files) // 1_000_000, "MB")
    with open(os.path.join(out_dir, "catalog.json"), "w") as f:
        json.dump({"format": FORMAT, "version": version, "regions": regions}, f, indent=1)


if __name__ == "__main__":
    main()
