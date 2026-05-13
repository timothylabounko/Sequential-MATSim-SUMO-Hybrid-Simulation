#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pyogrio
import sumolib
from lxml import etree as ET
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString


OUTPUT_CRS = "EPSG:2056"
HASH_MAX = 1 << 64


@dataclass(frozen=True)
class RouteRecord:
    vehicle_id: str
    depart: str | None
    vehicle_type: str | None
    edges: tuple[str, ...]


@dataclass
class ExportStats:
    total_routes: int = 0
    sampled_routes: int = 0
    exported_routes: int = 0
    skipped_empty_geometry: int = 0
    missing_edge_references: int = 0
    routes_with_missing_edges: int = 0


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clear_element(elem) -> None:
    elem.clear()
    parent = elem.getparent()
    if parent is None:
        return
    while elem.getprevious() is not None:
        del parent[0]


def _stable_sample(vehicle_id: str, percent: float, seed: int) -> bool:
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    token = f"{seed}:{vehicle_id}".encode("utf-8")
    value = int.from_bytes(hashlib.blake2b(token, digest_size=8).digest(), "big")
    return value < (percent / 100.0) * HASH_MAX


def _route_hash(edges: Iterable[str]) -> str:
    raw = " ".join(edges).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


def _safe_edge_value(edge, method_name: str, default=None):
    method = getattr(edge, method_name, None)
    if method is None:
        return default
    try:
        value = method()
    except Exception:
        return default
    return default if value is None else value


def _build_network_geometries(network_path: Path) -> tuple[dict[str, LineString], gpd.GeoDataFrame]:
    net = sumolib.net.readNet(str(network_path), withInternal=False)
    to_lv95 = Transformer.from_crs("EPSG:4326", OUTPUT_CRS, always_xy=True).transform

    rows = []
    edge_geometries: dict[str, LineString] = {}
    for edge in net.getEdges(withInternal=False):
        shape = edge.getShape()
        if len(shape) < 2:
            continue
        geometry = LineString([to_lv95(*net.convertXY2LonLat(x, y)) for x, y in shape])
        edge_id = edge.getID()
        edge_geometries[edge_id] = geometry
        rows.append(
            {
                "edge_id": edge_id,
                "from_node": edge.getFromNode().getID(),
                "to_node": edge.getToNode().getID(),
                "length_m": float(edge.getLength()),
                "speed_mps": float(edge.getSpeed()),
                "lane_count": len(edge.getLanes()),
                "priority": _safe_edge_value(edge, "getPriority"),
                "edge_type": _safe_edge_value(edge, "getType", ""),
                "name": _safe_edge_value(edge, "getName", ""),
                "geometry": geometry,
            }
        )

    if not rows:
        raise ValueError(f"No edges with geometry found in {network_path}")
    return edge_geometries, gpd.GeoDataFrame(rows, geometry="geometry", crs=OUTPUT_CRS)


