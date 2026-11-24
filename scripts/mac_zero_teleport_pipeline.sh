#!/usr/bin/env bash
# The whole network correction for one city in one call, from the unedited SUMO network (or OpenStreetMap)
# and a MATSim scenario to a run in which no vehicle is teleported:
#     bash scripts/mac_zero_teleport_pipeline.sh cities/<city>.toml      (default: cities/zurich.toml)
# Everything that differs between cities is in the city file; the steps are the same for every city.
#   Demand   MATSim trips through the network (plans_to_sumo.py); a trip that crosses the boundary and starts
#            or ends past the junction where it branches off is moved to before it (trip_ends.py).
#   Stage 1  Filtering rules on the unedited network.
# Then every demand level of the city file (the MATSim sample, then e.g. 25 %, 50 %, 100 % of the population)
# runs the same steps, each level starting from the network, signals and drivers of the level before:
#   Repair   The OpenStreetMap step and the evolutionary repair (national_repair.py). Network errors come first:
#            a wrong-lane stop, a missing connection, a collision or a trip without a route counts NETWORK_WEIGHT
#            times (default 10), and the search asks Google about those approaches before the others. Rounds,
#            each from the best network so far, stop at no stuck vehicle, after LEVEL_PATIENCE rounds without a
#            better network (default 3), or after FIRST_ROUNDS / LEVEL_ROUNDS (default 30). REPAIR_WORKERS
#            candidates (default 6; FULL_REPAIR_WORKERS, default 4, at full demand) run at the same time, and a
#            candidate stops as soon as it is worse than the network it was built from. The simulations use the
#            signals and drivers of the level before. When an earlier run of the level is moved away, its edits
#            are carried onto the new start (carry_edits.py); round 0 simulates the start without them, and the
#            better of the two is where the rounds begin.
#   Stage 3  Webster actuated signals; persistent junctions (short links joined, lane continuations, lane-change
#            bans lifted, from default-driver seeds 23/42); OpenStreetMap priority and lane continuations at
#            junctions with repeated yields (zero_teleport_repair.py). The network is fixed after this stage.
#   Stage 4  Driver behaviour (mac_zero_teleports.sh, behaviour_calibration.py), seeds 23 and 42, starting from
#            the best drivers of the level before; at most MAX_ROUNDS rounds (default 4), PATIENCE (default 2).
#            Below full demand the drivers are scored on gridlocks only; at full demand also against Google
#            travel times, which are measured at full traffic.
#   Stage 5  Signal calibration with the flows of the level (signal_calibration.py, CALIBRATION_ITERATIONS).
#   Stage 6  Iterative route assignment (mac_route_assignment.sh), only while vehicles are still teleported.
# At full demand only, if vehicles are still teleported after stage 6:
#   Stage 7  The forced network (mac_final_no_teleports.sh): signals or zippers where vehicles waited, longer
#            greens or a lane for jams, teleporting off; next to it the same with only the no-teleport
#            settings, for comparison (final-settings-only). FORCE_NETWORK=0 runs only the settings.
#   Counts   The final run with both seeds against the traffic counts of the city file (GEH, line of best fit).
#            The counts are never used by a stage.
# Everything is written to <work_dir>/PIPELINE (default pipeline): level-NNN/ holds the repair of a level and,
# below full demand, its stages in level-NNN/stages/; the stages at full demand are in PIPELINE itself.
# A step whose result is already there is skipped, so the same command continues after an interruption.
# When a level is run again, it and every later level are moved to <name>.old-<time>.
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
FIRST_ROUNDS="${FIRST_ROUNDS:-30}"
LEVEL_ROUNDS="${LEVEL_ROUNDS:-30}"
LEVEL_PATIENCE="${LEVEL_PATIENCE:-3}"
LEVEL_MAX_EFFORT="${LEVEL_MAX_EFFORT:-2}"
NETWORK_WEIGHT="${NETWORK_WEIGHT:-10}"
REPAIR_WORKERS="${REPAIR_WORKERS:-6}"
FULL_REPAIR_WORKERS="${FULL_REPAIR_WORKERS:-4}"
BEHAVIOUR_WORKERS="${BEHAVIOUR_WORKERS:-4}"
FORCE_NETWORK="${FORCE_NETWORK:-1}"
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

