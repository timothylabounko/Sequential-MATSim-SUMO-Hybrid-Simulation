from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

from analysis.mode_shares.common import COMPARISON_MODES, endpoint_mask, within_mask


def load_data(repo_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, set]:
    persons = pd.read_pickle(repo_root / "data/MZ/data.microcensus.persons__184cb99cc4380ea2dfc836a6cb1b6f9b.p")
    trips_obj = pd.read_pickle(repo_root / "data/MZ/data.microcensus.trips__dea3ee4144d5b4ad94b57e124312a765.p")
    if isinstance(trips_obj, tuple) and len(trips_obj) == 2:
        trips, filterout_ids = trips_obj
    else:
        trips, filterout_ids = trips_obj, set()
    return persons, trips, set(filterout_ids)


def clean_trips(repo_root: Path, swiss_border_geom=None) -> pd.DataFrame:
    persons, trips, filterout_ids = load_data(repo_root)
    persons = persons[["person_id", "person_weight", "household_weight", "weekend"]].copy()
    persons["person_id"] = persons["person_id"].astype(str)
    trips = trips.copy()
    trips["person_id"] = trips["person_id"].astype(str)
    trips = trips.merge(persons, how="left", on="person_id")

    remove_ids = {str(pid) for pid in filterout_ids}
    remove_ids |= set(persons.loc[persons["weekend"].eq(True), "person_id"])
    trips = trips.loc[~trips["person_id"].isin(remove_ids)].copy()

    keep = (
        trips["household_weight"].notna()
        & trips["person_weight"].notna()
        & pd.to_numeric(trips["crowfly_distance"], errors="coerce").gt(1.0)
        & trips["mode"].isin(COMPARISON_MODES)
    )
    trips = trips.loc[keep].reset_index(drop=True)

    if swiss_border_geom is not None:
        origin_inside = within_mask(trips, "origin_x", "origin_y", swiss_border_geom)
        destination_inside = within_mask(trips, "destination_x", "destination_y", swiss_border_geom)
        trips = trips.loc[origin_inside & destination_inside].reset_index(drop=True)

    return trips


def endpoint_mode_counts(repo_root: Path, boundary_geom, swiss_border_geom=None) -> dict[str, Counter]:
    trips = clean_trips(repo_root, swiss_border_geom=swiss_border_geom)
    counts = {"destination": Counter(), "origin": Counter()}
    for endpoint, x_col, y_col in (
        ("destination", "destination_x", "destination_y"),
        ("origin", "origin_x", "origin_y"),
    ):
        inside = endpoint_mask(trips, x_col, y_col, boundary_geom)
        counts[endpoint].update(dict(trips.loc[inside].groupby("mode")["person_weight"].sum()))
    return counts
