#!/bin/bash
# Copy this repository to your Euler home. Run it on your Mac, from the
# repository root, while connected to the ETH network or VPN.
set -euo pipefail

if [[ -z "${ETH_USER:-}" ]]; then
  echo "Set ETH_USER to your ETH username, then run this script again." >&2
  echo "Example: ETH_USER=tlabounko bash cluster/euler/sync_to_euler.sh" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE="${ETH_USER}@euler.ethz.ch:matsim-sumo/"

rsync -av --delete \
  --exclude '.venv/' \
  --exclude 'venvs/' \
  --exclude 'results/' \
  --exclude 'data/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$REPO_ROOT/" "$REMOTE"

echo "Copied to euler:~/matsim-sumo"
echo "Next: ssh ${ETH_USER}@euler.ethz.ch"
echo "Then: cd matsim-sumo && bash cluster/euler/setup_euler.sh && sbatch cluster/euler/smoke_sumo.sbatch"
