#!/usr/bin/env bash
set -euo pipefail

IN="${1:-results/run_03_transcality_network/7_scaled_trips.csv.gz}"
OUT="${2:-results/run_03_transcality_network/8_scaled_trips_matched.csv.gz}"
NET="${3:-data/sumo_scenario/Zurich_Scenario_Transcality/VMOD.net.xml}"
MATSIM_NET="${4:-data/matsim_output/output_network.xml.gz}"

PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

SEP="${SEP:-;}"
CHUNK_ROWS="${CHUNK_ROWS:-200000}"
MAX_RADIUS_M="${MAX_RADIUS_M:-500}"
ASSUME_XY="${ASSUME_XY:-0}"
ANGLE_MARGIN_DEG="${ANGLE_MARGIN_DEG:-10.0}"
SAME_DIST_REL="${SAME_DIST_REL:-0.05}"

ARGS=(
  --input "$IN"
  --output "$OUT"
  --sumo-net "$NET"
  --matsim-net "$MATSIM_NET"
  --sep "$SEP"
  --chunksize "$CHUNK_ROWS"
  --max-radius "$MAX_RADIUS_M"
  --angle-margin-deg "$ANGLE_MARGIN_DEG"
  --same-dist-rel "$SAME_DIST_REL"
)

if [[ "$ASSUME_XY" == "1" ]]; then
  ARGS+=(--assume-xy)
fi

PYTHONPATH=src "$PY" src/matsim_sumo/steps/step_8_match_sumo_edges.py "${ARGS[@]}"
