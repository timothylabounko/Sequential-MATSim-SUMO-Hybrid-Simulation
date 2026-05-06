#!/usr/bin/env python3
from pathlib import Path
import gzip
import math
import sys
import xml.etree.ElementTree as ET

import geopandas as gpd
import pandas as pd
import yaml
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from matsim_sumo.network_preprocessing.network_preprocessing import preprocess_sumo_network_to_lv95


CONFIG = Path("configs/base_config.yaml")
LINKSTATS = Path("data/matsim_output_3/120.linkstats.txt.gz")
COUNT_EDGES = Path("data/sumo_scenario/count_point_edges.csv")
DETAILED_NET = Path("data/switzerland_detailed_network.csv")


def line(wkt):
    pts = [tuple(map(float, p.split())) for p in wkt[11:-1].split(",")]
    return LineString(pts) if len(pts) > 1 else None


def vec(g):
    (x1, y1), (x2, y2) = g.coords[0], g.coords[-1]
    n = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1
    return ((x2 - x1) / n, (y2 - y1) / n)


def norm(x):
    return "" if pd.isna(x) else str(x).lower().replace("highway.", "").strip()


def sampled_dist(a, b, n=15):
    return sum(a.interpolate(i / (n - 1), normalized=True).distance(b) for i in range(n)) / n


def matsim_meta(path, ids):
    ids, out = set(ids), {}
    with gzip.open(path, "rb") as f:
        for _, e in ET.iterparse(f, events=("end",)):
            if e.tag != "link":
                continue
            if e.get("id") in ids:
                a = {x.get("name"): x.text for x in e.findall("./attributes/attribute")}
                out[e.get("id")] = (norm(a.get("osm:way:highway")), norm(a.get("osm:way:name")))
                if len(out) == len(ids):
                    break
            e.clear()
    return out


def sumo_meta(path, ids):
    ids, out = set(ids), {}
    for _, e in ET.iterparse(path, events=("end",)):
        if e.tag == "edge" and e.get("id") in ids:
            out[e.get("id")] = (norm(e.get("type")), norm(e.get("name")))
            if len(out) == len(ids):
                break
        e.clear()
    return out


