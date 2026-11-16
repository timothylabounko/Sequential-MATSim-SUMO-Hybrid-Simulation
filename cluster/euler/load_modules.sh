#!/bin/bash
# Load the Euler software stack used by the MATSim-SUMO jobs.
# Source this file from a login node or from an sbatch script. Do not execute it.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Source this file: source cluster/euler/load_modules.sh" >&2
  exit 1
fi

module purge
module load stack/2025-06
module load python/3.12.8

if ! module load sumo/1.22.0; then
  echo "sumo/1.22.0 is not in stack/2025-06. Trying stack/2024-06." >&2
  module purge
  module load stack/2024-06
  module load python/3.12.8
  module load sumo/1.22.0
fi

# Compute nodes have no direct internet. The proxy is unused when it is absent.
module load eth_proxy || true

if [[ -z "${SUMO_HOME:-}" ]]; then
  echo "SUMO_HOME is not set after loading sumo/1.22.0. Run: module spider sumo" >&2
  return 1
fi

export PYTHONPATH="${SUMO_HOME}/tools${PYTHONPATH:+:$PYTHONPATH}"
