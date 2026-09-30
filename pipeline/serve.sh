#!/usr/bin/env bash
#
# Serve data/out to the attached phone, to try a build before publishing it.
#
#   pipeline/serve.sh
#   (in the app) ./gradlew -PmapsUrl=http://localhost:8765/ :app:installDebug
#
# `adb reverse` makes the phone's localhost:8765 this machine's; a debug
# build given that address downloads from here. Ctrl-C stops.
#
set -euo pipefail
cd "$(dirname "$0")/../data/out"
PORT=8765
adb reverse tcp:$PORT tcp:$PORT
echo "==> Serving $(pwd) to the phone on localhost:$PORT"
exec python3 -m http.server $PORT --bind 127.0.0.1
