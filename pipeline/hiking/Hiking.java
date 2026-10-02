import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureMerge;
import com.onthegomap.planetiler.Planetiler;
import com.onthegomap.planetiler.Profile;
import com.onthegomap.planetiler.VectorTile;
import com.onthegomap.planetiler.config.Arguments;
import com.onthegomap.planetiler.geo.GeometryException;
import com.onthegomap.planetiler.reader.SourceFeature;
import com.onthegomap.planetiler.reader.osm.OsmElement;
import com.onthegomap.planetiler.reader.osm.OsmRelationInfo;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * The layers a hiking map needs that OpenMapTiles does not carry, built into
 * tiles of their own and merged with the OpenMapTiles ones afterwards (see
 * pipeline/tiles.py).
 *
 *   trail         every way a walker might use: paths, tracks, bridleways,
 *                 steps, and any way carrying a UK right of way. "row" is
 *                 the right of way (footpath, bridleway, restricted_byway,
 *                 byway), "sac" the SAC difficulty grade 1-6, "vis" poor
 *                 trail visibility, "private" access that shuts walkers out.
 *   hiking_route  waymarked routes, from route relations: "network" is
 *                 nwn, rwn or lwn, with the route's name and ref.
 *   contour       10 m contours from OS Terrain 50 (pipeline/terrain.py), "idx"
 *                 on every 50 m.
 *   feature       named places a walk is named after that OpenMapTiles
 *                 leaves out: "class" waterfall (a point) or valley (a
 *                 point, line or area, as mapped), and its "name". Not
 *                 drawn; the app reads them to name a walk.
 *   peak          every named peak, from zoom 10, with its "name" and "ele"
 *                 (metres, a whole number, if it has one). OpenMapTiles'
 *                 mountain_peak keeps only a few per patch below zoom 12,
 *                 not the highest; the app labels these, the highest first.
 *
 *   java -cp planetiler.jar Hiking.java --osm-path=... --contours=<dir> --output=...
 */
public class Hiking implements Profile {

  record Route(long id, String network, String name, String ref) implements OsmRelationInfo {}

  static final Set<String> PATHS = Set.of("path", "footway", "bridleway", "track", "steps", "cycleway", "pedestrian");

  /** An OSM height: "978", "978.4", "978 m", or in feet, "3209 ft" or "3209'". */
  static final Pattern HEIGHT = Pattern.compile("\\s*(-?\\d+(?:\\.\\d+)?)\\s*(m|ft|feet|')?\\s*");

  @Override
  public List<OsmRelationInfo> preprocessOsmRelation(OsmElement.Relation rel) {
    if (rel.hasTag("type", "route") && rel.hasTag("route", "hiking", "foot", "walking")) {
      String network = rel.getString("network", "lwn");
      return List.of(new Route(rel.id(), network, rel.getString("name"), rel.getString("ref")));
    }
    return null;
  }

