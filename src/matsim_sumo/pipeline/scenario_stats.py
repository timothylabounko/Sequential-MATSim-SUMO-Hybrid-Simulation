from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import geopandas as gpd
import pandas as pd
import sumolib

from matsim_sumo.pipeline.state import PipelineState


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _csv_row_count(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as src:
        header = src.readline()
        if not header:
            return 0
        return sum(1 for _ in src)


def _frame_len(frame: Any, path: Path | None = None) -> int | None:
    if frame is not None:
        return int(len(frame))
    return _csv_row_count(path)


def _read_match_ok_count(state: PipelineState) -> int | None:
    if state.scaled_trips_matched is not None and "match_ok" in state.scaled_trips_matched.columns:
        return int(state.scaled_trips_matched["match_ok"].sum())
    if not state.scaled_trips_matched_path.exists():
        return None
    try:
        chunks = pd.read_csv(state.scaled_trips_matched_path, sep=";", usecols=["match_ok"], chunksize=250_000)
    except ValueError:
        return _csv_row_count(state.scaled_trips_matched_path)
    return int(sum(chunk["match_ok"].fillna(False).astype(bool).sum() for chunk in chunks))


def _xml_count(path: Path | None, tags: set[str]) -> int | None:
    if path is None or not path.exists():
        return None
    count = 0
    for _, elem in ET.iterparse(path, events=("end",)):
        if _local_name(elem.tag) in tags:
            count += 1
        elem.clear()
    return count


def _boundary_area_km2(state: PipelineState) -> float | None:
    boundary = state.boundary
    if boundary is None:
        if not state.boundary_path.exists():
            return None
        boundary = gpd.read_file(state.boundary_path)
    if hasattr(boundary, "area") and not hasattr(boundary, "geometry"):
        return float(boundary.area / 1_000_000.0)
    if getattr(boundary, "crs", None) is not None and not boundary.crs.is_projected:
        boundary = boundary.to_crs("EPSG:2056")
    return float(boundary.geometry.area.sum() / 1_000_000.0)


def _sumo_net(state: PipelineState) -> Any:
    if state.sumo_net is not None:
        return state.sumo_net
    if not state.sumo_network_path.exists():
        return None
    return sumolib.net.readNet(str(state.sumo_network_path), withInternal=False)


def _sumo_network_geo_count(state: PipelineState) -> int | None:
    if state.sumo_network_geo is not None:
        return int(len(state.sumo_network_geo))
    if state.sumo_network_lv95_path.exists():
        return int(len(gpd.read_file(state.sumo_network_lv95_path)))
    return None


def _row(section: str, metric: str, value: Any, unit: str = "count") -> dict[str, Any]:
    if isinstance(value, float):
        value = round(value, 6)
    return {"section": section, "metric": metric, "value": "" if value is None else value, "unit": unit}


def build_scenario_stats(state: PipelineState) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    rows.append(_row("Scenario", "boundary_area", _boundary_area_km2(state), "km2"))

    net = _sumo_net(state)
    rows.extend(
        [
            _row("SUMO network", "edges_total", len(net.getEdges(withInternal=False)) if net else None),
            _row("SUMO network", "nodes_total", len(net.getNodes()) if net else None),
            _row("SUMO network", "tls_total", len(net.getTrafficLights()) if net else None),
            _row("SUMO network", "vclass_edges", _sumo_network_geo_count(state)),
        ]
    )

    plan_legs = _frame_len(state.car_legs, state.car_legs_path)
    crossing_legs = _frame_len(state.car_legs_cut, state.car_legs_cut_path)
    crossing_segments = _frame_len(state.crossing_segments, state.crossing_segments_path)
    crossing_segments_timed = _frame_len(state.crossing_segments_timed, state.crossing_segments_timed_path)
    inside_trips = _frame_len(state.trips_inside, state.trips_inside_path)
    combined_trips = _frame_len(state.combined_trips, state.combined_trips_path)
    scaled_trips = _frame_len(state.scaled_trips, state.scaled_trips_path)
    matched_trips = _frame_len(state.scaled_trips_matched, state.scaled_trips_matched_path)
    match_ok_trips = _read_match_ok_count(state)
    sumo_trips = state.sumo_trips_count if state.sumo_trips_count is not None else _xml_count(state.sumo_trips_path, {"trip"})

    rows.extend(
        [
            _row("Demand pipeline", "plan_legs_extracted", plan_legs),
            _row("Demand pipeline", "crossing_legs", crossing_legs),
            _row("Demand pipeline", "crossing_segments", crossing_segments),
            _row("Demand pipeline", "timed_crossing_segments", crossing_segments_timed),
            _row(
                "Demand pipeline",
                "crossing_segments_outside_boundary_dropped",
                state.crossing_segments_outside_boundary_dropped_count,
            ),
            _row("Demand pipeline", "inside_trips", inside_trips),
            _row("Demand pipeline", "unscaled_combined_trips", combined_trips),
            _row("Demand pipeline", "scaled_trips", scaled_trips),
            _row("Demand pipeline", "matched_trips", matched_trips),
            _row("Demand pipeline", "match_ok_trips", match_ok_trips),
            _row("Demand pipeline", "sumo_trips_written", sumo_trips),
        ]
    )

    dropped_total = state.sumo_endpoint_guardrail_dropped_count
    if dropped_total is None and scaled_trips is not None and matched_trips is not None:
        dropped_total = max(int(scaled_trips) - int(matched_trips), 0)
    rows.append(_row("Matching drops", "total", dropped_total))
    drops = state.sumo_endpoint_guardrail_dropped_by_type or {}
    if drops:
        rows.extend(_row("Matching drops", reason, count) for reason, count in sorted(drops.items()))
    elif dropped_total:
        rows.append(_row("Matching drops", "unknown_reason", dropped_total))

    routes_input = state.sumo_routes_input_count if state.sumo_routes_input_count is not None else sumo_trips
    routed = state.sumo_routes_count
    if routed is None:
        routed = _xml_count(state.sumo_routes_path, {"vehicle", "trip", "flow"})
    unrouted = state.sumo_unrouted_count
    if unrouted is None:
        unrouted = _xml_count(state.sumo_unrouted_trips_path, {"trip"})
    if state.sumo_unrouted_pct is not None:
        unrouted_pct = state.sumo_unrouted_pct
    elif routes_input:
        unrouted_pct = 100.0 * float(unrouted or 0) / float(routes_input)
    else:
        unrouted_pct = None
    duarouter_ran = bool(
        state.sumo_routes_count is not None
        or state.config.get("run_duarouter", False)
        or (state.sumo_routes_path.exists() and state.duarouter_log_path.exists())
    )

    rows.extend(
        [
            _row("Routing", "duarouter_ran", duarouter_ran, "bool"),
            _row("Routing", "routed_trips", routed),
            _row("Routing", "unrouted_trips", unrouted),
            _row("Routing", "unrouted_percentage", unrouted_pct, "percent"),
        ]
    )
    return pd.DataFrame(rows, columns=["section", "metric", "value", "unit"])


def write_scenario_stats(state: PipelineState) -> Path:
    stats = build_scenario_stats(state)
    state.scenario_stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats.to_csv(state.scenario_stats_path, index=False)
    state.scenario_stats = stats
    return state.scenario_stats_path


def run_scenario_stats_helper(state: PipelineState) -> None:
    write_scenario_stats(state)
