#!/usr/bin/env bash
# The whole day (00:00-24:00) on the final morning network, compared with the Zurich daily counts.
#   1  MATSim car and truck trips of the whole MATSim day (to 48:00) through the corrected network
#      (plans_to_sumo.py), vans retyped, boundary trip ends moved to before their junction (trip_ends).
#   2  Scaled to the full population like the morning demand (scale_trips.py: x 1 / sample share,
#      clones +-30 min, free ends moved within 500 m to activity locations), with --wrap: the window
#      is one periodic day, so trips after midnight (24:00-30:00) become the night at its start.
#   3  Routed on the final network with the final run's driver (daily_run route), and simulated with
#      every setting of final.sumocfg for 86400 s, with edgeData for the whole day and every hour.
#   4  Compared with the median daily totals of the typical weekdays (count_check --daily), and the
#      GEH of the daily totals divided by 24 (geh_analysis --daily).
# The signal programs were timed for 07:00-08:00 and are used unchanged all day.
# TEST_FROM/TEST_TO (seconds) simulate only that part of the day, to estimate the runtime.
# SEEDS="23 42" simulates more seeds one after the other (default 23).
# ROUTING_THREADS=4 runs the rerouting device's queries in 4 threads (about 70 % of the wall time at
# full demand single-threaded; 06:00-08:00 took 37 min with one thread).
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$BUNDLE/scripts"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
P="${P:-pipeline}"
OUT="${OUT:-$P/daily}"
MATSIM_DIR="${MATSIM_DIR:-$HOME/Downloads/National}"
SCALE="${SCALE:-10}"
COUNT_YEAR="${COUNT_YEAR:-2026}"
COUNTS_FILE="${COUNTS_FILE:-$HOME/Downloads/sid_dav_verkehrszaehlung_miv_od2031_2026.csv}"
SEEDS="${SEEDS:-23}"
if [ -z "${SUMO_HOME:-}" ]; then
    SUMO_BIN="$(python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("sumo")))')"
    SUMO_HOME="$(cd "$(dirname "$SUMO_BIN")/../share/sumo" && pwd)"
    export SUMO_HOME
fi
PY="$VENV/bin/python"
export PYTHONPATH="$BUNDLE/src"
cd "$SIM"

CORRECTED="$P/correction/zurich.corrected.net.xml"
FINAL_CONFIG="$P/final/final.sumocfg"
FINAL_ROUTES="$("$PY" -c 'import sys, xml.etree.ElementTree as ET; print(ET.parse(sys.argv[1]).find("input/route-files").get("value"))' "$FINAL_CONFIG")"
FINAL_NET="$("$PY" -c 'import sys, xml.etree.ElementTree as ET; print(ET.parse(sys.argv[1]).find("input/net-file").get("value"))' "$FINAL_CONFIG")"
DEMAND="$OUT/demand"
WIDE="$DEMAND/plans.wide.trips.xml"
TRIPS="$DEMAND/day-100.trips.xml"
ROUTES="$DEMAND/day-100.rou.xml"
mkdir -p "$DEMAND" "$OUT/counts"

echo "##### 1 daily demand: MATSim trips 00:00-48:00 #####"
if [ -s "$WIDE" ] && [ -s "$DEMAND/trip-ends.txt" ]; then
    echo "done before: $WIDE"
else
    "$PY" "$SCRIPTS/plans_to_sumo.py" --plans "$MATSIM_DIR/output_plans.xml.gz" --network "$MATSIM_DIR/output_network.xml.gz" \
        --net "$CORRECTED" --output "$WIDE" --margin 0 --from 00:00:00 --to 48:00:00 --modes car,truck \
        --endpoints "$DEMAND/ends.csv" --amenities "$DEMAND/amenities.csv" 2>&1 | tee "$DEMAND/run.txt"
    "$PY" "$SCRIPTS/retype_vans.py" "$WIDE"
    "$PY" -m matsim_sumo.network.trip_ends --net "$CORRECTED" --trips "$WIDE" --ends "$DEMAND/ends.csv" \
        --output "$WIDE" --moves "$DEMAND/trip-ends.csv" 2>&1 | tee "$DEMAND/trip-ends.txt"
fi

echo "##### 2 scaled by $SCALE to the full population, one periodic day #####"
if [ -s "$TRIPS" ]; then
    echo "done before: $TRIPS"
else
    "$PY" "$SCRIPTS/scale_trips.py" --input "$WIDE" --output "$TRIPS" --scale "$SCALE" --window 86400 --wrap \
        --endpoints "$DEMAND/ends.csv" --amenities "$DEMAND/amenities.csv" --net "$CORRECTED" 2>&1 | tee "$DEMAND/scaling.txt"
fi

echo "##### 3 routes on the final network, driver of the final run #####"
if [ -s "$ROUTES" ]; then
    echo "done before: $ROUTES"
else
    "$PY" -m matsim_sumo.network.daily_run route --net "$FINAL_NET" --trips "$TRIPS" --vtypes "$FINAL_ROUTES" \
        --output "$ROUTES" 2>&1 | tee "$DEMAND/routing.txt"
fi

BEGIN="${TEST_FROM:-0}"
END="${TEST_TO:-86400}"
for seed in $SEEDS; do
    if [ -n "${TEST_FROM:-}${TEST_TO:-}" ]; then
        RUN="$OUT/test-$BEGIN-$END-seed$seed"
    else
        RUN="$OUT/seed$seed"
    fi
    echo "##### 3 simulation $BEGIN-$END s, seed $seed -> $RUN #####"
    if [ -s "$RUN/sim.stats.xml" ]; then
        echo "done before: $RUN"
    else
        CONFIG="$("$PY" -m matsim_sumo.network.daily_run config --final "$FINAL_CONFIG" --routes "$ROUTES" \
            --out-dir "$RUN" --begin "$BEGIN" --end "$END")"
        start="$(date +%s)"
        sumo -c "$CONFIG" --seed "$seed" ${ROUTING_THREADS:+--device.rerouting.threads "$ROUTING_THREADS"}
        echo "simulated $BEGIN-$END s in $(( $(date +%s) - start )) s wall time" | tee "$RUN/runtime.txt"
    fi
    [ -n "${TEST_FROM:-}${TEST_TO:-}" ] && continue
    echo "##### 4 daily counts, seed $seed #####"
    mkdir -p "$OUT/counts/seed$seed"
    [ -s "$OUT/counts/counts-$COUNT_YEAR-daily.csv" ] && cp "$OUT/counts/counts-$COUNT_YEAR-daily.csv" "$OUT/counts/seed$seed/"
    "$PY" -m matsim_sumo.network.count_check --daily --config "$RUN/daily.sumocfg" --work-dir "$OUT/counts/seed$seed" \
        --year "$COUNT_YEAR" --raw "$COUNTS_FILE" --edge-data "$RUN/edgedata.xml" \
        --hourly-edge-data "$RUN/edgedata-hourly.xml" 2>&1 | tee "$OUT/counts/seed$seed/run.txt"
done
[ -n "${TEST_FROM:-}${TEST_TO:-}" ] && exit 0

echo "##### 4 GEH of the daily totals / 24 #####"
COUNTS=()
for seed in $SEEDS; do COUNTS+=("$OUT/counts/seed$seed/counts.csv"); done
mkdir -p "$OUT/counts/geh"
"$PY" -m matsim_sumo.network.geh_analysis --daily --counts "${COUNTS[@]}" --out-dir "$OUT/counts/geh" \
    --title "Zurich, 00:00-24:00: daily totals against the $COUNT_YEAR counts (seeds $SEEDS)" | tee "$OUT/counts/geh/run.txt"
