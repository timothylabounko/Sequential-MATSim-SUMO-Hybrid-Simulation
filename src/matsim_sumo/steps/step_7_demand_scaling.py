from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import covers, points

from matsim_sumo.amenities_kdtree import AmenitiesKDTree
from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep
from matsim_sumo.pipeline_state import PipelineState
from matsim_sumo.plot.scaled_trips_start_end_plots import run_scaled_trips_plot_stage

AMENITIES_CSV_SEP = ","
AMENITIES_X_COL = "x"
AMENITIES_Y_COL = "y"
AMENITIES_WEIGHT_COL = "weight"
DEMAND_SCALING_LOGIC_VERSION = 2


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
    amenities: AmenitiesKDTree | None = None,
) -> None:
    if int(mask.sum()) == 0:
        return
    idx = df.index[mask]
    xy = df.loc[idx, [x_col, y_col]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float, copy=True)
    valid = ~np.isnan(xy).any(axis=1)
    if not valid.any():
        return
    base = xy.copy()
    pos = np.flatnonzero(valid)
    take = np.zeros(valid.sum(), dtype=bool)
    if amenities is not None:
        ax, ay, take = amenities.sample_within_radius(base[valid, 0], base[valid, 1], radius_m, rng)
        xy[pos[take], 0] = ax[take]
        xy[pos[take], 1] = ay[take]
    miss = ~take
    if miss.any():
        r = radius_m * np.sqrt(rng.random(int(miss.sum())))
        a = 2 * np.pi * rng.random(int(miss.sum()))
        xy[pos[miss], 0] = base[pos[miss], 0] + r * np.cos(a)
        xy[pos[miss], 1] = base[pos[miss], 1] + r * np.sin(a)
    if boundary is not None:
        inside = np.asarray(covers(boundary, points(xy[valid, 0], xy[valid, 1])), dtype=bool)
        xy[pos[~inside]] = base[pos[~inside]]
    df.loc[idx, [x_col, y_col]] = xy


def _nearest_link_ids(network_geo: gpd.GeoDataFrame, x: pd.Series, y: pd.Series) -> pd.Series:
    out = pd.Series(pd.NA, index=x.index, dtype="string")
    xy = pd.DataFrame({"x": pd.to_numeric(x, errors="coerce"), "y": pd.to_numeric(y, errors="coerce")})
    valid = xy["x"].notna() & xy["y"].notna()
    if not valid.any():
        return out
    pts = gpd.GeoSeries(points(xy.loc[valid, "x"].to_numpy(), xy.loc[valid, "y"].to_numpy()))
    q_idx, l_idx = network_geo.sindex.nearest(pts, return_all=False)
    out.loc[xy.index[valid].to_numpy()[np.asarray(q_idx, dtype=int)]] = network_geo["link_id"].astype("string").to_numpy()[
        np.asarray(l_idx, dtype=int)
    ]
    return out


def scale_demand(
    combined: pd.DataFrame,
    scale_factor: float,
    dep_time_jitter_s: int,
    coord_radius_m: float,
    seed: int,
    boundary=None,
    network_geo: pd.DataFrame | None = None,
    amenities: AmenitiesKDTree | None = None,
) -> pd.DataFrame:
    factor = int(round(float(scale_factor)))
    if factor <= 1:
        out = combined.copy()
        out["new_start_link"] = out["start_link"].astype("string")
        out["new_end_link"] = out["end_link"].astype("string")
        return out

    rng = np.random.default_rng(seed)
    originals = combined.copy()
    originals["new_start_link"] = originals["start_link"].astype("string")
    originals["new_end_link"] = originals["end_link"].astype("string")
    clones = originals.loc[originals.index.repeat(factor - 1)].reset_index(drop=True)
    clones[["start_x", "start_y", "end_x", "end_y"]] = clones[
        ["start_x", "start_y", "end_x", "end_y"]
    ].apply(pd.to_numeric, errors="coerce").astype(float)
    clones["new_start_link"] = clones["start_link"].astype("string")
    clones["new_end_link"] = clones["end_link"].astype("string")

    father = clones["new_trip_id"].astype("string").str.rsplit("_", n=1).str[0]
    cno = clones.groupby("new_trip_id", sort=False).cumcount().add(1).astype(str)
    clones["new_trip_id"] = father + "_" + cno
    clones["dep_time"] = _jitter_dep_time(clones["dep_time"], dep_time_jitter_s, rng)

    ctype = clones["crossing_type"].astype(str)
    inside = ctype.eq("inside")
    entering = ctype.eq("entering")
    leaving = ctype.eq("leaving")

    _jitter_xy(clones, inside | leaving, "start_x", "start_y", coord_radius_m, rng, boundary=boundary, amenities=amenities)
    _jitter_xy(clones, inside | entering, "end_x", "end_y", coord_radius_m, rng, boundary=boundary, amenities=amenities)
    if network_geo is not None:
        net_geo = network_geo if isinstance(network_geo, gpd.GeoDataFrame) else gpd.GeoDataFrame(network_geo, geometry="geometry")
        rematch_start = inside | leaving
        rematch_end = inside | entering
        start_links = _nearest_link_ids(net_geo, clones.loc[rematch_start, "start_x"], clones.loc[rematch_start, "start_y"])
        end_links = _nearest_link_ids(net_geo, clones.loc[rematch_end, "end_x"], clones.loc[rematch_end, "end_y"])
        clones.loc[rematch_start, "new_start_link"] = start_links.fillna(clones.loc[rematch_start, "new_start_link"])
        clones.loc[rematch_end, "new_end_link"] = end_links.fillna(clones.loc[rematch_end, "new_end_link"])

    return pd.concat([originals, clones], ignore_index=True)


