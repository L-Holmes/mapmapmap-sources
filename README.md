# mapmapmap-sources

The map data the [mapmapmap](https://github.com/L-Holmes/APP-map-map-map)
Android app downloads: offline hiking maps and walking-route graphs for
Great Britain, and the pipeline that builds them.

The files are not in the git history. Each **release** holds a full set of
regions, and the app always reads the newest:

```
https://github.com/L-Holmes/mapmapmap-sources/releases/latest/download/catalog.json
```

| File | What it is |
| --- | --- |
| `catalog.json` | Every region's files, with sizes and SHA-256, the data's date, and the format version |
| `<region>.mbtiles` | Vector map tiles, zooms 8 to 14: roads, paths, public rights of way, waymarked routes, 10 m contours, land and water |
| `<region>.graph` | The walking graph the app plans routes on |
| `overview.mbtiles`, `app-regions.json` | What the app ships inside itself: zooms 0 to 7 everywhere, and the region outlines. The app repository's `tools/refresh-assets.sh` copies them in. |

Regions: all of Great Britain, England, Scotland, Wales, and England's 47
counties (Geofabrik's boundaries). A county is typically 50 to 90 MB; all of
Great Britain is 3.2 GB.

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
- ~40 GB of disk and ~24 GB of RAM

Everything it downloads and builds stays in `data/` (gitignored).
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
