from matsim_sumo.network.sumo_network import preprocess_sumo_network_to_lv95
from matsim_sumo.pipeline.state import PipelineState


def run_sumo_network_preprocessing_stage(state: PipelineState, vclasses: tuple[str, ...]) -> None:
    state.sumo_net, state.sumo_network_geo = preprocess_sumo_network_to_lv95(
        state.sumo_network_path,
        vclasses,
        state.sumo_network_lv95_path,
        no_cache=bool(state.config.get("no_cache", False)),
    )
