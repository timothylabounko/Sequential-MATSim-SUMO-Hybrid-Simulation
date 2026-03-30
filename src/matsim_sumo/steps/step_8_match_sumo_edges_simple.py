from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState

EDGE_CONNECTION_DEPTH = 4


def _unit_vector_xy(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    dx = float(x1) - float(x0)
    dy = float(y1) - float(y0)
    n = float(np.hypot(dx, dy))
    return (np.nan, np.nan) if not np.isfinite(n) or n <= 0 else (dx / n, dy / n)


def _build_link_vectors(state: PipelineState) -> dict[str, tuple[float, float]]:
    if state.network is None:
        raise ValueError("state.network must be available before step 8.")
    links = state.network.links.loc[:, ["link_id", "from_node", "to_node"]].copy()
    nodes = state.network.nodes.loc[:, ["node_id", "x", "y"]].copy()
    links = links.merge(
        nodes.rename(columns={"node_id": "from_node", "x": "fx", "y": "fy"}),
        on="from_node",
        how="left",
    ).merge(
        nodes.rename(columns={"node_id": "to_node", "x": "tx", "y": "ty"}),
        on="to_node",
        how="left",
    )
    vecs: dict[str, tuple[float, float]] = {}
    for row in links.itertuples(index=False):
        vx, vy = _unit_vector_xy(row.fx, row.fy, row.tx, row.ty)
        if np.isfinite(vx) and np.isfinite(vy):
            vecs[str(row.link_id)] = (vx, vy)
    return vecs


def _nearest_edges(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    x_col: str,
    y_col: str,
    max_radius_m: float,
    point_type: str,
    link_vectors: dict[str, tuple[float, float]],
) -> tuple[pd.Series, pd.Series]:
    xy = trips[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
    valid = xy[x_col].notna() & xy[y_col].notna()
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    if not valid.any():
        return edge, dist

    link_col = "new_start_link" if point_type == "start" else "new_end_link"
    if link_col not in trips.columns:
        link_col = "start_link" if point_type == "start" else "end_link"
    matsim_vec = trips.loc[valid, link_col].astype(str).map(link_vectors)
    pts = gpd.GeoDataFrame(
        {
            "row_id": trips.index[valid],
            "mvx": matsim_vec.map(lambda v: v[0] if isinstance(v, tuple) else np.nan).to_numpy(dtype=float),
            "mvy": matsim_vec.map(lambda v: v[1] if isinstance(v, tuple) else np.nan).to_numpy(dtype=float),
        },
        geometry=gpd.points_from_xy(xy.loc[valid, x_col], xy.loc[valid, y_col]),
        crs=edges.crs,
    )

    # Stage A: absolute nearest edge (fallback).
    nearest = gpd.sjoin_nearest(
        pts,
        edges[["edge_id", "edge_dx", "edge_dy", "geometry"]],
        how="left",
        max_distance=float(max_radius_m),
        distance_col="dist_m",
    )
    nearest = nearest.sort_values(["row_id", "dist_m"]).drop_duplicates("row_id", keep="first")
    edge.loc[nearest["row_id"].to_numpy()] = nearest["edge_id"].to_numpy()
    dist.loc[nearest["row_id"].to_numpy()] = nearest["dist_m"].to_numpy(dtype=float)

    # Stage B: only rows with wrong nearest direction; check next-nearest candidates (max 5).
    dot0 = nearest["mvx"] * nearest["edge_dx"] + nearest["mvy"] * nearest["edge_dy"]
    need_rows = nearest.loc[dot0.notna() & (dot0 < 0.985), "row_id"].to_numpy(dtype=int) # +- 10 degrees tolerance
    if need_rows.size == 0:
        return edge, dist

    need_pts = pts.set_index("row_id").loc[need_rows].reset_index()
    pairs = edges.sindex.query(need_pts.geometry, predicate="dwithin", distance=float(max_radius_m))
    if pairs.size == 0:
        return edge, dist

    q_idx = np.asarray(pairs[0], dtype=int)
    e_idx = np.asarray(pairs[1], dtype=int)
    cand = pd.DataFrame(
        {
            "row_id": need_pts["row_id"].to_numpy()[q_idx],
            "mvx": need_pts["mvx"].to_numpy()[q_idx],
            "mvy": need_pts["mvy"].to_numpy()[q_idx],
            "edge_id": edges["edge_id"].to_numpy()[e_idx],
            "edge_dx": edges["edge_dx"].to_numpy()[e_idx],
            "edge_dy": edges["edge_dy"].to_numpy()[e_idx],
        }
    )
    cand["dist_m"] = need_pts.geometry.iloc[q_idx].distance(edges.geometry.iloc[e_idx], align=False).to_numpy(dtype=float)
    cand = cand.sort_values(["row_id", "dist_m"]).groupby("row_id", sort=False).head(5)
    cand["dot"] = cand["mvx"] * cand["edge_dx"] + cand["mvy"] * cand["edge_dy"]
    cand = cand[cand["dot"] >= 0.5]
    if cand.empty:
        return edge, dist

    best = cand.sort_values(["row_id", "dist_m"]).drop_duplicates("row_id", keep="first")
    edge.loc[best["row_id"].to_numpy()] = best["edge_id"].to_numpy()
    dist.loc[best["row_id"].to_numpy()] = best["dist_m"].to_numpy(dtype=float)
    return edge, dist


def _match_trips(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    max_radius_m: float,
    link_vectors: dict[str, tuple[float, float]],
    start_edge_ids: set[str],
    end_edge_ids: set[str],
) -> pd.DataFrame:
    out = trips.copy()
    edge_vec = edges.geometry.map(
        lambda g: _unit_vector_xy(g.coords[0][0], g.coords[0][1], g.coords[-1][0], g.coords[-1][1])
    )
    edges = edges.copy()
    edges["edge_dx"] = edge_vec.map(lambda v: v[0]).to_numpy(dtype=float)
    edges["edge_dy"] = edge_vec.map(lambda v: v[1]).to_numpy(dtype=float)
    start_edges = edges[edges["edge_id"].isin(start_edge_ids)].copy()
    end_edges = edges[edges["edge_id"].isin(end_edge_ids)].copy()
    out["from_edge"], out["from_edge_dist_m"] = _nearest_edges(
        out,
        start_edges,
        "start_x",
        "start_y",
        max_radius_m,
        point_type="start",
        link_vectors=link_vectors,
    )
    out["to_edge"], out["to_edge_dist_m"] = _nearest_edges(
        out,
        end_edges,
        "end_x",
        "end_y",
        max_radius_m,
        point_type="end",
        link_vectors=link_vectors,
    )
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    return out


def _load_sumo_edges(state: PipelineState) -> gpd.GeoDataFrame:
    if state.sumo_network_geo is not None:
        geo = state.sumo_network_geo
    elif Path(state.sumo_network_lv95_path).exists():
        geo = gpd.read_file(state.sumo_network_lv95_path)
    else:
        raise ValueError("SUMO network GeoDataFrame not available in state and LV95 GeoJSON is missing.")
    if "edge_id" not in geo.columns:
        raise ValueError("SUMO network GeoDataFrame must contain an 'edge_id' column.")
    return geo[["edge_id", "geometry"]].copy()


def _eligible_edge_ids(state: PipelineState, valid_edge_ids: set[str]) -> tuple[set[str], set[str]]:
    if state.sumo_net is None:
        raise ValueError("state.sumo_net must be available before step 8.")
    out_neighbors: dict[str, set[str]] = {}
    in_neighbors: dict[str, set[str]] = {}
    for e in state.sumo_net.getEdges(withInternal=False):
        eid = e.getID()
        if eid not in valid_edge_ids:
            continue
        out_neighbors[eid] = {n.getID() for n in e.getOutgoing().keys() if n.getID() in valid_edge_ids}
        in_neighbors[eid] = {n.getID() for n in e.getIncoming().keys() if n.getID() in valid_edge_ids}
    start_ids = {eid for eid, outs in out_neighbors.items() if outs}
    end_ids = {eid for eid, ins in in_neighbors.items() if ins}
    for _ in range(2, EDGE_CONNECTION_DEPTH + 1):
        start_ids = {eid for eid, outs in out_neighbors.items() if any(n in start_ids for n in outs)}
        end_ids = {eid for eid, ins in in_neighbors.items() if any(n in end_ids for n in ins)}
    return start_ids, end_ids


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)
    edges = _load_sumo_edges(state)
    link_vectors = _build_link_vectors(state)
    valid_edge_ids = set(edges["edge_id"].astype(str))
    start_edge_ids, end_edge_ids = _eligible_edge_ids(state, valid_edge_ids)
    matched = _match_trips(
        state.scaled_trips,
        edges=edges,
        max_radius_m=float(state.config["sumo_match_max_radius_m"]),
        link_vectors=link_vectors,
        start_edge_ids=start_edge_ids,
        end_edge_ids=end_edge_ids,
    )

    if "dep_time" in matched.columns:
        matched = matched.sort_values("dep_time").reset_index(drop=True)
    else:
        matched = matched.reset_index(drop=True)
    state.scaled_trips_matched = matched
    CsvWriter.write_df(state.scaled_trips_matched, state.scaled_trips_matched_path)