def main():
    config = yaml.safe_load(CONFIG.read_text())
    matsim_dir, out_dir = Path(config["matsim_output_dir"]), Path(config["output_dir"])
    net_path, boundary_path = matsim_dir / config["network_file"], Path(config["boundary_file"])
    sumo_path = Path(config["sumo_network_path"])
    if not sumo_path.is_absolute():
        sumo_path = Path(config.get("sumo_input_dir", "")) / sumo_path
    sumo_lv95 = Path(config["sumo_network_lv95_tmp_file"])
    out_dir.mkdir(parents=True, exist_ok=True)
    sumo_geo = (
        gpd.read_file(sumo_lv95)
        if sumo_lv95.exists()
        else preprocess_sumo_network_to_lv95(
            sumo_path,
            tuple(dict.fromkeys(config["sumo_vclass_map"].values())),
            sumo_lv95,
        )[1]
    )
    count_edges = pd.read_csv(COUNT_EDGES, dtype="string").dropna(subset=["edge_id"])
    sumo_geo = sumo_geo[sumo_geo["edge_id"].astype("string").isin(count_edges["edge_id"])].reset_index(drop=True)

    cache = out_dir / "matsim_links_in_boundary_with_meta.pkl.gz"
    if cache.exists():
        matsim_geo = pd.read_pickle(cache)
    else:
        link_ids = pd.read_csv(LINKSTATS, sep="\t", usecols=["LINK"], dtype={"LINK": "string"})["LINK"]
        matsim_geo = pd.read_csv(DETAILED_NET, dtype="string")
        matsim_geo = matsim_geo[matsim_geo["LinkId"].isin(link_ids)].assign(geometry=lambda d: d["Geometry"].map(line))
        matsim_geo = gpd.GeoDataFrame(matsim_geo.dropna(subset=["geometry"])[["LinkId", "geometry"]], geometry="geometry", crs=config.get("network_crs", "EPSG:2056")).reset_index(drop=True)
        matsim_geo = matsim_geo[matsim_geo.geometry.intersects(gpd.read_file(boundary_path).union_all())].reset_index(drop=True)
        mm = matsim_meta(net_path, set(matsim_geo["LinkId"]))
        matsim_geo[["type", "name"]] = matsim_geo["LinkId"].map(lambda x: mm.get(str(x), ("", ""))).apply(pd.Series)
        matsim_geo.to_pickle(cache, compression="gzip")
    sm = sumo_meta(sumo_path, set(sumo_geo["edge_id"].astype(str)))
    sumo_geo[["type", "name"]] = sumo_geo["edge_id"].astype(str).map(lambda x: sm.get(x, ("", ""))).apply(pd.Series)

    mv, sv, min_dot = matsim_geo.geometry.map(vec).to_list(), sumo_geo.geometry.map(vec).to_list(), math.cos(math.radians(30))
    rows = []
    for radius in (25, 50, 100, 250, 500):
        q, s = matsim_geo.sindex.query(sumo_geo.geometry.buffer(radius), predicate="intersects")
        cand = pd.DataFrame(set(zip(q, s)), columns=["sumo", "matsim"])
        if cand.empty:
            continue
        cand = cand[
            [
                sumo_geo["type"].iloc[i] == matsim_geo["type"].iloc[j] and sumo_geo["name"].iloc[i] == matsim_geo["name"].iloc[j]
                and sv[i][0] * mv[j][0] + sv[i][1] * mv[j][1] >= min_dot
                for i, j in cand[["sumo", "matsim"]].itertuples(False, None)
            ]
        ]
        if cand.empty:
            continue
        cand["score"] = [
            sampled_dist(sumo_geo.geometry.iloc[i], matsim_geo.geometry.iloc[j])
            for i, j in cand[["sumo", "matsim"]].itertuples(False, None)
        ]
        used_sumo, used_matsim, rows = set(), set(), []
        for i, j in cand.sort_values("score")[["sumo", "matsim"]].itertuples(False, None):
            if i not in used_sumo and j not in used_matsim:
                used_sumo.add(i); used_matsim.add(j); rows.append((sumo_geo.edge_id.iloc[i], matsim_geo.LinkId.iloc[j]))
        if len(rows) == len(sumo_geo):
            break
    if len(rows) != len(sumo_geo):
        raise RuntimeError(f"Matched {len(rows)}/{len(sumo_geo)} SUMO edges to unique MATSim links.")
    matched = count_edges.merge(pd.DataFrame(rows, columns=["edge_id", "LINK"]), on="edge_id")
    by_link = matched.set_index("LINK").rename(columns={"edge_id": "SUMO_EDGE_ID", "MSID": "COUNT_POINT_ID"})[["SUMO_EDGE_ID", "COUNT_POINT_ID"]]

    out_path = out_dir / "120.linkstats.count_point_sumo_edges.txt.gz"
    meta, n = by_link.to_dict("index"), 0
    with gzip.open(LINKSTATS, "rt") as src, gzip.open(out_path, "wt") as dst:
        header = src.readline().rstrip("\n").split("\t")
        dst.write("\t".join(header + ["SUMO_EDGE_ID", "COUNT_POINT_ID"]) + "\n")
        k = header.index("LINK")
        for raw in src:
            parts = raw.rstrip("\n").split("\t")
            if parts[k] in meta:
                r = meta[parts[k]]
                dst.write("\t".join(parts + [r["SUMO_EDGE_ID"], r["COUNT_POINT_ID"]]) + "\n")
                n += 1
    print(f"Wrote {out_path} ({n} MATSim links matched to {len(matched)} SUMO count edges)")


if __name__ == "__main__":
    main()