def _route_edges_from_element(elem, route_definitions: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    raw_edges = elem.get("edges")
    if raw_edges:
        return tuple(raw_edges.split())

    route_ref = elem.get("route")
    if route_ref and route_ref in route_definitions:
        return route_definitions[route_ref]

    for child in elem:
        if _local_name(child.tag) == "route":
            child_edges = child.get("edges")
            if child_edges:
                return tuple(child_edges.split())
            child_ref = child.get("ref") or child.get("route")
            if child_ref and child_ref in route_definitions:
                return route_definitions[child_ref]

    return ()


def _iter_routes(routes_path: Path) -> Iterable[RouteRecord]:
    route_definitions: dict[str, tuple[str, ...]] = {}
    stack: list[str] = []
    context = ET.iterparse(str(routes_path), events=("start", "end"), huge_tree=True, recover=True)
    for event, elem in context:
        local = _local_name(elem.tag)
        if event == "start":
            stack.append(local)
            continue

        parent = stack[-2] if len(stack) >= 2 else None
        if local == "route" and parent == "routes":
            route_id = (elem.get("id") or "").strip()
            raw_edges = elem.get("edges")
            if route_id and raw_edges:
                route_definitions[route_id] = tuple(raw_edges.split())
            _clear_element(elem)
        elif local in {"vehicle", "trip", "flow"}:
            vehicle_id = (elem.get("id") or "").strip()
            edges = _route_edges_from_element(elem, route_definitions)
            if vehicle_id and edges:
                yield RouteRecord(
                    vehicle_id=vehicle_id,
                    depart=elem.get("depart") or elem.get("begin"),
                    vehicle_type=elem.get("type"),
                    edges=edges,
                )
            _clear_element(elem)

        if stack:
            stack.pop()


def _route_geometry(edges: tuple[str, ...], edge_geometries: dict[str, LineString]) -> tuple[MultiLineString | None, int]:
    geometries = [edge_geometries[edge_id] for edge_id in edges if edge_id in edge_geometries]
    missing_count = len(edges) - len(geometries)
    if not geometries:
        return None, missing_count
    return MultiLineString(geometries), missing_count


def _route_feature(record: RouteRecord, edge_geometries: dict[str, LineString]) -> tuple[dict | None, int]:
    geometry, missing_count = _route_geometry(record.edges, edge_geometries)
    if geometry is None:
        return None, missing_count
    feature = {
        "vehicle_id": record.vehicle_id,
        "depart": record.depart,
        "vehicle_type": record.vehicle_type,
        "route_hash": _route_hash(record.edges),
        "edge_count": len(record.edges),
        "found_edge_count": len(record.edges) - missing_count,
        "missing_edge_count": missing_count,
        "first_edge": record.edges[0],
        "last_edge": record.edges[-1],
        "geometry": geometry,
    }
    return feature, missing_count


def _write_layer(
    output_path: Path,
    layer: str,
    rows: list[dict],
    geometry_type: str,
    append: bool = False,
) -> None:
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=OUTPUT_CRS)
    pyogrio.write_dataframe(gdf, output_path, layer=layer, driver="GPKG", geometry_type=geometry_type, append=append)


def _write_empty_layer(output_path: Path, layer: str, columns: dict[str, list], geometry_type: str) -> None:
    gdf = gpd.GeoDataFrame(columns, geometry=[], crs=OUTPUT_CRS)
    pyogrio.write_dataframe(gdf, output_path, layer=layer, driver="GPKG", geometry_type=geometry_type)


def _write_route_patterns(
    output_path: Path,
    pattern_counts: Counter[tuple[str, ...]],
    edge_geometries: dict[str, LineString],
) -> int:
    rows = []
    for pattern_idx, (edges, trip_count) in enumerate(pattern_counts.most_common(), start=1):
        geometry, missing_count = _route_geometry(edges, edge_geometries)
        if geometry is None:
            continue
        rows.append(
            {
                "pattern_id": pattern_idx,
                "route_hash": _route_hash(edges),
                "trip_count": int(trip_count),
                "edge_count": len(edges),
                "found_edge_count": len(edges) - missing_count,
                "missing_edge_count": missing_count,
                "first_edge": edges[0],
                "last_edge": edges[-1],
                "geometry": geometry,
            }
        )

    if rows:
        _write_layer(output_path, "route_patterns", rows, "MultiLineString")
    else:
        _write_empty_layer(
            output_path,
            "route_patterns",
            {
                "pattern_id": [],
                "route_hash": [],
                "trip_count": [],
                "edge_count": [],
                "found_edge_count": [],
                "missing_edge_count": [],
                "first_edge": [],
                "last_edge": [],
            },
            "MultiLineString",
        )
    return len(rows)


def _write_edge_usage(
    output_path: Path,
    network_edges: gpd.GeoDataFrame,
    route_counts: Counter[str],
    traversal_counts: Counter[str],
) -> int:
    if not route_counts:
        _write_empty_layer(
            output_path,
            "edge_usage",
            {
                "edge_id": [],
                "route_count": [],
                "traversal_count": [],
                "length_m": [],
                "speed_mps": [],
            },
            "LineString",
        )
        return 0

    usage = (
        network_edges[network_edges["edge_id"].isin(route_counts)]
        .loc[:, ["edge_id", "length_m", "speed_mps", "geometry"]]
        .copy()
    )
    usage["route_count"] = usage["edge_id"].map(route_counts).astype("int64")
    usage["traversal_count"] = usage["edge_id"].map(traversal_counts).fillna(0).astype("int64")
    usage = usage.sort_values(["route_count", "traversal_count", "edge_id"], ascending=[False, False, True])
    pyogrio.write_dataframe(usage, output_path, layer="edge_usage", driver="GPKG", geometry_type="LineString")
    return len(usage)


