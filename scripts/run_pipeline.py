import argparse
import os
import sys
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.matsim_trip_inside_filter import filter_inside_car_trips


def load_config(path):
    """Load YAML configuration file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def validate_inputs(config):
    """Check that required MATSim files exist."""
    matsim_dir = config["matsim_output_dir"]
    plans_path = os.path.join(matsim_dir, config["plans_file"])
    network_path = os.path.join(matsim_dir, config["network_file"])
    trips_path = os.path.join(matsim_dir, config["trips_file"])
    boundary_path = config["boundary_file"]

    if not os.path.exists(plans_path):
        raise FileNotFoundError(f"Plans file not found: {plans_path}")

    if not os.path.exists(network_path):
        raise FileNotFoundError(f"Network file not found: {network_path}")

    if not os.path.exists(trips_path):
        raise FileNotFoundError(f"Trips file not found: {trips_path}")

    if not os.path.exists(boundary_path):
        raise FileNotFoundError(f"Boundary file not found: {boundary_path}")

    print("✔ MATSim input files found.")


def create_output_dir(config):
    """Create output directory if it does not exist."""
    output_dir = config["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    print(f"✔ Output directory ready: {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="MATSim → SUMO pipeline")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML config file"
    )
    args = parser.parse_args()

    config = load_config(args.config)

    print("Loaded configuration:")
    print(config)

    validate_inputs(config)
    create_output_dir(config)

    trips_path = os.path.join(config["matsim_output_dir"], config["trips_file"])
    out_path = os.path.join(config["output_dir"], "trips_inside.csv.gz")
    trips_inside = filter_inside_car_trips(trips_path, config["boundary_file"], out_path)
    print(f"✔ Wrote {len(trips_inside)} inside-boundary car trips to {out_path}")

    print("\nPipeline step successful.")


if __name__ == "__main__":
    main()
