#!/usr/bin/env bash
# Whole day at 100% of the population, after the morning hour and before any force-no-teleport.
#
# Starts from pipeline-v2/signal-calibration (the 07:00-08:00 network, signals and
# drivers). Ignores pipeline-v2/final. Repeats the same steps as the hour:
# evolutionary repair, signals and junctions, driver behaviour, signal
# calibration, route assignment. Each of those stops on its own when it stops
# improving. Force-no-teleport is not run.
#
#   cd ~/zurich-sim/v2/zurich-repair
#   export GOOGLE_MAPS_API_KEY=...
#   bash scripts/mac_whole_day.sh
set -euo pipefail

if [ -z "${CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
    exec env CAFFEINATED=1 caffeinate -i bash "$0" "$@"
fi

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$BUNDLE/scripts"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
P="${PIPELINE:-pipeline-v2}"
DAY="$P/day"
END="${END:-86400}"
TELEPORT="${TELEPORT:-300}"
LEVEL_ROUNDS="${LEVEL_ROUNDS:-30}"
LEVEL_PATIENCE="${LEVEL_PATIENCE:-3}"
LEVEL_MAX_EFFORT="${LEVEL_MAX_EFFORT:-2}"
NETWORK_WEIGHT="${NETWORK_WEIGHT:-10}"
DAY_WORKERS="${DAY_WORKERS:-2}"
MAX_ROUNDS="${MAX_ROUNDS:-4}"
PATIENCE="${PATIENCE:-2}"
BEHAVIOUR_WORKERS="${BEHAVIOUR_WORKERS:-2}"
export END TELEPORT

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

HOUR="$P/signal-calibration"
if [ ! -s "$HOUR/repaired.net.xml" ] || [ ! -s "$HOUR/signals.add.xml" ] || [ ! -s "$HOUR/behaviour/summary.json" ]; then
    echo "The morning hour has not finished signal calibration yet ($HOUR)." >&2
    echo "Wait until the terminal prints stage 7, then Ctrl-C, then run this again." >&2
    exit 1
fi

TRIPS_SRC="${TRIPS_SRC:-$SIM/pipeline/daily/demand/day-100.trips.xml}"
if [ ! -s "$TRIPS_SRC" ]; then
    echo "Missing the whole-day trips: $TRIPS_SRC" >&2
    exit 1
fi
mkdir -p "$DAY/demand"
TRIPS="$DAY/demand/day-100.trips.xml"
if [ ! -s "$TRIPS" ]; then
    ln -s "$TRIPS_SRC" "$TRIPS"
fi

GOOGLE_CACHE="$P/google-cache.json"
OSM_CACHE="$P/osm-nodes.json"
teleports() { "$PY" -m matsim_sumo.network.pipeline_progress teleports "$1"; }
less_than() { "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)' "$1" "$2"; }
is_zero() { "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) == 0 else 1)' "$1"; }

echo "##### whole day 00:00-24:00, 100% of the population #####"
echo "start (morning hour, before force-no-teleport): $HOUR"
echo "ignoring $P/final"
echo "trips: $TRIPS"
echo "simulation length: ${END}s. Repair stops after ${LEVEL_PATIENCE} rounds without a better score (at most ${LEVEL_ROUNDS})."
echo "Driver search stops after ${PATIENCE} rounds without a lower teleport count (at most ${MAX_ROUNDS})."
echo "Force-no-teleport is not part of this run."

REPAIR="$DAY/repair"
if [ -s "$REPAIR/summary.json" ] && [ -s "$REPAIR/final.net.xml" ]; then
    echo "##### repair: done before #####"
else
    echo "##### repair: evolutionary search on the whole day #####"
    mkdir -p "$REPAIR"
    "$PY" -m matsim_sumo.network.national_repair \
        --net "$HOUR/repaired.net.xml" --routes "$TRIPS" --work-dir "$REPAIR" \
        --rounds "$LEVEL_ROUNDS" --patience "$LEVEL_PATIENCE" --max-effort "$LEVEL_MAX_EFFORT" \
        --network-weight "$NETWORK_WEIGHT" --workers "$DAY_WORKERS" \
        --google-cache "$GOOGLE_CACHE" --osm-cache "$OSM_CACHE" \
        --signals "$HOUR/signals.add.xml" --signals-net "$HOUR/repaired.net.xml" \
        --behaviour "$HOUR/behaviour/summary.json" \
        --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$REPAIR/run.txt"
fi
echo "repair: $(teleports "$REPAIR") vehicles stuck in the best round"

echo "##### stage 3: signal timing (Webster, actuated) #####"
SIGNALS_DIR="$DAY/signals"
if [ -s "$SIGNALS_DIR/summary.json" ] && [ -s "$SIGNALS_DIR/final.net.xml" ]; then
    echo "done before: $SIGNALS_DIR"
else
    mkdir -p "$SIGNALS_DIR"
    "$PY" -m matsim_sumo.network.signal_timing --net "$REPAIR/final.net.xml" --routes "$TRIPS" \
        --work-dir "$SIGNALS_DIR" --collision-log "$REPAIR/final.log" --signals actuated \
        --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$SIGNALS_DIR/run.txt"
fi

echo "##### stage 3: persistent junctions #####"
JUNCTIONS="$DAY/junctions"
if [ -s "$JUNCTIONS/plain.stats.xml" ]; then
    echo "done before: $JUNCTIONS"
else
    mkdir -p "$JUNCTIONS"
    "$PY" -m matsim_sumo.network.persistent_junctions --net "$SIGNALS_DIR/final.net.xml" \
        --signals "$SIGNALS_DIR/signals.add.xml" --trips "$TRIPS" \
        --out-dir "$JUNCTIONS" --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$JUNCTIONS/run.txt"
fi

echo "##### stage 3: OpenStreetMap priority and lane continuations #####"
INTERSECTIONS="$DAY/intersections"
if [ -s "$INTERSECTIONS/plain.stats.xml" ]; then
    echo "done before: $INTERSECTIONS"
else
    mkdir -p "$INTERSECTIONS"
    "$PY" -m matsim_sumo.network.zero_teleport_repair \
        --net "$JUNCTIONS/repaired.net.xml" --log "$JUNCTIONS/plain.log" \
        --routes "$JUNCTIONS/plain.rou.xml" --demand "$JUNCTIONS/plain.rou.xml" \
        --signals "$JUNCTIONS/signals.add.xml" --out-dir "$INTERSECTIONS" \
        --trips "$TRIPS" --end "$END" --teleport "$TELEPORT" \
        --min-yields "${MIN_YIELDS:-2}" 2>&1 | tee "$INTERSECTIONS/repair.txt"
fi
STAGE3="$INTERSECTIONS"
[ -s "$STAGE3/repaired.net.xml" ] || STAGE3="$JUNCTIONS"
echo "stage 3: $(teleports "$STAGE3") vehicles teleported on the fixed network"

echo "##### stage 4: driver behaviour, against Google travel times #####"
BEST=""
BEST_TELEPORTS=""
lowest="$(teleports "$STAGE3")"
stale=0
start_behaviour="$HOUR/behaviour"
google_times=""
[ -s "$HOUR/behaviour/google-times.json" ] && google_times="$HOUR/behaviour/google-times.json"
for round in $(seq 1 "$MAX_ROUNDS"); do
    to="$DAY/zero-teleport/round$round"
    if [ -s "$to/behaviour/summary.json" ]; then
        echo "done before: $to"
    else
        mkdir -p "$to/behaviour"
        cp "$STAGE3/repaired.net.xml" "$to/repaired.net.xml"
        cp "$STAGE3/signals.add.xml" "$to/signals.add.xml"
        FROM_NET="$to/repaired.net.xml" FROM_SIGNALS="$to/signals.add.xml" TO="$to" TRIPS="$TRIPS" \
            START_BEHAVIOUR="$start_behaviour" GOOGLE_TIMES="$google_times" \
            TRAVEL_TIME_WEIGHT=1 WORKERS="$BEHAVIOUR_WORKERS" END="$END" TELEPORT="$TELEPORT" \
            bash "$SCRIPTS/mac_zero_teleports.sh"
    fi
    current="$(teleports "$to")"
    echo "stage 4, round $round: $current vehicles teleported (lowest so far: $lowest)"
    if [ -z "$BEST" ] || less_than "$current" "$BEST_TELEPORTS"; then
        BEST="$to"
        BEST_TELEPORTS="$current"
    fi
    if less_than "$current" "$lowest"; then
        lowest="$current"
        stale=0
    else
        stale=$((stale + 1))
    fi
    if is_zero "$current"; then
        echo "no vehicle teleported: stage 4 is done"
        break
    fi
    if [ "$stale" -ge "$PATIENCE" ]; then
        echo "no new lowest count in $PATIENCE rounds: stage 4 stops"
        break
    fi
    start_behaviour="$BEST/behaviour"
    [ -s "$BEST/behaviour/google-times.json" ] && google_times="$BEST/behaviour/google-times.json"
done
echo "best behaviour round $BEST with $BEST_TELEPORTS teleported vehicles"

echo "##### stage 5: signal calibration #####"
CALIBRATION="$DAY/signal-calibration"
if [ -s "$CALIBRATION/behaviour/summary.json" ]; then
    echo "done before: $CALIBRATION"
else
    mkdir -p "$CALIBRATION"
    "$PY" -m matsim_sumo.network.signal_calibration --round "$BEST" --out-dir "$CALIBRATION" \
        --iterations "${CALIBRATION_ITERATIONS:-8}" --end "$END" --teleport "$TELEPORT" \
        --workers "${CALIBRATION_WORKERS:-2}" 2>&1 | tee "$CALIBRATION/run.txt"
fi
CALIBRATED="$(teleports "$CALIBRATION")"
echo "stage 5: $CALIBRATED vehicles teleported with the calibrated signals"

if is_zero "$CALIBRATED"; then
    echo "##### no teleports after stage 5. Route assignment is not needed. Force-no-teleport was not run. #####"
    echo "result: $CALIBRATION"
    exit 0
fi

echo "##### stage 6: iterative route assignment #####"
ASSIGNMENT="$DAY/assignment"
if [ -s "$ASSIGNMENT/assignment.json" ]; then
    echo "done before: $ASSIGNMENT"
else
    FROM="$CALIBRATION" TO="$ASSIGNMENT" TRIPS="$TRIPS" NO_FALLBACK=1 ITERATIONS="${ITERATIONS:-10}" \
        END="$END" bash "$SCRIPTS/mac_route_assignment.sh"
fi
echo "stage 6: $(teleports "$ASSIGNMENT") vehicles teleported with the assigned routes"
echo "##### stopped before force-no-teleport. Result: $ASSIGNMENT #####"
