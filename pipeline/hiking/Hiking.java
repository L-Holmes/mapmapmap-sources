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
import com.onthegomap.planetiler.reader.osm.OsmSourceFeature;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
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
 *   contour       contours (pipeline/terrain.py): every 10 m from OS Terrain
 *                 50 in Great Britain, "idx" on every 50 m; every 20 m from the
 *                 Copernicus DEM elsewhere (pipeline/copernicus.py), "idx" on
 *                 every 100 m.
 *   feature       named places a walk is named after that OpenMapTiles
 *                 leaves out: "class" waterfall (a point) or valley (a
 *                 point, line or area, as mapped), and its "name". Not
 *                 drawn; the app reads them to name a walk.
 *   peak          every named peak, from zoom 10, with its "name" and "ele"
 *                 (metres, a whole number, if it has one). OpenMapTiles'
 *                 mountain_peak keeps only a few per patch below zoom 12,
 *                 not the highest; the app labels these, the highest first.
 *   parking       every car park the public may use, as a point, from zoom
 *                 10: "spaces", its capacity, or for one without, worked
 *                 out from its area ("est" 1); "fee" no, yes, times (paid
 *                 at some times) or donation, where it is mapped; "access"
 *                 customers, for a shop's or a pub's; "kind" multi-storey,
 *                 underground, rooftop, street_side or layby, for any but a
 *                 car park on the ground; its "name"; and "path_m", the
 *                 metres to the nearest path a walk would use, as
 *                 pipeline/parking.py has it (to 10 m, at most 5000), for
 *                 the app to leave out those in town.
 *   jut           each named peak's scores (pipeline/jut.py): how impressively
 *                 it rises above where someone can stand round it, "kind"
 *                 path (a path or road), sea, or lake, lake10 or lake50 (a
 *                 lake of 1, 10 or 50 hectares or more). For each, a point
 *                 at the summit with its "score" (whole metres), "rank" (1,
 *                 the highest within 5 km; 2, the highest in its county,
 *                 with none higher within 15 km over its border either),
 *                 "close" (1, within a tenth of the highest within 5 km),
 *                 and the peak's "name", "ele", "county" and "country" (for
 *                 the app's list of them), from zoom 8, the highest ranks
 *                 and scores first; and from zoom 12 a line from the summit
 *                 to its base, the place it rises most from, and a point
 *                 there, with "kind" and "peak_score", the summit's score.
 *   walked        how much ways are walked, as lines, where there is a
 *                 reference heat tile (pipeline/walked.py), from zoom 10:
 *                 "heat" 1 to 255 (half-octave steps); "mapped" 1 along
 *                 a way the map has, absent where people walk and the map
 *                 has no way; "road" 1 along a way cars use; "park_m", the
 *                 metres on foot from the car park a walker would start
 *                 from, and "loop_m", the shortest round walk from it that
 *                 takes the line in (each to the next 100, where there is one).
 *
 *   java -cp planetiler.jar Hiking.java --osm-path=... --contours=<dir> --jut=<dir> --parking=<tsv> --walked=<dir> --output=...
 */
public class Hiking implements Profile {

  record Route(long id, String network, String name, String ref) implements OsmRelationInfo {}

  static final Set<String> PATHS = Set.of("path", "footway", "bridleway", "track", "steps", "cycleway", "pedestrian");

  /** An OSM height: "978", "978.4", "978 m", or in feet, "3209 ft" or "3209'". */
  static final Pattern HEIGHT = Pattern.compile("\\s*(-?\\d+(?:\\.\\d+)?)\\s*(m|ft|feet|')?\\s*");

  /** A count as mapped: "40", and the odd "~40", "c. 40", "40-ish" or "1000+". */
  static final Pattern COUNT = Pattern.compile("\\s*(?:~|c\\.|ca\\.|approx\\.?)?\\s*(\\d+).*");

  /** Car parks the public may not use: private, residents', staff's, by permit, the disabled's alone. */
  static final Set<String> CLOSED = Set.of("private", "no", "residents", "staff", "employees", "permit", "delivery",
    "disabled", "emergency", "military", "agricultural", "forestry");

