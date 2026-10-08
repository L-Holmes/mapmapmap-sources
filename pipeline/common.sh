# Shared by the pipeline's scripts (sourced, from the repository root).

REPO=${REPO:-L-Holmes/mapmapmap-sources}
RELEASES="https://github.com/$REPO/releases"
STARTED=${STARTED:-$SECONDS}
export STARTED

# One build or upload at a time in this folder: two would overwrite each
# other's files. A script run by another (update-maps.sh running build.sh)
# shares its parent's hold.
lock() {
  [[ -n "${MAPS_LOCK_HELD:-}" ]] && return
  mkdir -p data
  exec 9>data/.lock
  if ! flock -n 9; then
    echo "error: another map build or upload is already running in this folder" >&2
    echo "       (started by: $(cat data/.lock.who 2>/dev/null || echo unknown)). Wait for it to finish." >&2
    exit 1
  fi
  echo "$0, pid $$, $(date '+%F %T')" > data/.lock.who
  export MAPS_LOCK_HELD=1
}

# The pipeline's revision: the parts of it that shape the files built,
# hashed, with the reference heat tiles the walked layer is read from
# (data/src/walked, pipeline/walked.py). It is in the published version,
# after the map data's date, so a change to the pipeline, or a reference
# added, is a new version: update-maps.sh rebuilds for it even when
# OpenStreetMap has nothing newer, and apps offer it as an update.
revision() {
  cat $(ls pipeline/build.sh pipeline/*.py pipeline/hiking/*.java | sort) \
    $(find data/src/walked -name '*.png' 2>/dev/null | sort) | sha256sum | cut -c1-7
}

elapsed() {
  local s=$((SECONDS - STARTED))
  printf '%dh%02dm' $((s / 3600)) $((s % 3600 / 60))
}

# step <n> <of> <what> <how long it usually takes>
step() {
  printf '\n==> [%s/%s] %s  (usually %s; %s in so far, %s)\n' "$1" "$2" "$3" "$4" "$(elapsed)" "$(date +%H:%M)"
}
