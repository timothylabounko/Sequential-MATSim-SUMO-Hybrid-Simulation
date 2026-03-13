from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd
from shapely import points

from matsim_sumo.pipeline_state import PipelineState


def filter_inside_car_trips(
    trips_path,
    boundary_path=None,
    out_path=None,
    modes_keep: Iterable[str] | None = None,
    boundary=None,
):
    trips = pd.read_csv(trips_path, sep=";")
    if boundary is None:
        if boundary_path is None:
            raise ValueError("Either boundary or boundary_path must be provided.")
        boundary = gpd.read_file(boundary_path).union_all()

    modes = list(modes_keep or ["car"])
    dep_hour = pd.to_numeric(trips["dep_time"].astype(str).str.split(":").str[0], errors="coerce")
    dep_ok = dep_hour.lt(24)
    start_in = gpd.GeoSeries(points(trips["start_x"], trips["start_y"])).covered_by(boundary)
    end_in = gpd.GeoSeries(points(trips["end_x"], trips["end_y"])).covered_by(boundary)
    out = trips.loc[trips["main_mode"].isin(modes) & dep_ok & start_in & end_in].copy()
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(out_path, sep=";", index=False, compression="infer")
    return out


def run_inside_trip_filter_stage(state: PipelineState) -> None:
    if state.boundary is None:
        state.boundary = gpd.read_file(state.boundary_path).union_all()

    state.trips_inside = filter_inside_car_trips(
        trips_path=state.trips_path,
        out_path=state.trips_inside_path,
        modes_keep=state.config.get("modes_keep"),
        boundary=state.boundary,
    )
    