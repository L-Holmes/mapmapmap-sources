"""
Where a height grid, or any grid laid out like one, lies: the pipeline's
grids are numpy arrays of square cells in a projected coordinate system
(metres), row 0 the southmost, the first cell's south-west corner at
(x0, y0).

Each grid's .npy has a .grid.json beside it saying so: its "crs" (what
pyproj takes: "EPSG:27700", or a PROJ string), "x0", "y0", "cell"
(metres), "scale" (metres a stored unit: 0.1 for decimetres), "sea" (a
stored value at or below which a cell is sea, or nothing: nothing to
shade), "source" (whose heights, for the tiles' metadata), and "smooth"
(how many cells' Gaussian the relief blurs them by first: a surface
model's trees and roofs are a speckle of steep ground otherwise). Great
Britain's from before there were any (OS Terrain 50's dem.npy, 50 m
metres; the relief's heights.npy, 20 m decimetres) are known without.
"""
import json
import os

import numpy as np

OSGB = "EPSG:27700"
GB_WIDTH = 700_000
OS_TERRAIN = "OS Terrain 50"


class Grid:
    def __init__(self, crs, x0, y0, cell, rows, cols, scale=1.0, sea=0.0, source="", smooth=0.0):
        self.crs, self.x0, self.y0, self.cell = crs, float(x0), float(y0), float(cell)
        self.rows, self.cols = int(rows), int(cols)
        self.scale, self.sea, self.source, self.smooth = float(scale), float(sea), source, float(smooth)

    @classmethod
    def of(cls, npy_path):
        """The grid an .npy is laid out on: from its .grid.json, or Great Britain's as it always was."""
        shape = np.load(npy_path, mmap_mode="r").shape
        side = sidecar(npy_path)
        if os.path.exists(side):
            g = json.load(open(side))
            return cls(g["crs"], g["x0"], g["y0"], g["cell"], shape[0], shape[1], g.get("scale", 1.0),
                       g.get("sea", 0.0), g.get("source", ""), g.get("smooth", 0.0))
        cell = GB_WIDTH / shape[1]
        if np.load(npy_path, mmap_mode="r").dtype == np.int16:
            return cls(OSGB, 0, 0, cell, *shape, scale=0.1, sea=-25, source=OS_TERRAIN)
        return cls(OSGB, 0, 0, cell, *shape, scale=1.0, sea=-2.5, source=OS_TERRAIN)

    def save(self, npy_path):
        """Writes the .grid.json for an .npy laid out on this grid."""
        with open(sidecar(npy_path), "w") as f:
            json.dump({"crs": self.crs, "x0": self.x0, "y0": self.y0, "cell": self.cell, "scale": self.scale,
                       "sea": self.sea, "source": self.source, "smooth": self.smooth}, f)

    def to_grid(self):
        """A transformer from longitude and latitude to this grid's x and y, metres."""
        import pyproj
        return pyproj.Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)

    def to_wgs(self):
        import pyproj
        return pyproj.Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)

    def open(self, path, dtype, mode="w+"):
        """A new grid file of this shape: sparse on disk, every cell 0 until written."""
        return np.lib.format.open_memmap(path, mode=mode, dtype=dtype, shape=(self.rows, self.cols))


def sidecar(npy_path):
    return npy_path[:-len(".npy")] + ".grid.json" if npy_path.endswith(".npy") else npy_path + ".grid.json"
