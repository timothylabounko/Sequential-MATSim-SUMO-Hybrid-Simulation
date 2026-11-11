#!/bin/bash
#SBATCH --job-name=zurich-sumo-smoke
#SBATCH --time=00:30:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem-per-cpu=4G
#SBATCH --tmp=10G
#SBATCH --output=slurm-smoke-%j.out
#SBATCH --error=slurm-smoke-%j.err

# Short SUMO run on the Zürich network. Submit from the repository root:
#   sbatch cluster/euler/smoke_sumo.sbatch
set -euo pipefail

REPO_ROOT="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/cluster/euler/load_modules.sh"

NETWORK="${SUMO_NET:-$REPO_ROOT/networks/zurich/zurich.net.xml}"
if [[ ! -f "$NETWORK" ]]; then
  echo "Network not found: $NETWORK" >&2
  exit 1
fi

OUT="$SCRATCH/matsim-sumo/smoke/${SLURM_JOB_ID:-local}"
mkdir -p "$OUT"
cp "$NETWORK" "$TMPDIR/network.net.xml"

echo "Job ${SLURM_JOB_ID:-local} on $(hostname)"
sumo --version >"$OUT/sumo-version.txt" 2>&1 || true
echo "Network $NETWORK"

sumo -n "$TMPDIR/network.net.xml" --end 0 --no-step-log >"$OUT/load.log" 2>&1
echo "Network loaded."

python "$SUMO_HOME/tools/randomTrips.py" \
  -n "$TMPDIR/network.net.xml" \
  -o "$TMPDIR/trips.xml" \
  --begin 0 \
  --end 300 \
  --period 10 \
  --seed 42 \
  --validate \
  --trip-attributes 'departLane="best" departSpeed="max"' \
  >"$OUT/randomTrips.log" 2>&1

sumo \
  -n "$TMPDIR/network.net.xml" \
  -r "$TMPDIR/trips.xml" \
  --begin 0 \
  --end 300 \
  --no-step-log \
  --duration-log.statistics \
  --summary "$OUT/summary.xml" \
  >"$OUT/sumo.log" 2>&1

cp "$TMPDIR/trips.xml" "$OUT/trips.xml"
echo "Smoke simulation finished. Output: $OUT"
echo "Summary:"
grep -E "vehicles|teleports|duration" "$OUT/sumo.log" || true
