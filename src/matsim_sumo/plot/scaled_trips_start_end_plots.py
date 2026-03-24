from pathlib import Path

import geopandas as gpd
import matplotlib
import pandas as pd

from matsim_sumo.pipeline_state import PipelineState

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _points(df: pd.DataFrame, x_col: str, y_col: str) -> gpd.GeoDataFrame:
    xy = df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce").dropna()
    return gpd.GeoDataFrame(geometry=gpd.points_from_xy(xy[x_col], xy[y_col]), crs="EPSG:2056")


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
