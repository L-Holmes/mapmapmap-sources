#!/usr/bin/env python3
"""
The areas the maps can be built in. Each is built on its own, start to
end, from its own OpenStreetMap extract (Geofabrik's) and its own height
grid, and its regions (pipeline/regions.py) are cut from what it builds:

    gb        Great Britain: OS Terrain 50, and the Environment Agency's
              LIDAR in England (pipeline/terrain.py, pipeline/lidar.py)
    italy     Italy, San Marino and the Vatican

and, defined and tried (October 2026: their parts 0.3 to 1.8 GB each)
but not built, as BUILT leaves them out:

    alps      the Alps, from Geofabrik's Alps extract, which reaches from
              Marseille to Vienna
    slovenia  Slovenia, which is only partly in that
    norway    Norway's mainland; the extract's Svalbard and Jan Mayen are
              left out

To build one, add it to BUILT: its regions, the download, everything else
follows (some 45 minutes and 5 GB more a run for the Alps, 10 minutes for
Slovenia, 45 minutes for Norway, more the first time).

Outside Great Britain the heights are the Copernicus DEM's
(pipeline/copernicus.py), on a grid of the area's own: a transverse
Mercator centred on it, so that a cell is the same size, near enough, all
over it.

    areas.py ids           the areas, in the order they are built
    areas.py shell <area>  the area's settings, as shell variables
    areas.py poly <area> <regions.json> <out.poly>
                           the outline of the area's regions, and a few km
                           round, as Planetiler's --polygon takes it
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from grid import OSGB, Grid  # noqa: E402

COPERNICUS = "the Copernicus DEM (GLO-30)"
COPERNICUS_CREDIT = ("heights from the Copernicus DEM, © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH"
                     " 2014-2018 provided under COPERNICUS by the European Union and ESA")
OS_CREDIT = "contains OS data © Crown copyright and database right"


class Area:
    def __init__(self, id, name, extract, bounds, heights="copernicus", lon0=None, cell=25,
                 interval=20, index=100):
        self.id, self.name, self.extract = id, name, extract
        # West, south, east, north: what tiles are made for, and the grid covers.
        self.bounds = bounds
        self.heights = heights
        self.lon0, self.cell = lon0, cell
        # Contours: every [interval] metres, an index contour every [index].
        self.interval, self.index = interval, index

    @property
    def stem(self):
        """The extract's file name, less its date: "united-kingdom"."""
        return self.extract.rsplit("/", 1)[1]

    @property
    def crs(self):
        if self.heights == "gb":
            return OSGB
        return f"+proj=tmerc +lat_0=0 +lon_0={self.lon0} +k=1 +x_0=0 +y_0=0 +ellps=GRS80 +units=m +no_defs"

    @property
    def source(self):
        return "OS Terrain 50, and the Environment Agency's LIDAR in England" if self.heights == "gb" else COPERNICUS

    @property
    def credit(self):
        return OS_CREDIT if self.heights == "gb" else COPERNICUS_CREDIT

    def grid(self, cell=None):
        """
        The area's grid, of [cell] metres (its own if not given): Great
        Britain's is OSGB's, 700 by 1300 km from its origin; another's, its
        bounds in its transverse Mercator, from a 10 km line.
        """
        cell = cell or self.cell
        if self.heights == "gb":
            return Grid(OSGB, 0, 0, cell, 1_300_000 // cell, 700_000 // cell, sea=-2.5, source=self.source)
        import numpy as np
        import pyproj
        w, s, e, n = self.bounds
        t = pyproj.Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        # Round the edge, which curves in the grid's projection.
        k = np.linspace(0, 1, 200)
        lon = np.concatenate([w + (e - w) * k, np.full(200, e), e - (e - w) * k, np.full(200, w)])
        lat = np.concatenate([np.full(200, s), s + (n - s) * k, np.full(200, n), n - (n - s) * k])
        x, y = t.transform(lon, lat)
        x0, y0 = math.floor(x.min() / 10_000) * 10_000, math.floor(y.min() / 10_000) * 10_000
        rows, cols = math.ceil((y.max() - y0) / cell), math.ceil((x.max() - x0) / cell)
        return Grid(self.crs, x0, y0, cell, rows, cols, scale=0.1, sea=0, source=self.source, smooth=1.0)


DEFINED = {a.id: a for a in [
    Area("gb", "Great Britain", "europe/united-kingdom", (-8.8, 49.8, 2.0, 61.0), heights="gb", cell=20,
         interval=10, index=50),
    Area("alps", "the Alps", "europe/alps", (4.7, 42.55, 16.9, 48.55), lon0=10.8),
    Area("slovenia", "Slovenia", "europe/slovenia", (13.15, 45.25, 16.75, 47.05), lon0=15),
    Area("italy", "Italy", "europe/italy", (6.45, 34.95, 19.3, 47.25), lon0=12.8),
    # Copernicus has 2" of longitude a cell north of 60°: 30 m cells are as fine as it is.
    Area("norway", "Norway", "europe/norway", (2.2, 57.45, 33.8, 72.55), lon0=18, cell=30),
]}
# Those built and published, in the order they are built.
BUILT = ("gb", "italy")
AREAS = {id: DEFINED[id] for id in BUILT}


def main():
    if sys.argv[1] == "ids":
        print(" ".join(AREAS))
    elif sys.argv[1] == "shell":
        a = AREAS[sys.argv[2]]
        print(f"AREA_NAME='{a.name}' EXTRACT={a.extract} STEM={a.stem} HEIGHTS={a.heights}"
              f" BOUNDS={','.join(map(str, a.bounds))} CREDIT='{a.credit}'")
    elif sys.argv[1] == "poly":
        import shapely
        from regions import outline
        area, regions_path, out = sys.argv[2:5]
        shape = shapely.simplify(outline(regions_path, area, 0.05), 0.01)
        with open(out, "w") as f:
            f.write(f"{area}\n")
            for i, polygon in enumerate(shapely.get_parts(shape), 1):
                f.write(f"{i}\n" + "".join(f"   {x:.5f} {y:.5f}\n" for x, y in polygon.exterior.coords) + "END\n")
            f.write("END\n")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