# One Google and one OpenStreetMap cache for every level, seeded with the answers of earlier runs.
GOOGLE_CACHE="$P/google-cache.json"
OSM_CACHE="$P/osm-nodes.json"
merge_caches() {
    local output="$1"
    shift
    "$PY" -c 'import json, pathlib, sys
merged = {}
for path in sys.argv[2:]:
    p = pathlib.Path(path)
    if p.is_file():
        merged.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if not (isinstance(v, dict) and v.get("error"))})
pathlib.Path(sys.argv[1]).write_text(json.dumps(merged), encoding="utf-8")
print(f"{sys.argv[1]}: {len(merged)} cached answers from {len(sys.argv) - 2} earlier files")' "$output" "$@"
}
[ -s "$GOOGLE_CACHE" ] || merge_caches "$GOOGLE_CACHE" $(ls "$P"/level-*/google-cache.json 2>/dev/null || true)
[ -s "$OSM_CACHE" ] || merge_caches "$OSM_CACHE" $(ls "$P"/level-*/osm-nodes.json 2>/dev/null || true)

STAMP="old-$(date +%Y%m%d-%H%M%S)"
CHANGED=0
STAGE_RAN=0

repair_done() {
    [ -s "$1/summary.json" ] && [ -s "$1/final.net.xml" ] && "$PY" -c 'import json, sys
s = json.load(open(sys.argv[1]))
same_rule = s.get("patience") == int(sys.argv[2]) and float(s.get("network_weight", 1)) == float(sys.argv[3])
sys.exit(0 if s["final_stuck"] == 0 or same_rule else 1)' "$1/summary.json" "$LEVEL_PATIENCE" "$NETWORK_WEIGHT"
}

move_aside() {
    if [ -e "$1" ]; then
        mv "$1" "$1.$STAMP"
        echo "moved to $1.$STAMP"
    fi
}

FULL_STAGE_DIRS="signals junctions intersections zero-teleport signal-calibration assignment final final-settings-only
                 counts/seed23 counts/seed42 counts/geh"

