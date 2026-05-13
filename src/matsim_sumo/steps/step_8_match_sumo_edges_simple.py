import gzip
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState

SECOND_NEAREST_EXTRA_M = 50.0


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_road_class(raw: object) -> str | None:
    if raw is None or pd.isna(raw):
        return None
    value = str(raw).strip().lower()
    if not value or value in {"nan", "none", "<na>"}:
        return None
    if value.startswith("highway."):
        value = value.removeprefix("highway.")
    if value.endswith("_link"):
        value = value.removesuffix("_link")
    return value or None


def _normalize_name(raw: object) -> str | None:
    if raw is None or pd.isna(raw):
        return None
    value = str(raw).strip().lower()
    return None if not value or value in {"nan", "none", "<na>"} else value


def _normalize_osm_id(raw: object) -> str | None:
    if raw is None or pd.isna(raw):
        return None
    value = str(raw).strip()
    return None if not value or value in {"nan", "None", "<NA>"} else value


def _sumo_base_osm_id(edge_id: object) -> str:
    return str(edge_id).lstrip("-").split("#", 1)[0]


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


def _reference_link_col(trips: pd.DataFrame, point_type: str) -> str:
    link_col = "new_start_link" if point_type == "start" else "new_end_link"
    if link_col not in trips.columns:
        link_col = "start_link" if point_type == "start" else "end_link"
    return link_col


def _read_matsim_road_classes(network_path: Path, link_ids: Iterable[object]) -> dict[str, str | None]:
    wanted = {str(link_id) for link_id in link_ids if not pd.isna(link_id)}
    if not wanted:
        return {}

    opener = gzip.open if str(network_path).endswith(".gz") else open
    out: dict[str, str | None] = {}
    with opener(network_path, "rb") as src:
        for _, elem in ET.iterparse(src, events=("end",)):
            if _local_name(elem.tag) != "link":
                continue
            link_id = elem.get("id")
            if link_id not in wanted:
                elem.clear()
                continue

            road_type = None
            for child in elem:
                if _local_name(child.tag) != "attributes":
                    continue
                for attr in child:
                    if _local_name(attr.tag) == "attribute" and attr.get("name") == "osm:way:highway":
                        road_type = attr.text
                        break
                if road_type is not None:
                    break

            out[str(link_id)] = _normalize_road_class(road_type)
            elem.clear()
            if len(out) == len(wanted):
                break
    return out


def _read_matsim_link_metadata(
    network_path: Path,
    link_ids: Iterable[object],
    link_vectors: dict[str, tuple[float, float]],
) -> dict[str, dict[str, object]]:
    wanted = {str(link_id) for link_id in link_ids if not pd.isna(link_id)}
    if not wanted:
        return {}

    opener = gzip.open if str(network_path).endswith(".gz") else open
    out: dict[str, dict[str, object]] = {}
    with opener(network_path, "rb") as src:
        for _, elem in ET.iterparse(src, events=("end",)):
            if _local_name(elem.tag) != "link":
                continue
            link_id = elem.get("id")
            if link_id not in wanted:
                elem.clear()
                continue

            attrs: dict[str, str | None] = {"road_class": None, "osm_id": None, "name_norm": None}
            for child in elem:
                if _local_name(child.tag) != "attributes":
                    continue
                for attr in child:
                    if _local_name(attr.tag) != "attribute":
                        continue
                    name = attr.get("name")
                    if name == "osm:way:highway":
                        attrs["road_class"] = _normalize_road_class(attr.text)
                    elif name == "osm:way:id":
                        attrs["osm_id"] = _normalize_osm_id(attr.text)
                    elif name == "osm:way:name":
                        attrs["name_norm"] = _normalize_name(attr.text)

            vx, vy = link_vectors.get(str(link_id), (np.nan, np.nan))
            out[str(link_id)] = {**attrs, "vx": vx, "vy": vy}
            elem.clear()
            if len(out) == len(wanted):
                break
    return out


def _build_sumo_road_classes(state: PipelineState, edge_ids: Iterable[object]) -> dict[str, str | None]:
    if state.sumo_net is None:
        raise ValueError("state.sumo_net must be available before step 8.")
    needed = {str(edge_id) for edge_id in edge_ids if not pd.isna(edge_id)}
    out: dict[str, str | None] = {}
    for edge in state.sumo_net.getEdges(withInternal=False):
        edge_id = edge.getID()
        if edge_id in needed:
            out[edge_id] = _normalize_road_class(edge.getType())
            if len(out) == len(needed):
                break
    return out


