import argparse
from collections import defaultdict, deque
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import sumolib
from pyproj import Transformer
from shapely.geometry import LineString


def _edge_rows(net, boundary_path, vclass="passenger"):
    boundary = gpd.read_file(boundary_path).union_all()
    to_lv95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True).transform

    rows = []
    for edge in net.getEdges(withInternal=False):
        if not edge.allows(vclass):
            continue
        shape = edge.getShape()
        if len(shape) < 2:
            continue
        xy = [to_lv95(*net.convertXY2LonLat(x, y)) for x, y in shape]
        line = LineString(xy)
        if line.length > 0 and boundary.covers(line):
            rows.append((edge.getID(), line, float(edge.getLength())))
    if not rows:
        raise ValueError(f"No {vclass} SUMO edges inside {boundary_path}")
    return rows


def _connection_allows(connection, vclass):
    try:
        return connection.allows(vclass)
    except AttributeError:
        return True


def _directed_passenger_graph(net, vclass="passenger"):
    edge_ids = {edge.getID() for edge in net.getEdges(withInternal=False) if edge.allows(vclass)}
    graph = defaultdict(list)
    for edge in net.getEdges(withInternal=False):
        edge_id = edge.getID()
        if edge_id not in edge_ids:
            continue
        for to_edge, connections in edge.getOutgoing().items():
            to_edge_id = to_edge.getID()
            if to_edge_id not in edge_ids:
                continue
            if any(_connection_allows(connection, vclass) for connection in connections):
                graph[edge_id].append(to_edge_id)
        graph.setdefault(edge_id, [])
    return graph


def _reachable_from(graph, source):
    seen = {source}
    queue = deque([source])
    while queue:
        edge_id = queue.popleft()
        for next_edge_id in graph.get(edge_id, []):
            if next_edge_id not in seen:
                seen.add(next_edge_id)
                queue.append(next_edge_id)
    return seen


def generate_random_trips(
    network_path,
    boundary_path,
    output_path,
    count=25000,
    start_s=18000,
    end_s=79200,
    seed=31,
    min_distance_km=3.0,
    vclass="passenger",
):
    rng = np.random.default_rng(seed)
    net = sumolib.net.readNet(str(network_path), withInternal=False)
    edges = _edge_rows(net, boundary_path, vclass)
    graph = _directed_passenger_graph(net, vclass)
    reachability_cache = {}
    edge_ids = np.array([edge_id for edge_id, _, _ in edges], dtype=object)
    lines = np.array([line for _, line, _ in edges], dtype=object)
    lengths = np.array([line.length for line in lines], dtype=float)
    sumo_lengths = np.array([sumo_length for _, _, sumo_length in edges], dtype=float)
    to_wgs84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True).transform

    rows = []
    attempts = 0
    max_attempts = count * 200
    while len(rows) < count and attempts < max_attempts:
        attempts += 1
        oi, di = rng.integers(0, len(edges), size=2)
        if oi == di:
            continue
        from_edge = edge_ids[oi]
        to_edge = edge_ids[di]
        if from_edge not in reachability_cache:
            reachability_cache[from_edge] = _reachable_from(graph, from_edge)
        reachable = reachability_cache[from_edge]
        if to_edge not in reachable:
            continue
        of = float(rng.random())
        df = float(rng.random())
        op = lines[oi].interpolate(of * lengths[oi])
        dp = lines[di].interpolate(df * lengths[di])
        distance_km = float(op.distance(dp) / 1000)
        if distance_km < min_distance_km:
            continue
        from_lon, from_lat = to_wgs84(op.x, op.y)
        to_lon, to_lat = to_wgs84(dp.x, dp.y)
        depart = int(rng.integers(start_s, end_s + 1))
        rows.append({
            "trip_id": f"random_{len(rows):05d}",
            "depart_time": depart,
            "departure_hour": depart // 3600,
            "from_edge": from_edge,
            "to_edge": to_edge,
            "from_pos": of * sumo_lengths[oi],
            "to_pos": df * sumo_lengths[di],
            "from_lon": from_lon,
            "from_lat": from_lat,
            "to_lon": to_lon,
            "to_lat": to_lat,
            "origin_x": op.x,
            "origin_y": op.y,
            "destination_x": dp.x,
            "destination_y": dp.y,
            "euclidean_distance_km": distance_km,
        })
    if len(rows) < count:
        raise RuntimeError(f"Generated {len(rows)} trips after {attempts} attempts; requested {count}")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    return df


def main():
    parser = argparse.ArgumentParser(description="Generate stable random Zurich trips from SUMO passenger edges.")
    parser.add_argument("--network", required=True)
    parser.add_argument("--boundary", default="data/boundaries/zurich_boundary.geojson")
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=25000)
    parser.add_argument("--start-s", type=int, default=18000)
    parser.add_argument("--end-s", type=int, default=79200)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--min-distance-km", type=float, default=3.0)
    parser.add_argument("--vclass", default="passenger")
    args = parser.parse_args()

    trips = generate_random_trips(
        args.network,
        args.boundary,
        args.output,
        args.count,
        args.start_s,
        args.end_s,
        args.seed,
        args.min_distance_km,
        args.vclass,
    )
    print(f"Wrote {len(trips)} random trips to {args.output}")


if __name__ == "__main__":
    main()
