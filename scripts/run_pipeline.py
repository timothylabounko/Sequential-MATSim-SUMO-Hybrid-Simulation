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

    create_output_dir(config)

    trips_path = os.path.join(config["matsim_output_dir"], config["trips_file"])
    out_path = os.path.join(config["output_dir"], "trips_inside.csv.gz")
    trips_inside = filter_inside_car_trips(trips_path, config["boundary_file"], out_path)
    print(f"✔ Wrote {len(trips_inside)} inside-boundary car trips to {out_path}")

    print("\nPipeline step successful.")


if __name__ == "__main__":
    main()
