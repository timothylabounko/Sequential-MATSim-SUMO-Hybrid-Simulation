import argparse
import gzip
import os
import shutil
import sys
import warnings

import yaml

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.matsim_trip_inside_filter import build_pipeline_state, run_pipeline_steps

REQUIRED_CONFIG_KEYS = {
    "matsim_output_dir",
    "boundary_file",
    "network_file",
    "plans_file",
    "trips_file",
    "events_file",
    "output_dir",
    "modes_keep",
    "write_intermediates",
    "write_review_csv",
    "scale_factor",
    "dep_time_jitter_seconds",
    "coord_jitter_radius_m",
    "demand_scaling_seed",
    "amenity_kdtree_enabled",
    "amenities_path",
    "sumo_input_dir",
    "sumo_network_path",
    "sumo_network_lv95_tmp_file",
    "sumo_vclass_map",
    "run_duarouter",
    "duarouter_bin",
    "sumo_routes_file",
    "sumo_unrouted_trips_file",
    "duarouter_log_file",
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


def write_config_copy(config: dict, output_dir: Path) -> Path:
    path = Path(output_dir) / "pipeline_config.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False)
    return path


# Writes a plain CSV review copy from a CSV.GZ pipeline output
def write_review_csv(source_path: Path) -> Path:
    source = Path(source_path)
    target = source.with_suffix("") if source.suffix == ".gz" else source.with_name(f"{source.stem}.review.csv")
    target.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix == ".gz":
        with gzip.open(source, "rt", newline="") as src, open(target, "w", newline="") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(source, target)
    return target


# Main function to run the pipeline
def main():
    warnings.filterwarnings(
        "ignore",
        message=r".*\+init=<authority>:<code>.*",
        category=FutureWarning,
        module=r"pyproj\.crs\.crs",
    )

    parser = argparse.ArgumentParser(description="MATSim → SUMO pipeline")
    parser.add_argument(
        "--config",
        default="configs/base_config.yaml",
        help="Path to YAML config file (default: configs/base_config.yaml)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    config.setdefault("csv_gzip_compresslevel", 1)
    validate_config(config)
    CsvWriter.set_gzip_compresslevel(int(config["csv_gzip_compresslevel"]))
    state = build_pipeline_state(config)

    print(f"✔ Loaded config: {args.config}")

    create_output_dir(state.output_dir)
    config_copy = write_config_copy(config, state.output_dir)
    print(f"✔ Wrote config copy: {config_copy}")

    stats = run_pipeline_steps(state)
    print(f"✔ Wrote {stats['cut_links']} cut links to {state.cut_links_csv_path}")
    print(f"✔ Wrote {stats['plan_legs']} plan legs to {state.car_legs_path}")
    print(f"✔ Wrote {stats['crossing_legs']} crossing legs to {state.car_legs_cut_path}")
    print(f"✔ Wrote {stats['crossing_segments']} crossing segments to {state.crossing_segments_path}")
    print(f"✔ Wrote extracted cut events to {state.events_cut_csv_path}")
    print(f"✔ Wrote timed crossing segments to {state.crossing_segments_timed_path}")
    print(f"✔ Wrote {stats['inside_trips']} inside-boundary trips to {state.trips_inside_path}")
    print(f"✔ Wrote {stats['combined_trips']} combined trips to {state.combined_trips_path}")
    print(f"✔ Wrote {stats['scaled_trips']} scaled trips to {state.scaled_trips_path}")
    print(
        f"✔ Wrote {stats['scaled_trips_matched']} matched scaled trips "
        f"({stats['scaled_trips_match_ok']} match_ok) to {state.scaled_trips_matched_path}"
    )
    print(f"✔ Wrote {stats['sumo_trips_xml_rows']} SUMO trips XML rows to {state.sumo_trips_path}")
    if stats["duarouter_ran"]:
        print(f"✔ Wrote {stats['sumo_routes_rows']} routed trips ({stats['sumo_routes_pct']:.2f}%) to {state.sumo_routes_path}")
        print(
            f"✔ Wrote {stats['sumo_unrouted_rows']} unrouted trips "
            f"({stats['sumo_unrouted_pct']:.2f}%) to {state.sumo_unrouted_trips_path}"
        )
        print(f"✔ Wrote duarouter log to {state.duarouter_log_path}")
    else:
        print("✔ Skipped duarouter routing (run_duarouter: false)")

    if config["write_review_csv"]:
        timed_review = write_review_csv(state.crossing_segments_timed_path)
        inside_review = write_review_csv(state.trips_inside_path)
        combined_review = write_review_csv(state.combined_trips_path)
        scaled_review = write_review_csv(state.scaled_trips_path)
        matched_review = write_review_csv(state.scaled_trips_matched_path)
        print(f"✔ Wrote review CSV: {timed_review}")
        print(f"✔ Wrote review CSV: {inside_review}")
        print(f"✔ Wrote review CSV: {combined_review}")
        print(f"✔ Wrote review CSV: {scaled_review}")
        print(f"✔ Wrote review CSV: {matched_review}")

    print("\nPipeline step successful.")


if __name__ == "__main__":
    main()
