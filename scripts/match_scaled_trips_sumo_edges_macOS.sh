#!/usr/bin/env bash
set -euo pipefail

IN="${1:-results/run_03_transcality_network/7_scaled_trips.csv.gz}"
OUT="${2:-results/run_03_transcality_network/8_scaled_trips_matched.csv.gz}"
NET="${3:-data/sumo_scenario/Zurich_Scenario_Transcality/VMOD.net.xml}"

PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

SEP="${SEP:-;}"
CHUNK_ROWS="${CHUNK_ROWS:-200000}"
RADIUS_M="${RADIUS_M:-150}"
MAX_RADIUS_M="${MAX_RADIUS_M:-2000}"
CACHE_ROUND_M="${CACHE_ROUND_M:-1.0}"
ASSUME_XY="${ASSUME_XY:-0}"

ARGS=(
  --input "$IN"
  --output "$OUT"
  --sumo-net "$NET"
  --sep "$SEP"
  --chunksize "$CHUNK_ROWS"
  --radius "$RADIUS_M"
  --max-radius "$MAX_RADIUS_M"
  --cache-round-m "$CACHE_ROUND_M"
)

if [[ "$ASSUME_XY" == "1" ]]; then
  ARGS+=(--assume-xy)
fi

PYTHONPATH=src "$PY" src/matsim_sumo/steps/step_8_match_sumo_edges.py "${ARGS[@]}"
