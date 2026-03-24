from pathlib import Path
from time import perf_counter

from matsim_sumo.network_preprocessing.network_preprocessing import preprocess_sumo_network_to_lv95
from matsim_sumo.network_preprocessing.sumo_boundary import compute_and_save_sumo_boundary
from matsim_sumo.pipeline_state import PipelineState
from matsim_sumo.steps.step_1_boundary_cut_links import run_boundary_cut_links_stage
from matsim_sumo.steps.step_2_plans_car_legs import run_plans_car_legs_stage
from matsim_sumo.steps.step_3_crossing_segments import run_crossing_segments_stage
from matsim_sumo.steps.step_4_segment_timing import run_segment_timing_stage
from matsim_sumo.steps.step_5_inside_trip_filter import run_inside_trip_filter_stage
from matsim_sumo.steps.step_6_combine_trips import run_combine_trips_stage
from matsim_sumo.steps.step_7_demand_scaling import run_demand_scaling_stage
from matsim_sumo.steps.step_8_match_sumo_edges import run_match_sumo_edges_stage
from matsim_sumo.steps.step_9_write_sumo_trips_xml import run_write_sumo_trips_xml_stage
from matsim_sumo.steps.step_10_duarouter import run_duarouter_stage


def build_pipeline_state(config: dict) -> PipelineState:
    matsim_output_dir = Path(config["matsim_output_dir"])
    output_dir = Path(config["output_dir"])

    return PipelineState(
        config=config,
        matsim_output_dir=matsim_output_dir,
        output_dir=output_dir,
        boundary_path=Path(config["boundary_file"]),
        network_path=matsim_output_dir / config["network_file"],
        plans_path=matsim_output_dir / config["plans_file"],
        trips_path=matsim_output_dir / config["trips_file"],
        events_path=matsim_output_dir / config["events_file"],
        extract_events_script_path=Path(
            config.get("extract_events_script", "scripts/extract_entered_link_events_macOS.sh")
        ),
        cut_links_csv_path=Path(config.get("cut_links_csv", output_dir / "1_cut_links.csv")),
        events_cut_csv_path=output_dir / config.get(
            "events_cut_file",
            "4_output_events_link_events_car_truck_cut.csv",
        ),
        car_legs_path=output_dir / config.get("car_legs_file", "2_car_legs.csv.gz"),
        car_legs_cut_path=output_dir / config.get("car_legs_cut_file", "3_car_legs_cut.csv.gz"),
        crossing_segments_path=output_dir / config.get("crossing_segments_file", "3_crossing_segments.csv.gz"),
        crossing_segments_timed_path=output_dir / config.get(
            "crossing_segments_timed_file",
            "4_crossing_segments_timed.csv.gz",
        ),
        trips_inside_path=output_dir / config.get("inside_trips_file", "5_trips_inside.csv.gz"),
        combined_trips_path=output_dir / config.get("combined_trips_file", "6_combined_trips.csv.gz"),
        scaled_trips_path=output_dir / config.get("scaled_trips_file", "7_scaled_trips.csv.gz"),
        sumo_network_path=Path(
            config.get(
                "sumo_network_path",
                "data/sumo_scenario/Zurich_Scenario_Transcality/VMOD.net.xml",
            )
        ),
        sumo_network_lv95_path=Path(
            config.get("sumo_network_lv95_tmp_file", output_dir / "tmp/0_sumo_network_lv95.geojson")
        ),
        match_sumo_edges_script_path=Path(
            config.get("match_sumo_edges_script", "scripts/match_scaled_trips_sumo_edges_macOS.sh")
        ),
        scaled_trips_matched_path=output_dir
        / config.get("scaled_trips_matched_file", "8_scaled_trips_matched.csv.gz"),
        sumo_trips_path=output_dir / config.get("sumo_trips_file", "9_sumo_trips.xml"),
        sumo_routes_path=output_dir / config.get("sumo_routes_file", "10_sumo_routes.rou.xml"),
    )


def run_pipeline_steps(state: PipelineState) -> dict[str, int]:
    def _run_step(label: str, fn) -> None:
        t0 = perf_counter()
        fn()
        print(f"✔ {label} done in {perf_counter() - t0:.2f}s")

    network_crs = state.config.get("network_crs", "EPSG:2056")
    vclasses = tuple(dict.fromkeys(state.config["sumo_vclass_map"].values()))

    # Step 0: SUMO network preprocessing and boundary creation
    def _run_sumo_network_preprocessing() -> None:
        state.sumo_net, state.sumo_network_geo = preprocess_sumo_network_to_lv95(
            state.sumo_network_path,
            vclasses,
            state.sumo_network_lv95_path,
        )

    _run_step("Step 0 SUMO network preprocessing", _run_sumo_network_preprocessing)
    if not state.boundary_path.exists():
        _run_step(
            "Step 0 SUMO boundary",
            lambda: compute_and_save_sumo_boundary(state.sumo_network_geo, state.boundary_path),
        )
    else:
        print(f"✔ Step 0 SUMO boundary skipped (exists): {state.boundary_path}")
    _run_step("Step 1 boundary cut links", lambda: run_boundary_cut_links_stage(state, projection=network_crs))
    _run_step("Step 2 plans car legs", lambda: run_plans_car_legs_stage(state))
    _run_step("Step 3 crossing segments", lambda: run_crossing_segments_stage(state))

    missing_before = int(state.crossing_segments["dep_time"].isna().sum())
    _run_step("Step 4 segment timing", lambda: run_segment_timing_stage(state))
    missing_after = int(state.crossing_segments_timed["dep_time"].isna().sum())

    _run_step("Step 5 inside trip filter", lambda: run_inside_trip_filter_stage(state))
    _run_step("Step 6 combine trips", lambda: run_combine_trips_stage(state))
    _run_step("Step 7 demand scaling", lambda: run_demand_scaling_stage(state))
    _run_step("Step 8 match SUMO edges", lambda: run_match_sumo_edges_stage(state))
    _run_step("Step 9 write SUMO trips XML", lambda: run_write_sumo_trips_xml_stage(state))
    _run_step("Step 10 duarouter routing", lambda: run_duarouter_stage(state))

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
        "sumo_trips_xml_rows": int(state.sumo_trips_count or 0),
        "sumo_routes_rows": int(state.sumo_routes_count or 0),
        "sumo_routes_pct": float(state.sumo_routes_pct or 0.0),
    }
