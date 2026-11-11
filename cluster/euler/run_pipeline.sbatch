#!/bin/bash
#SBATCH --job-name=matsim-sumo
#SBATCH --time=04:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem-per-cpu=8G
#SBATCH --output=slurm-pipeline-%j.out
#SBATCH --error=slurm-pipeline-%j.err

# Run the MATSim to SUMO pipeline. Submit from the repository root after
# cluster/euler/setup_euler.sh and after data/inputs has been copied in:
#   sbatch cluster/euler/run_pipeline.sbatch
#   CONFIG=configs/my_run.yaml sbatch cluster/euler/run_pipeline.sbatch
set -euo pipefail

REPO_ROOT="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/cluster/euler/load_modules.sh"

VENV="${EULER_VENV:-$HOME/venvs/matsim-sumo}"
CONFIG="${CONFIG:-$REPO_ROOT/configs/my_run.yaml}"

if [[ ! -x "$VENV/bin/matsim-sumo" ]]; then
  echo "matsim-sumo is not installed. On a login node run: bash cluster/euler/setup_euler.sh" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "Config not found: $CONFIG" >&2
  echo "Copy configs/templates/pipeline.yaml to configs/my_run.yaml and set the input paths." >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
export SUMO_HOME

echo "Job ${SLURM_JOB_ID:-local} on $(hostname)"
echo "Config $CONFIG"
matsim-sumo run --config "$CONFIG"
echo "Pipeline finished."
