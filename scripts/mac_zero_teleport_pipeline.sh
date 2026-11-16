#!/usr/bin/env bash
# The whole network correction for one city in one call, from the unedited SUMO network (or OpenStreetMap)
# and a MATSim scenario to a run in which no vehicle is teleported:
#     bash scripts/mac_zero_teleport_pipeline.sh cities/<city>.toml      (default: cities/zurich.toml)
# Everything that differs between cities is in the city file; the steps are the same for every city.
#   Demand   MATSim trips through the network (plans_to_sumo.py); a trip that crosses the boundary and starts
#            or ends past the junction where it branches off is moved to before it (trip_ends.py).
#   Stage 1  Filtering rules on the unedited network, then the OpenStreetMap step and the evolutionary repair
#            with the MATSim sample as it is. Repair rounds, each from the lowest stuck count so far, go on
#            until no vehicle is stuck or LEVEL_PATIENCE rounds (default 2) bring no new lowest count; a round
#            after a search that found nothing searches wider (more approaches, more generations, another
#            seed). FIRST_ROUNDS (default 10) only caps the rounds. final.net.xml is the best round's network.
#   Stage 2  The same repair, with the same rule, at every higher demand level of the city file (default
#            25 %, 50 %, 100 % of the population), each level starting from the best network of the level
#            before; LEVEL_ROUNDS (default 10) caps the rounds. Teleporting stays on at every level: a vehicle
#            counts as stuck after 300 s, so the shares of all levels are measured the same way.
#   Stage 3  Signals and intersections at full demand: Webster signal timing; persistent junctions
#            (short links joined, lane continuations, lane-change bans lifted from default-driver seeds 23/42);
#            OpenStreetMap priority and any further lane continuations at junctions with repeated yields
#            (zero_teleport_repair.py), with signal programs carried over and Webster for new signals; then
#            one run with the SUMO default driver. The network and signals are fixed after this stage.
#   Stage 4  Driver behaviour on that fixed network (mac_zero_teleports.sh): rounds of an evolutionary search
#            over car-following, lane changing / overtaking, junction behaviour and en-route rerouting
#            (behaviour_calibration.py), scored on the mean of seeds 23 and 42 against Google travel times.
#            Each round starts from the best drivers so far; stop at 0 teleports, PATIENCE rounds (default 2)
#            without a new lowest count, or MAX_ROUNDS (default 4).
#   Stage 5  Signal calibration on the best behaviour round (signal_calibration.py): Webster again with the
#            flows of the last accepted run, kept only when teleported and not inserted vehicles fall
#            (CALIBRATION_ITERATIONS, default 8).
#   Stage 6  Iterative route assignment on the calibrated signals (mac_route_assignment.sh), two seeds.
#   Stage 7  Only if vehicles are still teleported: teleporting off on the calibrated network, signals and
#            driver, counting vehicles that stand still 300 s instead (mac_final_no_teleports.sh,
#            SETTINGS_ONLY=1; FORCE_NETWORK=1 uses the forced network instead).
#   Counts   The final run with both seeds against the traffic counts of the city file (GEH, line of best fit).
#            The counts are never used by a stage; stage 5 only reports the GEH of its runs.
# Everything is written to <work_dir>/PIPELINE (default pipeline). A stage whose result is already there is
# skipped, so the same command continues after an interruption; use a new PIPELINE for a fresh run.
# progress.csv lists the teleported share after every iteration. Needs GOOGLE_MAPS_API_KEY.
set -euo pipefail

