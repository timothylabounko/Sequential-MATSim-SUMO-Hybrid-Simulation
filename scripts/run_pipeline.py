import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.boundary_cut_links import run_boundary_cut_links_stage
from matsim_sumo.crossing_segments import run_crossing_segments_stage
from matsim_sumo.matsim_trip_inside_filter import run_inside_trip_filter_stage
from matsim_sumo.pipeline_state import PipelineState
from matsim_sumo.plans_car_legs import run_plans_car_legs_stage
from matsim_sumo.segment_timing import run_segment_timing_stage

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_CONFIG_KEYS = {
    "matsim_output_dir",
    "boundary_file",
    "network_file",
    "plans_file",
    "trips_file",
    "events_file",
    "output_dir",
}


def load_config(path):
    """Load YAML configuration file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def validate_config(config):
    missing = sorted(REQUIRED_CONFIG_KEYS - set(config))
    if missing:
        keys = ", ".join(missing)
        raise KeyError(f"Missing required config keys: {keys}")


def resolve_path(path_value):
    p = Path(path_value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def build_pipeline_state(config, config_path):
    matsim_output_dir = resolve_path(config["matsim_output_dir"])
    output_dir = resolve_path(config["output_dir"])

    return PipelineState(
        project_root=PROJECT_ROOT,
        config_path=resolve_path(config_path),
        config=config,
        matsim_output_dir=matsim_output_dir,
        output_dir=output_dir,
        boundary_path=resolve_path(config["boundary_file"]),
        network_path=matsim_output_dir / config["network_file"],
        plans_path=matsim_output_dir / config["plans_file"],
        trips_path=matsim_output_dir / config["trips_file"],
        events_path=matsim_output_dir / config["events_file"],
        extract_events_script_path=resolve_path(
            config.get("extract_events_script", "scripts/extract_entered_link_events_macOS.sh")
        ),
        cut_links_csv_path=resolve_path(config.get("cut_links_csv", "data/tmp/cut_links.csv")),
        events_cut_csv_path=output_dir / config.get(
            "events_cut_file", "output_events_link_events_car_truck_cut.csv"
        ),
        car_legs_path=output_dir / config.get("car_legs_file", "car_legs.csv.gz"),
        car_legs_cut_path=output_dir / config.get("car_legs_cut_file", "car_legs_cut.csv.gz"),
        crossing_segments_path=output_dir / config.get("crossing_segments_file", "crossing_segments.csv.gz"),
        crossing_segments_timed_path=output_dir / config.get(
            "crossing_segments_timed_file", "crossing_segments_timed.csv.gz"
        ),
        trips_inside_path=output_dir / config.get("inside_trips_file", "trips_inside.csv.gz"),
    )


def create_output_dir(state):
    """Create output directory if it does not exist."""
    os.makedirs(state.output_dir, exist_ok=True)
    print(f"✔ Output directory ready: {state.output_dir}")


def main():
    parser = argparse.ArgumentParser(description="MATSim → SUMO pipeline")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML config file"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    validate_config(config)
    state = build_pipeline_state(config, args.config)

    print("Loaded configuration:")
    print(config)

    create_output_dir(state)

    network_crs = config.get("network_crs", "EPSG:2056")
    run_boundary_cut_links_stage(state, projection=network_crs)
    print(f"✔ Wrote {len(state.cut_link_ids)} cut links to {state.cut_links_csv_path}")

    run_plans_car_legs_stage(state)
    print(f"✔ Wrote {len(state.car_legs)} plan legs to {state.car_legs_path}")

    run_crossing_segments_stage(state)
    print(f"✔ Wrote {len(state.car_legs_cut)} crossing legs to {state.car_legs_cut_path}")
    print(f"✔ Wrote {len(state.crossing_segments)} crossing segments to {state.crossing_segments_path}")

    missing_before = int(state.crossing_segments["dep_time"].isna().sum())
    run_segment_timing_stage(state)
    missing_after = int(state.crossing_segments_timed["dep_time"].isna().sum())
    filled = missing_before - missing_after
    print(f"✔ Wrote extracted cut events to {state.events_cut_csv_path}")
    print(
        "✔ Wrote timed crossing segments to "
        f"{state.crossing_segments_timed_path} (filled {filled}, unresolved {missing_after})"
    )

    run_inside_trip_filter_stage(state)
    print(f"✔ Wrote {len(state.trips_inside)} inside-boundary trips to {state.trips_inside_path}")

    print("\nPipeline step successful.")


if __name__ == "__main__":
    main()
