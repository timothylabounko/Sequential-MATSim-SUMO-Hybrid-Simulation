from pathlib import Path
from time import perf_counter

from matsim_sumo.config.defaults import default_extract_events_script_path
from matsim_sumo.pipeline.state import PipelineState
from matsim_sumo.pipeline.stages.step_0_1_prepare_sumo_network import run_sumo_network_preprocessing_stage
from matsim_sumo.pipeline.stages.step_0_2_generate_boundary import run_sumo_boundary_stage
from matsim_sumo.pipeline.stages.step_1_1_boundary_cut_links import run_boundary_cut_links_stage
from matsim_sumo.pipeline.stages.step_1_2_extract_car_legs import run_plans_car_legs_stage
from matsim_sumo.pipeline.stages.step_1_3_crossing_segments import run_crossing_segments_stage
from matsim_sumo.pipeline.stages.step_1_4_segment_timing import run_segment_timing_stage
from matsim_sumo.pipeline.stages.step_1_5_inside_trip_filter import run_inside_trip_filter_stage
from matsim_sumo.pipeline.stages.step_1_6_combine_filtered_trips import run_combine_trips_stage
from matsim_sumo.pipeline.stages.step_2_demand_scaling import run_demand_scaling_stage
from matsim_sumo.pipeline.stages.step_3_map_matching import run_match_sumo_edges_stage
from matsim_sumo.pipeline.stages.step_4_1_write_sumo_trips import run_write_sumo_trips_xml_stage
from matsim_sumo.pipeline.stages.step_4_2_duarouter import run_duarouter_stage


def resolve_sumo_network_path(config: dict) -> Path:
    path = Path(config["sumo_network_path"])
    if path.is_absolute():
        return path
    return Path(config.get("sumo_input_dir", "")) / path


def _is_shared_generated_path(path: str | Path) -> bool:
    parts = Path(path).parts
    shared_roots = {("data", "generated"), ("data", "tmp")}
    return any(parts[i : i + 2] in shared_roots for i in range(len(parts) - 1))


def _resolve_run_generated_path(path: str | Path, output_dir: Path, filename: str) -> Path:
    path = Path(path)
    if _is_shared_generated_path(path):
        return output_dir / filename
    return path


def build_pipeline_state(config: dict) -> PipelineState:
    matsim_output_dir = Path(config["matsim_output_dir"])
    output_dir = Path(config["output_dir"])

    return PipelineState(
        config=config,
        matsim_output_dir=matsim_output_dir,
        output_dir=output_dir,
        boundary_path=_resolve_run_generated_path(
            config["boundary_file"],
            output_dir,
            "0_sumo_boundary.geojson",
        ),
        network_path=matsim_output_dir / config["network_file"],
        plans_path=matsim_output_dir / config["plans_file"],
        trips_path=matsim_output_dir / config["trips_file"],
        events_path=matsim_output_dir / config["events_file"],
        extract_events_script_path=Path(config.get("extract_events_script") or default_extract_events_script_path()),
        cut_links_csv_path=Path(config.get("cut_links_csv", output_dir / "1_1_cut_links.csv")),
        events_cut_csv_path=output_dir / config.get(
            "events_cut_file",
            "1_4_output_events_link_events_car_truck_cut.csv",
        ),
        car_legs_path=output_dir / config.get("car_legs_file", "1_2_car_legs.csv.gz"),
        car_legs_cut_path=output_dir / config.get("car_legs_cut_file", "1_3_car_legs_cut.csv.gz"),
        crossing_segments_path=output_dir / config.get("crossing_segments_file", "1_3_crossing_segments.csv.gz"),
        crossing_segments_timed_path=output_dir / config.get(
            "crossing_segments_timed_file",
            "1_4_crossing_segments_timed.csv.gz",
        ),
        trips_inside_path=output_dir / config.get("inside_trips_file", "1_5_trips_inside.csv.gz"),
        combined_trips_path=output_dir / config.get("combined_trips_file", "1_6_combined_trips.csv.gz"),
        scaled_trips_path=output_dir / config.get("scaled_trips_file", "2_scaled_trips.csv.gz"),
        sumo_network_path=resolve_sumo_network_path(config),
        sumo_network_lv95_path=_resolve_run_generated_path(
            config["sumo_network_lv95_tmp_file"],
            output_dir,
            "0_sumo_network_lv95.geojson",
        ),
        scaled_trips_matched_path=output_dir
        / config.get("scaled_trips_matched_file", "3_matched_trips.csv.gz"),
        sumo_trips_path=output_dir / config.get("sumo_trips_file", "4_1_sumo_trips.xml"),
        sumo_routes_path=output_dir / config["sumo_routes_file"],
        sumo_unrouted_trips_path=output_dir / config["sumo_unrouted_trips_file"],
        duarouter_log_path=output_dir / config["duarouter_log_file"],
    )


