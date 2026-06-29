import argparse
from datetime import datetime
import json
import os
import shutil
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _plot.scatter import plot_scatter
from _plot.violin import plot_violin
from generate_random_trips import generate_random_trips
from matsim_router.get import read_routed_trips
from matsim_router.route import route_csv
from sumo_router.estimate_from_edges import (
    estimate_travel_times,
    load_hourly_edge_travel_times,
    run_duarouter as run_edge_duarouter,
    write_trips_xml as write_edge_trips_xml,
)
from sumo_router.route_random_trips import route_random_trips
from tomtom_router.get_from_tomtom import convert_crs, route_one_trip


def resolve_path(path):
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def load_config(path):
    with open(resolve_path(path)) as f:
        cfg = yaml.safe_load(f)
    env_var = cfg.get("tomtom_api_key_env")
    if env_var and not cfg.get("tomtom_api_key"):
        cfg["tomtom_api_key"] = os.environ.get(env_var)
    return cfg


def resolve_sumo_network(cfg):
    if cfg.get("sumo_network_path"):
        path = Path(cfg["sumo_network_path"])
        return path if path.is_absolute() else resolve_path(Path(cfg.get("sumo_input_dir", "")) / path)
    return _sumo_config_path(cfg["sumo_output_dir"], "net-file")


def _sumo_base_dir(sumo_output_dir):
    path = resolve_path(sumo_output_dir)
    return path if path.is_dir() else path.parent


def _sumo_config_snapshot(sumo_output_dir):
    root = _sumo_base_dir(sumo_output_dir)
    candidates = [
        root / "sumo_config_snapshot.txt",
        root / "config_snapshot.sumocfg",
        root / "sumo.sumocfg",
    ]
    candidates.extend(sorted(root.glob("*.sumocfg")))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _sumo_config_path(sumo_output_dir, tag):
    cfg_path = _sumo_config_snapshot(sumo_output_dir)
    if cfg_path is None:
        raise KeyError(f"sumo_network_path is required because no SUMO config snapshot was found below {sumo_output_dir}")
    root = ET.parse(cfg_path).getroot()
    elem = root.find(f".//{tag}")
    if elem is None or not elem.get("value"):
        raise KeyError(f"Missing <{tag} value=...> in {cfg_path}")
    path = Path(elem.get("value"))
    if path.is_absolute():
        return path
    direct = (cfg_path.parent / path).resolve()
    if direct.exists():
        return direct
    stripped = Path(*[part for part in path.parts if part != ".."])
    for base in [cfg_path.parent, *cfg_path.parent.parents]:
        candidate = (base / stripped).resolve()
        if candidate.exists():
            return candidate
        if tag == "net-file" and len(path.parts) == 1:
            candidate = (base / "networks" / path).resolve()
            if candidate.exists():
                return candidate
    return direct


def resolve_sumo_binary(cfg, name):
    if cfg.get(name):
        return cfg[name]
    env_root = _sumo_env_root(cfg["sumo_output_dir"])
    if env_root:
        return str(env_root / "bin" / ("duarouter" if name == "duarouter_bin" else "sumo"))
    return "duarouter" if name == "duarouter_bin" else "sumo"


def _sumo_env_root(sumo_output_dir):
    for parent in _sumo_base_dir(sumo_output_dir).parents:
        if parent.name == "sumo-env":
            return parent
    return None


def resolve_random_trips_file(cfg):
    path = Path(cfg.get("travel_time_random_trips_file", "random_trips_25000.csv"))
    return path if path.is_absolute() else ROOT / path


def _run_root(cfg):
    path = Path(cfg.get("travel_time_analysis_dir", "travel_times_random"))
    return path if path.is_absolute() else ROOT / path


def _new_run_dir(root):
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"travel_times_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not base.exists():
        return base
    i = 1
    while (root / f"{base.name}_{i:02d}").exists():
        i += 1
    return root / f"{base.name}_{i:02d}"


def _latest_run_dir(root):
    runs = sorted(p for p in root.glob("travel_times_*") if p.is_dir())
    if not runs:
        raise FileNotFoundError(f"No travel-time run folders found below {root}")
    return runs[-1]


