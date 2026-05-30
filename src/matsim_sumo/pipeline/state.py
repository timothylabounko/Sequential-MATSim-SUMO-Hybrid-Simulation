from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


@dataclass
class PipelineState:
    """Shared mutable state passed between pipeline stages."""

    config: dict[str, Any]

    matsim_output_dir: Path
    output_dir: Path
    boundary_path: Path
    network_path: Path
    plans_path: Path
    trips_path: Path
    events_path: Path
    extract_events_script_path: Path
    cut_links_csv_path: Path
    events_cut_csv_path: Path
    car_legs_path: Path
    car_legs_cut_path: Path
    crossing_segments_path: Path
    crossing_segments_timed_path: Path
    trips_inside_path: Path
    combined_trips_path: Path
    scaled_trips_path: Path
    sumo_network_path: Path
    sumo_network_lv95_path: Path
    scaled_trips_matched_path: Path
    sumo_trips_path: Path
    sumo_routes_path: Path
    sumo_unrouted_trips_path: Path
    duarouter_log_path: Path

    match_sumo_edges_script_path: Optional[Path] = None
    boundary: Any = None
    network: Any = None
    network_geo: Any = None
    sumo_net: Any = None
    sumo_network_geo: Any = None
    sumo_edge_indexes: Any = None
    matsim_link_angles: Optional[dict[str, float]] = None
    cut_link_ids: Optional[pd.Series] = None
    cut_link_info: Optional[pd.DataFrame] = None
    car_legs: Optional[pd.DataFrame] = None
    car_legs_cut: Optional[pd.DataFrame] = None
    crossing_segments: Optional[pd.DataFrame] = None
    events_cut: Optional[pd.DataFrame] = None
    crossing_events: Optional[pd.DataFrame] = None
    crossing_segments_timed: Optional[pd.DataFrame] = None
    crossing_segments_outside_boundary_dropped_count: Optional[int] = None
    trips_inside: Optional[pd.DataFrame] = None
    combined_trips: Optional[pd.DataFrame] = None
    scaled_trips: Optional[pd.DataFrame] = None
    scaled_trips_matched: Optional[pd.DataFrame] = None
    sumo_endpoint_guardrail_dropped_count: Optional[int] = None
    sumo_endpoint_guardrail_dropped_by_type: Optional[dict[str, int]] = None
    sumo_endpoint_distance_diagnostics: Optional[dict[str, dict[str, float | int]]] = None
    sumo_trips_input_count: Optional[int] = None
    sumo_trips_count: Optional[int] = None
    sumo_routes_input_count: Optional[int] = None
    sumo_routes_count: Optional[int] = None
    sumo_routes_pct: Optional[float] = None
    sumo_unrouted_count: Optional[int] = None
    sumo_unrouted_pct: Optional[float] = None
