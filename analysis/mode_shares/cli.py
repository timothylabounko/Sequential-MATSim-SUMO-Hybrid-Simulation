from __future__ import annotations

import argparse
from pathlib import Path

from analysis.mode_shares.common import resolve_matsim_dir, resolve_output_dir
from analysis.mode_shares.runner import compute_boundary_mode_shares


def main() -> None:
    parser = argparse.ArgumentParser(description="Run boundary MATSim/Microcensus mode-share analysis.")
    parser.add_argument("--matsim-output-dir", default="data/inputs/matsim_output_50pct")
    parser.add_argument("--boundary", required=True, help="Boundary polygon file.")
    parser.add_argument("--scenario-name", help="Output filename prefix.")
    parser.add_argument("--output-dir", help="Directory for mode-share CSV/PNG outputs.")
    parser.add_argument("--mz-swiss-border", help="Optional Swiss-border polygon for exact MZ filtering.")
    args = parser.parse_args()

    repo_root = Path.cwd()
    matsim_dir = resolve_matsim_dir(args.matsim_output_dir, repo_root)
    out_dir = resolve_output_dir(args.output_dir, matsim_dir, repo_root)
    written = compute_boundary_mode_shares(
        matsim_dir=matsim_dir,
        repo_root=repo_root,
        boundary_path=Path(args.boundary).expanduser(),
        out_dir=out_dir,
        output_prefix=args.scenario_name,
        mz_swiss_border_path=Path(args.mz_swiss_border).expanduser() if args.mz_swiss_border else None,
    )
    print("Saved mode-share outputs:")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
