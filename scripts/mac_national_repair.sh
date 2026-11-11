#!/usr/bin/env bash
# Run the OpenStreetMap check, the National simulation, the Google turn check,
# and the evolutionary search on a Mac. Expects GOOGLE_MAPS_API_KEY to be set
# and the National folder in ~/Downloads. The EQASIM output is a sample of the
# population, so trips are scaled by SCALE (default 10) before simulating.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
SIM="${SIM:-$HOME/zurich-sim/national-morning}"
NATIONAL="${NATIONAL:-$HOME/Downloads/National}"
VENV="${VENV:-$HOME/zurich-sim/venv}"
NET="$BUNDLE/zurich.corrected.net.xml"

if [ -z "${GOOGLE_MAPS_API_KEY:-}" ]; then
    echo "Set GOOGLE_MAPS_API_KEY first: export GOOGLE_MAPS_API_KEY=..." >&2
    exit 1
fi
if [ "$GOOGLE_MAPS_API_KEY" = "PASTE_YOUR_KEY_HERE" ]; then
    echo "GOOGLE_MAPS_API_KEY is still the placeholder. Paste the real key: export GOOGLE_MAPS_API_KEY=..." >&2
    exit 1
fi
if [ -z "${SUMO_HOME:-}" ]; then
    SUMO_BIN="$(python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("sumo")))')"
    SUMO_HOME="$(cd "$(dirname "$SUMO_BIN")/../share/sumo" && pwd)"
    export SUMO_HOME
fi
echo "SUMO_HOME=$SUMO_HOME"

[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q sumolib pyproj
PY="$VENV/bin/python"
export PYTHONPATH="$BUNDLE/src"

mkdir -p "$SIM"
cd "$SIM"
cp "$BUNDLE/view.settings.xml" .

if [ -f "$BUNDLE/zurich.net.xml.gz" ] && [ -f "$BUNDLE/zurich.osm.xml.gz" ]; then
    echo "filtering: correcting the unedited Zurich network"
    [ -f "$BUNDLE/zurich.net.xml" ] || gunzip -k "$BUNDLE/zurich.net.xml.gz"
    [ -f "$BUNDLE/zurich.osm.xml" ] || gunzip -k "$BUNDLE/zurich.osm.xml.gz"
    if "$PY" -m matsim_sumo.network.zurich_correction --source "$BUNDLE/zurich.net.xml" \
        --osm "$BUNDLE/zurich.osm.xml" --output-dir correction 2>&1 | tee correction.txt | tail -25; then
        [ -s correction/zurich.corrected.net.xml ] && NET="$SIM/correction/zurich.corrected.net.xml"
    fi
fi
[ -f "$NET" ] || gunzip -k "$NET.gz"
echo "network: $NET"

if [ ! -s plans.connected.trips.xml ]; then
    "$PY" "$BUNDLE/scripts/plans_to_sumo.py" \
        --plans "$NATIONAL/output_plans.xml.gz" \
        --network "$NATIONAL/output_network.xml.gz" \
        --net "$NET" --output plans.connected.trips.xml
fi

SCALE="${SCALE:-10}"
"$PY" "$BUNDLE/scripts/scale_trips.py" --input plans.connected.trips.xml \
    --output "plans.x$SCALE.trips.xml" --scale "$SCALE"

REPAIR="${REPAIR:-repair-x$SCALE}"
mkdir -p "$REPAIR"
"$PY" -m matsim_sumo.network.national_repair \
    --net "$NET" --routes "plans.x$SCALE.trips.xml" --work-dir "$REPAIR" --rounds "${ROUNDS:-10}" --end 3600 \
    2>&1 | tee "$REPAIR/run.txt"
