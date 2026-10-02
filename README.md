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
| `catalog.json` | Every region's files, with sizes and SHA-256, the data's date, and the format version |
| `<region>.mbtiles` | Vector map tiles, zooms 8 to 14: roads, paths, public rights of way, waymarked routes, 10 m contours, land and water, and named waterfalls and valleys (for the app to name walks by) |
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

1. finishes an upload that stopped partway, if there is one;
2. checks whether OpenStreetMap has newer data than what is published, and
   stops there if not (`--force` rebuilds anyway);
3. downloads the newest data and rebuilds every region (about an hour);
4. publishes them here as a new release (about 50 minutes for ~8.5 GB at
   3 MB/s).

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
`pipeline/build.sh` and `pipeline/publish.sh` are its two halves, to run
separately if wanted (`pipeline/build.sh --download-only` just fetches the
data). `pipeline/serve.sh` serves `data/out` to a
USB-attached phone, to try a build before publishing it.

### How a release is published

- Every release holds every region under the same names, so `latest` is
  always a complete set. It is made as a draft and published only once every
  file is up, so no app ever sees a catalogue whose files are still
  uploading. While a new release uploads, the previous one stays `latest`.
- The download addresses (`releases/latest/download/<file>`) work only once
  a release is published: GitHub answers them with a redirect to the newest
  published release's file, and with 404 while there is none. There is no
  page at `releases/latest/download/` itself.
- The catalogue's `version` is the OSM data's date. An installed region
  older than it is offered its update; the old files stay in use until the
  new ones are in and checked.
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
| Planetiler, OpenMapTiles profile | The base map: land, water, roads, places, peaks, to z14. |
| `pipeline/hiking/Hiking.java` | A Planetiler profile of our own for what OpenMapTiles leaves out: every path with its UK right of way (`row`), SAC difficulty, faint or private access; waymarked routes from route relations; the contours. |
| `pipeline/tiles.py merge` | Both tile sets in one file. A vector tile's layers are a repeated protobuf field, so two tiles' bytes, concatenated, are one tile with both sets of layers. |
| `pipeline/tiles.py cut` | Each region's tiles, z8 and up, within ~2 km of its outline, and the z0-7 overview the app ships (`overview.mbtiles`, 1.5 MB). Tiles are deduplicated: the sea is stored once. |
| `pipeline/graph.py build` | The walking graph for all of Great Britain: walkable ways split at junctions, each edge costed both ways. |
| `pipeline/graph.py build-driving` | The driving graph for all of Great Britain: roads cars may use, and car ferries, costed by speed each way. |
| `pipeline/graph.py cut` | Each region's graph, in the app's binary format (documented in the app's `routing/Graph.kt`). Networks of under 50 edges, mapped without joining anything, are dropped: snapping to one would strand a route. |
| `pipeline/catalog.py` | `catalog.json`: each region's files, sizes and SHA-256, and the version (the OSM data's date). |

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
- Tiles follow the [OpenMapTiles](https://openmaptiles.org/schema/) schema
  (CC BY 4.0), built with [Planetiler](https://github.com/onthegomap/planetiler).
