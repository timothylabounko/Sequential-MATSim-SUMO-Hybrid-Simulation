#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
import matsim
import numpy as np
import pandas as pd
import yaml
from matplotlib import colors
from matplotlib.lines import Line2D
from matplotlib.cm import ScalarMappable

matplotlib.use("Agg")
import matplotlib.pyplot as plt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from matsim_sumo.matsim_trip_inside_filter import resolve_sumo_network_path
from matsim_sumo.network_preprocessing.network_preprocessing import preprocess_sumo_network_to_lv95


def _project_path(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _normalize_link_ids(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)


def _mode_tokens(raw: str) -> set[str]:
    return {tok.strip().lower() for tok in str(raw).split(",") if tok.strip()}


def _read_linkstats_flow(linkstats_path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for sep in ("\t", ";"):
        try:
            df = pd.read_csv(
                linkstats_path,
                sep=sep,
                usecols=["LINK", "HRS0-24avg"],
                low_memory=False,
            )
            out = pd.DataFrame(
                {
                    "link_id": _normalize_link_ids(df["LINK"]),
                    "flow_0_24_avg": pd.to_numeric(df["HRS0-24avg"], errors="coerce"),
                }
            )
            out = out.dropna(subset=["link_id", "flow_0_24_avg"])
            return out.groupby("link_id", as_index=False)["flow_0_24_avg"].mean()
        except Exception as err:
            last_error = err
    raise RuntimeError(f"Could not read LINK/HRS0-24avg from {linkstats_path}: {last_error}")


def _load_sumo_geo(cfg: dict, root: Path, matsim_crs: str) -> gpd.GeoDataFrame:
    sumo_geo_path = root / cfg["sumo_network_lv95_tmp_file"]
    if sumo_geo_path.exists():
        sumo_geo = gpd.read_file(sumo_geo_path)
    else:
        vclasses = tuple(dict.fromkeys(cfg["sumo_vclass_map"].values()))
        _, sumo_geo = preprocess_sumo_network_to_lv95(
            network_path=_project_path(resolve_sumo_network_path(cfg), root),
            vclasses=vclasses,
            out_path=sumo_geo_path,
        )
    if sumo_geo.crs is None:
        sumo_geo = sumo_geo.set_crs("EPSG:2056")
    if str(sumo_geo.crs) != matsim_crs:
        sumo_geo = sumo_geo.to_crs(matsim_crs)
    return sumo_geo


def _build_norm(values: pd.Series, log_scale: bool) -> tuple[colors.Normalize, bool]:
    vv = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    vv = vv[np.isfinite(vv)]
    vv = vv[vv > 0]
    if vv.size == 0:
        return colors.Normalize(vmin=0.0, vmax=1.0, clip=True), False

    if log_scale:
        vmin = max(float(np.percentile(vv, 5)), float(vv.min()))
        vmax = max(float(np.percentile(vv, 99)), vmin * (1.0 + 1e-9))
        return colors.LogNorm(vmin=vmin, vmax=vmax, clip=True), True

    vmax = max(float(np.percentile(vv, 99)), float(vv.max()))
    return colors.Normalize(vmin=0.0, vmax=vmax, clip=True), False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Overlay MATSim and SUMO networks; color MATSim links by HRS0-24avg flow."
    )
    parser.add_argument("--config", default="configs/base_config.yaml", help="Pipeline config YAML path")
    parser.add_argument(
        "--linkstats",
        default="data/matsim_output_2/ITERS/it.90/90.linkstats.txt.gz",
        help="MATSim linkstats file (.txt.gz)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output PNG path (default: <config.output_dir>/plots/network_overlay_matsim_flow_vs_sumo.png)",
    )
    parser.add_argument(
        "--road-modes",
        default="car,car_passenger,truck,taxi",
        help="Comma-separated modes to keep from MATSim network (PT links are removed by excluding non-road links)",
    )
    parser.add_argument("--projection", default="", help="CRS for MATSim network (default: config network_crs or EPSG:2056)")
    parser.add_argument("--cmap", default="turbo", help="Colormap for MATSim flow")
    parser.add_argument("--linear", action="store_true", help="Use linear scale instead of log color scale")
    parser.add_argument("--dpi", type=int, default=260, help="Output DPI")
    parser.add_argument("--matsim-linewidth", type=float, default=0.65, help="MATSim link linewidth")
    parser.add_argument("--sumo-linewidth", type=float, default=0.05, help="SUMO edge linewidth")
    parser.add_argument("--sumo-alpha", type=float, default=0.14, help="SUMO edge alpha")
    args = parser.parse_args()

    cfg_path = PROJECT_ROOT / args.config
    cfg = yaml.safe_load(cfg_path.read_text())

    matsim_network_path = PROJECT_ROOT / cfg["matsim_output_dir"] / cfg["network_file"]
    linkstats_path = PROJECT_ROOT / args.linkstats
    out_path = (
        PROJECT_ROOT / args.output
        if args.output
        else PROJECT_ROOT / cfg["output_dir"] / "plots" / "network_overlay_matsim_flow_vs_sumo.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    projection = args.projection or str(cfg.get("network_crs", "EPSG:2056"))
    road_modes = {m.strip().lower() for m in args.road_modes.split(",") if m.strip()}

    matsim_net = matsim.read_network(str(matsim_network_path))
    matsim_geo = matsim_net.as_geo(projection=projection).copy()
    matsim_geo = matsim_geo.loc[matsim_geo.geometry.notna()].copy()
    if matsim_geo.crs is None:
        matsim_geo = matsim_geo.set_crs(projection)
    matsim_geo["link_id"] = _normalize_link_ids(matsim_geo["link_id"])

    mode_mask = matsim_geo["modes"].fillna("").astype(str).map(lambda raw: bool(_mode_tokens(raw) & road_modes))
    matsim_geo = matsim_geo.loc[mode_mask].copy()

    flow = _read_linkstats_flow(linkstats_path)
    matsim_geo = matsim_geo.merge(flow, on="link_id", how="left")
    matsim_geo["flow_0_24_avg"] = pd.to_numeric(matsim_geo["flow_0_24_avg"], errors="coerce")

    sumo_geo = _load_sumo_geo(cfg=cfg, root=PROJECT_ROOT, matsim_crs=str(matsim_geo.crs))

    norm, is_log = _build_norm(matsim_geo["flow_0_24_avg"], log_scale=(not args.linear))
    has_flow = matsim_geo["flow_0_24_avg"].fillna(0) > 0
    matsim_zero = matsim_geo.loc[~has_flow]
    matsim_flow = matsim_geo.loc[has_flow]

    fig, ax = plt.subplots(figsize=(14, 14), dpi=args.dpi)
    ax.set_facecolor("#f7f7f7")

    # Keep SUMO visible but not dominant.
    sumo_geo.plot(
        ax=ax,
        color="#111111",
        linewidth=args.sumo_linewidth,
        alpha=args.sumo_alpha,
        zorder=1,
    )

    if not matsim_zero.empty:
        matsim_zero.plot(
            ax=ax,
            color="#d0d0d0",
            linewidth=max(0.05, args.matsim_linewidth * 0.5),
            alpha=0.35,
            zorder=2,
        )

    matsim_flow.plot(
        ax=ax,
        column="flow_0_24_avg",
        cmap=args.cmap,
        norm=norm,
        linewidth=args.matsim_linewidth,
        alpha=0.95,
        zorder=3,
    )

    sm = ScalarMappable(norm=norm, cmap=args.cmap)
    cbar = fig.colorbar(sm, ax=ax, fraction=0.035, pad=0.01)
    cbar.set_label("MATSim flow HRS0-24avg [veh/h]", rotation=90)

    handles = [
        Line2D([0], [0], color="#111111", lw=1.5, alpha=0.35, label=f"SUMO background ({len(sumo_geo):,} edges)"),
        Line2D(
            [0],
            [0],
            color=plt.get_cmap(args.cmap)(0.8),
            lw=2.5,
            label=f"MATSim road links ({len(matsim_geo):,}, PT removed)",
        ),
        Line2D([0], [0], color="#d0d0d0", lw=2.0, label=f"MATSim links with zero/NA flow ({len(matsim_zero):,})"),
    ]
    ax.legend(handles=handles, loc="lower left", frameon=True, framealpha=0.96, facecolor="white")

    ax.set_title(
        f"MATSim Flow Overlay on SUMO (HRS0-24avg, {'log' if is_log else 'linear'} scale)",
        fontsize=14,
        pad=10,
    )
    ax.set_aspect("equal")
    ax.axis("off")
    fig.tight_layout(pad=0.9)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)

    print(f"Wrote: {out_path.resolve()}")
    print(f"MATSim links (road modes kept): {len(matsim_geo):,}")
    print(f"MATSim links with positive flow: {len(matsim_flow):,}")
    print(f"SUMO edges plotted in background: {len(sumo_geo):,}")


if __name__ == "__main__":
    main()