def export_routes_to_qgis(
    routes_path: Path,
    network_path: Path,
    output_path: Path,
    percent: float,
    seed: int,
    batch_size: int,
) -> ExportStats:
    if percent < 0 or percent > 100:
        raise ValueError("--percent must be between 0 and 100")
    if batch_size < 1:
        raise ValueError("--batch-size must be a positive integer")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    print(f"Loading SUMO network: {network_path}")
    edge_geometries, network_edges = _build_network_geometries(network_path)
    print(f"Writing network_edges layer ({len(network_edges):,} edges): {output_path}")
    pyogrio.write_dataframe(network_edges, output_path, layer="network_edges", driver="GPKG", geometry_type="LineString")

    stats = ExportStats()
    sampled_rows: list[dict] = []
    sampled_layer_written = False
    route_patterns: Counter[tuple[str, ...]] = Counter()
    route_counts: Counter[str] = Counter()
    traversal_counts: Counter[str] = Counter()

    print(f"Streaming routes: {routes_path}")
    for record in _iter_routes(routes_path):
        stats.total_routes += 1
        if not _stable_sample(record.vehicle_id, percent, seed):
            continue

        stats.sampled_routes += 1
        route_patterns[record.edges] += 1
        route_counts.update(set(record.edges))
        traversal_counts.update(record.edges)

        feature, missing_count = _route_feature(record, edge_geometries)
        stats.missing_edge_references += missing_count
        if missing_count:
            stats.routes_with_missing_edges += 1
        if feature is None:
            stats.skipped_empty_geometry += 1
            continue

        sampled_rows.append(feature)
        stats.exported_routes += 1

        if len(sampled_rows) >= batch_size:
            _write_layer(
                output_path,
                "sampled_routes",
                sampled_rows,
                "MultiLineString",
                append=sampled_layer_written,
            )
            sampled_layer_written = True
            sampled_rows = []
            print(f"  exported {stats.exported_routes:,}/{stats.sampled_routes:,} sampled routes...")

    if sampled_rows:
        _write_layer(
            output_path,
            "sampled_routes",
            sampled_rows,
            "MultiLineString",
            append=sampled_layer_written,
        )
        sampled_layer_written = True

    if not sampled_layer_written:
        _write_empty_layer(
            output_path,
            "sampled_routes",
            {
                "vehicle_id": [],
                "depart": [],
                "vehicle_type": [],
                "route_hash": [],
                "edge_count": [],
                "found_edge_count": [],
                "missing_edge_count": [],
                "first_edge": [],
                "last_edge": [],
            },
            "MultiLineString",
        )

    pattern_count = _write_route_patterns(output_path, route_patterns, edge_geometries)
    usage_count = _write_edge_usage(output_path, network_edges, route_counts, traversal_counts)

    print("Done.")
    print(f"  total routes parsed:      {stats.total_routes:,}")
    print(f"  sampled routes:           {stats.sampled_routes:,} ({percent:g}%, seed={seed})")
    print(f"  sampled routes exported:  {stats.exported_routes:,}")
    print(f"  unique route patterns:    {len(route_patterns):,} ({pattern_count:,} exported)")
    print(f"  used network edges:       {usage_count:,}")
    print(f"  missing edge references:  {stats.missing_edge_references:,}")
    print(f"  routes with missing refs: {stats.routes_with_missing_edges:,}")
    print(f"  routes without geometry:  {stats.skipped_empty_geometry:,}")
    print(f"  output:                   {output_path}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export sampled SUMO routes and the SUMO network to a QGIS-ready GeoPackage."
    )
    parser.add_argument(
        "--routes",
        required=True,
        type=Path,
        help="SUMO .rou.xml file containing routed vehicles.",
    )
    parser.add_argument(
        "--network",
        required=True,
        type=Path,
        help="SUMO .net.xml file used for routing.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output GeoPackage path.",
    )
    parser.add_argument(
        "--percent",
        type=float,
        default=1.0,
        help="Percentage of routed vehicles to export, e.g. 1 means 1%%. Default: 1.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed used for deterministic vehicle-id sampling. Default: 42.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10_000,
        help="Number of sampled route features to write per GeoPackage batch. Default: 10000.",
    )
    args = parser.parse_args()

    export_routes_to_qgis(
        routes_path=args.routes,
        network_path=args.network,
        output_path=args.output,
        percent=args.percent,
        seed=args.seed,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