def _demand_scaling_deps(state: PipelineState):
    deps = [
        file_dep(state.combined_trips_path),
        file_dep(state.crossing_segments_timed_path),
        file_dep(state.trips_inside_path),
        file_dep(state.network_path),
        file_dep(state.boundary_path),
        param_dep("output_dir", str(state.output_dir)),
        param_dep("modes_keep", tuple(state.config["modes_keep"])),
        param_dep("network_crs", state.config.get("network_crs", "EPSG:2056")),
        param_dep("scale_factor", float(state.config["scale_factor"])),
        param_dep("dep_time_jitter_seconds", int(state.config["dep_time_jitter_seconds"])),
        param_dep("coord_jitter_radius_m", float(state.config["coord_jitter_radius_m"])),
        param_dep("demand_scaling_seed", int(state.config["demand_scaling_seed"])),
        param_dep("amenity_kdtree_enabled", bool(state.config["amenity_kdtree_enabled"])),
        param_dep("demand_scaling_logic_version", DEMAND_SCALING_LOGIC_VERSION),
    ]
    if state.config["amenity_kdtree_enabled"]:
        deps.append(file_dep(state.config["amenities_path"]))
    return deps


@disk_cache(depends_on=lambda state: _demand_scaling_deps(state), step_name="demand_scaling")
def _compute_scaled_demand_cached(state: PipelineState) -> pd.DataFrame:
    if state.combined_trips is None or state.network_geo is None:
        raise ValueError("combined_trips and network_geo must be available before demand scaling stage.")
    amenities = None
    if state.config["amenity_kdtree_enabled"]:
        path = Path(state.config["amenities_path"])
        if not path.exists():
            raise FileNotFoundError(f"Amenities CSV not found: {path}")
        amenities = AmenitiesKDTree.from_csv(
            path,
            sep=AMENITIES_CSV_SEP,
            x_col=AMENITIES_X_COL,
            y_col=AMENITIES_Y_COL,
            weight_col=AMENITIES_WEIGHT_COL,
            boundary_path=state.boundary_path,
        )
    return scale_demand(
        state.combined_trips,
        scale_factor=float(state.config["scale_factor"]),
        dep_time_jitter_s=int(state.config["dep_time_jitter_seconds"]),
        coord_radius_m=float(state.config["coord_jitter_radius_m"]),
        seed=int(state.config["demand_scaling_seed"]),
        boundary=state.boundary,
        network_geo=state.network_geo,
        amenities=amenities,
    )


def run_demand_scaling_stage(state: PipelineState) -> None:
    state.scaled_trips = _compute_scaled_demand_cached(state, no_cache=bool(state.config.get("no_cache", False)))
    if output_needs_refresh(_compute_scaled_demand_cached, state.scaled_trips_path):
        CsvWriter.write_df(state.scaled_trips, state.scaled_trips_path)
        run_scaled_trips_plot_stage(state)
