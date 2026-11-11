#!/usr/bin/env bash
# One more repair round towards zero teleports, after repair_stuck_junctions.py and one plain run.
#   1  Priority against OpenStreetMap at junctions with repeated yields, a lane continuation for each
#      wrong-lane or no-connection stop, the actuated signal programs carried over link by link, and
#      Webster timing for every signal that has no timed program yet.
#   2  A small behaviour search on that network: only impatience and jmTimegapMinor change. Every
#      setting is simulated with SEEDS and scored on the mean. The teleported share counts
#      TELEPORT_WEIGHT times, and each collision above the SUMO default driver's costs
#      COLLISION_WEIGHT points. Its first individual is the SUMO default driver, the plain-driver check.
#   3  Where, when and which vehicles are still stuck in the best run.
# FROM is the round to repair (its network, one run's log and routes, its signals). TO is written.
# For the next round run again with FROM set to this TO. SKIP_REPAIR=1 reuses TO/repaired.net.xml.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
ROUNDS_DIR="${ROUNDS_DIR:-full-traffic/junction-repair}"
FROM="${FROM:-$ROUNDS_DIR/iteration3}"
TO="${TO:-$ROUNDS_DIR/iteration4}"
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
if [ -s "$FROM/plain.log" ]; then
    LOG="${LOG:-$FROM/plain.log}"
    ROUTES="${ROUTES:-$FROM/plain.rou.xml}"
    DEMAND="${DEMAND:-$FROM/plain.rou.xml}"
else
    LAST="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["best"]["label"])' "$FROM/behaviour/summary.json")"
    LOG="${LOG:-$FROM/behaviour/$LAST/sim.log}"
    ROUTES="${ROUTES:-$FROM/behaviour/$LAST/vehroutes.xml.gz}"
    [ -s "$ROUTES" ] || ROUTES="$FROM/behaviour/routes/routed.rou.xml"
    DEMAND="${DEMAND:-$FROM/behaviour/routes/routed.rou.xml}"
fi
SIGNALS="${SIGNALS:-$FROM/signals.add.xml}"
[ -s "$SIGNALS" ] || SIGNALS="full-traffic/signals/signals.add.xml"
TRIPS="${TRIPS:-plans.full.trips.xml}"
for file in "$FROM/repaired.net.xml" "$LOG" "$ROUTES" "$DEMAND" "$SIGNALS" "$TRIPS"; do
    if [ ! -s "$file" ]; then
        echo "$SIM/$file is missing." >&2
        exit 1
    fi
done
mkdir -p "$TO/behaviour" "$TO/diagnostics"

if [ "${SKIP_REPAIR:-0}" != "1" ]; then
    echo "=== 1 repair $FROM from $LOG ==="
    "$PY" -m matsim_sumo.network.zero_teleport_repair \
        --net "$FROM/repaired.net.xml" --log "$LOG" --routes "$ROUTES" --demand "$DEMAND" \
        --signals "$SIGNALS" --out-dir "$TO" --min-yields "${MIN_YIELDS:-2}" 2>&1 | tee "$TO/repair.txt"
fi

echo "=== 2 behaviour search: impatience and jmTimegapMinor, mean of two seeds, extra collisions penalised ==="
GOOGLE_TIMES="${GOOGLE_TIMES:-full-traffic/behaviour/google-times.json}"
if [ ! -s "$TO/behaviour/google-times.json" ] && [ -s "$GOOGLE_TIMES" ]; then
    cp "$GOOGLE_TIMES" "$TO/behaviour/google-times.json"
fi
"$PY" -m matsim_sumo.network.behaviour_calibration \
    --net "$TO/repaired.net.xml" --additional "$TO/signals.add.xml" \
    --routes "$TRIPS" --work-dir "$TO/behaviour" --teleport "$TELEPORT" --end "${END:-3600}" \
    --timezone "${CITY_TIMEZONE:-Europe/Zurich}" --morning "${DEMAND_FROM:-07:00:00}" \
    --genes "${GENES:-impatience,jmTimegapMinor}" --seeds "${SEEDS:-23,42}" \
    --teleport-weight "${TELEPORT_WEIGHT:-10}" --collision-weight "${COLLISION_WEIGHT:-1}" \
    --population "${POPULATION:-6}" --generations "${GENERATIONS:-2}" \
    --workers "${WORKERS:-2}" 2>&1 | tee "$TO/behaviour/run.txt"

echo "=== 3 where, when and which vehicles are still stuck in the best run ==="
BEST="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["best"]["label"])' "$TO/behaviour/summary.json")"
"$PY" -m matsim_sumo.network.stuck_report --net "$TO/repaired.net.xml" \
    --routes "$TO/behaviour/best.rou.xml" --log "$TO/behaviour/$BEST/sim.log" \
    --output-dir "$TO/diagnostics" 2>&1 | tee "$TO/diagnostics/run.txt"
echo "open the best run in the GUI: sumo-gui -c $SIM/$TO/behaviour/best.sumocfg"
if [[ "$TO" =~ ^(.*[^0-9])([0-9]+)$ ]]; then
    echo "next round: FROM=$TO TO=${BASH_REMATCH[1]}$(( 10#${BASH_REMATCH[2]} + 1 )) bash $BUNDLE/scripts/mac_zero_teleports.sh"
fi
