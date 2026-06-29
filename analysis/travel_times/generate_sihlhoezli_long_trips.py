from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import sumolib
from pyproj import Transformer

TRAVEL_TIMES = Path(__file__).resolve().parent
ROOT = TRAVEL_TIMES.parents[1]
if str(TRAVEL_TIMES) not in sys.path:
    sys.path.insert(0, str(TRAVEL_TIMES))

from generate_random_trips import _directed_passenger_graph, _edge_rows


DEFAULT_NETWORK = ROOT / "data" / "1_scenario_sihlhoelzli" / "network_scenario_sihlhoelzli_adapted.net.xml"
DEFAULT_SOURCE_DIR = TRAVEL_TIMES / "3_output_scenario_sihlhoelzli"
DEFAULT_OUTPUT_DIR = TRAVEL_TIMES / "4_output_scenario_sihlhoezli"
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "random_trips_5000_sihlhoezli_min2km_sumo_routable.csv"


def _largest_inside_component(net, boundary_path: Path, vclass: str):
    rows = _edge_rows(net, boundary_path, vclass)
    inside_ids = {edge_id for edge_id, _, _ in rows}
    directed = _directed_passenger_graph(net, vclass)

    graph = nx.DiGraph()
    for edge_id, next_edges in directed.items():
        if edge_id not in inside_ids:
            continue
        graph.add_node(edge_id)
        for next_edge_id in next_edges:
            if next_edge_id in inside_ids:
                graph.add_edge(edge_id, next_edge_id)

    components = sorted(nx.strongly_connected_components(graph), key=len, reverse=True)
    if not components:
        raise RuntimeError(f"No strongly connected {vclass} component found inside {boundary_path}")
    keep = set(components[0])
    return [row for row in rows if row[0] in keep], graph.subgraph(keep).copy()


def _route_length_m(path, edge_lengths, from_pos, to_pos):
    if len(path) < 2:
        return 0.0
    first = max(edge_lengths[path[0]] - from_pos, 0.0)
    middle = sum(edge_lengths[edge_id] for edge_id in path[1:-1])
    last = max(to_pos, 0.0)
    return first + middle + last


def generate_trips(
    network_path: Path,
    boundary_path: Path,
    output_path: Path,
    count: int,
    min_route_distance_km: float,
    start_s: int,
    end_s: int,
    seed: int,
    vclass: str,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    net = sumolib.net.readNet(str(network_path), withInternal=False)
    rows, graph = _largest_inside_component(net, boundary_path, vclass)

    edge_ids = np.array([edge_id for edge_id, _, _ in rows], dtype=object)
    lines = np.array([line for _, line, _ in rows], dtype=object)
    lv95_lengths = np.array([line.length for _, line, _ in rows], dtype=float)
    sumo_lengths = np.array([length for _, _, length in rows], dtype=float)
    edge_lengths = dict(zip(edge_ids, sumo_lengths))

    weighted = nx.DiGraph()
    for u, v in graph.edges:
        weighted.add_edge(u, v, weight=edge_lengths[v])

    to_wgs84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True).transform
    path_cache: dict[tuple[str, str], list[str] | None] = {}

    trips = []
    attempts = 0
    max_attempts = count * 2000
    while len(trips) < count and attempts < max_attempts:
        attempts += 1
        oi, di = rng.integers(0, len(edge_ids), size=2)
        if oi == di:
            continue

        from_edge = str(edge_ids[oi])
        to_edge = str(edge_ids[di])
        key = (from_edge, to_edge)
        if key not in path_cache:
            try:
                path_cache[key] = nx.shortest_path(weighted, from_edge, to_edge, weight="weight")
            except nx.NetworkXNoPath:
                path_cache[key] = None
        path = path_cache[key]
        if not path:
            continue

        of = float(rng.random())
        df = float(rng.random())
        from_pos = of * sumo_lengths[oi]
        to_pos = df * sumo_lengths[di]
        route_distance_km = _route_length_m(path, edge_lengths, from_pos, to_pos) / 1000.0
        if route_distance_km < min_route_distance_km:
            continue

        op = lines[oi].interpolate(of * lv95_lengths[oi])
        dp = lines[di].interpolate(df * lv95_lengths[di])
        from_lon, from_lat = to_wgs84(op.x, op.y)
        to_lon, to_lat = to_wgs84(dp.x, dp.y)
        depart = int(rng.integers(start_s, end_s + 1))
        trips.append(
            {
                "trip_id": f"random_{len(trips):05d}",
                "depart_time": depart,
                "departure_hour": depart // 3600,
                "from_edge": from_edge,
                "to_edge": to_edge,
                "from_pos": from_pos,
                "to_pos": to_pos,
                "from_lon": from_lon,
                "from_lat": from_lat,
                "to_lon": to_lon,
                "to_lat": to_lat,
                "origin_x": op.x,
                "origin_y": op.y,
                "destination_x": dp.x,
                "destination_y": dp.y,
                "euclidean_distance_km": float(op.distance(dp) / 1000.0),
                "sumo_shortest_route_distance_km": route_distance_km,
                "sumo_shortest_route_edge_count": len(path),
            }
        )

    if len(trips) < count:
        raise RuntimeError(f"Generated {len(trips)} trips after {attempts} attempts; requested {count}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(trips)
    df.to_csv(output_path, index=False)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SUMO-routable Sihlhoelzli trips with route length >= 2 km.")
    parser.add_argument("--network", type=Path, default=DEFAULT_NETWORK)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--min-route-distance-km", type=float, default=2.0)
    parser.add_argument("--start-s", type=int, default=18000)
    parser.add_argument("--end-s", type=int, default=79200)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--vclass", default="passenger")
    args = parser.parse_args()

    boundary_src = args.source_dir / "sihlhoelzli_boundary_lv95.geojson"
    router_src = args.source_dir / "router_config_50pct.xml"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(boundary_src, args.output_dir / boundary_src.name)
    shutil.copy2(router_src, args.output_dir / router_src.name)

    trips = generate_trips(
        args.network,
        args.output_dir / boundary_src.name,
        args.output,
        args.count,
        args.min_route_distance_km,
        args.start_s,
        args.end_s,
        args.seed,
        args.vclass,
    )
    print(f"Wrote {len(trips)} trips to {args.output}")
    print(f"Minimum SUMO shortest-route distance: {trips['sumo_shortest_route_distance_km'].min():.3f} km")
    print(f"Maximum SUMO shortest-route distance: {trips['sumo_shortest_route_distance_km'].max():.3f} km")
    print(f"Maximum Euclidean distance inside boundary: {trips['euclidean_distance_km'].max():.3f} km")


if __name__ == "__main__":
    main()