  /** Each car park's metres to the nearest path a walk would use (pipeline/parking.py), by [key]. */
  static Map<Long, Integer> PATH_M = Map.of();

  /** Garages and the like, each someone's own. */
  static final Set<String> GARAGES = Set.of("garage_boxes", "garages", "garage", "carports", "sheds");

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
    if ("jut".equals(sf.getSource())) {
      String kind = sf.getString("kind");
      if (sf.hasTag("score")) {
        long score = sf.getLong("score");
        long rank = sf.getLong("rank");
        long close = sf.getLong("close");
        features.point("jut")
          .setAttr("kind", kind)
          .setAttr("score", score)
          .setAttr("rank", rank > 0 ? rank : null)
          .setAttr("close", close > 0 ? 1 : null)
          .setAttr("name", sf.getString("name"))
          .setAttr("ele", sf.getLong("ele"))
          .setAttr("county", blankless(sf.getString("county")))
          .setAttr("country", blankless(sf.getString("country")))
          .setSortKey((int) -(rank * 200_000 + close * 100_000 + Math.min(score, 99_999)))
          .setMinZoom(8);
      } else {
        (sf.isPoint() ? features.point("jut") : features.line("jut"))
          .setAttr("kind", kind)
          .setAttr("peak_score", sf.getLong("peak_score"))
          .setMinZoom(12)
          .setMinPixelSize(0);
      }
      return;
    }
    if ("walked".equals(sf.getSource())) {
      features.line("walked")
        .setAttr("heat", sf.getLong("heat"))
        .setAttr("mapped", sf.getLong("mapped") == 1 ? 1 : null)
        .setAttr("road", sf.getLong("road") == 1 ? 1 : null)
        .setAttr("park_m", sf.getLong("park_m") > 0 ? sf.getLong("park_m") : null)
        .setAttr("loop_m", sf.getLong("loop_m") > 0 ? sf.getLong("loop_m") : null)
        .setMinZoom(10)
        .setMinPixelSize(0);
      return;
    }
    if (sf.hasTag("amenity", "parking")) {
      parking(sf, features);
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

  /**
   * A car park, as a point: on it, for one mapped as an area. Its spaces
   * where they are mapped, which is one car park in ten; for most others,
   * from its area, at the area a space takes in those that are (UK data,
   * September 2026: the median of 20,000 car parks on the ground, 24 m², and
   * within a factor of two of the count for 86% of them; 14 m² a space
   * along a street; a multi-storey's 26 m² a floor, which, with its floors
   * not mapped, comes to 6 m² of its footprint, and 12 m² underground).
   */
  static void parking(SourceFeature sf, FeatureCollector features) {
    String kind = sf.getString("parking", "surface");
    if (CLOSED.contains(sf.getString("access", "")) || GARAGES.contains(kind)
      || !(sf.isPoint() || sf.canBePolygon() || sf.canBeLine())) {
      return;
    }
    kind = switch (kind) {
      case "multi-storey", "underground", "rooftop", "layby" -> kind;
      case "street_side", "lane", "on_kerb", "half_on_kerb", "shoulder" -> "street_side";
      default -> null;
    };
    Integer spaces = count(sf.getString("capacity"));
    boolean estimated = false;
    if (spaces == null && sf.canBePolygon()) {
      Integer levels = count(sf.getString("parking:levels", sf.getString("building:levels")));
      double perSpace = switch (kind == null ? "" : kind) {
        case "street_side" -> 14;
        case "multi-storey" -> levels != null && levels > 0 ? 26.0 / levels : 6;
        case "underground" -> levels != null && levels > 0 ? 26.0 / levels : 12;
        default -> 24;
      };
      try {
        double area = sf.areaMeters();
        if (area > 0) {
          spaces = (int) Math.max(1, Math.round(area / perSpace));
          estimated = true;
        }
      } catch (GeometryException e) {
        // No area to go by.
      }
    }
    features.pointOnSurface("parking")
      .setAttr("spaces", spaces)
      .setAttr("est", estimated ? 1 : null)
      .setAttr("fee", fee(sf))
      .setAttr("access", sf.hasTag("access", "customers") ? "customers" : null)
      .setAttr("kind", kind)
      .setAttr("name", sf.getString("name"))
      .setAttr("path_m", PATH_M.get(key(sf)))
      // The biggest first: drawn under the smaller ones round it, not over them.
      .setSortKey(spaces == null ? 0 : -Math.min(spaces, 100_000))
      .setMinZoom(10);
  }

  /** An OpenStreetMap element's id and type in one number, as pathM() reads them from parking.py's "n123", "w456", "r789". */
  static long key(SourceFeature sf) {
    int type = 2;
    if (sf instanceof OsmSourceFeature<?> osm) {
      var element = osm.originalElement();
      type = element instanceof OsmElement.Node ? 0 : element instanceof OsmElement.Way ? 1 : 2;
    }
    return sf.id() * 3 + type;
  }

  static Map<Long, Integer> pathM(Path tsv) throws IOException {
    Map<Long, Integer> out = new HashMap<>();
    for (String line : Files.readAllLines(tsv)) {
      int tab = line.indexOf('\t');
      int type = "nwr".indexOf(line.charAt(0));
      out.put(Long.parseLong(line.substring(1, tab)) * 3 + type, Integer.parseInt(line.substring(tab + 1)));
    }
    return out;
  }

  static Integer count(String value) {
    if (value == null) {
      return null;
    }
    var m = COUNT.matcher(value);
    if (!m.matches() || m.group(1).length() > 6) {
      return null;
    }
    int n = Integer.parseInt(m.group(1));
    return n > 0 ? n : null;
  }

  /** Whether a car park charges: no, yes, times (some hours, as "Mo-Sa 08:00-18:00"), donation; null if not mapped. */
  static String fee(SourceFeature sf) {
    String fee = sf.getString("fee");
    if (fee == null) {
      return sf.hasTag("charge") ? "yes" : null;
    }
    fee = fee.trim().toLowerCase();
    return switch (fee) {
      case "no", "free", "none" -> "no";
      case "yes", "paid", "pay", "pay_and_display", "pay_and_display;pay_on_exit", "pay_on_exit", "ticket" -> "yes";
      case "donation", "donations" -> "donation";
      default -> fee.matches(".*\\d\\d:\\d\\d.*") ? "times" : null;
    };
  }

  /** A shapefile's text, null for none: its blanks are "". */
  static String blankless(String text) {
    return text == null || text.isBlank() ? null : text;
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
    if (layer.equals("feature") || layer.equals("peak") || layer.equals("parking") || layer.equals("jut")) {
      return items;
    }
    if (layer.equals("walked")) {
      // Joined the same, but none dropped for being short: a way's heat changes along it, and a
      // short stretch of one heat left out would be a gap in the line.
      return FeatureMerge.mergeLineStrings(items, 0, 0.25, 4);
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
    Path jut = arguments.file("jut", "peak score shapefile directory (pipeline/jut.py)", Path.of("jut"));
    Path walked = arguments.file("walked", "how much ways are walked, shapefile directory (pipeline/walked.py)", Path.of("walked"));
    PATH_M = pathM(arguments.file("parking", "car parks' metres to a walk's path (pipeline/parking.py)", Path.of("parking.tsv")));
    Planetiler.create(arguments)
      .setProfile(new Hiking())
      .addOsmSource("osm", arguments.inputFile("osm_path", "OSM input file", Path.of("input.osm.pbf")))
      .addShapefileGlobSource("EPSG:4326", "contours", contours, "*.shp", null)
      .addShapefileGlobSource("EPSG:4326", "jut", jut, "*.shp", null)
      .addShapefileGlobSource("EPSG:4326", "walked", walked, "*.shp", null)
      .overwriteOutput(arguments.file("output", "output file", Path.of("hiking.mbtiles")))
      .run();
  }
}
