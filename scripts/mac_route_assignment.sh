#!/usr/bin/env bash
# Better routes first, no teleports only if needed, after the last mac_zero_teleports.sh round.
#   1  Iterative assignment (SUMO duaIterate.py, ITERATIONS runs) on FROM's network, signals and driver:
#      drivers move to faster routes until travel times settle. Network and demand stay as they are.
#   2  The routes of the iteration with the fewest teleports.
#   3  A check with both seeds, teleporting after 300 s.
#   4  Only if a vehicle is still teleported: the same routes with teleporting off, junction blockers
#      ignored after 60 s and collisions as warnings, counting vehicles that stand still 300 s instead.
# SKIP_ASSIGNMENT=1 reuses TO/assignment; NO_FALLBACK=1 stops after the check (step 4 is left to the caller).
# No Google key is needed: FROM's cached Google times are used.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
ROUNDS_DIR="${ROUNDS_DIR:-full-traffic/junction-repair}"
FROM="${FROM:-$ROUNDS_DIR/iteration5}"
TO="${TO:-$ROUNDS_DIR/iteration7-assigned}"

if [ -z "${SUMO_HOME:-}" ]; then
    SUMO_BIN="$(python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("sumo")))')"
    SUMO_HOME="$(cd "$(dirname "$SUMO_BIN")/../share/sumo" && pwd)"
    export SUMO_HOME
fi
PY="$VENV/bin/python"
export PYTHONPATH="$BUNDLE/src"

cd "$SIM"
TRIPS="${TRIPS:-plans.full.trips.xml}"
for file in "$FROM/repaired.net.xml" "$FROM/signals.add.xml" "$FROM/behaviour/summary.json" "$TRIPS"; do
    if [ ! -s "$file" ]; then
        echo "$SIM/$file is missing." >&2
        exit 1
    fi
done
mkdir -p "$TO"

FLAGS=()
[ "${SKIP_ASSIGNMENT:-0}" = "1" ] && FLAGS+=(--skip-assignment)
[ "${NO_FALLBACK:-0}" = "1" ] && FLAGS+=(--no-fallback)
echo "=== route assignment: $FROM -> $TO, then no teleports if needed ==="
"$PY" -m matsim_sumo.network.route_assignment --round "$FROM" --trips "$TRIPS" --out-dir "$TO" \
    --iterations "${ITERATIONS:-10}" --end "${END:-3600}" --workers "${WORKERS:-2}" ${FLAGS[@]+"${FLAGS[@]}"} \
    2>&1 | tee "$TO/run.txt"
