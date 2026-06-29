import argparse
import math
import shutil
import subprocess
from pathlib import Path
from xml.etree.ElementTree import iterparse
from xml.sax.saxutils import escape

import pandas as pd
import sumolib


def _resolve_binary(binary):
    exe = shutil.which(binary) if not Path(binary).exists() else str(Path(binary))
    if exe is None:
        raise FileNotFoundError(f"Binary not found: {binary}")
    return exe


def _float_attr(elem, name, default=None):
    value = elem.get(name)
    if value is None:
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return value


def load_hourly_edge_travel_times(sumo_output_dir, min_traveltime_s=0.01):
    state_root = Path(sumo_output_dir) / "state" / "hourly"
    files = sorted(state_root.glob("*/edgedata.xml"))
    if not files:
        raise FileNotFoundError(f"No hourly edgedata.xml files found below {state_root}")

    by_hour = {}
    for path in files:
        current_hour = None
        for event, elem in iterparse(path, events=("start", "end")):
            if event == "start" and elem.tag == "interval":
                begin = _float_attr(elem, "begin")
                if begin is None:
                    current_hour = None
                else:
                    current_hour = int(begin // 3600)
                    by_hour.setdefault(current_hour, {})
            elif event == "end" and elem.tag == "edge":
                if current_hour is not None:
                    traveltime = _float_attr(elem, "traveltime")
                    edge_id = elem.get("id")
                    if edge_id and traveltime is not None and traveltime >= min_traveltime_s:
                        by_hour[current_hour][edge_id] = traveltime
                elem.clear()
    if not by_hour:
        raise ValueError(f"No edge travel times parsed from {state_root}")
    return by_hour


def write_trips_xml(trips, output_path, vclass="passenger"):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    trips = trips.sort_values(["depart_time", "trip_id"])
    with open(output_path, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        f.write(f'    <vType id="{escape(vclass)}" vClass="{escape(vclass)}"/>\n')
        for row in trips.itertuples(index=False):
            f.write(
                f'    <trip id="{escape(str(row.trip_id))}" type="{escape(vclass)}" '
                f'depart="{float(row.depart_time):.0f}" '
                f'from="{escape(str(row.from_edge))}" to="{escape(str(row.to_edge))}" '
                f'departPos="{float(row.from_pos):.2f}" arrivalPos="{float(row.to_pos):.2f}"/>\n'
            )
        f.write("</routes>\n")
    return output_path


def run_duarouter(network_path, trips_xml, routes_xml, duarouter_bin="duarouter"):
    exe = _resolve_binary(duarouter_bin)
    routes_xml = Path(routes_xml)
    routes_xml.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "--net-file",
        str(network_path),
        "--route-files",
        str(trips_xml),
        "--output-file",
        str(routes_xml),
        "--ignore-errors",
        "true",
        "--repair",
        "true",
        "--route-length",
        "true",
        "--xml-validation",
        "never",
        "--no-warnings",
        "true",
    ]
    subprocess.run(cmd, check=True)
    return routes_xml


def load_routes(routes_xml):
    routes = {}
    for _, elem in iterparse(routes_xml, events=("end",)):
        if elem.tag == "vehicle":
            vehicle_id = elem.get("id")
            route = elem.find("route")
            edges = route.get("edges", "").split() if route is not None else []
            if vehicle_id and edges:
                routes[vehicle_id] = edges
            elem.clear()
    return routes


def _hour_for_time(time_s, available_hours):
    hour = int(max(0, time_s) // 3600)
    if hour in available_hours:
        return hour
    if hour < available_hours[0]:
        return available_hours[0]
    return available_hours[-1]


def _edge_freeflow_time(edge):
    speed = float(edge.getSpeed() or 0.0)
    if speed <= 0:
        return None
    return float(edge.getLength()) / speed


def _edge_segment_lengths(net, edges, from_pos, to_pos):
    if not edges:
        return []

    segments = []
    for index, edge_id in enumerate(edges):
        edge = net.getEdge(edge_id)
        length = float(edge.getLength())
        if length <= 0:
            continue

        if len(edges) == 1:
            start = min(max(float(from_pos), 0.0), length)
            end = min(max(float(to_pos), 0.0), length)
            segment_length = max(0.0, end - start) if end >= start else length
        elif index == 0:
            start = min(max(float(from_pos), 0.0), length)
            segment_length = max(0.0, length - start)
        elif index == len(edges) - 1:
            end = min(max(float(to_pos), 0.0), length)
            segment_length = max(0.0, end)
        else:
            segment_length = length

        if segment_length > 0:
            segments.append((edge, segment_length, segment_length / length))
    return segments


def estimate_travel_times(network_path, trips_csv, routes_xml, edge_times_by_hour, output_csv, vclass="passenger"):
    net = sumolib.net.readNet(str(network_path), withInternal=False)
    trips = pd.read_csv(trips_csv)
    routes = load_routes(routes_xml)
    available_hours = sorted(edge_times_by_hour)

    rows = []
    missing_routes = 0
    failed_edges = 0
    fallback_edges_total = 0
    for row in trips.itertuples(index=False):
        trip_id = str(row.trip_id)
        edges = routes.get(trip_id)
        if not edges:
            missing_routes += 1
            continue

        depart_time = float(row.depart_time)
        current_time = depart_time
        travel_time = 0.0
        route_length = 0.0
        fallback_edges = 0
        segments = _edge_segment_lengths(net, edges, row.from_pos, row.to_pos)
        if not segments:
            failed_edges += 1
            continue

        for edge, segment_length, fraction in segments:
            edge_id = edge.getID()
            hour = _hour_for_time(current_time, available_hours)
            edge_travel_time = edge_times_by_hour.get(hour, {}).get(edge_id)
            if edge_travel_time is None:
                edge_travel_time = _edge_freeflow_time(edge)
                fallback_edges += 1
            if edge_travel_time is None:
                failed_edges += 1
                continue
            increment = float(edge_travel_time) * fraction
            travel_time += increment
            route_length += segment_length
            current_time += increment

        if route_length <= 0 or travel_time <= 0:
            failed_edges += 1
            continue

        fallback_edges_total += fallback_edges
        rows.append(
            {
                "trip_id": trip_id,
                "depart_time": depart_time,
                "arrival_time": depart_time + travel_time,
                "travel_time_seconds": travel_time,
                "route_length_m": route_length,
                "edge_count": len(edges),
                "fallback_edge_count": fallback_edges,
                "sumo_hour": int(depart_time // 3600),
                "sumo_background_mode": "hourly_edge_traveltime_estimate",
            }
        )

    result = pd.DataFrame(rows)
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    print(f"Trips input: {len(trips)}")
    print(f"Routes found: {len(routes)}")
    print(f"Estimated trips written: {len(result)}")
    print(f"Missing duarouter routes: {missing_routes}")
    print(f"Failed edge segments: {failed_edges}")
    print(f"Fallback free-flow edge uses: {fallback_edges_total}")
    print(f"Wrote estimated SUMO travel times: {output_csv}")
    return result


def write_comparison_with_estimate(comparison_input, estimate, comparison_output):
    comparison = pd.read_csv(comparison_input)
    comparison["trip_id"] = comparison["trip_id"].astype(str)
    estimate = estimate.copy()
    estimate["trip_id"] = estimate["trip_id"].astype(str)
    estimate = pd.DataFrame(
        {
            "trip_id": estimate["trip_id"],
            "sumo_distance_km": pd.to_numeric(estimate["route_length_m"], errors="coerce") / 1000,
            "sumo_travel_time_min": pd.to_numeric(estimate["travel_time_seconds"], errors="coerce") / 60,
        }
    )
    comparison = comparison.drop(columns=["sumo_distance_km", "sumo_travel_time_min"], errors="ignore")
    comparison = comparison.merge(estimate, on="trip_id", how="left")
    comparison_output = Path(comparison_output)
    comparison_output.parent.mkdir(parents=True, exist_ok=True)
    comparison.to_csv(comparison_output, index=False)
    print(f"Wrote comparison table with estimated SUMO columns: {comparison_output}")
    return comparison


def main():
    parser = argparse.ArgumentParser(description="Estimate SUMO travel times from hourly edgedata.xml outputs.")
    parser.add_argument("--network", required=True)
    parser.add_argument("--sumo-output-dir", required=True)
    parser.add_argument("--trips-csv", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--duarouter-bin", default="duarouter")
    parser.add_argument("--vclass", default="passenger")
    parser.add_argument("--output-csv")
    parser.add_argument("--routes-xml")
    parser.add_argument("--reuse-routes", action="store_true")
    parser.add_argument("--max-trips", type=int, help="Optional sample size for quick smoke tests.")
    parser.add_argument("--comparison-input", help="Existing travel_times.csv to update with estimated SUMO columns.")
    parser.add_argument("--comparison-output", help="Output CSV for the updated comparison table.")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    trips_csv = Path(args.trips_csv)
    if args.max_trips:
        trips = pd.read_csv(trips_csv).head(args.max_trips)
        trips_csv = work_dir / f"trips_estimate_sample_{args.max_trips}.csv"
        trips.to_csv(trips_csv, index=False)

    trips_xml = work_dir / "trips_edge_estimate.xml"
    routes_xml = Path(args.routes_xml) if args.routes_xml else work_dir / "routes_edge_estimate.rou.xml"
    output_csv = Path(args.output_csv) if args.output_csv else work_dir / "sumo_travel_times_edge_estimate.csv"

    trips = pd.read_csv(trips_csv)
    write_trips_xml(trips, trips_xml, args.vclass)
    if not args.reuse_routes or not routes_xml.exists():
        run_duarouter(args.network, trips_xml, routes_xml, args.duarouter_bin)

    edge_times = load_hourly_edge_travel_times(args.sumo_output_dir)
    estimate = estimate_travel_times(args.network, trips_csv, routes_xml, edge_times, output_csv, args.vclass)
    if args.comparison_input:
        comparison_output = args.comparison_output or Path(args.comparison_input).with_name("travel_times_edge_estimate.csv")
        write_comparison_with_estimate(args.comparison_input, estimate, comparison_output)


if __name__ == "__main__":
    main()
