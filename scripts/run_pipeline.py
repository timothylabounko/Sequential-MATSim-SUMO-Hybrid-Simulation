import argparse
import os
import sys

import yaml

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.matsim_trip_inside_filter import build_pipeline_state, run_pipeline_steps

REQUIRED_CONFIG_KEYS = {
    "matsim_output_dir",
    "boundary_file",
    "network_file",
    "plans_file",
    "trips_file",
    "events_file",
    "output_dir",
}

# Read YAML config file into a Python dictionary
def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)

# Checks if required keys are present 
def validate_config(config):
    missing = sorted(REQUIRED_CONFIG_KEYS - set(config))
    if missing:
        raise KeyError(f"Missing required config keys: {', '.join(missing)}")

# Creates output directory if it doesn't exist
def create_output_dir(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    print(f"✔ Output directory ready: {output_dir}")

# Main function to run the pipeline
def main():
    parser = argparse.ArgumentParser(description="MATSim → SUMO pipeline")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    args = parser.parse_args()

    config = load_config(args.config)
    validate_config(config)
    state = build_pipeline_state(config)

    print("Loaded configuration:")
    print(config)

    create_output_dir(state.output_dir)

    stats = run_pipeline_steps(state)
    print(f"✔ Wrote {stats['cut_links']} cut links to {state.cut_links_csv_path}")
    print(f"✔ Wrote {stats['plan_legs']} plan legs to {state.car_legs_path}")
    print(f"✔ Wrote {stats['crossing_legs']} crossing legs to {state.car_legs_cut_path}")
    print(f"✔ Wrote {stats['crossing_segments']} crossing segments to {state.crossing_segments_path}")
    print(f"✔ Wrote extracted cut events to {state.events_cut_csv_path}")
    print(f"✔ Wrote timed crossing segments to {state.crossing_segments_timed_path}")
    print(f"✔ Wrote {stats['inside_trips']} inside-boundary trips to {state.trips_inside_path}")

    print("\nPipeline step successful.")


if __name__ == "__main__":
    main()
