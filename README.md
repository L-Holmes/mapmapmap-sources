# mapmapmap-sources

The map data the [mapmapmap](https://github.com/L-Holmes/APP-map-map-map)
Android app downloads: offline hiking maps and walking-route graphs for Great
Britain.

Nothing lives in the git history. Each **release** holds a full set of
regions, and the app always reads the newest:

```
https://github.com/L-Holmes/mapmapmap-sources/releases/latest/download/catalog.json
```

| File | What it is |
| --- | --- |
| `catalog.json` | Every region's files, with sizes and SHA-256, the data's date, and the format version |
| `<region>.mbtiles` | Vector map tiles, zooms 8 to 14: roads, paths, public rights of way, waymarked routes, 10 m contours, land and water |
| `<region>.graph` | The walking graph the app plans routes on |

Regions: all of Great Britain, England, Scotland, Wales, and England's 47
counties (Geofabrik's boundaries). A county is typically 50 to 90 MB; all of
Great Britain is 3.2 GB.

## Updating

```sh
./update-maps.sh
```

That fetches the newest OpenStreetMap data, rebuilds every region (about an
hour, on the machine with the app's repository checked out beside this one,
at `../mapmapmap`), and publishes it here as a new release. Apps notice the
newer data the next time they check and offer each region's update; no app
release is needed. The previous release is kept so downloads already under
way can finish; older ones are deleted.

The pipeline itself lives in the app repository (`tools/`), because the app's
map styles and this data change together.

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
