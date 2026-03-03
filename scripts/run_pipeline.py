import argparse
import os
import yaml


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

    if not os.path.exists(plans_path):
        raise FileNotFoundError(f"Plans file not found: {plans_path}")

    if not os.path.exists(network_path):
        raise FileNotFoundError(f"Network file not found: {network_path}")

    if not os.path.exists(trips_path):
        raise FileNotFoundError(f"Trips file not found: {trips_path}")

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

    print("\nPipeline setup successful.")
    print("Next step: implement conversion logic.")


if __name__ == "__main__":
    main()