from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

from matsim_sumo.pipeline.state import PipelineState

matplotlib.use("Agg")
import matplotlib.pyplot as plt


DISTANCE_THRESHOLDS_M = (50.0, 100.0, 150.0, 250.0)


def _points(df: pd.DataFrame, x_col: str, y_col: str) -> gpd.GeoDataFrame:
    xy = df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce").dropna()
    return gpd.GeoDataFrame(geometry=gpd.points_from_xy(xy[x_col], xy[y_col]), crs="EPSG:2056")


def _nearest_distances_m(net_gdf: gpd.GeoDataFrame, df: pd.DataFrame, x_col: str, y_col: str) -> np.ndarray:
    xy = df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
    valid = xy[x_col].notna() & xy[y_col].notna()
    out = np.full(len(df), np.nan, dtype=float)
    if not valid.any() or net_gdf.empty:
        return out

    points = gpd.GeoSeries(
        gpd.points_from_xy(xy.loc[valid, x_col], xy.loc[valid, y_col]),
        crs=net_gdf.crs,
    )
    nearest, distances = net_gdf.sindex.nearest(points, return_all=False, return_distance=True)
    if nearest.size == 0:
        return out
    valid_positions = np.flatnonzero(valid.to_numpy())
    out[valid_positions[np.asarray(nearest[0], dtype=int)]] = np.asarray(distances, dtype=float)
    return out


def _distance_summary_rows(net_gdf: gpd.GeoDataFrame, df: pd.DataFrame, dataset: str) -> list[dict[str, float | int | str]]:
    start = pd.Series(_nearest_distances_m(net_gdf, df, "start_x", "start_y"))
    end = pd.Series(_nearest_distances_m(net_gdf, df, "end_x", "end_y"))
    max_endpoint = pd.concat([start, end], axis=1).max(axis=1)
    rows = []
    for endpoint, values in (("start", start), ("end", end), ("max", max_endpoint)):
        valid = values.dropna().astype(float)
        row: dict[str, float | int | str] = {"dataset": dataset, "endpoint": endpoint, "count": int(valid.size)}
        for pct in (50, 90, 95, 99, 100):
            row[f"p{pct}_m"] = float(np.percentile(valid.to_numpy(dtype=float), pct)) if not valid.empty else np.nan
        for threshold in DISTANCE_THRESHOLDS_M:
            row[f"gt_{int(threshold)}m"] = int(valid.gt(threshold).sum())
        rows.append(row)
    return rows


def _write_distance_diagnostics(
    net_gdf: gpd.GeoDataFrame,
    specs: tuple[tuple[pd.DataFrame, str, str], ...],
    out_dir: Path,
) -> None:
    rows = []
    for df, _, name in specs:
        dataset = name.removesuffix(".png")
        rows.extend(_distance_summary_rows(net_gdf, df, dataset))
    pd.DataFrame.from_records(rows).to_csv(
        out_dir / "trips_start_end_sumo_distance_diagnostics.csv",
        sep=";",
        index=False,
    )


def _save_plot(
    net_gdf: gpd.GeoDataFrame,
    boundary_gdf: gpd.GeoDataFrame,
    start_gdf: gpd.GeoDataFrame,
    end_gdf: gpd.GeoDataFrame,
    title: str,
    out_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 10))
    net_gdf.plot(ax=ax, color="black", linewidth=0.15, alpha=0.7, label="SUMO Network")
    boundary_gdf.boundary.plot(ax=ax, color="black", linewidth=1.2, label="Boundary")
    start_gdf.plot(ax=ax, color="dodgerblue", markersize=2, alpha=0.4, label="Start points")
    end_gdf.plot(ax=ax, color="tomato", markersize=2, alpha=0.4, label="End points")
    ax.set_title(title)
    ax.set_aspect("equal")
    ax.legend()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def run_scaled_trips_plot_stage(state: PipelineState) -> None:
    if state.scaled_trips is None or state.combined_trips is None:
        return

    out_dir = Path(state.output_dir) / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "6_unscaled_trips_start_end_sumo_network.png",
        "7_scaled_trips_start_end_sumo_network.png",
    ):
        p = out_dir / name
        if p.exists():
            p.unlink()

    sumo_gdf = state.sumo_network_geo
    if sumo_gdf is None:
        if not state.sumo_network_lv95_path.exists():
            return
        sumo_gdf = gpd.read_file(state.sumo_network_lv95_path)
    boundary = gpd.GeoDataFrame(geometry=[gpd.read_file(state.boundary_path).union_all()], crs="EPSG:2056")
    specs = (
        (
            state.combined_trips,
            "Unscaled Trips Start and End Points with Boundary and SUMO Network",
            "6_unscaled_trips_start_end_boundary_sumo_network.png",
        ),
        (
            state.scaled_trips,
            "Scaled Trips Start and End Points with Boundary and SUMO Network",
            "7_scaled_trips_start_end_boundary_sumo_network.png",
        ),
    )
    for df, title, name in specs:
        _save_plot(
            sumo_gdf,
            boundary,
            _points(df, "start_x", "start_y"),
            _points(df, "end_x", "end_y"),
            title,
            out_dir / name,
        )
    _write_distance_diagnostics(sumo_gdf, specs, out_dir)