def _resolve_run_dir(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def resolve_matsim_config(cfg, matsim_dir):
    if cfg.get("matsim_config_file"):
        return resolve_path(cfg["matsim_config_file"])
    router_config = matsim_dir / "router_config.xml"
    return router_config if router_config.exists() else matsim_dir / "output_config.xml"


def paths_from_config(cfg, run_dir=None, latest=False):
    run_root = _run_root(cfg)
    work_dir = _resolve_run_dir(run_dir) if run_dir else (_latest_run_dir(run_root) if latest else _new_run_dir(run_root))
    matsim_dir = resolve_path(cfg["matsim_output_dir"])
    stable_random_trips = resolve_random_trips_file(cfg)
    return {
        "run_root": run_root,
        "work_dir": work_dir,
        "stable_random_trips": stable_random_trips,
        "random_trips": work_dir / stable_random_trips.name,
        "boundary": resolve_path(cfg.get("travel_time_boundary_file", "data/boundaries/zurich_boundary.geojson")),
        "sumo_network": resolve_sumo_network(cfg),
        "sumo_dir": resolve_path(cfg["sumo_output_dir"]),
        "sumo_work_dir": work_dir / "sumo",
        "sumo_edge_source_dir": resolve_path(cfg.get("sumo_edge_output_dir", cfg["sumo_output_dir"])),
        "sumo_edge_work_dir": work_dir / "sumo_edge_estimate",
        "sumo_edge_result": work_dir / cfg.get("sumo_edge_comparison_file", "travel_times_edge_estimate.csv"),
        "matsim_dir": matsim_dir,
        "matsim_config": resolve_matsim_config(cfg, matsim_dir),
        "events": matsim_dir / cfg.get("events_file", "output_events.xml.gz"),
        "jar": resolve_path(cfg.get("matsim_router_jar", "lib/switzerland_run.jar")),
        "matsim_input": work_dir / "matsim_router_input.csv",
        "matsim_output": work_dir / "matsim_routed.csv",
        "tomtom_raw": work_dir / "tomtom_raw.json",
        "tomtom_errors": work_dir / "tomtom_errors.csv",
        "result": work_dir / "travel_times.csv",
    }


def copy_previous_artifacts(paths):
    try:
        previous = _latest_run_dir(paths["run_root"])
    except FileNotFoundError:
        return
    if previous == paths["work_dir"]:
        return
    for rel in ["tomtom_raw.json", "matsim_routed.csv", "travel_times.csv", "sumo/sumo_travel_times.csv"]:
        src, dst = previous / rel, paths["work_dir"] / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def sumo_scenario_label(sumo_dir):
    parts = Path(sumo_dir).parts
    kind = next((p.split("_")[1] for p in parts if p.startswith("output_") and ("micro" in p or "meso" in p)), "")
    pct = next((p.rsplit("_", 1)[-1].replace("pct", "%") for p in parts if p.startswith("scale_") and p.endswith("pct")), "")
    return "SUMO" + (f" {kind}" if kind else "") + (f" {pct}" if pct else "")


def ensure_random_trips(cfg, paths):
    paths["work_dir"].mkdir(parents=True, exist_ok=True)
    if paths["random_trips"].exists():
        return pd.read_csv(paths["random_trips"])
    if paths["stable_random_trips"].exists():
        if paths["stable_random_trips"].resolve() != paths["random_trips"].resolve():
            shutil.copy2(paths["stable_random_trips"], paths["random_trips"])
        return pd.read_csv(paths["random_trips"])
    paths["stable_random_trips"].parent.mkdir(parents=True, exist_ok=True)
    trips = generate_random_trips(
        paths["sumo_network"],
        paths["boundary"],
        paths["stable_random_trips"],
        int(cfg.get("travel_time_random_trip_count", 25000)),
        int(cfg.get("travel_time_departure_start_s", 18000)),
        int(cfg.get("travel_time_departure_end_s", 79200)),
        int(cfg.get("travel_time_random_seed", 31)),
        float(cfg.get("min_trip_distance_km", 3)),
        cfg.get("travel_time_sumo_vclass", "passenger"),
    )
    if paths["stable_random_trips"].resolve() != paths["random_trips"].resolve():
        shutil.copy2(paths["stable_random_trips"], paths["random_trips"])
    return trips


def router_input(trips):
    return pd.DataFrame({
        "identifier": trips["trip_id"].astype(str),
        "origin_x": trips["origin_x"],
        "origin_y": trips["origin_y"],
        "destination_x": trips["destination_x"],
        "destination_y": trips["destination_y"],
        "departure_time": trips["depart_time"].astype(int),
    })


def tomtom_input(trips):
    out = router_input(trips)
    out["origin_x"], out["origin_y"] = convert_crs(out["origin_x"], out["origin_y"])
    out["destination_x"], out["destination_y"] = convert_crs(out["destination_x"], out["destination_y"])
    return out


def load_tomtom_raw(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def write_tomtom_raw(raw, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(raw, f)
    tmp.replace(path)


def route_tomtom_incremental(cfg, trips, paths):
    if not cfg.get("tomtom_api_key"):
        env_var = cfg.get("tomtom_api_key_env")
        hint = f" or set {env_var}" if env_var else ""
        raise KeyError(f"Missing tomtom_api_key in config{hint}.")
    raw = load_tomtom_raw(paths["tomtom_raw"])
    done = set(map(str, raw))
    remaining = trips[~trips["trip_id"].astype(str).isin(done)]
    limit = int(cfg.get("tomtom_daily_request_limit", 2500))
    errors = []
    if limit <= 0 or remaining.empty:
        return raw

    paths["work_dir"].mkdir(parents=True, exist_ok=True)
    save_every = max(1, int(cfg.get("tomtom_save_every", 50)))
    saved_since_write = 0
    def safe_message(exc):
        return str(exc).replace(cfg["tomtom_api_key"], "<redacted>")

    for row in tomtom_input(remaining).head(limit).itertuples(index=False):
        try:
            route_info = route_one_trip(row, cfg["tomtom_api_key"])
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            errors.append({"trip_id": row.identifier, "error": type(exc).__name__, "status": status, "message": safe_message(exc)})
            print(f"TomTom failed for {row.identifier}: HTTP {status}")
            if status in {401, 403, 429}:
                print("Stopping TomTom routing because the API rejected the key/quota/rate limit.")
                break
            continue
        except requests.RequestException as exc:
            errors.append({"trip_id": row.identifier, "error": type(exc).__name__, "status": pd.NA, "message": safe_message(exc)})
            print(f"TomTom failed for {row.identifier}: {type(exc).__name__}")
            continue

        raw[str(row.identifier)] = {
            "origin_x": row.origin_x,
            "origin_y": row.origin_y,
            "destination_x": row.destination_x,
            "destination_y": row.destination_y,
            "departure_time": int(row.departure_time),
            "route_info": route_info,
        }
        saved_since_write += 1
        if saved_since_write >= save_every:
            write_tomtom_raw(raw, paths["tomtom_raw"])
            saved_since_write = 0
        time.sleep(float(cfg.get("tomtom_request_delay_s", 0.1)))

    if saved_since_write:
        write_tomtom_raw(raw, paths["tomtom_raw"])
    if errors:
        pd.DataFrame(errors).to_csv(paths["tomtom_errors"], index=False)
    return raw


def _scaled(summary, key, divisor):
    value = summary.get(key)
    return pd.NA if value is None else value / divisor


def tomtom_times(raw):
    rows = []
    for trip_id, data in raw.items():
        summary = data.get("route_info", {}).get("summary", {})
        rows.append({
            "trip_id": str(trip_id),
            "tomtom_distance_km": _scaled(summary, "lengthInMeters", 1000),
            "tomtom_travel_time_min": _scaled(summary, "travelTimeInSeconds", 60),
            "tomtom_historic_travel_time_min": _scaled(summary, "historicTrafficTravelTimeInSeconds", 60),
            "tomtom_no_traffic_travel_time_min": _scaled(summary, "noTrafficTravelTimeInSeconds", 60),
            "tomtom_live_incidents_travel_time_min": _scaled(summary, "liveTrafficIncidentsTravelTimeInSeconds", 60),
            "tomtom_traffic_delay_min": _scaled(summary, "trafficDelayInSeconds", 60),
        })
    return pd.DataFrame(rows)


def route_matsim(cfg, trips, paths):
    router_input(trips).to_csv(paths["matsim_input"], index=False)
    route_csv(
        paths["matsim_config"],
        paths["matsim_input"],
        paths["events"],
        paths["matsim_output"],
        paths["matsim_dir"],
        paths["jar"],
        cfg.get("threads", 4),
        True,
        cfg.get("matsim_router_java_xmx", "8g"),
    )
    return read_matsim_existing(paths)


def read_matsim_existing(paths):
    if not paths["matsim_output"].exists():
        return pd.DataFrame(columns=["trip_id"])
    df = read_routed_trips(paths["matsim_output"])
    return df.rename(columns={"identifier": "trip_id"})


def route_sumo(cfg, paths):
    background_routes = cfg.get("sumo_background_routes_file")
    if background_routes:
        background_routes = resolve_path(background_routes)
    include_background = cfg.get("sumo_include_background_routes", bool(background_routes))
    out = route_random_trips(
        paths["sumo_network"],
        paths["sumo_dir"],
        paths["random_trips"],
        paths["sumo_work_dir"],
        resolve_sumo_binary(cfg, "sumo_bin"),
        cfg.get("travel_time_sumo_vclass", "passenger"),
        False,
        background_routes,
        cfg.get("sumo_simulation_end_s"),
        cfg.get("sumo_simulation_begin_s", 0),
        include_background,
        cfg.get("sumo_background_mode"),
        cfg.get("sumo_random_depart_offset_s", 300),
        cfg.get("sumo_completion_buffer_s", 1800),
        cfg.get("sumo_load_state_thread_rngs", 64),
        cfg.get("sumo_load_state_threads", 8),
        cfg.get("sumo_tls_all_off", False),
        not cfg.get("sumo_skip_path_check", False),
    )
    return read_sumo_existing(out or (paths["sumo_work_dir"] / "sumo_travel_times.csv"))


def read_sumo_existing(path):
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=["trip_id"])
    df = pd.read_csv(path)
    return pd.DataFrame({
        "trip_id": df["trip_id"].astype(str),
        "sumo_distance_km": pd.to_numeric(df["route_length_m"], errors="coerce") / 1000,
        "sumo_travel_time_min": pd.to_numeric(df["travel_time_seconds"], errors="coerce") / 60,
    })


def build_results(trips, raw=None, matsim=None, sumo=None):
    result = trips.copy()
    result["trip_id"] = result["trip_id"].astype(str)
    for extra in [tomtom_times(raw or {}), matsim, sumo]:
        if extra is not None and not extra.empty:
            result = result.merge(extra, on="trip_id", how="left")
    return result


def write_results(result, paths):
    paths["work_dir"].mkdir(parents=True, exist_ok=True)
    result.to_csv(paths["result"], index=False)


def add_plots(result, paths, plots_subdir="plots"):
    if "tomtom_travel_time_min" not in result or result["tomtom_travel_time_min"].dropna().empty:
        print("Skipping plots because TomTom travel times are unavailable.")
        return []
    plots_dir = paths["work_dir"] / plots_subdir
    plot_paths = [
        plot_scatter(result, plots_dir / "scatter_all_day.png", "All day", sumo_scenario_label(paths["sumo_dir"])),
        plot_violin(result, plots_dir / "violin_all_day.png", "All day"),
    ]
    hourly_dir = plots_dir / "hourly"
    for hour in sorted(result["departure_hour"].dropna().astype(int).unique()):
        hourly = result[result["departure_hour"].eq(hour)]
        label = f"{hour:02d}:00-{hour + 1:02d}:00"
        plot_paths.append(plot_scatter(hourly, hourly_dir / f"scatter_hour_{hour:02d}.png", label, sumo_scenario_label(paths["sumo_dir"])))
        plot_paths.append(plot_violin(hourly, hourly_dir / f"violin_hour_{hour:02d}.png", label))
    return plot_paths


def report(result, paths, result_path=None):
    result_path = Path(result_path) if result_path is not None else paths["result"]
    print(f"Wrote run folder: {paths['work_dir']}")
    print(f"Wrote random trips copy: {paths['random_trips']}")
    print(f"Wrote comparison table: {result_path}")
    print(f"Trips: {len(result)}")
    for col, label in [
        ("tomtom_travel_time_min", "TomTom"),
        ("matsim_travel_time_min", "MATSim"),
        ("sumo_travel_time_min", "SUMO"),
    ]:
        if col in result:
            print(f"{label} routed: {result[col].notna().sum()} / {len(result)}")


def route_sumo_edge_estimate(cfg, paths):
    work_dir = paths["sumo_edge_work_dir"]
    work_dir.mkdir(parents=True, exist_ok=True)
    trips_xml = work_dir / "trips_edge_estimate.xml"
    routes_xml = work_dir / "routes_edge_estimate.rou.xml"
    estimate_csv = work_dir / "sumo_travel_times_edge_estimate.csv"

    trips = pd.read_csv(paths["random_trips"])
    write_edge_trips_xml(trips, trips_xml, cfg.get("travel_time_sumo_vclass", "passenger"))
    if not cfg.get("sumo_edge_reuse_routes", True) or not routes_xml.exists():
        run_edge_duarouter(paths["sumo_network"], trips_xml, routes_xml, resolve_sumo_binary(cfg, "duarouter_bin"))

    edge_times = load_hourly_edge_travel_times(paths["sumo_edge_source_dir"])
    estimate = estimate_travel_times(
        paths["sumo_network"],
        paths["random_trips"],
        routes_xml,
        edge_times,
        estimate_csv,
        cfg.get("travel_time_sumo_vclass", "passenger"),
    )
    return pd.DataFrame({
        "trip_id": estimate["trip_id"].astype(str),
        "sumo_distance_km": pd.to_numeric(estimate["route_length_m"], errors="coerce") / 1000,
        "sumo_travel_time_min": pd.to_numeric(estimate["travel_time_seconds"], errors="coerce") / 60,
    })


def main():
    parser = argparse.ArgumentParser(description="Random-trip TomTom/MATSim/SUMO travel-time analysis.")
    parser.add_argument("--config", default="configs/base_config.yaml")
    parser.add_argument("--generate-trips-only", action="store_true")
    parser.add_argument("--tomtom-only", action="store_true")
    parser.add_argument("--matsim-only", action="store_true")
    parser.add_argument("--sumo-only", action="store_true")
    parser.add_argument("--sumo-edge-only", action="store_true")
    parser.add_argument("--plots-only", action="store_true")
    parser.add_argument("--plots-edge-only", action="store_true")
    parser.add_argument("--run-dir", help="Existing or new run folder below analysis/travel_times, or an absolute path.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    paths = paths_from_config(cfg, args.run_dir, latest=(args.plots_only or args.plots_edge_only) and not args.run_dir)

    if args.plots_only:
        result = pd.read_csv(paths["result"])
        print(f"Regenerated {len(add_plots(result, paths))} plots below {paths['work_dir'] / 'plots'}")
        return
    if args.plots_edge_only:
        result = pd.read_csv(paths["sumo_edge_result"])
        plots_subdir = cfg.get("sumo_edge_plots_dir", "plots_edge_estimate")
        print(f"Regenerated {len(add_plots(result, paths, plots_subdir))} plots below {paths['work_dir'] / plots_subdir}")
        return

    copy_previous_artifacts(paths)
    trips = ensure_random_trips(cfg, paths)
    if args.generate_trips_only:
        print(f"Wrote/reused {len(trips)} random trips at {paths['random_trips']}")
        return

    raw = load_tomtom_raw(paths["tomtom_raw"])
    matsim = read_matsim_existing(paths)
    sumo = read_sumo_existing(paths["sumo_work_dir"] / "sumo_travel_times.csv")

    if args.matsim_only:
        matsim = route_matsim(cfg, trips, paths)
        result = build_results(trips, raw, matsim, sumo)
        write_results(result, paths)
        report(result, paths)
        return

    if args.sumo_only:
        sumo = route_sumo(cfg, paths)
        result = build_results(trips, raw, matsim, sumo)
        write_results(result, paths)
        plot_paths = add_plots(result, paths)
        report(result, paths)
        print(f"Wrote {len(plot_paths)} plots below {paths['work_dir'] / 'plots'}")
        return

    if args.sumo_edge_only:
        sumo = route_sumo_edge_estimate(cfg, paths)
        result = build_results(trips, raw, matsim, sumo)
        result.to_csv(paths["sumo_edge_result"], index=False)
        plots_subdir = cfg.get("sumo_edge_plots_dir", "plots_edge_estimate")
        plot_paths = add_plots(result, paths, plots_subdir)
        report(result, paths, paths["sumo_edge_result"])
        print(f"Wrote {len(plot_paths)} edge-estimate plots below {paths['work_dir'] / plots_subdir}")
        return

    if not args.sumo_only:
        raw = route_tomtom_incremental(cfg, trips, paths)
        result = build_results(trips, raw, matsim, sumo)
        write_results(result, paths)
        if args.tomtom_only:
            report(result, paths)
            return

    matsim = route_matsim(cfg, trips, paths)
    result = build_results(trips, raw, matsim, sumo)
    write_results(result, paths)

    sumo = route_sumo(cfg, paths)
    result = build_results(trips, raw, matsim, sumo)
    write_results(result, paths)
    plot_paths = add_plots(result, paths)
    report(result, paths)
    print(f"Wrote {len(plot_paths)} plots below {paths['work_dir'] / 'plots'}")


if __name__ == "__main__":
    main()