def _collect_route_link_ids(trips: pd.DataFrame) -> set[str]:
    if "route_links" not in trips.columns:
        return set()
    out: set[str] = set()
    route_rows = trips["route_links"].dropna().astype(str)
    for route in route_rows:
        if route and route != "nan":
            out.update(route.split())
    return out


def _nearest_edges(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    x_col: str,
    y_col: str,
    point_type: str,
    link_vectors: dict[str, tuple[float, float]],
    max_distance: float | None = None,
) -> tuple[pd.Series, pd.Series]:
    xy = trips[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
    valid = xy[x_col].notna() & xy[y_col].notna()
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    if not valid.any() or edges.empty:
        return edge, dist

    link_col = _reference_link_col(trips, point_type)
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
    sidx = edges.sindex
    nearest_kwargs = {"return_all": False, "return_distance": True}
    if max_distance is not None:
        nearest_kwargs["max_distance"] = float(max_distance)
    nearest, nearest_dist = sidx.nearest(pts.geometry, **nearest_kwargs)
    if nearest.size == 0:
        return edge, dist

    n = len(pts)
    p_idx = np.asarray(nearest[0], dtype=int)
    e1_idx = np.full(n, -1, dtype=int)
    d1 = np.full(n, np.nan, dtype=float)
    e1_idx[p_idx] = np.asarray(nearest[1], dtype=int)
    d1[p_idx] = np.asarray(nearest_dist, dtype=float)

    e2_idx = np.full(n, -1, dtype=int)
    d2 = np.full(n, np.nan, dtype=float)
    pairs = sidx.query(
        pts.geometry.iloc[p_idx],
        predicate="dwithin",
        distance=np.maximum(d1[p_idx] + SECOND_NEAREST_EXTRA_M, 1e-6),
    )
    if pairs.size:
        p2 = p_idx[np.asarray(pairs[0], dtype=int)]
        e2 = np.asarray(pairs[1], dtype=int)
        d = pts.geometry.iloc[p2].distance(edges.geometry.iloc[e2], align=False).to_numpy(dtype=float)
        keep = e2 != e1_idx[p2]
        if keep.any():
            p2, e2, d = p2[keep], e2[keep], d[keep]
            order = np.lexsort((d, p2))
            p2, e2, d = p2[order], e2[order], d[order]
            first = np.ones(p2.shape[0], dtype=bool)
            first[1:] = p2[1:] != p2[:-1]
            sel = p2[first]
            e2_idx[sel] = e2[first]
            d2[sel] = d[first]

    mvx = pts["mvx"].to_numpy(dtype=float)
    mvy = pts["mvy"].to_numpy(dtype=float)
    edge_dx = edges["edge_dx"].to_numpy(dtype=float)
    edge_dy = edges["edge_dy"].to_numpy(dtype=float)
    dot1 = np.full(n, np.nan, dtype=float)
    dot2 = np.full(n, np.nan, dtype=float)
    m1 = (e1_idx >= 0) & np.isfinite(mvx) & np.isfinite(mvy)
    m2 = (e2_idx >= 0) & np.isfinite(mvx) & np.isfinite(mvy)
    dot1[m1] = mvx[m1] * edge_dx[e1_idx[m1]] + mvy[m1] * edge_dy[e1_idx[m1]]
    dot2[m2] = mvx[m2] * edge_dx[e2_idx[m2]] + mvy[m2] * edge_dy[e2_idx[m2]]
    use2 = (e2_idx >= 0) & np.isfinite(dot2) & (~np.isfinite(dot1) | (dot2 > dot1))

    pick_idx = e1_idx.copy()
    pick_dist = d1.copy()
    pick_idx[use2] = e2_idx[use2]
    pick_dist[use2] = d2[use2]

    row_ids = pts["row_id"].to_numpy(dtype=int)
    ok = pick_idx >= 0
    edge_ids = edges["edge_id"].to_numpy(dtype=object)
    edge.loc[row_ids[ok]] = edge_ids[pick_idx[ok]]
    dist.loc[row_ids[ok]] = pick_dist[ok]
    return edge, dist


def _nearest_edges_type_aware(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    x_col: str,
    y_col: str,
    point_type: str,
    link_vectors: dict[str, tuple[float, float]],
    matsim_road_classes: dict[str, str | None],
    same_type_max_radius_m: float,
    fallback_enabled: bool,
    road_type_enabled: bool,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    policy = pd.Series(index=trips.index, dtype=object)
    link_col = _reference_link_col(trips, point_type)
    matsim_class = trips[link_col].astype(str).map(matsim_road_classes)

    if road_type_enabled:
        for road_class, idx in matsim_class[matsim_class.notna()].groupby(matsim_class).groups.items():
            same_type_edges = edges[edges["road_class"].eq(road_class)]
            if same_type_edges.empty:
                continue
            matched_edge, matched_dist = _nearest_edges(
                trips.loc[idx],
                same_type_edges,
                x_col,
                y_col,
                point_type,
                link_vectors,
                max_distance=same_type_max_radius_m,
            )
            ok = matched_edge.notna()
            if ok.any():
                edge.loc[matched_edge.index[ok]] = matched_edge.loc[ok]
                dist.loc[matched_dist.index[ok]] = matched_dist.loc[ok]
                policy.loc[matched_edge.index[ok]] = "same_type"

    fallback_mask = edge.isna()
    missing_type = matsim_class.isna()
    if road_type_enabled:
        fallback_any = fallback_mask & matsim_class.notna()
        missing_matsim = fallback_mask & missing_type
    else:
        fallback_any = fallback_mask
        missing_matsim = pd.Series(False, index=trips.index)

    if fallback_enabled:
        for mask, fallback_policy in ((fallback_any, "fallback_any"), (missing_matsim, "missing_matsim_type")):
            if not mask.any():
                continue
            matched_edge, matched_dist = _nearest_edges(
                trips.loc[mask],
                edges,
                x_col,
                y_col,
                point_type,
                link_vectors,
            )
            ok = matched_edge.notna()
            if ok.any():
                edge.loc[matched_edge.index[ok]] = matched_edge.loc[ok]
                dist.loc[matched_dist.index[ok]] = matched_dist.loc[ok]
                policy.loc[matched_edge.index[ok]] = fallback_policy

    sumo_class = edge.astype(str).map(dict(zip(edges["edge_id"].astype(str), edges["road_class"])))
    return edge, dist, matsim_class, sumo_class, policy


def _build_route_candidate_indexes(start_edges: gpd.GeoDataFrame) -> tuple[dict[str, list[dict]], dict[tuple[str, str], list[dict]]]:
    by_osm: dict[str, list[dict]] = {}
    by_name_class: dict[tuple[str, str], list[dict]] = {}
    for row in start_edges.itertuples(index=False):
        rec = {
            "edge_id": str(row.edge_id),
            "osm_id": _sumo_base_osm_id(row.edge_id),
            "road_class": row.road_class,
            "name_norm": _normalize_name(getattr(row, "sumo_name", None)),
            "dx": float(row.edge_dx),
            "dy": float(row.edge_dy),
            "geometry": row.geometry,
        }
        by_osm.setdefault(rec["osm_id"], []).append(rec)
        if rec["name_norm"] and rec["road_class"]:
            by_name_class.setdefault((rec["name_norm"], rec["road_class"]), []).append(rec)
    return by_osm, by_name_class


def _best_route_candidate(
    meta: dict[str, object],
    candidates: list[dict],
    point: Point,
    max_radius_m: float,
    min_dot: float,
) -> tuple[dict | None, float]:
    best: tuple[float, float, dict] | None = None
    mvx, mvy = float(meta.get("vx", np.nan)), float(meta.get("vy", np.nan))
    if not np.isfinite(mvx) or not np.isfinite(mvy):
        return None, np.nan
    for candidate in candidates:
        if meta.get("road_class") and candidate.get("road_class") and meta["road_class"] != candidate["road_class"]:
            continue
        dot = mvx * candidate["dx"] + mvy * candidate["dy"]
        if not np.isfinite(dot) or dot < min_dot:
            continue
        dist = float(point.distance(candidate["geometry"]))
        if dist > max_radius_m:
            continue
        score = (dot, -dist, candidate)
        if best is None or score[:2] > best[:2]:
            best = score
    if best is None:
        return None, np.nan
    return best[2], -best[1]


def _route_sequence_start_matches(
    trips: pd.DataFrame,
    start_edges: gpd.GeoDataFrame,
    matsim_link_metadata: dict[str, dict[str, object]],
    max_radius_m: float,
    min_dot: float,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    matsim_class = pd.Series(index=trips.index, dtype=object)
    sumo_class = pd.Series(index=trips.index, dtype=object)
    route_link = pd.Series(index=trips.index, dtype=object)
    route_osm_id = pd.Series(index=trips.index, dtype=object)
    if "route_links" not in trips.columns or start_edges.empty:
        return edge, dist, matsim_class, sumo_class, route_link, route_osm_id

    by_osm, by_name_class = _build_route_candidate_indexes(start_edges)
    edge_class = dict(zip(start_edges["edge_id"].astype(str), start_edges["road_class"]))
    route_rows = trips["route_links"].fillna("").astype(str)
    crossing = trips.get("crossing_type", pd.Series("", index=trips.index)).astype(str)
    x = pd.to_numeric(trips["start_x"], errors="coerce")
    y = pd.to_numeric(trips["start_y"], errors="coerce")
    eligible = crossing.isin({"entering", "pass_through"}) & route_rows.ne("") & x.notna() & y.notna()
    cache: dict[tuple[str, float, float], tuple[object, float, object, object, object]] = {}

    for idx in trips.index[eligible]:
        px, py = float(x.loc[idx]), float(y.loc[idx])
        route = route_rows.loc[idx]
        key = (route, px, py)
        if key in cache:
            cached = cache[key]
        else:
            point = Point(px, py)
            cached = (pd.NA, np.nan, pd.NA, pd.NA, pd.NA)
            for link_id in route.split():
                meta = matsim_link_metadata.get(link_id)
                if not meta:
                    continue
                osm_id = meta.get("osm_id")
                candidate = None
                candidate_dist = np.nan
                if osm_id and osm_id in by_osm:
                    candidate, candidate_dist = _best_route_candidate(meta, by_osm[osm_id], point, max_radius_m, min_dot)
                elif meta.get("name_norm") and meta.get("road_class"):
                    candidate, candidate_dist = _best_route_candidate(
                        meta,
                        by_name_class.get((meta["name_norm"], meta["road_class"]), []),
                        point,
                        max_radius_m,
                        min_dot,
                    )
                if candidate is not None:
                    cached = (
                        candidate["edge_id"],
                        candidate_dist,
                        link_id,
                        meta.get("road_class"),
                        meta.get("osm_id"),
                    )
                    break
            cache[key] = cached

        if not pd.isna(cached[0]):
            edge.loc[idx] = cached[0]
            dist.loc[idx] = cached[1]
            route_link.loc[idx] = cached[2]
            matsim_class.loc[idx] = cached[3]
            route_osm_id.loc[idx] = cached[4]
            sumo_class.loc[idx] = edge_class.get(str(cached[0]))

    return edge, dist, matsim_class, sumo_class, route_link, route_osm_id


def _match_trips(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    link_vectors: dict[str, tuple[float, float]],
    start_edge_ids: set[str],
    end_edge_ids: set[str],
    matsim_road_classes: dict[str, str | None],
    matsim_link_metadata: dict[str, dict[str, object]],
    road_type_enabled: bool,
    same_type_max_radius_m: float,
    fallback_enabled: bool,
    route_sequence_enabled: bool,
    route_sequence_max_radius_m: float,
    route_sequence_min_dot: float,
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
    (
        out["from_edge"],
        out["from_edge_dist_m"],
        out["from_matsim_road_class"],
        out["from_sumo_road_class"],
        out["from_match_policy"],
    ) = _nearest_edges_type_aware(
        out,
        start_edges,
        "start_x",
        "start_y",
        point_type="start",
        link_vectors=link_vectors,
        matsim_road_classes=matsim_road_classes,
        same_type_max_radius_m=same_type_max_radius_m,
        fallback_enabled=fallback_enabled,
        road_type_enabled=road_type_enabled,
    )
    out["from_route_match_link"] = pd.NA
    out["from_route_match_osm_id"] = pd.NA
    if route_sequence_enabled:
        (
            seq_edge,
            seq_dist,
            seq_matsim_class,
            seq_sumo_class,
            seq_route_link,
            seq_route_osm_id,
        ) = _route_sequence_start_matches(
            out,
            start_edges,
            matsim_link_metadata,
            max_radius_m=route_sequence_max_radius_m,
            min_dot=route_sequence_min_dot,
        )
        ok = seq_edge.notna()
        if ok.any():
            out.loc[ok, "from_edge"] = seq_edge.loc[ok]
            out.loc[ok, "from_edge_dist_m"] = seq_dist.loc[ok]
            out.loc[ok, "from_matsim_road_class"] = seq_matsim_class.loc[ok]
            out.loc[ok, "from_sumo_road_class"] = seq_sumo_class.loc[ok]
            out.loc[ok, "from_match_policy"] = "route_sequence"
            out.loc[ok, "from_route_match_link"] = seq_route_link.loc[ok]
            out.loc[ok, "from_route_match_osm_id"] = seq_route_osm_id.loc[ok]
    (
        out["to_edge"],
        out["to_edge_dist_m"],
        out["to_matsim_road_class"],
        out["to_sumo_road_class"],
        out["to_match_policy"],
    ) = _nearest_edges_type_aware(
        out,
        end_edges,
        "end_x",
        "end_y",
        point_type="end",
        link_vectors=link_vectors,
        matsim_road_classes=matsim_road_classes,
        same_type_max_radius_m=same_type_max_radius_m,
        fallback_enabled=fallback_enabled,
        road_type_enabled=road_type_enabled,
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

    def _fixed_point(neighbors: dict[str, set[str]]) -> set[str]:
        keep = set(neighbors)
        while True:
            nxt = {eid for eid in keep if any(n in keep for n in neighbors[eid])}
            if len(nxt) == len(keep):
                return nxt
            keep = nxt

    start_ids = _fixed_point(out_neighbors)
    end_ids = _fixed_point(in_neighbors)
    return start_ids, end_ids


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)
    edges = _load_sumo_edges(state)
    link_vectors = _build_link_vectors(state)
    valid_edge_ids = set(edges["edge_id"].astype(str))
    start_edge_ids, end_edge_ids = _eligible_edge_ids(state, valid_edge_ids)
    road_type_enabled = bool(state.config.get("sumo_match_road_type_enabled", True))
    fallback_enabled = bool(state.config.get("sumo_match_fallback_enabled", True))
    same_type_max_radius_m = float(state.config.get("sumo_match_same_type_max_radius_m", 1000.0))
    route_sequence_enabled = bool(state.config.get("sumo_match_route_sequence_enabled", True))
    route_sequence_max_radius_m = float(state.config.get("sumo_match_route_sequence_max_radius_m", 1000.0))
    route_sequence_min_dot = float(state.config.get("sumo_match_route_sequence_min_dot", 0.3))
    sumo_road_classes = _build_sumo_road_classes(state, valid_edge_ids)
    sumo_names = {e.getID(): e.getName() for e in state.sumo_net.getEdges(withInternal=False)}
    edges["road_class"] = edges["edge_id"].astype(str).map(sumo_road_classes)
    edges["sumo_name"] = edges["edge_id"].astype(str).map(sumo_names)
    start_link_col = _reference_link_col(state.scaled_trips, "start")
    end_link_col = _reference_link_col(state.scaled_trips, "end")
    needed_links = pd.concat(
        [
            state.scaled_trips[start_link_col].astype("string"),
            state.scaled_trips[end_link_col].astype("string"),
        ],
        ignore_index=True,
    ).dropna()
    route_link_ids = _collect_route_link_ids(state.scaled_trips) if route_sequence_enabled else set()
    needed_link_ids = set(needed_links.astype(str).unique()).union(route_link_ids)
    matsim_link_metadata = _read_matsim_link_metadata(state.network_path, needed_link_ids, link_vectors)
    matsim_road_classes = {
        link_id: meta.get("road_class")
        for link_id, meta in matsim_link_metadata.items()
    } if road_type_enabled else {}
    matched = _match_trips(
        state.scaled_trips,
        edges=edges,
        link_vectors=link_vectors,
        start_edge_ids=start_edge_ids,
        end_edge_ids=end_edge_ids,
        matsim_road_classes=matsim_road_classes,
        matsim_link_metadata=matsim_link_metadata,
        road_type_enabled=road_type_enabled,
        same_type_max_radius_m=same_type_max_radius_m,
        fallback_enabled=fallback_enabled,
        route_sequence_enabled=route_sequence_enabled,
        route_sequence_max_radius_m=route_sequence_max_radius_m,
        route_sequence_min_dot=route_sequence_min_dot,
    )

    if "dep_time" in matched.columns:
        matched = matched.sort_values("dep_time").reset_index(drop=True)
    else:
        matched = matched.reset_index(drop=True)
    state.scaled_trips_matched = matched
    CsvWriter.write_df(state.scaled_trips_matched, state.scaled_trips_matched_path)
