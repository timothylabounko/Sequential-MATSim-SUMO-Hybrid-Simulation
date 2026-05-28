#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from matsim_sumo.matsim_trip_inside_filter import resolve_sumo_network_path
from matsim_sumo.network_preprocessing.network_preprocessing import preprocess_sumo_network_to_lv95
from matsim_sumo.plot.boundary_confirmation import plot_sumo_network_boundary


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_sumo_geo(cfg: dict) -> gpd.GeoDataFrame:
    out_path = PROJECT_ROOT / cfg["sumo_network_lv95_tmp_file"]
    if not out_path.exists():
        preprocess_sumo_network_to_lv95(
            network_path=_project_path(resolve_sumo_network_path(cfg)),
            vclasses=tuple(dict.fromkeys(cfg["sumo_vclass_map"].values())),
            out_path=out_path,
        )
    return gpd.read_file(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot SUMO network with boundary.")
    parser.add_argument("--config", default="configs/base_config.yaml", help="Pipeline config YAML path")
    parser.add_argument(
        "--output",
        default="",
        help="Output PNG path (default: <config.output_dir>/plots/sumo_network_boundary.png)",
    )
    args = parser.parse_args()

    cfg = yaml.safe_load((PROJECT_ROOT / args.config).read_text())
    out_path = (
        PROJECT_ROOT / args.output
        if args.output
        else PROJECT_ROOT / cfg["output_dir"] / "plots" / "sumo_network_boundary.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sumo_gdf = _load_sumo_geo(cfg)
    boundary_gdf = gpd.GeoDataFrame(geometry=[gpd.read_file(PROJECT_ROOT / cfg["boundary_file"]).union_all()], crs="EPSG:2056")
    if sumo_gdf.crs is None:
        sumo_gdf = sumo_gdf.set_crs("EPSG:2056")
    if str(boundary_gdf.crs) != str(sumo_gdf.crs):
        boundary_gdf = boundary_gdf.to_crs(sumo_gdf.crs)

    plot_sumo_network_boundary(sumo_gdf, boundary_gdf, out_path)
    print(f"Wrote: {out_path.resolve()}")


if __name__ == "__main__":
    main()
