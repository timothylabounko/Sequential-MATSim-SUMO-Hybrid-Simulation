#!/usr/bin/env bash
# Stage 2 and 3 on a Mac, after the repair at sample demand (mac_national_repair.sh with SCALE=1).
#   3a  Cut the National car trips with a 30 min margin around the window and scale them to the
#       full population as Baggenstos (2026) does: factor 1 / sample share from output_config.xml,
#       clones jittered by +-30 min, free trip ends moved within 500 m to agents' homes and workplaces.
#   3b  Simulate the network before the repair at full traffic, for comparison.
#   3c  Repair the repaired network again at full traffic (OpenStreetMap, Google, evolutionary search).
#   3d  Signal timing from the full demand: Webster (tlsCycleAdaptation.py), actuated with detectors,
#       compared with the guessed programs and a coordinated fixed-time plan (tlsCoordinator.py).
#   3e  Calibrate driver behaviour on that network with those signals.
#   3f  Check the best behaviour with a second seed, list the remaining hotspots, break the stuck
#       vehicles down by time, place and vehicle type, and compare with the Zurich traffic counts.
# A vehicle counts as stuck after TELEPORT seconds without moving (default 300).
# SKIP_BEFORE=1, SKIP_REPAIR=1, SKIP_SIGNALS=1 or SKIP_BEHAVIOUR=1 reuse the results of a stage;
# SKIP_COUNTS=1 leaves out the comparison with the traffic counts.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
NATIONAL="${NATIONAL:-$HOME/Downloads/National}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
START="${START:-repair-x1/final.net.xml}"
OUT="${OUT:-full-traffic}"
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
if [ ! -s "$START" ]; then
    echo "$SIM/$START is missing. Finish the repair first: SCALE=1 bash $BUNDLE/scripts/mac_national_repair.sh" >&2
    exit 1
fi
NET="correction/zurich.corrected.net.xml"
[ -s "$NET" ] || NET="$BUNDLE/zurich.corrected.net.xml"
[ -s "$NET" ] || gunzip -k "$NET.gz"
mkdir -p "$OUT/repair" "$OUT/signals" "$OUT/behaviour" "$OUT/diagnostics" "$OUT/counts"

echo "=== 3a demand: cut with a 30 min margin, scale as Baggenstos (2026) ==="
if [ ! -s plans.wide.trips.xml ] || [ ! -s plans.wide.ends.csv ] || [ ! -s plans.amenities.csv ]; then
    "$PY" "$BUNDLE/scripts/plans_to_sumo.py" \
        --plans "$NATIONAL/output_plans.xml.gz" --network "$NATIONAL/output_network.xml.gz" \
        --net "$NET" --output plans.wide.trips.xml --margin 1800 \
        --endpoints plans.wide.ends.csv --amenities plans.amenities.csv
fi
"$PY" "$BUNDLE/scripts/retype_vans.py" plans.wide.trips.xml
if [ -n "${SCALE:-}" ]; then
    FACTOR=(--scale "$SCALE")
else
    CONFIG=""
    while IFS= read -r candidate; do
        case "$candidate" in
            *.gz) reader=(gzip -dc) ;;
            *) reader=(cat) ;;
        esac
        if { "${reader[@]}" "$candidate" 2>/dev/null || true; } | grep -Eq 'name="(sampleSize|flowCapacityFactor)"'; then
            CONFIG="$candidate"
            break
        fi
    done < <(find "$NATIONAL" -maxdepth 3 -type f \( -name 'output_config.xml*' -o -name '*config*.xml' -o -name '*config*.xml.gz' \) 2>/dev/null | sort)
    if [ -z "$CONFIG" ]; then
        echo "No MATSim config with sampleSize or flowCapacityFactor under $NATIONAL." >&2
        echo "Set the factor yourself as 1 / sample share, e.g. SCALE=10 for a 10 % sample." >&2
        exit 1
    fi
    echo "reading the sample share from $CONFIG"
    FACTOR=(--matsim-config "$CONFIG")
fi
"$PY" "$BUNDLE/scripts/scale_trips.py" --input plans.wide.trips.xml --output plans.full.trips.xml \
    "${FACTOR[@]}" --endpoints plans.wide.ends.csv --amenities plans.amenities.csv --net "$NET" \
    2>&1 | tee "$OUT/scaling.txt"

if [ "${SKIP_BEFORE:-0}" != "1" ]; then
    echo "=== 3b network before the repair at full traffic ==="
    mkdir -p "$OUT/before-repair"
    "$PY" -m matsim_sumo.network.load_check --net "$NET" --routes plans.full.trips.xml \
        --work-dir "$OUT/before-repair" --teleport "$TELEPORT" 2>&1 | tee "$OUT/before-repair/run.txt"
fi

if [ "${SKIP_REPAIR:-0}" != "1" ]; then
    echo "=== 3c repaired network at full traffic, repairing again where needed ==="
    "$PY" -m matsim_sumo.network.national_repair \
        --net "$START" --routes plans.full.trips.xml --work-dir "$OUT/repair" --rounds "${ROUNDS:-3}" \
        --end 3600 --teleport "$TELEPORT" 2>&1 | tee "$OUT/repair/run.txt"
fi

if [ "${SKIP_SIGNALS:-0}" != "1" ]; then
    echo "=== 3d signal timing from the full demand (Webster, actuated) ==="
    "$PY" -m matsim_sumo.network.signal_timing \
        --net "$OUT/repair/final.net.xml" --routes plans.full.trips.xml --work-dir "$OUT/signals" \
        --collision-log "$OUT/repair/final.log" --signals "${SIGNALS:-actuated}" \
        --teleport "$TELEPORT" 2>&1 | tee "$OUT/signals/run.txt"
fi

if [ "${SKIP_BEHAVIOUR:-0}" != "1" ]; then
    echo "=== 3e/3f driver behaviour with those signals, then hotspots ==="
    "$PY" -m matsim_sumo.network.behaviour_calibration \
        --net "$OUT/signals/final.net.xml" --additional "$OUT/signals/signals.add.xml" \
        --routes plans.full.trips.xml --work-dir "$OUT/behaviour" \
        --teleport "$TELEPORT" --population "${POPULATION:-6}" --generations "${GENERATIONS:-3}" \
        --workers "${WORKERS:-2}" 2>&1 | tee "$OUT/behaviour/run.txt"
fi

echo "=== 3f where, when and which vehicles are stuck in the best run ==="
BEST="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["best"]["label"])' "$OUT/behaviour/summary.json")"
"$PY" -m matsim_sumo.network.stuck_report --net "$OUT/signals/final.net.xml" \
    --routes "$OUT/behaviour/best.rou.xml" --log "$OUT/behaviour/$BEST/sim.log" \
    --output-dir "$OUT/diagnostics" 2>&1 | tee "$OUT/diagnostics/run.txt"

if [ "${SKIP_COUNTS:-0}" != "1" ]; then
    echo "=== 3f comparison with the Zurich traffic counts, 07:00-08:00 ==="
    "$PY" -m matsim_sumo.network.count_check --config "$OUT/behaviour/best.sumocfg" \
        --work-dir "$OUT/counts" --year "${COUNT_YEAR:-2024}" 2>&1 | tee "$OUT/counts/run.txt"
fi
echo "open the best run in the GUI: sumo-gui -c $SIM/$OUT/behaviour/best.sumocfg"
