#!/bin/bash
# One-time setup on an Euler login node. This installs the Python package.
# It does not run the simulation. Submit cluster/euler/smoke_sumo.sbatch for that.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${EULER_VENV:-$HOME/venvs/matsim-sumo}"

cd "$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/cluster/euler/load_modules.sh"

echo "Repository: $REPO_ROOT"
echo "Python: $(command -v python)"
echo "SUMO: $(command -v sumo) (SUMO_HOME=$SUMO_HOME)"

python -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install -e "$REPO_ROOT"

mkdir -p "$SCRATCH/matsim-sumo"
echo "Installed matsim-sumo into $VENV"
echo "Scratch directory: $SCRATCH/matsim-sumo"
echo "Next, from this repository, run: sbatch cluster/euler/smoke_sumo.sbatch"
