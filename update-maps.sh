#!/usr/bin/env bash
#
# Rebuild and republish every map region. See README.md.
#
# The pipeline is in the app's repository, expected beside this one.
#
set -euo pipefail
cd "$(dirname "$0")"
APP=${MAPMAPMAP_APP:-../mapmapmap}
[[ -x "$APP/tools/update-maps.sh" ]] || {
  echo "error: the app repository is not at $APP; set MAPMAPMAP_APP to where it is" >&2
  exit 1
}
exec "$APP/tools/update-maps.sh" L-Holmes/mapmapmap-sources
