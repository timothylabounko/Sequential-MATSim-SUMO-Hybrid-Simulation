from matsim_sumo.network.sumo_boundary import compute_and_save_sumo_boundary
from matsim_sumo.pipeline.state import PipelineState
from matsim_sumo.visualization.boundary import confirm_boundary_preview


def run_sumo_boundary_stage(state: PipelineState, vclasses: tuple[str, ...]) -> None:
    compute_and_save_sumo_boundary(
        state.sumo_network_geo,
        state.boundary_path,
        network_buffer_m=float(state.config["sumo_boundary_network_buffer_m"]),
        gap_close_m=float(state.config["sumo_boundary_gap_close_m"]),
        inward_buffer_m=float(state.config["sumo_boundary_inward_buffer_m"]),
        simplify_m=float(state.config["sumo_boundary_simplify_m"]),
        outward_buffer_m=float(state.config["sumo_boundary_outward_buffer_m"]),
        sumo_network_path=state.sumo_network_path,
        vclasses=vclasses,
        no_cache=bool(state.config.get("no_cache", False)) or bool(state.config["boundary_force_rebuild"]),
    )
    confirm_boundary_preview(state.sumo_network_geo, state.boundary_path, state.output_dir)
