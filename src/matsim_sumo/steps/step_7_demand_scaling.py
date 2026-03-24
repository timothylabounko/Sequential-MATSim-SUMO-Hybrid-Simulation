import numpy as np
import pandas as pd
from shapely import covers, points

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState
from matsim_sumo.plot.scaled_trips_start_end_plots import run_scaled_trips_plot_stage


def _jitter_dep_time(dep_time: pd.Series, jitter_s: int, rng) -> pd.Series:
    parts = dep_time.astype(str).str.split(":", n=2, expand=True)
    if parts.shape[1] != 3:
        return dep_time
    sec = (
        pd.to_numeric(parts[0], errors="coerce") * 3600
        + pd.to_numeric(parts[1], errors="coerce") * 60
        + pd.to_numeric(parts[2], errors="coerce")
    )
    valid = sec.notna()
    if not valid.any():
        return dep_time
    sec.loc[valid] = (
        sec.loc[valid] + rng.integers(-jitter_s, jitter_s + 1, int(valid.sum()))
    ).clip(lower=0)
    h = (sec // 3600).astype("Int64").astype(str).str.zfill(2)
    m = ((sec % 3600) // 60).astype("Int64").astype(str).str.zfill(2)
    s = (sec % 60).astype("Int64").astype(str).str.zfill(2)
    out = dep_time.copy()
    out.loc[valid] = h.loc[valid] + ":" + m.loc[valid] + ":" + s.loc[valid]
    return out


def _jitter_xy(
    df: pd.DataFrame,
    mask: pd.Series,
    x_col: str,
    y_col: str,
    radius_m: float,
    rng,
    boundary=None,
) -> None:
    if int(mask.sum()) == 0:
        return
    idx = df.index[mask]
    xy = df.loc[idx, [x_col, y_col]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float, copy=True)
    valid = ~np.isnan(xy).any(axis=1)
    if not valid.any():
        return
    base = xy.copy()
    r = radius_m * np.sqrt(rng.random(valid.sum()))
    a = 2 * np.pi * rng.random(valid.sum())
    xy[valid, 0] = base[valid, 0] + r * np.cos(a)
    xy[valid, 1] = base[valid, 1] + r * np.sin(a)
    if boundary is not None:
        inside = np.asarray(covers(boundary, points(xy[valid, 0], xy[valid, 1])), dtype=bool)
        pos = np.flatnonzero(valid)
        xy[pos[~inside]] = base[pos[~inside]]
    df.loc[idx, [x_col, y_col]] = xy


def scale_demand(
    combined: pd.DataFrame,
    scale_factor: float,
    dep_time_jitter_s: int,
    coord_radius_m: float,
    seed: int,
    boundary=None,
) -> pd.DataFrame:
    factor = int(round(float(scale_factor)))
    if factor <= 1:
        return combined.copy()

    rng = np.random.default_rng(seed)
    originals = combined.copy()
    clones = originals.loc[originals.index.repeat(factor - 1)].reset_index(drop=True)
    clones[["start_x", "start_y", "end_x", "end_y"]] = clones[
        ["start_x", "start_y", "end_x", "end_y"]
    ].apply(pd.to_numeric, errors="coerce").astype(float)

    father = clones["new_trip_id"].astype("string").str.rsplit("_", n=1).str[0]
    cno = clones.groupby("new_trip_id", sort=False).cumcount().add(1).astype(str)
    clones["new_trip_id"] = father + "_" + cno
    clones["dep_time"] = _jitter_dep_time(clones["dep_time"], dep_time_jitter_s, rng)

    ctype = clones["crossing_type"].astype(str)
    inside = ctype.eq("inside")
    entering = ctype.eq("entering")
    leaving = ctype.eq("leaving")

    _jitter_xy(clones, inside | leaving, "start_x", "start_y", coord_radius_m, rng, boundary=boundary)
    _jitter_xy(clones, inside | entering, "end_x", "end_y", coord_radius_m, rng, boundary=boundary)

    return pd.concat([originals, clones], ignore_index=True)


def run_demand_scaling_stage(state: PipelineState) -> None:
    if state.combined_trips is None:
        raise ValueError("combined_trips must be available before demand scaling stage.")
    state.scaled_trips = scale_demand(
        state.combined_trips,
        scale_factor=float(state.config["scale_factor"]),
        dep_time_jitter_s=int(state.config["dep_time_jitter_seconds"]),
        coord_radius_m=float(state.config["coord_jitter_radius_m"]),
        seed=int(state.config["demand_scaling_seed"]),
        boundary=state.boundary,
    )
    CsvWriter.write_df(state.scaled_trips, state.scaled_trips_path)
    run_scaled_trips_plot_stage(state)
