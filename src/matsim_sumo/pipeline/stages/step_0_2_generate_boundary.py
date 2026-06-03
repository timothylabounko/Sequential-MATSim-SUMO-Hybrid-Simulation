from matsim_sumo.network.sumo_boundary import compute_and_save_sumo_boundary
from matsim_sumo.pipeline.state import PipelineState


def run_sumo_boundary_stage(state: PipelineState, vclasses: tuple[str, ...]) -> None:
    compute_and_save_sumo_boundary(
        state.sumo_network_geo,
        state.boundary_path,
        network_buffer_m=float(state.config.get("sumo_boundary_network_buffer_m", 35.0)),
        gap_close_m=float(state.config.get("sumo_boundary_gap_close_m", 150.0)),
        inward_buffer_m=float(state.config.get("sumo_boundary_inward_buffer_m", 50.0)),
        simplify_m=float(state.config.get("sumo_boundary_simplify_m", 40.0)),
        outward_buffer_m=float(state.config.get("sumo_boundary_outward_buffer_m", 100.0)),
        sumo_network_path=state.sumo_network_path,
        vclasses=vclasses,
        no_cache=bool(state.config.get("no_cache", False)) or bool(state.config.get("boundary_force_rebuild", False)),
    )