# Stages 3-6 (and 7 at full demand) of one level. Sets the hand-off for the next level:
# HANDOFF_NET, HANDOFF_SIGNALS, HANDOFF_BEHAVIOUR; and STAGE_RAN=1 when a stage was computed now.
run_stages() {
    local name="$1" trips="$2" repair="$3" out="$4" full="$5"
    export TRIPS="$trips"
    local weight=0
    [ "$full" = "1" ] && weight=1
    mkdir -p "$out"

    echo "##### $name, stage 3: signal timing (Webster, actuated) #####"
    SIGNALS_DIR="$out/signals"
    if [ -s "$SIGNALS_DIR/summary.json" ] && [ -s "$SIGNALS_DIR/final.net.xml" ]; then
        echo "done before: $SIGNALS_DIR"
    else
        STAGE_RAN=1
        mkdir -p "$SIGNALS_DIR"
        "$PY" -m matsim_sumo.network.signal_timing --net "$repair/final.net.xml" --routes "$trips" \
            --work-dir "$SIGNALS_DIR" --collision-log "$repair/final.log" --signals actuated \
            --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$SIGNALS_DIR/run.txt"
    fi

    echo "##### $name, stage 3: persistent junctions (default-driver seeds), then SUMO default run #####"
    JUNCTIONS="$out/junctions"
    if [ -s "$JUNCTIONS/plain.stats.xml" ]; then
        echo "done before: $JUNCTIONS"
    else
        STAGE_RAN=1
        mkdir -p "$JUNCTIONS"
        "$PY" -m matsim_sumo.network.persistent_junctions --net "$SIGNALS_DIR/final.net.xml" \
            --signals "$SIGNALS_DIR/signals.add.xml" --trips "$trips" \
            --out-dir "$JUNCTIONS" --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$JUNCTIONS/run.txt"
    fi

    echo "##### $name, stage 3: OpenStreetMap priority and lane continuations at yield junctions #####"
    INTERSECTIONS="$out/intersections"
    if [ -s "$INTERSECTIONS/plain.stats.xml" ]; then
        echo "done before: $INTERSECTIONS"
    else
        STAGE_RAN=1
        mkdir -p "$INTERSECTIONS"
        "$PY" -m matsim_sumo.network.zero_teleport_repair \
            --net "$JUNCTIONS/repaired.net.xml" --log "$JUNCTIONS/plain.log" \
            --routes "$JUNCTIONS/plain.rou.xml" --demand "$JUNCTIONS/plain.rou.xml" \
            --signals "$JUNCTIONS/signals.add.xml" --out-dir "$INTERSECTIONS" \
            --trips "$trips" --end "$END" --teleport "$TELEPORT" \
            --min-yields "${MIN_YIELDS:-2}" 2>&1 | tee "$INTERSECTIONS/repair.txt"
    fi
    STAGE3="$INTERSECTIONS"
    [ -s "$STAGE3/repaired.net.xml" ] || STAGE3="$JUNCTIONS"
    PREVIOUS_TELEPORTS="$(teleports "$STAGE3" 2>/dev/null || teleports "$JUNCTIONS")"
    echo "$name, stage 3: $PREVIOUS_TELEPORTS vehicles teleported on the fixed network"

    if [ "$full" = "1" ]; then
        echo "##### $name, stage 4: driver behaviour rounds (at most $MAX_ROUNDS), against Google travel times #####"
    else
        echo "##### $name, stage 4: driver behaviour rounds (at most $MAX_ROUNDS), scored on gridlocks only #####"
    fi
    BEST=""
    BEST_TELEPORTS=""
    local lowest="$PREVIOUS_TELEPORTS" stale=0 start_behaviour="$HANDOFF_BEHAVIOUR" google_times="" round to current
    ROUND_DIRS=()
    for round in $(seq 1 "$MAX_ROUNDS"); do
        to="$out/zero-teleport/round$round"
        if [ -s "$to/behaviour/summary.json" ]; then
            echo "done before: $to"
        else
            STAGE_RAN=1
            mkdir -p "$to/behaviour"
            cp "$STAGE3/repaired.net.xml" "$to/repaired.net.xml"
            cp "$STAGE3/signals.add.xml" "$to/signals.add.xml"
            FROM_NET="$to/repaired.net.xml" FROM_SIGNALS="$to/signals.add.xml" TO="$to" \
                START_BEHAVIOUR="$start_behaviour" GOOGLE_TIMES="$google_times" \
                TRAVEL_TIME_WEIGHT="$weight" WORKERS="$BEHAVIOUR_WORKERS" \
                bash "$SCRIPTS/mac_zero_teleports.sh"
        fi
        ROUND_DIRS+=("$to")
        current="$(teleports "$to")"
        echo "$name, stage 4, round $round: $current vehicles teleported (lowest so far: $lowest)"
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
    echo "$name: best behaviour round $BEST with $BEST_TELEPORTS teleported vehicles"

    echo "##### $name, stage 5: signal calibration with the simulated flows, on $BEST #####"
    CALIBRATION="$out/signal-calibration"
    if [ -s "$CALIBRATION/behaviour/summary.json" ]; then
        echo "done before: $CALIBRATION"
    else
        STAGE_RAN=1
        mkdir -p "$CALIBRATION"
        local calibrate=(--round "$BEST" --out-dir "$CALIBRATION" --iterations "${CALIBRATION_ITERATIONS:-8}"
                         --end "$END" --teleport "$TELEPORT" --workers "${CALIBRATION_WORKERS:-4}")
        [ "$full" = "1" ] && [ -n "$COUNT_REFERENCE" ] && calibrate+=(--counts "$COUNT_REFERENCE")
        "$PY" -m matsim_sumo.network.signal_calibration "${calibrate[@]}" 2>&1 | tee "$CALIBRATION/run.txt"
    fi
    CALIBRATED_TELEPORTS="$(teleports "$CALIBRATION")"
    echo "$name, stage 5: $CALIBRATED_TELEPORTS vehicles teleported with the calibrated signals (mean of the seeds)"
    HANDOFF_NET="$BEST/repaired.net.xml"
    HANDOFF_SIGNALS="$CALIBRATION/signals.add.xml"
    [ -s "$HANDOFF_SIGNALS" ] || HANDOFF_SIGNALS="$BEST/signals.add.xml"
    HANDOFF_BEHAVIOUR="$BEST/behaviour"

    ASSIGNMENT="$out/assignment"
    FINAL="$out/final"
    FINAL_CONFIG="$CALIBRATION/behaviour/best.sumocfg"
    ASSIGNED_DONE=""
    FINAL_DONE=""
    if is_zero "$CALIBRATED_TELEPORTS"; then
        echo "##### $name: no teleports after stage 5, stage 6 is not needed #####"
        return
    fi
    echo "##### $name, stage 6: iterative route assignment on $CALIBRATION #####"
    local assigned="$CALIBRATED_TELEPORTS"
    if [ "${SKIP_ASSIGNMENT_STAGE:-0}" = "1" ]; then
        echo "skipped (SKIP_ASSIGNMENT_STAGE=1)"
    else
        if [ -s "$ASSIGNMENT/assignment.json" ]; then
            echo "done before: $ASSIGNMENT"
        else
            STAGE_RAN=1
            FROM="$CALIBRATION" TO="$ASSIGNMENT" NO_FALLBACK=1 ITERATIONS="${ITERATIONS:-10}" \
                bash "$SCRIPTS/mac_route_assignment.sh"
        fi
        ASSIGNED_DONE=1
        assigned="$(teleports "$ASSIGNMENT")"
        echo "$name, stage 6: $assigned vehicles teleported with the assigned routes (mean of the seeds)"
        if is_zero "$assigned"; then
            FINAL_CONFIG="$ASSIGNMENT/final.sumocfg"
            return
        fi
    fi
    [ "$full" = "1" ] || return 0

    echo "##### $name, stage 7: teleporting off on $CALIBRATION, $assigned vehicles still teleported #####"
    local settings_only="$out/final-settings-only"
    if [ "$FORCE_NETWORK" = "1" ]; then
        if [ -s "$settings_only/final.json" ] && grep -q '"results"' "$settings_only/final.json"; then
            echo "done before: $settings_only"
        else
            FROM="$CALIBRATION" TO="$settings_only" SETTINGS_ONLY=1 bash "$SCRIPTS/mac_final_no_teleports.sh"
        fi
    fi
    if [ -s "$FINAL/final.json" ] && grep -q '"results"' "$FINAL/final.json"; then
        echo "done before: $FINAL"
    elif [ "$FORCE_NETWORK" = "1" ]; then
        FROM="$CALIBRATION" TO="$FINAL" bash "$SCRIPTS/mac_final_no_teleports.sh"
    else
        FROM="$CALIBRATION" TO="$FINAL" SETTINGS_ONLY=1 bash "$SCRIPTS/mac_final_no_teleports.sh"
    fi
    FINAL_DONE=1
    FINAL_CONFIG="$FINAL/final.sumocfg"
}

