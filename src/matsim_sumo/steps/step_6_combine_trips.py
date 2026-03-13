import pandas as pd

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState


TARGET_COLS = [
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
]


def combine_trips(cross: pd.DataFrame, inside: pd.DataFrame) -> pd.DataFrame:
    cross_combined = cross.assign(trip_id=pd.NA)[TARGET_COLS]
    inside_combined = (
        inside.rename(columns={"person": "person_id", "main_mode": "mode"})
        .assign(crossing_type="inside")[TARGET_COLS]
    )
    return pd.concat([cross_combined, inside_combined], ignore_index=True)
def run_combine_trips_stage(state: PipelineState) -> None:
    if state.crossing_segments_timed is None or state.trips_inside is None:
        raise ValueError("crossing_segments_timed and trips_inside must be available before combine stage.")
    state.combined_trips = combine_trips(state.crossing_segments_timed, state.trips_inside)
    CsvWriter.write_df(state.combined_trips, state.combined_trips_path)
