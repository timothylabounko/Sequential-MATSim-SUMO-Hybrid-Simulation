from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

from analysis.mode_shares.common import CHUNKSIZE, COMPARISON_MODES, endpoint_mask, find_matsim_file


def valid_person_ids(persons_path: Path) -> set[str]:
    valid: set[str] = set()
    for chunk in pd.read_csv(
        persons_path,
        sep=";",
        compression="gzip",
        usecols=["person", "subpopulation"],
        chunksize=CHUNKSIZE,
        low_memory=False,
    ):
        keep = ~chunk["subpopulation"].astype("string").isin(["crossborder", "freight"])
        valid.update(chunk.loc[keep, "person"].astype(str))
    return valid


def endpoint_mode_counts(matsim_dir: Path, boundary_geom) -> dict[str, Counter]:
    trips_path = find_matsim_file(matsim_dir, "output_trips.csv.gz")
    persons_path = find_matsim_file(matsim_dir, "output_persons.csv.gz")
    valid_persons = valid_person_ids(persons_path)
    counts = {"destination": Counter(), "origin": Counter()}

    for chunk in pd.read_csv(
        trips_path,
        sep=";",
        compression="gzip",
        usecols=["person", "main_mode", "euclidean_distance", "start_x", "start_y", "end_x", "end_y"],
        chunksize=CHUNKSIZE,
        low_memory=False,
    ):
        base = (
            chunk["person"].astype(str).isin(valid_persons)
            & pd.to_numeric(chunk["euclidean_distance"], errors="coerce").gt(1.0)
            & chunk["main_mode"].isin(COMPARISON_MODES)
        )
        origin = base & endpoint_mask(chunk, "start_x", "start_y", boundary_geom)
        destination = base & endpoint_mask(chunk, "end_x", "end_y", boundary_geom)
        counts["origin"].update(chunk.loc[origin, "main_mode"].astype(str))
        counts["destination"].update(chunk.loc[destination, "main_mode"].astype(str))

    return counts
