from pathlib import Path

from matsim_sumo.pipeline_state import PipelineState
from matsim_sumo.steps.step_1_boundary_cut_links import run_boundary_cut_links_stage
from matsim_sumo.steps.step_2_plans_car_legs import run_plans_car_legs_stage
from matsim_sumo.steps.step_3_crossing_segments import run_crossing_segments_stage
from matsim_sumo.steps.step_4_segment_timing import run_segment_timing_stage
from matsim_sumo.steps.step_5_inside_trip_filter import run_inside_trip_filter_stage


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
        cut_links_csv_path=Path(config.get("cut_links_csv", "data/tmp/cut_links.csv")),
        events_cut_csv_path=output_dir / config.get(
            "events_cut_file",
            "output_events_link_events_car_truck_cut.csv",
        ),
        car_legs_path=output_dir / config.get("car_legs_file", "car_legs.csv.gz"),
        car_legs_cut_path=output_dir / config.get("car_legs_cut_file", "car_legs_cut.csv.gz"),
        crossing_segments_path=output_dir / config.get("crossing_segments_file", "crossing_segments.csv.gz"),
        crossing_segments_timed_path=output_dir / config.get(
            "crossing_segments_timed_file",
            "crossing_segments_timed.csv.gz",
        ),
        trips_inside_path=output_dir / config.get("inside_trips_file", "trips_inside.csv.gz"),
    )


def run_pipeline_steps(state: PipelineState) -> dict[str, int]:
    network_crs = state.config.get("network_crs", "EPSG:2056")
    run_boundary_cut_links_stage(state, projection=network_crs)
    run_plans_car_legs_stage(state)
    run_crossing_segments_stage(state)

    missing_before = int(state.crossing_segments["dep_time"].isna().sum())
    run_segment_timing_stage(state)
    missing_after = int(state.crossing_segments_timed["dep_time"].isna().sum())

    run_inside_trip_filter_stage(state)

    return {
        "cut_links": len(state.cut_link_ids),
        "plan_legs": len(state.car_legs),
        "crossing_legs": len(state.car_legs_cut),
        "crossing_segments": len(state.crossing_segments),
        "timed_filled": missing_before - missing_after,
        "timed_unresolved": missing_after,
        "inside_trips": len(state.trips_inside),
    }
