from __future__ import annotations

import os
import tempfile
from pathlib import Path
import webbrowser

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matsim_sumo_matplotlib"))

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


BOUNDARY_CONFIRMATION_FILENAME = "sumo_network_boundary_confirmation.png"
BOUNDARY_DECLINED_MESSAGE = "Boundary confirmation declined; pipeline stopped before Step 1."


def _read_boundary(boundary_path: Path) -> gpd.GeoDataFrame:
    boundary_file = gpd.read_file(boundary_path)
    if boundary_file.empty:
        raise ValueError(f"Boundary file is empty: {boundary_path}")
    crs = boundary_file.crs or "EPSG:2056"
    return gpd.GeoDataFrame(geometry=[boundary_file.union_all()], crs=crs)


def _align_crs(
    sumo_gdf: gpd.GeoDataFrame, boundary_gdf: gpd.GeoDataFrame
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    if sumo_gdf.crs is None:
        sumo_gdf = sumo_gdf.set_crs(boundary_gdf.crs or "EPSG:2056")
    if boundary_gdf.crs is None:
        boundary_gdf = boundary_gdf.set_crs(sumo_gdf.crs or "EPSG:2056")
    if str(boundary_gdf.crs) != str(sumo_gdf.crs):
        boundary_gdf = boundary_gdf.to_crs(sumo_gdf.crs)
    return sumo_gdf, boundary_gdf


def plot_sumo_network_boundary(
    sumo_gdf: gpd.GeoDataFrame, boundary_gdf: gpd.GeoDataFrame, out_path: Path
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sumo_gdf, boundary_gdf = _align_crs(sumo_gdf, boundary_gdf)

    fig, ax = plt.subplots(figsize=(10, 10))
    sumo_gdf.plot(ax=ax, color="black", linewidth=0.15, alpha=0.7, label="SUMO Network")
    boundary_gdf.boundary.plot(ax=ax, color="red", linewidth=1.4, label="Boundary")
    ax.set_title("SUMO Network with Boundary")
    ax.set_aspect("equal")
    ax.legend()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out_path


def confirm_boundary_preview(sumo_gdf: gpd.GeoDataFrame, boundary_path: Path, output_dir: Path) -> Path:
    boundary_path = Path(boundary_path)
    output_dir = Path(output_dir)
    out_path = output_dir / "plots" / BOUNDARY_CONFIRMATION_FILENAME
    boundary_gdf = _read_boundary(boundary_path)

    plot_path = plot_sumo_network_boundary(sumo_gdf, boundary_gdf, out_path)
    print(f"Boundary preview written to: {plot_path.resolve()}")
    try:
        webbrowser.open(plot_path.resolve().as_uri())
    except Exception as exc:
        print(f"Could not open boundary preview automatically: {exc}")

    try:
        answer = input("Use this boundary for the pipeline? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(BOUNDARY_DECLINED_MESSAGE)

    if answer.strip().lower() not in {"y", "yes"}:
        raise SystemExit(BOUNDARY_DECLINED_MESSAGE)
    return plot_path
