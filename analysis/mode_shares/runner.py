from __future__ import annotations

from pathlib import Path

from analysis.mode_shares.common import COMPARISON_MODES, comparison_table, read_boundary_geom, share_table
from analysis.mode_shares.matsim import endpoint_mode_counts as matsim_endpoint_mode_counts
from analysis.mode_shares.microcensus import endpoint_mode_counts as mz_endpoint_mode_counts
from analysis.mode_shares.plotting import plot_mode_share_comparison


def _base_name(prefix: str | None, endpoint: str) -> str:
    return f"{prefix}_{endpoint}_mode_share" if prefix else f"mode_share_{endpoint}"


def compute_boundary_mode_shares(
    matsim_dir: Path,
    repo_root: Path,
    boundary_path: Path,
    out_dir: Path,
    *,
    output_prefix: str | None = None,
    mz_swiss_border_path: Path | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    boundary_geom = read_boundary_geom(boundary_path)
    swiss_border_geom = read_boundary_geom(mz_swiss_border_path) if mz_swiss_border_path else None

    matsim_counts = matsim_endpoint_mode_counts(matsim_dir, boundary_geom)
    mz_counts = mz_endpoint_mode_counts(repo_root, boundary_geom, swiss_border_geom=swiss_border_geom)

    written: list[Path] = []
    for endpoint in ("destination", "origin"):
        comparison = comparison_table(
            share_table(mz_counts[endpoint], "Microcensus_weighted_trips", "Microcensus_share", modes=COMPARISON_MODES),
            share_table(matsim_counts[endpoint], "MATSim_trips", "MATSim_share", modes=COMPARISON_MODES),
        )
        base = _base_name(output_prefix, endpoint)
        csv_path = out_dir / f"{base}_comparison.csv"
        png_path = out_dir / f"{base}.png"
        comparison.to_csv(csv_path, index=False)
        plot_mode_share_comparison(comparison, f"{endpoint.title()} Inside Mode Share", png_path)
        written.extend([csv_path, png_path])
    return written
