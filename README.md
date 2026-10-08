# mapmapmap-sources

The map data the [mapmapmap](https://github.com/L-Holmes/APP-map-map-map)
Android app downloads: offline hiking maps, and walking and driving route
graphs, for Great Britain and Italy, and the pipeline that builds them.

The files are not in the git history. Each **release** holds a full set of
regions, and the app always reads the newest:

```
https://github.com/L-Holmes/mapmapmap-sources/releases/latest/download/catalog.json
```

| File | What it is |
| --- | --- |
| `catalog.json` | Every region's files, with sizes and SHA-256, the version (the data's date and the pipeline's revision), and the format |
| `<region>.mbtiles` | Vector map tiles, zooms 8 to 14: roads, paths, public rights of way, waymarked routes, 10 m contours, land and water, car parks with their spaces and fees, each named peak's score and the place it is seen best from, and named waterfalls and valleys (for the app to name walks by) |
| `<region>.graph` | The walking graph the app plans routes on |
| `<region>.driving.graph` | The driving graph the app plans car routes on |
| `<region>.shade.mbtiles`, `<region>.slope.mbtiles` | The hiking map's relief, raster tiles: hill shading (zooms 8 to 12), and steep ground in bands from 25° (zooms 8 to 14); from the Environment Agency's LIDAR in England, OS Terrain 50 elsewhere in Great Britain, the Copernicus DEM everywhere else |
| `overview.mbtiles`, `app-regions.json` | What the app ships inside itself: zooms 0 to 7 everywhere, and the region outlines. The app repository's `tools/refresh-assets.sh` copies them in; the catalogue lists them too (`assets`), and an app with other copies fetches these, so new regions and the low zooms reach it with the maps. |

Regions (Geofabrik's boundaries):

- all of Great Britain, England, Scotland, Wales, and England's 47
  counties. A county is typically 50 to 90 MB; all of Great Britain is
  3.2 GB. The relief adds 5 to 30 MB a county (Lancashire 10, Cumbria 27),
  and an estimated 500 to 700 MB to all of Great Britain.
- Italy, in Geofabrik's five parts: North-West (1.4 GB), North-East (1.3),
  Central (1.0) and Southern Italy (1.0), and Sicily and Sardinia (0.6).
  Italy is a heading in the app, not a download: all of it would be over
  GitHub's 2 GiB a file, and more than a phone wants.

The Alps (by country, and all of Slovenia) and Norway (in its five
landsdeler) are defined too, and were built once (October 2026: the
Austrian Alps 1.6 GB, Northern Norway 1.8 GB, the rest 0.3 to 1.6), but are
not built or published: `BUILT` in `pipeline/areas.py` leaves them out.
Adding one there is all it takes to have it again.

## Updating the maps

```sh
./update-maps.sh
```

That is the whole job. In order, it:

1. finishes an upload that stopped partway, if there is one, and stops
   there: the maps are out (run it again for anything newer);
2. checks whether OpenStreetMap has newer data than what is published, or
   the pipeline has changed since, and stops there if neither (`--force`
   rebuilds anyway);
3. downloads the newest data and rebuilds every region, an area at a time
   (Great Britain, then Italy: about three hours, more the first time, for
   England's LIDAR and the Copernicus DEM);
4. publishes them here as a new release (~18 GB: under two hours at
   3 MB/s, more on a slow line; four files go up at a time).

Each step says what it is doing, how long it usually takes and how long the
run has taken so far; downloads and uploads show their progress. Only one
build or upload runs at a time in this folder: a second says so and stops,
rather than overwriting the first one's files. Anything interrupted picks up
where it left off when run again: a download, an upload, and a build, which
keeps the areas it had finished (from the same data, by the same pipeline)
and carries on with the data it had, not newer. An upload left unfinished
by a pipeline that has changed since is dropped, not finished. Apps notice newer data the next time they
check and offer each region's update; no app release is needed.

Monthly is plenty. It needs, on the machine that runs it:

- Java 21 or newer, and [uv](https://docs.astral.sh/uv/) (Python packages
  install themselves into `data/.venv` the first time)
- the GitHub CLI, `gh`, logged in once with `gh auth login`
- ~24 GB of RAM, and disk: ~100 GB free the first time, ~40 GB after (it
  says so and stops before building if there is less)

Everything it downloads and builds stays in `data/` (gitignored). The first
run also fetches the Environment Agency's LIDAR for England (about half an
hour; kept in `data/src/lidar`, so later runs fetch only squares that
failed), and the Copernicus DEM for Italy (some 3 GB, kept in
`data/src/copernicus`). Temporary files go in `data/tmp`, emptied each
run, not `/tmp`, which may be memory. An area's files are in `data/work/<area>`: Great
Britain's relief's 20 m height grid and what is worked out from it take
about 10 GB, and the other areas' grids are files mostly holes, taking
room only where there is land.
`pipeline/serve.sh` serves `data/out` to a USB-attached phone, to try a
build before publishing it.

### How a release is published

- Every release holds every region under the same names, so `latest` is
  always a complete set. It is made as a draft and published only once every
  file is up, so no app ever sees a catalogue whose files are still
  uploading. While a new release uploads, the previous one stays `latest`.
- The download addresses (`releases/latest/download/<file>`) work only once
  a release is published: GitHub answers them with a redirect to the newest
  published release's file, and with 404 while there is none. There is no
  page at `releases/latest/download/` itself.
- The catalogue's `version` is the OSM data's date and the pipeline's
  revision, a hash of the scripts that shape what is built
  (`pipeline/common.sh`), as in `2026-09-30.b4ed3ea`. So changing the
  pipeline makes a new version even with no newer data, and
  `update-maps.sh` builds and publishes it. An installed region of another
  version is offered its update, which fetches only the files whose
  SHA-256 differs from those it came with (an app from before it kept them
  fetches all of them, once); the old files stay in use until the new ones
  are in and checked.
- The release before the newest is kept, so a download already under way
  finishes; older ones are deleted. Publishing the same data twice replaces
  its release.
- `format` in the catalogue is what an app must understand to use the files
  (the tiles' layers, the graph's layout). Raise `FORMAT` in
  `pipeline/catalog.py` and in the app's `Downloads.kt` together when either
  changes in a way an installed app cannot read: older apps then download
  nothing rather than maps they would get wrong.

# The pipeline

## Sources

| | | Licence |
| --- | --- | --- |
| OpenStreetMap, Geofabrik's United Kingdom and Italy extracts | roads, paths, rights of way, land, names | ODbL: credit "© OpenStreetMap contributors" |
| OS Terrain 50, 50 m height grid for Great Britain | contours, and climb on the walking graph | OGL: credit "Contains OS data © Crown copyright and database right" |
| The Copernicus DEM, GLO-30: 1 arc-second (about 30 m) heights, everywhere but Great Britain | contours, relief, peak scores and climb | Free to use with credit: "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved" |
| Natural Earth, OSM water polygons | low zooms and the sea, fetched by Planetiler | public domain / ODbL |

Geofabrik's United Kingdom is England, Scotland and Wales; Northern Ireland
is in its Ireland extract. So the regions are Great Britain, its three
countries, and England's 47 counties.

The Copernicus DEM is a surface model: in a wood its height is the trees'
tops, near enough, not the ground. On open hills and mountains, what the
relief and the peak scores are for, it is the ground; its contours are
drawn every 20 m (an index every 100 m), as mountain maps draw them, from the heights a little smoothed, and the relief from them
smoothed by a cell, without the speckle of steep ground its trees and
roofs would draw.

## Areas

The pipeline builds an **area** at a time (`pipeline/areas.py`), start to
end, each from its own extract and height grid, and cuts that area's
regions from what it builds:

| Area | Extract | Heights | Grid | |
| --- | --- | --- | --- | --- |
| `gb` | United Kingdom | OS Terrain 50; the Environment Agency's LIDAR in England | OSGB, 20 m (50 m for the graphs' climbs) | built |
| `italy` | Italy | Copernicus DEM | transverse Mercator about 12.8°E, 25 m | built |
| `alps` | Alps | Copernicus DEM | transverse Mercator about 10.8°E, 25 m | defined |
| `slovenia` | Slovenia | Copernicus DEM | transverse Mercator about 15°E, 25 m | defined |
| `norway` | Norway (Svalbard and Jan Mayen left out) | Copernicus DEM | transverse Mercator about 18°E, 30 m | defined |

So the regions of an area join without a seam (their tiles are cut from
the same file), and regions of two areas, where they overlap (the Italian
Alps and North-East Italy, were the Alps built), are separate maps. Graph node ids are OpenStreetMap's,
the same in every area, so the app routes across both. Each grid file has
a `.grid.json` beside it saying where it lies (`pipeline/grid.py`).

## Steps

The regions first, then each area's steps, from its heights to its tiles,
then the overview and the catalogue.

| Script | Makes |
| --- | --- |
| `pipeline/regions.py` | The regions of the areas built (57: 56 to download, and Italy as a heading), each with its area, and their outlines from Geofabrik's index: in full for cutting, and simplified for the app (`app-regions.json`), which is how it tells which region you are in with no signal. |
| `pipeline/terrain.py` | Great Britain: OS Terrain 50 as one 1.4 GB height grid (`dem.npy`), and 10 m contours, traced with contourpy and written as shapefiles in WGS84. |
| `pipeline/copernicus.py` | The other areas: the Copernicus DEM's tiles touching the area's regions, fetched once into `data/src/copernicus`, as the area's height grid (`heights.npy`, 25 or 30 m), and 20 m contours from it, as terrain.py draws them. |
| `pipeline/lidar.py` | Great Britain: a 20 m height grid for the relief (`heights.npy`): the Environment Agency's LIDAR Composite DTM in England, fetched from its WCS at 10 m in 10 km squares (cached in `data/src/lidar`), OS Terrain 50 elsewhere, blended where they meet. Made again only when a square has come since. |
| `pipeline/relief.py` | The relief's raster tiles from the height grid, round the area's regions: hill shading (`shade.mbtiles`, zooms 8 to 12) and steep ground in bands from 25° (`slope.mbtiles`, zooms 8 to 14; the zooms below keep the steepest pixel, so it shows zoomed out). Kept from the last run while the heights and the script are as they were. |
| `pipeline/jut.py` | Each named peak's scores, how impressively it rises above the paths and roads, the sea and the lakes round it, and the place it rises most from (see below), for the hiking tiles' `jut` layer. |
| `pipeline/parking.py` | How far each car park is from the nearest path a walk would use (see below), for the app to leave out the car parks in town. |
| `pipeline/walked.py` | How much each way is walked, as lines, where there is a reference heat tile in `data/src/walked` (see below), for the hiking tiles' `walked` layer. |
| Planetiler, OpenMapTiles profile | The base map: land, water, roads, places, peaks, to z14. |
| `pipeline/hiking/Hiking.java` | A Planetiler profile of our own for what OpenMapTiles leaves out: every path with its UK right of way (`row`), SAC difficulty, faint or private access; waymarked routes from route relations; the contours; every named peak; and every car park the public may use, from zoom 10, with its spaces (mapped, or worked out from its area: see below), fee, and whether it is for customers only; the peak scores `jut.py` works out; and how walked the ways are, from `walked.py`. |
| `pipeline/tiles.py merge` | Both tile sets in one file. A vector tile's layers are a repeated protobuf field, so two tiles' bytes, concatenated, are one tile with both sets of layers. |
| `pipeline/tiles.py cut` | Each of the area's regions' tiles, z8 and up, within ~2 km of its outline, and the area's z0-7. Tiles are deduplicated: the sea is stored once. Outside Great Britain, Planetiler makes tiles only round the regions (`areas.py poly`), not for all the sea and the countries round them. |
| `pipeline/tiles.py overview` | The z0-7 overview the app ships (`overview.mbtiles`), from every area's: where two areas have the same tile, both in one, each layer's features from both and those alike (Natural Earth's) once. |
| `pipeline/graph.py build` | The walking graph for all of an area: walkable ways split at junctions, each edge costed both ways. |
| `pipeline/graph.py build-driving` | The driving graph for all of an area: roads cars may use, and car ferries, costed by speed each way. |
| `pipeline/graph.py cut` | Each region's graph, in the app's binary format (documented in the app's `routing/Graph.kt`). Networks of under 50 edges, mapped without joining anything, are dropped: snapping to one would strand a route. |
| `pipeline/catalog.py` | `catalog.json`: each region's files, sizes and SHA-256, the version (Great Britain's OSM data's date and the pipeline's revision), and the two files the app ships (`assets`). Files in `data/out` of regions no longer built are removed: what is there is what is published. |

### Car parks

The `parking` layer is every `amenity=parking` but those closed to the
public (`access` private, residents, staff, permit, disabled and the like)
and garages, a point each, from zoom 10. OpenStreetMap gives `capacity`
for one car park in ten (UK, September 2026) and `fee` for one in five.
A car park mapped as an area with no capacity has its spaces worked out
from its area, at what a space takes in those that do have one: 24 m² on
the ground (the median of 20,000; within a factor of two of the count for
86% of them), 14 m² along a street, 26 m² a floor in a multi-storey or
underground one whose floors are mapped, and otherwise 6 m² of a
multi-storey's footprint, 12 m² of an underground one's. Those carry
`est` 1, and the app says "About". The layer adds about 5% to the tiles
(Great Britain's hiking tiles 478 MB to 503 MB; Lancashire 47.0 MB to
47.7 MB).

Each car park also has `path_m`, the metres to the nearest path a walk
would use (`pipeline/parking.py`), and the app shows only those within
500 m. Such a path is one walkers may use (not a pavement or a crossing)
that is unpaved, a track or a bridleway, graded or faint; or, whatever it
is made of, out of built-up land (`landuse` residential, retail,
industrial and the like), in a park, a wood, a nature reserve, a common or
open country, or along a canal. So the car parks at the start of a walk
stay, and the supermarket's and the town centre's go.

### Peak scores

How impressively each named peak rises above where someone can stand
round it, and where from, which the app shows over the summit, with a
dotted line to that place zoomed in. It starts from Kai Xu's
[jut](https://peakjut.com/about): how impressively a summit P rises
above a point Q is h·sin θ, h its height above Q's horizon and θ the
angle Q looks up at it, at the Q that makes it most. Adjusted:

- Q must be somewhere a person is, looked for within 10 km. There are
  scores by where: a path or road (any way in the walking or driving
  graph, ferries aside); the sea's edge, at sea level (the coastline's
  sea, so sea lochs count); and a lake's edge, at its water's height
  (lakes, ponds and reservoirs; not rivers or canals), three times over:
  counting any lake of a hectare or more (`lake`), only those of 10 ha
  or more (`lake10`), and only those of 50 ha or more, lakes like
  Buttermere (`lake50`). The app's settings choose which lakes count.
- The steepness that counts is the climb's shape, not only its straight
  line: the distance each third of the height takes, the lowest third
  counted three times and the middle twice, as an angle. So ground rising
  straight from the path counts for more than a level stretch before the
  climb.
- Steepness counts for more: score = jut × (1 + 1.1 / (1 + e^(-0.45
  (steepness - 30°)))), ×1 on gentle ground, ×1.55 at 30°, to ×2.1.

P is the summit (the highest cell within 40 m of the peak as mapped), on
the relief's heights (20 m in Great Britain, 25 or 30 m elsewhere). Each
score is ranked too: the highest within 5 km, and the highest in its
county when nothing within 15 km, over the border either, is higher,
which the app draws bigger, and those within a tenth of the highest
within 5 km, a little bigger. A county is one of England's as the
regions have them, Scotland's council areas and Wales's principal areas;
elsewhere, OpenStreetMap's boundaries at the level a country's counties
are: Italy's provinces, Norway's fylker, Switzerland's cantons, Austria's
states, France's départements and Germany's Landkreise. Slovenia,
Liechtenstein and Monaco, with none mapped between the country and its
municipalities, are each a county of their own. A peak's country is its
county's, or outside every county, the one it is in by Geofabrik's
outline. The scores are in the tiles from zoom 8, each with its peak's
name, height, county and country, which is what the app's list of them
reads. `data/work/<area>/jut/jut.tsv` lists every score with what went
into it.

### How walked

The `walked` layer is read from reference heat tiles: Strava heatmap
tiles, one at a time, fetched with mapmapmap's `PYTHON/get-single-tile.py`
and put in `data/src/walked` under the name it gives them (which says which
tile each is). A tile's palette index is its heat. `walked.py` finds the
core of each line of heat (where it is at least half the heat round it),
read at ~2 m, and gives each bit of it within 1.5 reference pixels (34 m
at zoom 11) to the nearest way of the walking graph; heat within 11 m of a
way is its own too, the core of a line or not (in a town the streets are
closer than the reference tells apart). Along each way the heat is made to
run on: gaps of up to 50 m filled, the heat a median over 51 m, run on to
a junction from 60 m, and ways of up to 120 m between walked ones walked
too. On the Pendle tile, every way under heat of 100 or more gets a line,
and 3% of the line drawn is over no heat at all.
Where people walk and the map has no way (heat 2 or more), the core is
thinned to lines, joined on to the ways they end near; scraps on their own
and lines that only shadow a way are left out. Each line has its heat in
half-octave steps, `mapped` 1 along a way, and `road` 1 along a way cars
use (one of the driving graph's: not a track, path, footway or
driveway). The tiles carry them from zoom 10. The references are hashed into the pipeline's
revision (`common.sh`), so adding one is a new version, which
`update-maps.sh` builds and publishes.

### The walking graph

Cost is metres of flat walking. An edge costs its length times a factor for
its kind of way, plus Naismith's rule for its climb (1 m up costs 8 m
along), each direction separately.

| Way | Factor |
| --- | --- |
| Path, footway, bridleway, track, or any public right of way | 1.0 |
| Steps | 1.2 |
| Quiet roads (residential, service, unclassified) | 1.25 to 1.3 |
| Tertiary, secondary, primary, trunk | 1.6, 2.2, 3.0, 5.0 (1.3 with a pavement) |
| SAC grade 3, 4, 5, 6 | x1.2, x2, x4, x6 |
| Faint trail | x1.5 |

Motorways, `foot=no`, private access without a foot exception, areas and
indoor ways are left out. No factor is under 1, which is what lets the app's
A* use straight-line distance as its heuristic and still find the cheapest
route. Graph node ids are global, so regions that overlap share junctions,
and the app routes across them as one network.

### The driving graph

The same file layout, over roads a car may use. Cost is metres at motorway
speed (110 km/h): an edge's length times 110 over the speed a car makes on
it, so again no factor is under 1. The file's header carries the 110, which
is how the app turns a cost into a driving time.

| Road | km/h |
| --- | --- |
| Motorway, trunk, primary, secondary, tertiary | 110, 90, 70, 60, 50 |
| Their slip roads | 60, 50, 45, 40, 35 |
| Unclassified, residential, other road | 40, 30, 30 |
| Service road, living street | 15, 10 |
| Car ferry (`motorcar` or `motor_vehicle=yes`) | 20 |

A speed limit lowers a road's speed to 90% of the limit when that is lower
(`30 mph`, `GB:nsl_single` and the like; abroad, a country's limits by
kind of road where they are mapped in place of a number, `IT:rural` and
the like: 50 in towns). No road is faster than 110: an Italian motorway's
130 still counts as 110, so a drive there takes a little less than it
says. One-way roads, roundabouts and
motorways cost infinity the wrong way. Private roads, driveways and roads
closed to cars are left out. Turn restrictions are not modelled.

Only roads a car can both reach and leave are kept (strongly connected
components of 50 edges or more): a one-way road into a car park whose way
out is not mapped for cars would otherwise be a trap a route could start
in and never get out of.

For the app's directions, each edge also says what road it is: its number
(`ref`, "A59"; several joined with " / "), its name, its kind (the
`highway` value, or a ferry) and whether it is part of a roundabout
(`junction=roundabout` or `circular`). These come after the file's last
section, flagged in the header's first reserved word, so an app from
before them reads the file as it always did; they add 6 to 8% to a
driving graph (England 439 MB to 467 MB). Each region's file holds only
the names its edges use.

## Sizes (September 2026 data)

| | Tiles | Graph | Total |
| --- | --- | --- | --- |
| All of Great Britain | 2.06 GB | 1.13 GB | 3.2 GB |
| England | 1.33 GB | 0.87 GB | 2.2 GB |
| Scotland | | | 644 MB |
| Wales | | | 252 MB |
| A county | 4 to 80 MB | 1.5 to 76 MB | 6 to 156 MB (Cumbria 75 MB) |

Italy's parts are 0.6 to 1.4 GB (October 2026 data): about 45% tiles,
30% graphs, the rest relief, steep ground mostly.

## Licences and credits

- Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, available under the
  [Open Database Licence](https://opendatacommons.org/licenses/odbl/). The
  `.graph` files are a database derived from OpenStreetMap, and are shared
  here under the same licence.
- Contains OS data © Crown copyright and database right (OS Terrain 50,
  [Open Government Licence](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)).
- Contains Environment Agency LIDAR data © Environment Agency copyright
  and/or database right (LIDAR Composite DTM, Open Government Licence).
- Heights in Italy produced using Copernicus WorldDEM-30
  © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018
  provided under COPERNICUS by the European Union and ESA; all rights
  reserved ([the Copernicus DEM](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model),
  GLO-30, from its [open copy on AWS](https://registry.opendata.aws/copernicus-dem/)).
- Tiles follow the [OpenMapTiles](https://openmaptiles.org/schema/) schema
  (CC BY 4.0), built with [Planetiler](https://github.com/onthegomap/planetiler).
