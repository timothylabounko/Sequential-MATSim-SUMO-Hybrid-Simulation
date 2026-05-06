#!/usr/bin/env python3
from pathlib import Path
import sys

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
import yaml
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from matsim_sumo.matsim_trip_inside_filter import build_pipeline_state
from matsim_sumo.network_preprocessing.network_preprocessing import preprocess_sumo_network_to_lv95

CONFIG = Path("configs/base_config.yaml")
MATCHES = Path("results/run_21_MATSim_Scenario_3_no_trucks/120.linkstats.count_point_sumo_edges.txt.gz")
DETAILED_NET = Path("data/switzerland_detailed_network.csv")


def line(wkt):
    pts = [tuple(map(float, p.split())) for p in wkt[11:-1].split(",")]
    return LineString(pts) if len(pts) > 1 else None


cfg = yaml.safe_load(CONFIG.read_text())
state = build_pipeline_state(cfg)
m = pd.read_csv(MATCHES, sep="\t", dtype="string", usecols=["LINK", "SUMO_EDGE_ID", "COUNT_POINT_ID"])
sumo_all = gpd.read_file(state.sumo_network_lv95_path) if state.sumo_network_lv95_path.exists() else preprocess_sumo_network_to_lv95(state.sumo_network_path, tuple(dict.fromkeys(cfg["sumo_vclass_map"].values())), state.sumo_network_lv95_path)[1]
sumo = sumo_all.merge(m[["SUMO_EDGE_ID", "COUNT_POINT_ID"]], left_on=sumo_all["edge_id"].astype("string"), right_on="SUMO_EDGE_ID")
mat = pd.read_csv(DETAILED_NET, dtype="string")
mat_all = gpd.GeoDataFrame(mat.assign(geometry=lambda d: d["Geometry"].map(line)).dropna(subset=["geometry"]), geometry="geometry", crs=cfg.get("network_crs", "EPSG:2056"))
mat = mat_all[mat_all["LinkId"].isin(m["LINK"])]
boundary = gpd.read_file(state.boundary_path).union_all()
mat_bg = mat_all[mat_all.geometry.intersects(boundary)]
ax = mat_bg.plot(color="grey", linewidth=0.5, figsize=(75, 75), label="MATSim network in boundary")
sumo_all.plot(ax=ax, color="black", linewidth=0.2, alpha=0.45, label="SUMO network")
sumo.plot(ax=ax, color="red", linewidth=2.0, label="SUMO matched edges")
mat.plot(ax=ax, color="blue", linewidth=1.4, label="MATSim selected links")
sumo["label_base"] = sumo["COUNT_POINT_ID"].str.replace(r"M\d+$", "", regex=True)
sumo["label_i"] = sumo.groupby("label_base").cumcount()
for r in sumo.itertuples():
    p = r.geometry.interpolate(0.5, normalized=True)
    dx = -8 if r.label_i % 2 == 0 else 8
    ax.annotate(r.COUNT_POINT_ID, (p.x, p.y), xytext=(dx, 8), textcoords="offset points", ha="right" if dx < 0 else "left", va="center", color="red", fontsize=7, weight="bold")
ax.set_axis_off()
ax.legend()
plt.tight_layout()
out = MATCHES.with_suffix("").with_suffix(".overlay.png")
plt.savefig(out, dpi=220)
print(f"Wrote {out} ({len(mat)} MATSim, {len(sumo)} SUMO)")
