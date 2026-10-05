# mapmapmap-sources

The map data the [mapmapmap](https://github.com/L-Holmes/APP-map-map-map)
Android app downloads: offline hiking maps, and walking and driving route
graphs, for Great Britain, and the pipeline that builds them.

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
| `<region>.shade.mbtiles`, `<region>.slope.mbtiles` | The hiking map's relief, raster tiles: hill shading (zooms 8 to 12), and steep ground in bands from 25° (zooms 8 to 14); from the Environment Agency's LIDAR in England, OS Terrain 50 elsewhere |
| `overview.mbtiles`, `app-regions.json` | What the app ships inside itself: zooms 0 to 7 everywhere, and the region outlines. The app repository's `tools/refresh-assets.sh` copies them in. |

Regions: all of Great Britain, England, Scotland, Wales, and England's 47
counties (Geofabrik's boundaries). A county is typically 50 to 90 MB; all of
Great Britain is 3.2 GB. The relief adds 5 to 30 MB a county (Lancashire 10,
Cumbria 27), and an estimated 500 to 700 MB to all of Great Britain.

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
3. downloads the newest data and rebuilds every region (about two hours,
   half an hour more the first time, for England's LIDAR);
4. publishes them here as a new release (~12 GB: about an hour at 3 MB/s,
   four on a slow line; four files go up at a time).

Each step says what it is doing, how long it usually takes and how long the
run has taken so far; downloads and uploads show their progress. Only one
build or upload runs at a time in this folder: a second says so and stops,
rather than overwriting the first one's files. Anything interrupted picks up
where it left off when run again. Apps notice newer data the next time they
check and offer each region's update; no app release is needed.

Monthly is plenty. It needs, on the machine that runs it:

- Java 21 or newer, and [uv](https://docs.astral.sh/uv/) (Python packages
  install themselves into `data/.venv` the first time)
- the GitHub CLI, `gh`, logged in once with `gh auth login`
- ~55 GB of disk and ~24 GB of RAM

Everything it downloads and builds stays in `data/` (gitignored). The first
run also fetches the Environment Agency's LIDAR for England (about half an
hour; kept in `data/src/lidar`, so later runs fetch only squares that
failed). The relief's 20 m height grid and what is worked out from it take
about 10 GB of `data/work`.
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
| OpenStreetMap, Geofabrik's United Kingdom extract | roads, paths, rights of way, land, names | ODbL: credit "© OpenStreetMap contributors" |
| OS Terrain 50, 50 m height grid for Great Britain | contours, and climb on the walking graph | OGL: credit "Contains OS data © Crown copyright and database right" |
| Natural Earth, OSM water polygons | low zooms and the sea, fetched by Planetiler | public domain / ODbL |

Geofabrik's United Kingdom is England, Scotland and Wales; Northern Ireland
is in its Ireland extract. So the regions are Great Britain, its three
countries, and England's 47 counties.

## Steps

| Script | Makes |
| --- | --- |
| `pipeline/regions.py` | The 51 regions and their outlines from Geofabrik's index: in full for cutting, and simplified for the app (`app-regions.json`), which is how it tells which region you are in with no signal. |
| `pipeline/terrain.py` | OS Terrain 50 as one 1.4 GB height grid (`dem.npy`), and 10 m contours, traced with contourpy and written as shapefiles in WGS84. |
| `pipeline/lidar.py` | A 20 m height grid for the relief (`heights.npy`): the Environment Agency's LIDAR Composite DTM in England, fetched from its WCS at 10 m in 10 km squares (cached in `data/src/lidar`), OS Terrain 50 elsewhere, blended where they meet. |
| `pipeline/relief.py` | The relief's raster tiles from it: hill shading (`shade.mbtiles`, zooms 8 to 12) and steep ground in bands from 25° (`slope.mbtiles`, zooms 8 to 14; the zooms below keep the steepest pixel, so it shows zoomed out). |
| `pipeline/jut.py` | Each named peak's scores, how impressively it rises above the paths and roads, the sea and the lakes round it, and the place it rises most from (see below), for the hiking tiles' `jut` layer. |
| `pipeline/parking.py` | How far each car park is from the nearest path a walk would use (see below), for the app to leave out the car parks in town. |
| Planetiler, OpenMapTiles profile | The base map: land, water, roads, places, peaks, to z14. |
| `pipeline/hiking/Hiking.java` | A Planetiler profile of our own for what OpenMapTiles leaves out: every path with its UK right of way (`row`), SAC difficulty, faint or private access; waymarked routes from route relations; the contours; every named peak; and every car park the public may use, from zoom 10, with its spaces (mapped, or worked out from its area: see below), fee, and whether it is for customers only; and the peak scores `jut.py` works out. |
| `pipeline/tiles.py merge` | Both tile sets in one file. A vector tile's layers are a repeated protobuf field, so two tiles' bytes, concatenated, are one tile with both sets of layers. |
| `pipeline/tiles.py cut` | Each region's tiles, z8 and up, within ~2 km of its outline, and the z0-7 overview the app ships (`overview.mbtiles`, 1.5 MB). Tiles are deduplicated: the sea is stored once. |
| `pipeline/graph.py build` | The walking graph for all of Great Britain: walkable ways split at junctions, each edge costed both ways. |
| `pipeline/graph.py build-driving` | The driving graph for all of Great Britain: roads cars may use, and car ferries, costed by speed each way. |
| `pipeline/graph.py cut` | Each region's graph, in the app's binary format (documented in the app's `routing/Graph.kt`). Networks of under 50 edges, mapped without joining anything, are dropped: snapping to one would strand a route. |
| `pipeline/catalog.py` | `catalog.json`: each region's files, sizes and SHA-256, and the version (the OSM data's date and the pipeline's revision). |

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

P is the summit (the highest 20 m cell within 40 m of the peak as
mapped), on the relief's 20 m heights. Each score is ranked too: the
highest within 5 km, and the highest in its county (England's counties
as the regions have them; Scotland's council areas and Wales's principal
areas) when nothing within 15 km, over the border either, is higher,
which the app draws bigger, and those within a tenth of the
highest within 5 km, a little bigger. The scores are in the tiles from
zoom 8, each with its peak's name, height, county and country, which is
what the app's list of them reads. `data/work/jut/jut.tsv` lists every
score with what went into it.

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
(`30 mph`, `GB:nsl_single` and the like). One-way roads, roundabouts and
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
- Tiles follow the [OpenMapTiles](https://openmaptiles.org/schema/) schema
  (CC BY 4.0), built with [Planetiler](https://github.com/onthegomap/planetiler).
