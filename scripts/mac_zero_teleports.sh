#!/usr/bin/env bash
# One driver-behaviour round on a fixed network and its signals (stage 4).
# The network is not edited here: OpenStreetMap priority, lane continuations and
# signal programs belong to stage 3. This round only searches how drivers act on
# the road (car following, lane changing / overtaking, junction behaviour, and
# en-route rerouting), scored on the mean of SEEDS against Google travel times.
#
# FROM_NET / FROM_SIGNALS are the fixed network and signals (defaults: TO's own
# repaired.net.xml and signals.add.xml, which the pipeline copies in first).
# TO is the round folder; results go to TO/behaviour/. START_BEHAVIOUR is an
# earlier round's behaviour/ folder whose best drivers start this search.
# GOOGLE_TIMES is a shared google-times.json so every round compares the same trips.
# TRAVEL_TIME_WEIGHT=0 scores the drivers on gridlocks only, without Google travel times.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
TO="${TO:-full-traffic/zero-teleport/round1}"
TELEPORT="${TELEPORT:-300}"

if [ -z "${GOOGLE_MAPS_API_KEY:-}" ] || [ "$GOOGLE_MAPS_API_KEY" = "PASTE_YOUR_KEY_HERE" ]; then
    echo "Set GOOGLE_MAPS_API_KEY first: export GOOGLE_MAPS_API_KEY=..." >&2
    exit 1
fi
if [ -z "${SUMO_HOME:-}" ]; then
    SUMO_BIN="$(python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("sumo")))')"
    SUMO_HOME="$(cd "$(dirname "$SUMO_BIN")/../share/sumo" && pwd)"
    export SUMO_HOME
fi
PY="$VENV/bin/python"
export PYTHONPATH="$BUNDLE/src"

cd "$SIM"
FROM_NET="${FROM_NET:-$TO/repaired.net.xml}"
FROM_SIGNALS="${FROM_SIGNALS:-$TO/signals.add.xml}"
TRIPS="${TRIPS:-plans.full.trips.xml}"
for file in "$FROM_NET" "$FROM_SIGNALS" "$TRIPS"; do
    if [ ! -s "$file" ]; then
        echo "$SIM/$file is missing." >&2
        exit 1
    fi
done
mkdir -p "$TO/behaviour"
# Keep the round layout expected by stages 5–7 (signal_calibration, assignment, final).
if [ "$(cd "$(dirname "$FROM_NET")" && pwd)/$(basename "$FROM_NET")" != "$(cd "$TO" && pwd)/repaired.net.xml" ]; then
    cp "$FROM_NET" "$TO/repaired.net.xml"
fi
if [ "$(cd "$(dirname "$FROM_SIGNALS")" && pwd)/$(basename "$FROM_SIGNALS")" != "$(cd "$TO" && pwd)/signals.add.xml" ]; then
    cp "$FROM_SIGNALS" "$TO/signals.add.xml"
fi

echo "=== driver behaviour on the fixed network (car-following, lane-changing, junctions, rerouting) ==="
GOOGLE_TIMES="${GOOGLE_TIMES:-}"
if [ -n "$GOOGLE_TIMES" ] && [ -s "$GOOGLE_TIMES" ] && [ ! -s "$TO/behaviour/google-times.json" ]; then
    cp "$GOOGLE_TIMES" "$TO/behaviour/google-times.json"
fi
START_ARGS=()
if [ -n "${START_BEHAVIOUR:-}" ] && [ -s "$START_BEHAVIOUR/summary.json" ]; then
    START_ARGS=(--start "$START_BEHAVIOUR")
fi
# Default: all gene groups. Override with GENES=lane-changing,rerouting,... to narrow the search.
"$PY" -m matsim_sumo.network.behaviour_calibration \
    --net "$TO/repaired.net.xml" --additional "$TO/signals.add.xml" \
    --routes "$TRIPS" --work-dir "$TO/behaviour" --teleport "$TELEPORT" --end "${END:-3600}" \
    --timezone "${CITY_TIMEZONE:-Europe/Zurich}" --morning "${DEMAND_FROM:-07:00:00}" \
    --genes "${GENES:-}" --seeds "${SEEDS:-23,42}" \
    --teleport-weight "${TELEPORT_WEIGHT:-10}" --collision-weight "${COLLISION_WEIGHT:-1}" \
    --travel-time-weight "${TRAVEL_TIME_WEIGHT:-1}" \
    --population "${POPULATION:-6}" --generations "${GENERATIONS:-3}" \
    --workers "${WORKERS:-2}" --carry "${CARRY:-3}" \
    ${SEARCH_SEED:+--search-seed "$SEARCH_SEED"} \
    ${START_ARGS[@]+"${START_ARGS[@]}"} 2>&1 | tee "$TO/behaviour/run.txt"

echo "=== where, when and which vehicles are still stuck in the best run ==="
BEST="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["best"]["label"])' "$TO/behaviour/summary.json")"
mkdir -p "$TO/diagnostics"
"$PY" -m matsim_sumo.network.stuck_report --net "$TO/repaired.net.xml" \
    --routes "$TO/behaviour/best.rou.xml" --log "$TO/behaviour/$BEST/sim.log" \
    --output-dir "$TO/diagnostics" 2>&1 | tee "$TO/diagnostics/run.txt"
echo "open the best run in the GUI: sumo-gui -c $SIM/$TO/behaviour/best.sumocfg"
if [[ "$TO" =~ ^(.*[^0-9])([0-9]+)$ ]]; then
    next_to="${BASH_REMATCH[1]}$((10#${BASH_REMATCH[2]} + 1))"
    printf 'next round: TO=%s START_BEHAVIOUR=%s/behaviour bash %s/scripts/mac_zero_teleports.sh\n' \
        "$next_to" "$TO" "$BUNDLE"
fi
