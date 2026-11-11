#!/usr/bin/env bash
# Last step of the network correction, after the last mac_zero_teleports.sh round: no teleports.
#   1  Every stuck vehicle of FROM's best behaviour run (both seeds) is placed on its junction and movement.
#   2  The network is forced there: signals or zippers where vehicles waited to give way, protected
#      turns at signals, longer greens or an extra lane for jams, every lane reaching the next edge for
#      wrong-lane stops. Every change is listed in TO/forced.csv.
#   3  Both seeds are simulated with teleporting off, junction blockers ignored after 60 s and
#      collisions as warnings, and watched for vehicles standing still 300 s.
# This trades realism for zero teleports; use it only once the repair rounds stop improving.
# SKIP_NETWORK=1 reuses TO/repaired.net.xml; SKIP_RUN=1 only writes the network and final.sumocfg.
# SETTINGS_ONLY=1 skips step 2: FROM's network with only the no-teleport settings, to compare with.
#   SETTINGS_ONLY=1 TO=full-traffic/junction-repair/iteration6-settings-only bash mac_final_no_teleports.sh
# No Google key is needed: the travel times are compared with FROM's cached Google times.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
ROUNDS_DIR="${ROUNDS_DIR:-full-traffic/junction-repair}"
FROM="${FROM:-$ROUNDS_DIR/iteration5}"
TO="${TO:-$ROUNDS_DIR/iteration6-final}"

if [ -z "${SUMO_HOME:-}" ]; then
    SUMO_BIN="$(python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("sumo")))')"
    SUMO_HOME="$(cd "$(dirname "$SUMO_BIN")/../share/sumo" && pwd)"
    export SUMO_HOME
fi
PY="$VENV/bin/python"
export PYTHONPATH="$BUNDLE/src"

cd "$SIM"
for file in "$FROM/repaired.net.xml" "$FROM/signals.add.xml" "$FROM/behaviour/summary.json" "$FROM/behaviour/best.rou.xml"; do
    if [ ! -s "$file" ]; then
        echo "$SIM/$file is missing." >&2
        exit 1
    fi
done
mkdir -p "$TO"

FLAGS=()
[ "${SKIP_NETWORK:-0}" = "1" ] && FLAGS+=(--skip-network)
[ "${SKIP_RUN:-0}" = "1" ] && FLAGS+=(--skip-run)
[ "${SETTINGS_ONLY:-0}" = "1" ] && FLAGS+=(--settings-only)
echo "=== final step: $FROM -> $TO, no teleports ==="
"$PY" -m matsim_sumo.network.zero_teleport_final --round "$FROM" --out-dir "$TO" \
    --end "${END:-3600}" --workers "${WORKERS:-2}" ${FLAGS[@]+"${FLAGS[@]}"} 2>&1 | tee "$TO/run.txt"
