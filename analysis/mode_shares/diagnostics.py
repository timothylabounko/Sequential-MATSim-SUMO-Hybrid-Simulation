from __future__ import annotations

from pathlib import Path

from matsim_sumo.pipeline.state import PipelineState


def run_mode_share_diagnostics(state: PipelineState) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    from analysis.mode_shares.runner import compute_boundary_mode_shares

    swiss_border = state.config.get("mode_share_mz_swiss_border_file")
    state.mode_share_outputs = compute_boundary_mode_shares(
        matsim_dir=state.matsim_output_dir,
        repo_root=repo_root,
        boundary_path=state.boundary_path,
        out_dir=state.output_dir / "plots",
        mz_swiss_border_path=Path(swiss_border) if swiss_border else None,
    )
