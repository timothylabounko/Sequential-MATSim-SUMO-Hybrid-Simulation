import numpy as np
import pandas as pd

from matsim_sumo.io.csv_writer import CsvWriter
from matsim_sumo.pipeline.state import PipelineState


BASE_COLS = [
    "person_id",
    "trip_id",
    "mode",
    "dep_time",
    "start_link",
    "start_x",
    "start_y",
    "end_link",
    "end_x",
    "end_y",
    "crossing_type",
    "route_links",
]
TARGET_COLS = ["person_id", "trip_id", "new_trip_id", *BASE_COLS[2:]]


def _build_new_trip_ids(n: int, seed: int) -> pd.Series:
    nums = np.random.default_rng(seed).choice(np.arange(1_000_000, 10_000_000), size=n, replace=False)
    return pd.Series([f"{x}_0" for x in nums], dtype="string")


def combine_trips(cross: pd.DataFrame, inside: pd.DataFrame, seed: int) -> pd.DataFrame:
    cross_combined = cross.assign(trip_id=pd.NA)[BASE_COLS]
    inside_combined = (
        inside.rename(columns={"person": "person_id", "main_mode": "mode"})
        .assign(crossing_type="inside", route_links=pd.NA)[BASE_COLS]
    )
    out = pd.concat([cross_combined, inside_combined], ignore_index=True)
    out["new_trip_id"] = _build_new_trip_ids(len(out), seed)
    return out[TARGET_COLS]


def run_combine_trips_stage(state: PipelineState) -> None:
    if state.crossing_segments_timed is None or state.trips_inside is None:
        raise ValueError("crossing_segments_timed and trips_inside must be available before combine stage.")
    state.combined_trips = combine_trips(
        state.crossing_segments_timed,
        state.trips_inside,
        seed=int(state.config["demand_scaling_seed"]),
    )
    CsvWriter.write_df(state.combined_trips, state.combined_trips_path)
