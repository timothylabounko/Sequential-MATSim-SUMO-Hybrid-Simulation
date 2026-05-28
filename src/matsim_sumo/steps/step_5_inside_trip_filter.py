from typing import Iterable

import geopandas as gpd
import pandas as pd
from shapely import points

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep
from matsim_sumo.pipeline_state import PipelineState


def filter_inside_car_trips(
    trips_path,
    boundary_path=None,
    out_path=None,
    modes_keep: Iterable[str] | None = None,
    boundary=None,
):
    trips = pd.read_csv(trips_path, sep=";", low_memory=False)
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
        CsvWriter.write_df(out, out_path)
    return out


@disk_cache(
    depends_on=lambda trips_path, boundary_path, modes_keep, boundary: [
        file_dep(trips_path),
        file_dep(boundary_path),
        param_dep("modes_keep", tuple(modes_keep or ())),
    ],
    step_name="inside_trip_filter",
)
def _filter_inside_car_trips_cached(
    trips_path,
    boundary_path,
    modes_keep: Iterable[str],
    boundary,
) -> pd.DataFrame:
    return filter_inside_car_trips(
        trips_path=trips_path,
        boundary_path=boundary_path,
        modes_keep=modes_keep,
        boundary=boundary,
    )


def run_inside_trip_filter_stage(state: PipelineState) -> None:
    if state.boundary is None:
        state.boundary = gpd.read_file(state.boundary_path).union_all()

    state.trips_inside = _filter_inside_car_trips_cached(
        trips_path=state.trips_path,
        boundary_path=state.boundary_path,
        modes_keep=state.config["modes_keep"],
        boundary=state.boundary,
        no_cache=bool(state.config.get("no_cache", False)),
    )
    if output_needs_refresh(_filter_inside_car_trips_cached, state.trips_inside_path):
        CsvWriter.write_df(state.trips_inside, state.trips_inside_path)
    