if [ -z "${CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
    exec env CAFFEINATED=1 caffeinate -i bash "$0" "$@"
fi

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$BUNDLE/scripts"
CITY_FILE="$(cd "$(dirname "${1:-$BUNDLE/cities/zurich.toml}")" && pwd)/$(basename "${1:-$BUNDLE/cities/zurich.toml}")"
VENV="${VENV:-$HOME/zurich-sim/venv}"
P="${PIPELINE:-pipeline}"
MAX_ROUNDS="${MAX_ROUNDS:-4}"
PATIENCE="${PATIENCE:-2}"
FIRST_ROUNDS="${FIRST_ROUNDS:-10}"
LEVEL_ROUNDS="${LEVEL_ROUNDS:-10}"
LEVEL_PATIENCE="${LEVEL_PATIENCE:-2}"
export TELEPORT=300

if [ -z "${GOOGLE_MAPS_API_KEY:-}" ] || [ "$GOOGLE_MAPS_API_KEY" = "PASTE_YOUR_KEY_HERE" ]; then
    echo "Set GOOGLE_MAPS_API_KEY first: export GOOGLE_MAPS_API_KEY=..." >&2
    exit 1
fi
if [ -z "${SUMO_HOME:-}" ]; then
    SUMO_BIN="$(python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("sumo")))')"
    SUMO_HOME="$(cd "$(dirname "$SUMO_BIN")/../share/sumo" && pwd)"
    export SUMO_HOME
fi
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -c 'import sumolib, pyproj' 2>/dev/null || "$VENV/bin/pip" install -q sumolib pyproj
PY="$VENV/bin/python"
export PYTHONPATH="$BUNDLE/src"
export VENV

CITY_ENV="$("$PY" -m matsim_sumo.network.city_config "$CITY_FILE" --env)"
eval "$CITY_ENV"
export SIM="$WORK_DIR"
END="$("$PY" -c 'import sys
def s(t): h, m, *r = (t.split(":") + ["0"])[:3]; return int(h) * 3600 + int(m) * 60 + int(float(r[0] if r else 0))
print(s(sys.argv[2]) - s(sys.argv[1]))' "$DEMAND_FROM" "$DEMAND_TO")"
export END
mkdir -p "$SIM/$P"
cd "$SIM"
echo "city: $CITY_NAME ($CITY_FILE), work folder $SIM/$P, simulated $DEMAND_FROM-$DEMAND_TO ($END s)"

teleports() { "$PY" -m matsim_sumo.network.pipeline_progress teleports "$1"; }
less_than() { "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)' "$1" "$2"; }
is_zero() { "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) == 0 else 1)' "$1"; }

echo "##### network #####"
NETWORK="$P/network"
mkdir -p "$NETWORK"
if [ -n "$SUMO_NET" ]; then
    NET="$SUMO_NET"
    OSM="$OSM_FILE"
    if [[ "$NET" == *.gz ]]; then
        [ -s "$NETWORK/source.net.xml" ] || gunzip -c "$NET" > "$NETWORK/source.net.xml"
        NET="$NETWORK/source.net.xml"
    fi
    if [[ "$OSM" == *.gz ]]; then
        [ -s "$NETWORK/source.osm.xml" ] || gunzip -c "$OSM" > "$NETWORK/source.osm.xml"
        OSM="$NETWORK/source.osm.xml"
    fi
else
    NET="$NETWORK/$CITY_NAME.net.xml"
    OSM="$NETWORK/$CITY_NAME.osm.xml"
    if [ ! -s "$NET" ]; then
        BUILD=(--output "$NET" --bbox "$CITY_AREA")
        [ "$CITY_LEFTHAND" = "1" ] && BUILD+=(--lefthand)
        [ "$CITY_GERMAN_URBAN_SPEEDS" = "1" ] || BUILD+=(--no-german-urban-speeds)
        "$PY" -m matsim_sumo.network.osm_zurich "${BUILD[@]}" 2>&1 | tee "$NETWORK/run.txt"
    fi
fi
echo "unedited network: $NET"

echo "##### stage 1: filtering rules #####"
CORRECTION="$P/correction"
CORRECTED="$CORRECTION/zurich.corrected.net.xml"
if [ -s "$CORRECTED" ]; then
    echo "done before: $CORRECTION"
else
    mkdir -p "$CORRECTION"
    FILTER=(--source "$NET" --output-dir "$CORRECTION")
    [ -n "$OSM" ] && FILTER+=(--osm "$OSM")
    "$PY" -m matsim_sumo.network.zurich_correction "${FILTER[@]}" 2>&1 | tee "$CORRECTION/run.txt"
fi

echo "##### demand: MATSim trips through the network, $DEMAND_FROM-$DEMAND_TO with a 30 min margin #####"
DEMAND="$P/demand"
WIDE="$DEMAND/plans.wide.trips.xml"
mkdir -p "$DEMAND"
if [ -s "$WIDE" ]; then
    echo "done before: $WIDE"
else
    "$PY" "$SCRIPTS/plans_to_sumo.py" --plans "$MATSIM_PLANS" --network "$MATSIM_NETWORK" --net "$CORRECTED" \
        --output "$WIDE" --margin 1800 --from "$DEMAND_FROM" --to "$DEMAND_TO" --modes "$DEMAND_MODES" \
        --endpoints "$DEMAND/ends.csv" --amenities "$DEMAND/amenities.csv" 2>&1 | tee "$DEMAND/run.txt"
    "$PY" "$SCRIPTS/retype_vans.py" "$WIDE"
    "$PY" -m matsim_sumo.network.trip_ends --net "$CORRECTED" --trips "$WIDE" --ends "$DEMAND/ends.csv" \
        --output "$WIDE" --moves "$DEMAND/trip-ends.csv" 2>&1 | tee "$DEMAND/trip-ends.txt"
fi
if [ "$SAMPLE_SHARE" = "0" ]; then
    CONFIG="$MATSIM_CONFIG"
    if [ -z "$CONFIG" ]; then
        CONFIG="$(find "$MATSIM_DIR" -maxdepth 3 -type f \( -name 'output_config.xml*' -o -name '*config*.xml' -o -name '*config*.xml.gz' \) 2>/dev/null | sort | head -1)"
    fi
    [ -n "$CONFIG" ] || { echo "No MATSim config in $MATSIM_DIR: set demand.sample_share in $CITY_FILE" >&2; exit 1; }
    SAMPLE_SHARE="$(PYTHONPATH="$SCRIPTS:$PYTHONPATH" "$PY" -c 'import sys; from scale_trips import sample_share; print(sample_share(__import__("pathlib").Path(sys.argv[1]))[0])' "$CONFIG")"
fi
LEVELS=($("$PY" -c 'import sys
share = float(sys.argv[1]); levels = [float(x) for x in sys.argv[2:]]
print(" ".join(f"{x:g}" for x in [share] + [x for x in levels if x > share + 1e-9]))' "$SAMPLE_SHARE" $DEMAND_LEVELS))
echo "MATSim sample: $SAMPLE_SHARE of the population; demand levels: ${LEVELS[*]}"

START="$CORRECTED"
LEVEL_DIRS=()
for index in "${!LEVELS[@]}"; do
    level="${LEVELS[$index]}"
    name="$("$PY" -c 'import sys; print(f"level-{round(float(sys.argv[1]) * 100):03d}")' "$level")"
    factor="$("$PY" -c 'import sys; print(f"{float(sys.argv[1]) / float(sys.argv[2]):g}")' "$level" "$SAMPLE_SHARE")"
    WORK="$P/$name"
    TRIPS_LEVEL="$DEMAND/$name.trips.xml"
    if [ "$index" = "0" ]; then
        echo "##### stage 1: repair with the MATSim sample ($name, factor $factor) #####"
        rounds="$FIRST_ROUNDS"
    else
        echo "##### stage 2: repair at $((10#${name#level-})) % of the population (factor $factor), from $START #####"
        rounds="$LEVEL_ROUNDS"
    fi
    if [ ! -s "$TRIPS_LEVEL" ]; then
        SCALING=(--input "$WIDE" --output "$TRIPS_LEVEL" --scale "$factor" --window "$END")
        if less_than 1 "$factor"; then
            SCALING+=(--endpoints "$DEMAND/ends.csv" --amenities "$DEMAND/amenities.csv" --net "$CORRECTED")
        fi
        "$PY" "$SCRIPTS/scale_trips.py" "${SCALING[@]}" 2>&1 | tee "$DEMAND/$name.scaling.txt"
    fi
    if [ -s "$WORK/summary.json" ] && [ -s "$WORK/final.net.xml" ]; then
        echo "done before: $WORK"
    else
        mkdir -p "$WORK"
        "$PY" -m matsim_sumo.network.national_repair --net "$START" --routes "$TRIPS_LEVEL" --work-dir "$WORK" \
            --rounds "$rounds" --patience "$LEVEL_PATIENCE" --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$WORK/run.txt"
    fi
    echo "$name: $(teleports "$WORK") vehicles stuck in the best round (final.net.xml)"
    LEVEL_DIRS+=("$WORK")
    START="$WORK/final.net.xml"
    FULL_TRIPS="$TRIPS_LEVEL"
    LAST_LEVEL="$WORK"
done
export TRIPS="$FULL_TRIPS"

if [ "${SKIP_BEFORE:-0}" != "1" ] && [ ! -s "$P/before-repair/summary.json" ]; then
    echo "##### the unrepaired network at full demand, for comparison #####"
    mkdir -p "$P/before-repair"
    "$PY" -m matsim_sumo.network.load_check --net "$CORRECTED" --routes "$FULL_TRIPS" --work-dir "$P/before-repair" \
        --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$P/before-repair/run.txt"
fi

echo "##### stage 3: signal timing (Webster, actuated) at full demand #####"
SIGNALS_DIR="$P/signals"
if [ -s "$SIGNALS_DIR/summary.json" ] && [ -s "$SIGNALS_DIR/final.net.xml" ]; then
    echo "done before: $SIGNALS_DIR"
else
    mkdir -p "$SIGNALS_DIR"
    "$PY" -m matsim_sumo.network.signal_timing --net "$LAST_LEVEL/final.net.xml" --routes "$FULL_TRIPS" \
        --work-dir "$SIGNALS_DIR" --collision-log "$LAST_LEVEL/final.log" --signals actuated \
        --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$SIGNALS_DIR/run.txt"
fi
COUNT_REFERENCE=""
if [ "$COUNTS" = "zurich" ] && [ "${SKIP_COUNTS:-0}" != "1" ]; then
    mkdir -p "$P/counts"
    COUNT_ARGS=(--work-dir "$P/counts" --year "$COUNT_YEAR")
    [ -n "$COUNTS_FILE" ] && COUNT_ARGS+=(--raw "$COUNTS_FILE")
    if "$PY" -m matsim_sumo.network.count_check "${COUNT_ARGS[@]}" --reduce-only 2>&1 | tee "$P/counts/reference.txt"; then
        COUNT_REFERENCE="$P/counts/counts-$COUNT_YEAR-07h.csv"
    else
        echo "the traffic counts could not be read; the pipeline goes on without them"
    fi
fi

echo "##### stage 3: persistent junctions (default-driver seeds), then SUMO default run #####"
JUNCTIONS="$P/junctions"
if [ -s "$JUNCTIONS/plain.stats.xml" ]; then
    echo "done before: $JUNCTIONS"
else
    mkdir -p "$JUNCTIONS"
    "$PY" -m matsim_sumo.network.persistent_junctions --net "$SIGNALS_DIR/final.net.xml" \
        --signals "$SIGNALS_DIR/signals.add.xml" --trips "$FULL_TRIPS" \
        --out-dir "$JUNCTIONS" --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$JUNCTIONS/run.txt"
fi

echo "##### stage 3: OpenStreetMap priority and lane continuations at yield junctions #####"
INTERSECTIONS="$P/intersections"
if [ -s "$INTERSECTIONS/plain.stats.xml" ]; then
    echo "done before: $INTERSECTIONS"
else
    mkdir -p "$INTERSECTIONS"
    "$PY" -m matsim_sumo.network.zero_teleport_repair \
        --net "$JUNCTIONS/repaired.net.xml" --log "$JUNCTIONS/plain.log" \
        --routes "$JUNCTIONS/plain.rou.xml" --demand "$JUNCTIONS/plain.rou.xml" \
        --signals "$JUNCTIONS/signals.add.xml" --out-dir "$INTERSECTIONS" \
        --trips "$FULL_TRIPS" --end "$END" --teleport "$TELEPORT" \
        --min-yields "${MIN_YIELDS:-2}" 2>&1 | tee "$INTERSECTIONS/repair.txt"
fi
STAGE3="$INTERSECTIONS"
[ -s "$STAGE3/repaired.net.xml" ] || STAGE3="$JUNCTIONS"
PREVIOUS_TELEPORTS="$(teleports "$STAGE3" 2>/dev/null || teleports "$JUNCTIONS")"
echo "stage 3: $PREVIOUS_TELEPORTS vehicles teleported on the fixed network"

echo "##### stage 4: driver behaviour rounds (at most $MAX_ROUNDS) #####"
BEST=""
BEST_TELEPORTS=""
LOWEST="$PREVIOUS_TELEPORTS"
LOWEST_DIR=""
STALE=0
ROUND_DIRS=()
START_BEHAVIOUR=""
GOOGLE_TIMES=""
for round in $(seq 1 "$MAX_ROUNDS"); do
    TO="$P/zero-teleport/round$round"
    if [ -s "$TO/behaviour/summary.json" ]; then
        echo "done before: $TO"
    else
        mkdir -p "$TO/behaviour"
        cp "$STAGE3/repaired.net.xml" "$TO/repaired.net.xml"
        cp "$STAGE3/signals.add.xml" "$TO/signals.add.xml"
        FROM_NET="$TO/repaired.net.xml" FROM_SIGNALS="$TO/signals.add.xml" TO="$TO" \
            START_BEHAVIOUR="$START_BEHAVIOUR" GOOGLE_TIMES="$GOOGLE_TIMES" \
            bash "$SCRIPTS/mac_zero_teleports.sh"
    fi
    ROUND_DIRS+=("$TO")
    current="$(teleports "$TO")"
    echo "stage 4, round $round: $current vehicles teleported (lowest so far: $LOWEST)"
    if [ -z "$BEST" ] || less_than "$current" "$BEST_TELEPORTS"; then
        BEST="$TO"
        BEST_TELEPORTS="$current"
    fi
    if less_than "$current" "$LOWEST"; then
        LOWEST="$current"
        LOWEST_DIR="$TO"
        STALE=0
    else
        STALE=$((STALE + 1))
    fi
    if is_zero "$current"; then
        echo "no vehicle teleported: stage 4 is done"
        break
    fi
    if [ "$STALE" -ge "$PATIENCE" ]; then
        echo "no new lowest count in $PATIENCE rounds: stage 4 stops"
        break
    fi
    # Next round starts from the best drivers so far (lowest teleports), not the last round.
    START_BEHAVIOUR="$BEST/behaviour"
    [ -s "$BEST/behaviour/google-times.json" ] && GOOGLE_TIMES="$BEST/behaviour/google-times.json"
done
echo "best round: $BEST with $BEST_TELEPORTS teleported vehicles"

echo "##### stage 5: signal calibration with the simulated flows, on $BEST #####"
CALIBRATION="$P/signal-calibration"
if [ -s "$CALIBRATION/behaviour/summary.json" ]; then
    echo "done before: $CALIBRATION"
else
    mkdir -p "$CALIBRATION"
    CALIBRATE=(--round "$BEST" --out-dir "$CALIBRATION" --iterations "${CALIBRATION_ITERATIONS:-8}"
               --end "$END" --teleport "$TELEPORT" --workers "${CALIBRATION_WORKERS:-4}")
    [ -n "$COUNT_REFERENCE" ] && CALIBRATE+=(--counts "$COUNT_REFERENCE")
    "$PY" -m matsim_sumo.network.signal_calibration "${CALIBRATE[@]}" 2>&1 | tee "$CALIBRATION/run.txt"
fi
CALIBRATED_TELEPORTS="$(teleports "$CALIBRATION")"
echo "stage 5: $CALIBRATED_TELEPORTS vehicles teleported with the calibrated signals (mean of the seeds)"

ASSIGNMENT="$P/assignment"
FINAL="$P/final"
FINAL_CONFIG="$CALIBRATION/behaviour/best.sumocfg"
ASSIGNED_DONE=""
FINAL_DONE=""
if is_zero "$CALIBRATED_TELEPORTS"; then
    echo "##### no teleports after stage 5: stages 6 and 7 are not needed #####"
else
    echo "##### stage 6: iterative route assignment on $CALIBRATION #####"
    if [ "${SKIP_ASSIGNMENT_STAGE:-0}" = "1" ]; then
        echo "skipped (SKIP_ASSIGNMENT_STAGE=1)"
    else
        if [ -s "$ASSIGNMENT/assignment.json" ]; then
            echo "done before: $ASSIGNMENT"
        else
            FROM="$CALIBRATION" TO="$ASSIGNMENT" NO_FALLBACK=1 ITERATIONS="${ITERATIONS:-10}" \
                bash "$SCRIPTS/mac_route_assignment.sh"
        fi
        ASSIGNED_DONE=1
        assigned="$(teleports "$ASSIGNMENT")"
        echo "stage 6: $assigned vehicles teleported with the assigned routes (mean of the seeds)"
        if is_zero "$assigned"; then
            FINAL_CONFIG="$ASSIGNMENT/final.sumocfg"
        fi
    fi
    if [ "$FINAL_CONFIG" != "$ASSIGNMENT/final.sumocfg" ]; then
        echo "##### stage 7: teleporting off on $CALIBRATION #####"
        if [ -s "$FINAL/final.json" ] && grep -q '"results"' "$FINAL/final.json"; then
            echo "done before: $FINAL"
        elif [ "${FORCE_NETWORK:-0}" = "1" ]; then
            FROM="$CALIBRATION" TO="$FINAL" bash "$SCRIPTS/mac_final_no_teleports.sh"
        else
            FROM="$CALIBRATION" TO="$FINAL" SETTINGS_ONLY=1 bash "$SCRIPTS/mac_final_no_teleports.sh"
        fi
        FINAL_DONE=1
        FINAL_CONFIG="$FINAL/final.sumocfg"
    fi
fi

if [ -n "$COUNT_REFERENCE" ] && [ ! -s "$P/counts/geh/geh.json" ]; then
    echo "##### final run against the traffic counts ($COUNT_YEAR), seeds 23 and 42 #####"
    for seed in 23 42; do
        mkdir -p "$P/counts/seed$seed"
        cp "$COUNT_REFERENCE" "$P/counts/seed$seed/"
        "$PY" -m matsim_sumo.network.count_check --config "$FINAL_CONFIG" --work-dir "$P/counts/seed$seed" \
            --year "$COUNT_YEAR" --seed "$seed" > "$P/counts/seed$seed/run.txt" 2>&1 &
    done
    wait
    tail -n 3 "$P/counts/seed23/run.txt" "$P/counts/seed42/run.txt"
    mkdir -p "$P/counts/geh"
    "$PY" -m matsim_sumo.network.geh_analysis --counts "$P/counts/seed23/counts.csv" "$P/counts/seed42/counts.csv" \
        --out-dir "$P/counts/geh" --title "$CITY_NAME, $DEMAND_FROM-$DEMAND_TO: final run against the $COUNT_YEAR counts" \
        | tee "$P/counts/geh/run.txt" || echo "count comparison failed"
fi

echo "##### teleported vehicles after every iteration #####"
ARGS=(--levels "${LEVEL_DIRS[@]}" --signals "$SIGNALS_DIR" --junctions "$JUNCTIONS"
      --rounds "${ROUND_DIRS[@]}" --calibration "$CALIBRATION" --output "$P/progress.csv")
[ -d "$INTERSECTIONS" ] && ARGS+=(--intersections "$INTERSECTIONS")
[ -n "$ASSIGNED_DONE" ] && ARGS+=(--assignment "$ASSIGNMENT")
[ -n "$FINAL_DONE" ] && ARGS+=(--final "$FINAL")
"$PY" -m matsim_sumo.network.pipeline_progress table "${ARGS[@]}" | tee "$P/progress.txt"
echo "final run with no teleported vehicle: sumo-gui -c $SIM/$FINAL_CONFIG"