START="$CORRECTED"
BASE_NET=""
HANDOFF_NET=""
HANDOFF_SIGNALS=""
HANDOFF_BEHAVIOUR=""
LEVEL_DIRS=()
LAST_INDEX=$((${#LEVELS[@]} - 1))
for index in "${!LEVELS[@]}"; do
    level="${LEVELS[$index]}"
    name="$("$PY" -c 'import sys; print(f"level-{round(float(sys.argv[1]) * 100):03d}")' "$level")"
    factor="$("$PY" -c 'import sys; print(f"{float(sys.argv[1]) / float(sys.argv[2]):g}")' "$level" "$SAMPLE_SHARE")"
    percent=$((10#${name#level-}))
    WORK="$P/$name"
    TRIPS_LEVEL="$DEMAND/$name.trips.xml"
    FULL=0
    [ "$index" = "$LAST_INDEX" ] && FULL=1
    if [ "$index" = "0" ]; then
        rounds="$FIRST_ROUNDS"
        workers="$REPAIR_WORKERS"
    else
        rounds="$LEVEL_ROUNDS"
        workers="$REPAIR_WORKERS"
        [ "$FULL" = "1" ] && workers="$FULL_REPAIR_WORKERS"
    fi
    if [ ! -s "$TRIPS_LEVEL" ]; then
        SCALING=(--input "$WIDE" --output "$TRIPS_LEVEL" --scale "$factor" --window "$END")
        if less_than 1 "$factor"; then
            SCALING+=(--endpoints "$DEMAND/ends.csv" --amenities "$DEMAND/amenities.csv" --net "$CORRECTED")
        fi
        "$PY" "$SCRIPTS/scale_trips.py" "${SCALING[@]}" 2>&1 | tee "$DEMAND/$name.scaling.txt"
    fi

    echo "##### $name: network repair at $percent % of the population (factor $factor), from $START #####"
    if [ "$CHANGED" = "0" ] && repair_done "$WORK"; then
        echo "done before: $WORK"
    else
        OLD=""
        if [ -e "$WORK" ]; then
            OLD="$WORK.$STAMP"
            echo "$WORK was made under another rule or from another start:"
            move_aside "$WORK"
        fi
        if [ "$FULL" = "1" ]; then
            for stage in $FULL_STAGE_DIRS; do move_aside "$P/$stage"; done
        fi
        CHANGED=1
        mkdir -p "$WORK"
        FROM_NET="$START"
        FALLBACK=()
        if [ -n "$OLD" ] && [ -n "$BASE_NET" ] && ls -d "$OLD"/round1 >/dev/null 2>&1; then
            echo "##### $name: edits of $OLD carried onto $START #####"
            "$PY" -m matsim_sumo.network.carry_edits --base "$BASE_NET" --edited "$OLD" --onto "$START" \
                --output "$WORK/carried.net.xml" --network-weight "$NETWORK_WEIGHT" 2>&1 | tee "$WORK/carried.txt"
            FROM_NET="$WORK/carried.net.xml"
            FALLBACK=(--fallback-net "$START")
        fi
        REPAIR=(--net "$FROM_NET" ${FALLBACK[@]+"${FALLBACK[@]}"} --routes "$TRIPS_LEVEL" --work-dir "$WORK"
                --rounds "$rounds" --patience "$LEVEL_PATIENCE" --max-effort "$LEVEL_MAX_EFFORT"
                --network-weight "$NETWORK_WEIGHT" --workers "$workers"
                --google-cache "$GOOGLE_CACHE" --osm-cache "$OSM_CACHE"
                --end "$END" --teleport "$TELEPORT")
        if [ -n "$HANDOFF_SIGNALS" ]; then
            REPAIR+=(--signals "$HANDOFF_SIGNALS" --signals-net "$HANDOFF_NET")
        fi
        if [ -n "$HANDOFF_BEHAVIOUR" ] && [ -s "$HANDOFF_BEHAVIOUR/summary.json" ]; then
            REPAIR+=(--behaviour "$HANDOFF_BEHAVIOUR/summary.json")
        fi
        "$PY" -m matsim_sumo.network.national_repair "${REPAIR[@]}" 2>&1 | tee "$WORK/run.txt"
    fi
    [ "$index" = "0" ] && BASE_NET="$WORK/final.net.xml"
    if ! is_zero "$(teleports "$WORK")"; then
        echo "$name: vehicles still stuck after the repair; stages 3-6 work on them next"
    fi
    echo "$name: $(teleports "$WORK") vehicles stuck in the best repair round (final.net.xml)"
    LEVEL_DIRS+=("$WORK")
    FULL_TRIPS="$TRIPS_LEVEL"

    STAGES="$WORK/stages"
    [ "$FULL" = "1" ] && STAGES="$P"
    STAGE_RAN=0
    run_stages "$name" "$TRIPS_LEVEL" "$WORK" "$STAGES" "$FULL"
    [ "$STAGE_RAN" = "1" ] && CHANGED=1
    START="$HANDOFF_NET"
done
export TRIPS="$FULL_TRIPS"

if [ "${SKIP_BEFORE:-0}" != "1" ] && [ ! -s "$P/before-repair/summary.json" ]; then
    echo "##### the unrepaired network at full demand, for comparison #####"
    mkdir -p "$P/before-repair"
    "$PY" -m matsim_sumo.network.load_check --net "$CORRECTED" --routes "$FULL_TRIPS" --work-dir "$P/before-repair" \
        --end "$END" --teleport "$TELEPORT" 2>&1 | tee "$P/before-repair/run.txt"
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

echo "##### teleported vehicles after every iteration (full demand) #####"
ARGS=(--levels "${LEVEL_DIRS[@]}" --signals "$SIGNALS_DIR" --junctions "$JUNCTIONS"
      --rounds ${ROUND_DIRS[@]+"${ROUND_DIRS[@]}"} --calibration "$CALIBRATION" --output "$P/progress.csv")
[ -d "$INTERSECTIONS" ] && ARGS+=(--intersections "$INTERSECTIONS")
[ -n "$ASSIGNED_DONE" ] && ARGS+=(--assignment "$ASSIGNMENT")
[ -n "$FINAL_DONE" ] && ARGS+=(--final "$FINAL")
"$PY" -m matsim_sumo.network.pipeline_progress table "${ARGS[@]}" | tee "$P/progress.txt"
echo "##### every level #####"
for work in "${LEVEL_DIRS[@]}"; do
    stages="$work/stages"
    [ "$work" = "${LEVEL_DIRS[$LAST_INDEX]}" ] && stages="$P"
    line="$(basename "$work"): repair $(teleports "$work")"
    for stage in intersections signal-calibration assignment final; do
        if [ -e "$stages/$stage" ]; then
            line="$line, $stage $(teleports "$stages/$stage" 2>/dev/null || echo '?')"
        fi
    done
    echo "$line stuck vehicles"
done | tee "$P/levels.txt"
echo "final run with no teleported vehicle: sumo-gui -c $SIM/$FINAL_CONFIG"