def run_pipeline_steps(state: PipelineState) -> dict[str, int]:
    def _run_step(label: str, fn) -> None:
        t0 = perf_counter()
        fn()
        print(f"✔ {label} done in {perf_counter() - t0:.2f}s")

    network_crs = state.config.get("network_crs", "EPSG:2056")
    vclasses = tuple(dict.fromkeys(state.config["sumo_vclass_map"].values()))

    _run_step("Step 0.1 prepare SUMO network", lambda: run_sumo_network_preprocessing_stage(state, vclasses))
    _run_step("Step 0.2 generate and confirm SUMO boundary", lambda: run_sumo_boundary_stage(state, vclasses))
    _run_step("Step 1.1 boundary cut links", lambda: run_boundary_cut_links_stage(state, projection=network_crs))
    _run_step("Step 1.2 extract MATSim car legs", lambda: run_plans_car_legs_stage(state))
    _run_step("Step 1.3 crossing segments", lambda: run_crossing_segments_stage(state))

    missing_before = int(state.crossing_segments["dep_time"].isna().sum())
    _run_step("Step 1.4 segment timing", lambda: run_segment_timing_stage(state))
    missing_after = int(state.crossing_segments_timed["dep_time"].isna().sum())

    _run_step("Step 1.5 inside trip filter", lambda: run_inside_trip_filter_stage(state))
    _run_step("Step 1.6 combine filtered trips", lambda: run_combine_trips_stage(state))
    _run_step("Step 2 demand scaling", lambda: run_demand_scaling_stage(state))
    _run_step("Step 3 map-matching", lambda: run_match_sumo_edges_stage(state))
    _run_step("Step 4.1 write SUMO trips XML", lambda: run_write_sumo_trips_xml_stage(state))
    duarouter_ran = bool(state.config["run_duarouter"])
    if duarouter_ran:
        _run_step("Step 4.2 duarouter routing", lambda: run_duarouter_stage(state))
    else:
        print("✔ Step 4.2 duarouter routing skipped (run_duarouter: false)")

    return {
        "cut_links": len(state.cut_link_ids),
        "plan_legs": len(state.car_legs),
        "crossing_legs": len(state.car_legs_cut),
        "crossing_segments": len(state.crossing_segments),
        "timed_filled": missing_before - missing_after,
        "timed_unresolved": missing_after,
        "inside_trips": len(state.trips_inside),
        "combined_trips": len(state.combined_trips),
        "scaled_trips": len(state.scaled_trips),
        "scaled_trips_matched": len(state.scaled_trips_matched),
        "scaled_trips_match_ok": int(state.scaled_trips_matched["match_ok"].sum()),
        "scaled_trips_guardrail_dropped": int(state.sumo_endpoint_guardrail_dropped_count or 0),
        "sumo_trips_xml_rows": int(state.sumo_trips_count or 0),
        "duarouter_ran": duarouter_ran,
        "sumo_routes_rows": int(state.sumo_routes_count or 0),
        "sumo_routes_pct": float(state.sumo_routes_pct or 0.0),
        "sumo_unrouted_rows": int(state.sumo_unrouted_count or 0),
        "sumo_unrouted_pct": float(state.sumo_unrouted_pct or 0.0),
    }


def run_pipeline(config: dict) -> dict[str, int | float]:
    state = build_pipeline_state(config)
    state.output_dir.mkdir(parents=True, exist_ok=True)
    return run_pipeline_steps(state)
