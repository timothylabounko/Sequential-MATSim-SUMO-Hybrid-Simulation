import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matsim_sumo_matplotlib"))

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, LogNorm
from matplotlib.lines import Line2D

from matsim_sumo.pipeline.state import PipelineState
from matsim_sumo.visualization.plot_style import BOUNDARY_STYLE, NETWORK_STYLE, apply_spatial_axes

matplotlib.use("Agg")
import matplotlib.pyplot as plt


DISTANCE_THRESHOLDS_M = (50.0, 100.0, 150.0, 250.0)
ENDPOINT_DENSITY_COLOR = "#018571"


def _points(df: pd.DataFrame, x_col: str, y_col: str) -> gpd.GeoDataFrame:
    xy = df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce").dropna()
    return gpd.GeoDataFrame(geometry=gpd.points_from_xy(xy[x_col], xy[y_col]), crs="EPSG:2056")


def _xy(df: pd.DataFrame, x_col: str, y_col: str) -> np.ndarray:
    return df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce").dropna().to_numpy(dtype=float)


def _max_bin_count(arrays: tuple[np.ndarray, ...], extent: tuple[float, float, float, float], bins: int) -> int:
    x0, x1, y0, y1 = extent
    vmax = 1
    for xy in arrays:
        if xy.size:
            hist, _, _ = np.histogram2d(xy[:, 0], xy[:, 1], bins=bins, range=((x0, x1), (y0, y1)))
            vmax = max(vmax, int(hist.max()))
    return vmax


def _count_label(n: int) -> str:
    return f"{n:,}".replace(",", "'")


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


def _plot_density_panel(
    ax,
    net_gdf: gpd.GeoDataFrame,
    boundary_gdf: gpd.GeoDataFrame,
    xy: np.ndarray,
    title: str,
    color: str,
    extent: tuple[float, float, float, float],
    norm: LogNorm,
    gridsize: int,
    count: int,
):
    net_gdf.plot(ax=ax, label="SUMO network", **NETWORK_STYLE)
    hb = None
    if xy.size:
        cmap = LinearSegmentedColormap.from_list("", ["#ffffff", color])
        hb = ax.hexbin(
            xy[:, 0],
            xy[:, 1],
            gridsize=gridsize,
            extent=extent,
            mincnt=1,
            cmap=cmap,
            norm=norm,
            linewidths=0,
            alpha=0.95,
            zorder=3,
        )
    boundary_gdf.boundary.plot(ax=ax, label="Boundary", **BOUNDARY_STYLE)
    ax.set_title(title)
    apply_spatial_axes(ax, net_gdf.crs, legend=False)
    ax.legend(
        handles=[
            Line2D([0], [0], color=NETWORK_STYLE["color"], lw=0.8, alpha=NETWORK_STYLE["alpha"], label="SUMO network"),
            Line2D([0], [0], color=BOUNDARY_STYLE["color"], lw=1.6, label="Boundary"),
            Line2D([0], [0], color=color, lw=5, label=f"End points, n={_count_label(count)}"),
        ],
        loc="upper right",
        frameon=True,
        framealpha=0.95,
        fontsize=8,
        borderpad=0.45,
        labelspacing=0.35,
        handlelength=1.9,
    )
    return hb


def _save_density_comparison(
    net_gdf: gpd.GeoDataFrame,
    boundary_gdf: gpd.GeoDataFrame,
    unscaled: pd.DataFrame,
    scaled: pd.DataFrame,
    out_paths: tuple[Path, ...],
) -> None:
    data = {
        "Unscaled": _xy(unscaled, "end_x", "end_y"),
        "Scaled": _xy(scaled, "end_x", "end_y"),
    }
    x0, y0, x1, y1 = boundary_gdf.total_bounds
    pad = 0.03 * max(x1 - x0, y1 - y0)
    extent = (x0 - pad, x1 + pad, y0 - pad, y1 + pad)
    gridsize = 170
    norm = LogNorm(vmin=1, vmax=_max_bin_count(tuple(data.values()), extent, gridsize))

    fig = plt.figure(figsize=(13.5, 6.6))
    gs = fig.add_gridspec(1, 3, width_ratios=(1, 1, 0.045), wspace=0.18)
    axs = [fig.add_subplot(gs[0, 0]), None]
    axs[1] = fig.add_subplot(gs[0, 1], sharex=axs[0], sharey=axs[0])
    cax = fig.add_subplot(gs[0, 2])
    last_hb = None
    for ax, dataset in zip(axs, ("Unscaled", "Scaled")):
        last_hb = _plot_density_panel(
            ax,
            net_gdf,
            boundary_gdf,
            data[dataset],
            f"{dataset} End Point Density",
            ENDPOINT_DENSITY_COLOR,
            extent,
            norm,
            gridsize,
            len(data[dataset]),
        )
    axs[1].set_ylabel("")
    plt.setp(axs[1].get_yticklabels(), visible=False)
    if last_hb is not None:
        cbar = fig.colorbar(last_hb, cax=cax)
        cbar.set_label("Trips per hexagon (log scale)")
    fig.suptitle("Trip End Point Density with Boundary and SUMO Network", y=0.98)
    fig.subplots_adjust(top=0.86)
    for out_path in out_paths:
        fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def run_scaled_trips_plot_stage(state: PipelineState) -> None:
    if state.scaled_trips is None or state.combined_trips is None:
        return

    out_dir = Path(state.output_dir) / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "1_6_unscaled_trips_start_end_sumo_network.png",
        "2_scaled_trips_start_end_sumo_network.png",
        "1_6_unscaled_trips_start_end_boundary_sumo_network.png",
        "2_scaled_trips_start_end_boundary_sumo_network.png",
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
    _save_density_comparison(sumo_gdf, boundary, state.combined_trips, state.scaled_trips, tuple(out_dir / name for _, _, name in specs))
    _write_distance_diagnostics(sumo_gdf, specs, out_dir)
