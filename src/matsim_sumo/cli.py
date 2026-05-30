from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Sequence


def _write_review_csv(source_path: Path) -> Path:
    source = Path(source_path)
    target = source.with_suffix("") if source.suffix == ".gz" else source.with_name(f"{source.stem}.review.csv")
    target.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix == ".gz":
        with gzip.open(source, "rt", newline="") as src, target.open("w", newline="") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(source, target)
    return target


def _run_pipeline(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the MATSim-SUMO conversion pipeline.")
    parser.add_argument(
        "--config",
        default="configs/base_config.yaml",
        help="Path to a pipeline YAML config (default: configs/base_config.yaml).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass persistent disk cache and recompute cached pipeline stages.",
    )
    if any(arg in {"-h", "--help"} for arg in argv):
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    from matsim_sumo.config.loader import load_config, write_config_copy
    from matsim_sumo.io.csv_writer import CsvWriter
    from matsim_sumo.pipeline.runner import build_pipeline_state, run_pipeline_steps

    warnings.filterwarnings(
        "ignore",
        message=r".*\+init=<authority>:<code>.*",
        category=FutureWarning,
        module=r"pyproj\.crs\.crs",
    )

    config = load_config(args.config, no_cache=args.no_cache)
    CsvWriter.set_gzip_compresslevel(int(config["csv_gzip_compresslevel"]))
    state = build_pipeline_state(config)
    state.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loaded config: {args.config}")
    config_copy = write_config_copy(config, state.output_dir)
    print(f"Wrote config copy: {config_copy}")

    stats = run_pipeline_steps(state)
    print(f"Wrote {stats['cut_links']} cut links to {state.cut_links_csv_path}")
    print(f"Wrote {stats['plan_legs']} plan legs to {state.car_legs_path}")
    print(f"Wrote {stats['crossing_legs']} crossing legs to {state.car_legs_cut_path}")
    print(f"Wrote {stats['crossing_segments']} crossing segments to {state.crossing_segments_path}")
    print(f"Wrote extracted cut events to {state.events_cut_csv_path}")
    print(f"Wrote timed crossing segments to {state.crossing_segments_timed_path}")
    print(f"Wrote {stats['inside_trips']} inside-boundary trips to {state.trips_inside_path}")
    print(f"Wrote {stats['combined_trips']} combined trips to {state.combined_trips_path}")
    print(f"Wrote {stats['scaled_trips']} scaled trips to {state.scaled_trips_path}")
    print(
        f"Wrote {stats['scaled_trips_matched']} matched scaled trips "
        f"({stats['scaled_trips_match_ok']} match_ok) to {state.scaled_trips_matched_path}"
    )
    if stats.get("scaled_trips_guardrail_dropped", 0):
        dropped_by_type = ", ".join(
            f"{key}={value}" for key, value in (state.sumo_endpoint_guardrail_dropped_by_type or {}).items()
        )
        print(f"Dropped {stats['scaled_trips_guardrail_dropped']} trips by SUMO match guardrails ({dropped_by_type})")
    print(f"Wrote {stats['sumo_trips_xml_rows']} SUMO trips XML rows to {state.sumo_trips_path}")
    if stats["duarouter_ran"]:
        print(f"Wrote {stats['sumo_routes_rows']} routed trips ({stats['sumo_routes_pct']:.2f}%) to {state.sumo_routes_path}")
        print(
            f"Wrote {stats['sumo_unrouted_rows']} unrouted trips "
            f"({stats['sumo_unrouted_pct']:.2f}%) to {state.sumo_unrouted_trips_path}"
        )
        print(f"Wrote duarouter log to {state.duarouter_log_path}")
    else:
        print("Skipped duarouter routing (run_duarouter: false)")

    if config["write_review_csv"]:
        for path in (
            state.crossing_segments_timed_path,
            state.trips_inside_path,
            state.combined_trips_path,
            state.scaled_trips_path,
            state.scaled_trips_matched_path,
        ):
            print(f"Wrote review CSV: {_write_review_csv(path)}")

    print("Pipeline step successful.")
    return 0


def _delegate(command: str, argv: Sequence[str]) -> int:
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
    modules = {
        "plot-boundary": "matsim_sumo.visualization.sumo_network_boundary",
        "export-routes": "matsim_sumo.io.qgis_export",
        "analyze-mode-shares": "matsim_sumo.tools.mode_shares",
    }
    module_name = modules[command]
    module = __import__(module_name, fromlist=["main"])
    old_argv = sys.argv
    try:
        sys.argv = [f"matsim-sumo {command}", *argv]
        module.main()
    finally:
        sys.argv = old_argv
    return 0


def _print_help() -> None:
    print(
        "usage: matsim-sumo <command> [options]\n\n"
        "Commands:\n"
        "  run                 Run the full MATSim-SUMO pipeline.\n"
        "  plot-boundary       Plot the SUMO network and boundary from a config.\n"
        "  export-routes       Export SUMO routes to a GeoPackage for QGIS.\n"
        "  analyze-mode-shares Run the mode-share analysis utility.\n\n"
        "Use 'matsim-sumo <command> --help' for command options."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        _print_help()
        return 0

    command, command_args = args[0], args[1:]
    if command == "run":
        return _run_pipeline(command_args)
    if command in {"plot-boundary", "export-routes", "analyze-mode-shares"}:
        return _delegate(command, command_args)

    print(f"Unknown command: {command}", file=sys.stderr)
    _print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
