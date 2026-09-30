#!/usr/bin/env bash
#
# Update the maps everyone downloads: fetch the newest OpenStreetMap data,
# rebuild every region, and publish them here as a new release.
#
#   ./update-maps.sh
#
# Run it by hand whenever the maps should catch up with OpenStreetMap
# (monthly is plenty). Takes about an hour, then uploads ~8.5 GB. No app
# release is needed: apps see the new data the next time they check, and
# offer each region's update. See README.md for what it needs.
#
set -euo pipefail
cd "$(dirname "$0")"
pipeline/build.sh --fresh
pipeline/publish.sh