  @Override
  public void processFeature(SourceFeature sf, FeatureCollector features) {
    if ("contours".equals(sf.getSource())) {
      long ele = sf.getLong("ele");
      boolean idx = sf.getLong("idx") == 1;
      features.line("contour")
        .setAttr("ele", ele)
        .setAttr("idx", idx ? 1 : 0)
        .setMinZoom(idx ? 11 : 13)
        .setMinPixelSize(0)
        .setPixelTolerance(0.4);
      return;
    }
    String name = sf.getString("name");
    if (name != null && sf.hasTag("waterway", "waterfall")) {
      features.pointOnSurface("feature")
        .setAttr("class", "waterfall")
        .setAttr("name", name)
        .setMinZoom(12);
      return;
    }
    if (name != null && sf.isPoint() && sf.hasTag("natural", "peak", "volcano", "hill")) {
      Integer ele = metres(sf.getString("ele"));
      features.point("peak")
        .setAttr("name", name)
        .setAttr("ele", ele)
        .setSortKey(ele == null ? 0 : -ele)
        .setMinZoom(10);
      return;
    }
    if (name != null && sf.hasTag("natural", "valley")) {
      var valley = sf.isPoint() ? features.point("feature")
        : sf.canBePolygon() ? features.polygon("feature")
        : features.line("feature");
      valley
        .setAttr("class", "valley")
        .setAttr("name", name)
        .setMinZoom(12)
        .setMinPixelSize(0);
      return;
    }
    if (!sf.canBeLine() || sf.canBePolygon() && sf.hasTag("area", "yes")) {
      return;
    }
    String highway = sf.getString("highway");
    if (highway == null) {
      return;
    }
    String row = rightOfWay(sf.getString("designation"));
    boolean path = PATHS.contains(highway);
    if (path || row != null) {
      int minZoom = row != null || highway.equals("track") || highway.equals("bridleway") ? 12 : 13;
      var line = features.line("trail")
        .setAttr("highway", highway)
        .setAttr("row", row)
        .setAttr("sac", sacGrade(sf.getString("sac_scale")))
        .setAttr("vis", sf.hasTag("trail_visibility", "bad", "horrible", "no") ? 1 : null)
        .setAttr("private", isPrivate(sf) ? 1 : null)
        .setMinZoom(minZoom)
        .setMinPixelSize(0)
        .setPixelTolerance(0.4);
      if (sf.hasTag("bridge") && !sf.hasTag("bridge", "no")) {
        line.setAttr("brunnel", "bridge");
      } else if (sf.hasTag("tunnel") && !sf.hasTag("tunnel", "no")) {
        line.setAttr("brunnel", "tunnel");
      }
    }
    for (var member : sf.relationInfo(Route.class)) {
      Route route = member.relation();
      int minZoom = switch (route.network()) {
        case "iwn", "nwn" -> 7;
        case "rwn" -> 9;
        default -> 11;
      };
      features.line("hiking_route")
        .setAttr("network", route.network())
        .setAttr("name", route.name())
        .setAttr("ref", route.ref())
        .setMinZoom(minZoom)
        .setMinPixelSize(0)
        .setPixelTolerance(0.4);
    }
  }

  static Integer metres(String ele) {
    if (ele == null) {
      return null;
    }
    var m = HEIGHT.matcher(ele);
    if (!m.matches()) {
      return null;
    }
    double value = Double.parseDouble(m.group(1));
    if (m.group(2) != null && !m.group(2).equals("m")) {
      value *= 0.3048;
    }
    return (int) Math.round(value);
  }

  static String rightOfWay(String designation) {
    if (designation == null) {
      return null;
    }
    return switch (designation) {
      case "public_footpath" -> "footpath";
      case "public_bridleway" -> "bridleway";
      case "restricted_byway" -> "restricted_byway";
      case "byway_open_to_all_traffic", "public_byway", "byway" -> "byway";
      default -> null;
    };
  }

  static Integer sacGrade(String sac) {
    if (sac == null) {
      return null;
    }
    return switch (sac) {
      case "mountain_hiking" -> 2;
      case "demanding_mountain_hiking" -> 3;
      case "alpine_hiking" -> 4;
      case "demanding_alpine_hiking" -> 5;
      case "difficult_alpine_hiking" -> 6;
      default -> null;
    };
  }

  static boolean isPrivate(SourceFeature sf) {
    if (sf.hasTag("foot", "yes", "designated", "permissive")) {
      return false;
    }
    return sf.hasTag("foot", "no", "private") || sf.hasTag("access", "no", "private");
  }

  @Override
  public List<VectorTile.Feature> postProcessLayerFeatures(String layer, int zoom, List<VectorTile.Feature> items)
    throws GeometryException {
    if (layer.equals("feature") || layer.equals("peak")) {
      return items;
    }
    // Join the pieces of each line that share their attributes, so dashes
    // and labels run on across way boundaries.
    return FeatureMerge.mergeLineStrings(items, 0.5, 0.25, 4);
  }

  @Override
  public String name() {
    return "hiking";
  }

  @Override
  public String attribution() {
    return "© OpenStreetMap contributors; contains OS data © Crown copyright and database right";
  }

  public static void main(String[] args) throws Exception {
    Arguments arguments = Arguments.fromArgsOrConfigFile(args);
    Path contours = arguments.file("contours", "contour shapefile directory", Path.of("contours"));
    Planetiler.create(arguments)
      .setProfile(new Hiking())
      .addOsmSource("osm", arguments.inputFile("osm_path", "OSM input file", Path.of("input.osm.pbf")))
      .addShapefileGlobSource("EPSG:4326", "contours", contours, "*.shp", null)
      .overwriteOutput(arguments.file("output", "output file", Path.of("hiking.mbtiles")))
      .run();
  }
}
