import gzip
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.io.csv_writer import CsvWriter
from matsim_sumo.utils.cache import disk_cache, file_dep, output_needs_refresh, param_dep
from matsim_sumo.pipeline.state import PipelineState

SECOND_NEAREST_EXTRA_M = 50.0
DEFAULT_ROUTEABLE_CANDIDATE_LIMIT = 20
DEFAULT_ROUTEABLE_CANDIDATE_EXTRA_M = 100.0
DISTANCE_DIAGNOSTIC_THRESHOLDS_M = (50.0, 100.0, 150.0, 250.0)
LOCAL_ACTIVITY_ENDPOINT_CLASSES = {"residential", "living_street", "service"}
MATCH_SUMO_EDGES_LOGIC_VERSION = 3


def _config_float(config: dict, key: str, default: float | None) -> float | None:
    value = config.get(key, default)
    return None if value is None else float(value)


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

            attrs: dict[str, str | None] = {
                "road_class": None,
                "osm_id": None,
                "name_norm": None,
                "from_node": elem.get("from"),
                "to_node": elem.get("to"),
            }
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


def _build_sumo_edge_metadata(state: PipelineState, edge_ids: Iterable[object]) -> dict[str, dict[str, str | None]]:
    if state.sumo_net is None:
        raise ValueError("state.sumo_net must be available before step 8.")
    needed = {str(edge_id) for edge_id in edge_ids if not pd.isna(edge_id)}
    out: dict[str, dict[str, str | None]] = {}
    for edge in state.sumo_net.getEdges(withInternal=False):
        edge_id = edge.getID()
        if edge_id in needed:
            out[edge_id] = {
                "road_class": _normalize_road_class(edge.getType()),
                "sumo_name": edge.getName(),
                "from_node": edge.getFromNode().getID(),
                "to_node": edge.getToNode().getID(),
            }
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
    fallback_max_radius_m: float | None,
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
                max_distance=fallback_max_radius_m,
            )
            ok = matched_edge.notna()
            if ok.any():
                edge.loc[matched_edge.index[ok]] = matched_edge.loc[ok]
                dist.loc[matched_dist.index[ok]] = matched_dist.loc[ok]
                policy.loc[matched_edge.index[ok]] = fallback_policy

    sumo_class = edge.astype(str).map(dict(zip(edges["edge_id"].astype(str), edges["road_class"])))
    return edge, dist, matsim_class, sumo_class, policy


def _routeable_candidates_for_row(
    row: pd.Series,
    edges: gpd.GeoDataFrame,
    x_col: str,
    y_col: str,
    point_type: str,
    link_vectors: dict[str, tuple[float, float]],
    matsim_road_classes: dict[str, str | None],
    same_type_max_radius_m: float,
    fallback_enabled: bool,
    road_type_enabled: bool,
    fallback_max_radius_m: float | None,
    candidate_limit: int,
    candidate_extra_m: float,
) -> list[dict[str, object]]:
    x = pd.to_numeric(pd.Series([row.get(x_col)]), errors="coerce").iloc[0]
    y = pd.to_numeric(pd.Series([row.get(y_col)]), errors="coerce").iloc[0]
    if pd.isna(x) or pd.isna(y) or edges.empty:
        return []

    link_col = _reference_link_col(pd.DataFrame([row]), point_type)
    link_id = row.get(link_col)
    matsim_class = matsim_road_classes.get(str(link_id))
    mvx, mvy = link_vectors.get(str(link_id), (np.nan, np.nan))
    point = Point(float(x), float(y))

    def _collect(
        candidate_edges: gpd.GeoDataFrame,
        radius_m: float | None,
        policy: str,
    ) -> list[dict[str, object]]:
        if candidate_edges.empty:
            return []
        if radius_m is None:
            pairs = candidate_edges.sindex.nearest([point], return_all=True, return_distance=False)
        else:
            search_radius_m = float(radius_m) + float(candidate_extra_m)
            pairs = candidate_edges.sindex.query([point], predicate="dwithin", distance=search_radius_m)
        if pairs.size == 0:
            return []
        edge_positions = np.asarray(pairs[1], dtype=int)
        edge_positions = np.unique(edge_positions)
        distances = candidate_edges.geometry.iloc[edge_positions].distance(point, align=False).to_numpy(dtype=float)
        edge_dx = candidate_edges["edge_dx"].iloc[edge_positions].to_numpy(dtype=float)
        edge_dy = candidate_edges["edge_dy"].iloc[edge_positions].to_numpy(dtype=float)
        if np.isfinite(mvx) and np.isfinite(mvy):
            dots = mvx * edge_dx + mvy * edge_dy
        else:
            dots = np.zeros(edge_positions.shape[0], dtype=float)

        out = []
        for pos, dist, dot in zip(edge_positions, distances, dots):
            edge_row = candidate_edges.iloc[int(pos)]
            out.append(
                {
                    "edge_id": str(edge_row["edge_id"]),
                    "dist": float(dist),
                    "dot": float(dot) if np.isfinite(dot) else 0.0,
                    "matsim_class": matsim_class,
                    "sumo_class": edge_row.get("road_class"),
                    "policy": policy,
                }
            )
        out.sort(key=lambda item: (float(item["dist"]), -float(item["dot"]), str(item["edge_id"])))
        return out[:candidate_limit]

    if road_type_enabled and matsim_class is not None:
        same_type = edges[edges["road_class"].eq(matsim_class)]
        candidates = _collect(same_type, same_type_max_radius_m, "same_type")
        if candidates:
            return candidates

    if not fallback_enabled:
        return []
    fallback_policy = "missing_matsim_type" if road_type_enabled and matsim_class is None else "fallback_any"
    return _collect(edges, fallback_max_radius_m, fallback_policy)


def _is_reachable(
    from_edge: object,
    to_edge: object,
    out_neighbors: dict[str, set[str]],
    cache: dict[str, set[str]],
) -> bool:
    if from_edge is None or to_edge is None or pd.isna(from_edge) or pd.isna(to_edge):
        return False
    start = str(from_edge)
    target = str(to_edge)
    if start == target:
        return True
    if start not in cache:
        seen = {start}
        stack = [start]
        while stack:
            edge_id = stack.pop()
            for next_edge in out_neighbors.get(edge_id, set()):
                if next_edge not in seen:
                    seen.add(next_edge)
                    stack.append(next_edge)
        cache[start] = seen
    return target in cache[start]


def _apply_candidate(
    out: pd.DataFrame,
    idx,
    prefix: str,
    candidate: dict[str, object],
) -> None:
    out.loc[idx, f"{prefix}_edge"] = candidate["edge_id"]
    out.loc[idx, f"{prefix}_edge_dist_m"] = candidate["dist"]
    out.loc[idx, f"{prefix}_matsim_road_class"] = candidate["matsim_class"]
    out.loc[idx, f"{prefix}_sumo_road_class"] = candidate["sumo_class"]
    out.loc[idx, f"{prefix}_match_policy"] = candidate["policy"]


def _repair_unrouteable_matches(
    out: pd.DataFrame,
    start_edges: gpd.GeoDataFrame,
    end_edges: gpd.GeoDataFrame,
    link_vectors: dict[str, tuple[float, float]],
    out_neighbors: dict[str, set[str]],
    matsim_road_classes: dict[str, str | None],
    road_type_enabled: bool,
    same_type_max_radius_m: float,
    fallback_enabled: bool,
    fallback_max_radius_m: float | None,
    candidate_limit: int,
    candidate_extra_m: float,
) -> pd.DataFrame:
    if not out_neighbors:
        out["sumo_endpoint_routeable"] = out["from_edge"].notna() & out["to_edge"].notna()
        return out

    cache: dict[str, set[str]] = {}
    routeable = pd.Series(False, index=out.index, dtype=bool)
    matched = out["from_edge"].notna() & out["to_edge"].notna()
    routeable.loc[matched] = [
        _is_reachable(from_edge, to_edge, out_neighbors, cache)
        for from_edge, to_edge in zip(out.loc[matched, "from_edge"], out.loc[matched, "to_edge"])
    ]

    for idx in out.index[matched & ~routeable]:
        row = out.loc[idx]
        crossing_type = str(row.get("crossing_type", ""))
        start_candidates: list[dict[str, object]] = []
        end_candidates: list[dict[str, object]] = []

        if crossing_type in {"leaving", "inside"}:
            start_candidates = _routeable_candidates_for_row(
                row,
                start_edges,
                "start_x",
                "start_y",
                point_type="start",
                link_vectors=link_vectors,
                matsim_road_classes=matsim_road_classes,
                same_type_max_radius_m=same_type_max_radius_m,
                fallback_enabled=fallback_enabled,
                road_type_enabled=road_type_enabled,
                fallback_max_radius_m=fallback_max_radius_m,
                candidate_limit=candidate_limit,
                candidate_extra_m=candidate_extra_m,
            )
        if crossing_type in {"entering", "inside", "pass_through"}:
            end_candidates = _routeable_candidates_for_row(
                row,
                end_edges,
                "end_x",
                "end_y",
                point_type="end",
                link_vectors=link_vectors,
                matsim_road_classes=matsim_road_classes,
                same_type_max_radius_m=same_type_max_radius_m,
                fallback_enabled=fallback_enabled,
                road_type_enabled=road_type_enabled,
                fallback_max_radius_m=fallback_max_radius_m,
                candidate_limit=candidate_limit,
                candidate_extra_m=candidate_extra_m,
            )

        repaired = False
        if crossing_type in {"entering", "pass_through"}:
            fixed_from = row["from_edge"]
            for candidate in end_candidates:
                if _is_reachable(fixed_from, candidate["edge_id"], out_neighbors, cache):
                    _apply_candidate(out, idx, "to", candidate)
                    repaired = True
                    break
        elif crossing_type == "leaving":
            fixed_to = row["to_edge"]
            for candidate in start_candidates:
                if _is_reachable(candidate["edge_id"], fixed_to, out_neighbors, cache):
                    _apply_candidate(out, idx, "from", candidate)
                    repaired = True
                    break
        elif crossing_type == "inside":
            best: tuple[float, float, dict[str, object], dict[str, object]] | None = None
            for start_candidate in start_candidates:
                for end_candidate in end_candidates:
                    if not _is_reachable(start_candidate["edge_id"], end_candidate["edge_id"], out_neighbors, cache):
                        continue
                    score = (
                        float(start_candidate["dist"]) + float(end_candidate["dist"]),
                        -float(start_candidate["dot"]) - float(end_candidate["dot"]),
                        start_candidate,
                        end_candidate,
                    )
                    if best is None or score[:2] < best[:2]:
                        best = score
            if best is not None:
                _apply_candidate(out, idx, "from", best[2])
                _apply_candidate(out, idx, "to", best[3])
                repaired = True

        if repaired:
            routeable.loc[idx] = _is_reachable(out.loc[idx, "from_edge"], out.loc[idx, "to_edge"], out_neighbors, cache)

    matched = out["from_edge"].notna() & out["to_edge"].notna()
    routeable.loc[matched] = [
        _is_reachable(from_edge, to_edge, out_neighbors, cache)
        for from_edge, to_edge in zip(out.loc[matched, "from_edge"], out.loc[matched, "to_edge"])
    ]
    out["sumo_endpoint_routeable"] = routeable
    return out


def _build_route_candidate_indexes(
    candidate_edges: gpd.GeoDataFrame,
) -> tuple[dict[tuple[str, str], list[dict]], dict[str, list[dict]], dict[tuple[str, str], list[dict]]]:
    by_node_pair: dict[tuple[str, str], list[dict]] = {}
    by_osm: dict[str, list[dict]] = {}
    by_name_class: dict[tuple[str, str], list[dict]] = {}
    for row in candidate_edges.itertuples(index=False):
        rec = {
            "edge_id": str(row.edge_id),
            "osm_id": _sumo_base_osm_id(row.edge_id),
            "road_class": row.road_class,
            "name_norm": _normalize_name(getattr(row, "sumo_name", None)),
            "from_node": getattr(row, "from_node", None),
            "to_node": getattr(row, "to_node", None),
            "dx": float(row.edge_dx),
            "dy": float(row.edge_dy),
            "geometry": row.geometry,
        }
        if rec["from_node"] and rec["to_node"]:
            by_node_pair.setdefault((str(rec["from_node"]), str(rec["to_node"])), []).append(rec)
        by_osm.setdefault(rec["osm_id"], []).append(rec)
        if rec["name_norm"] and rec["road_class"]:
            by_name_class.setdefault((rec["name_norm"], rec["road_class"]), []).append(rec)
    return by_node_pair, by_osm, by_name_class


def _edges_reaching(
    target_edge: object,
    in_neighbors: dict[str, set[str]],
    cache: dict[str, set[str]],
) -> set[str]:
    if target_edge is None or pd.isna(target_edge):
        return set()
    target = str(target_edge)
    if target in cache:
        return cache[target]

    seen = {target}
    stack = [target]
    while stack:
        edge_id = stack.pop()
        for prev_edge in in_neighbors.get(edge_id, set()):
            if prev_edge not in seen:
                seen.add(prev_edge)
                stack.append(prev_edge)
    cache[target] = seen
    return seen


def _best_route_candidate(
    meta: dict[str, object],
    candidates: list[dict],
    point: Point,
    max_radius_m: float,
    min_dot: float,
    target_edge: object,
    in_neighbors: dict[str, set[str]],
    reachability_cache: dict[str, set[str]],
) -> tuple[dict | None, float]:
    best: tuple[float, float, dict] | None = None
    mvx, mvy = float(meta.get("vx", np.nan)), float(meta.get("vy", np.nan))
    if not np.isfinite(mvx) or not np.isfinite(mvy):
        return None, np.nan
    reachable = _edges_reaching(target_edge, in_neighbors, reachability_cache)
    if not reachable:
        return None, np.nan
    for candidate in candidates:
        if candidate["edge_id"] not in reachable:
            continue
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
    candidate_edges: gpd.GeoDataFrame,
    matsim_link_metadata: dict[str, dict[str, object]],
    in_neighbors: dict[str, set[str]],
    max_radius_m: float,
    min_dot: float,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    matsim_class = pd.Series(index=trips.index, dtype=object)
    sumo_class = pd.Series(index=trips.index, dtype=object)
    route_link = pd.Series(index=trips.index, dtype=object)
    route_osm_id = pd.Series(index=trips.index, dtype=object)
    if "route_links" not in trips.columns or candidate_edges.empty:
        return edge, dist, matsim_class, sumo_class, route_link, route_osm_id

    by_node_pair, by_osm, by_name_class = _build_route_candidate_indexes(candidate_edges)
    edge_class = dict(zip(candidate_edges["edge_id"].astype(str), candidate_edges["road_class"]))
    route_rows = trips["route_links"].fillna("").astype(str)
    crossing = trips.get("crossing_type", pd.Series("", index=trips.index)).astype(str)
    x = pd.to_numeric(trips["start_x"], errors="coerce")
    y = pd.to_numeric(trips["start_y"], errors="coerce")
    target_edges = trips.get("to_edge", pd.Series(pd.NA, index=trips.index))
    eligible = (
        crossing.isin({"entering", "pass_through"})
        & route_rows.ne("")
        & x.notna()
        & y.notna()
        & target_edges.notna()
    )
    cache: dict[tuple[str, float, float, str], tuple[object, float, object, object, object]] = {}
    reachability_cache: dict[str, set[str]] = {}

    for idx in trips.index[eligible]:
        px, py = float(x.loc[idx]), float(y.loc[idx])
        route = route_rows.loc[idx]
        target_edge = str(target_edges.loc[idx])
        key = (route, px, py, target_edge)
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
                candidate_groups = []
                node_key = (meta.get("from_node"), meta.get("to_node"))
                if all(node_key) and (str(node_key[0]), str(node_key[1])) in by_node_pair:
                    candidate_groups.append(by_node_pair[(str(node_key[0]), str(node_key[1]))])
                if osm_id and osm_id in by_osm:
                    candidate_groups.append(by_osm[osm_id])
                elif meta.get("name_norm") and meta.get("road_class"):
                    candidate_groups.append(by_name_class.get((meta["name_norm"], meta["road_class"]), []))

                for candidates in candidate_groups:
                    candidate, candidate_dist = _best_route_candidate(
                        meta,
                        candidates,
                        point,
                        max_radius_m,
                        min_dot,
                        target_edge=target_edge,
                        in_neighbors=in_neighbors,
                        reachability_cache=reachability_cache,
                    )
                    if candidate is not None:
                        break
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
    out_neighbors: dict[str, set[str]] | None,
    in_neighbors: dict[str, set[str]],
    matsim_road_classes: dict[str, str | None],
    matsim_link_metadata: dict[str, dict[str, object]],
    road_type_enabled: bool,
    same_type_max_radius_m: float,
    fallback_enabled: bool,
    fallback_max_radius_m: float | None,
    route_sequence_enabled: bool,
    route_sequence_max_radius_m: float,
    route_sequence_min_dot: float,
    routeable_endpoints_enabled: bool = True,
    routeable_candidate_limit: int = DEFAULT_ROUTEABLE_CANDIDATE_LIMIT,
    routeable_candidate_extra_m: float = DEFAULT_ROUTEABLE_CANDIDATE_EXTRA_M,
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
        fallback_max_radius_m=fallback_max_radius_m,
    )
    out["from_route_match_link"] = pd.NA
    out["from_route_match_osm_id"] = pd.NA
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
        fallback_max_radius_m=fallback_max_radius_m,
    )
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
            edges,
            matsim_link_metadata,
            in_neighbors=in_neighbors,
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
    if routeable_endpoints_enabled:
        out = _repair_unrouteable_matches(
            out,
            start_edges=start_edges,
            end_edges=end_edges,
            link_vectors=link_vectors,
            out_neighbors=out_neighbors or {},
            matsim_road_classes=matsim_road_classes,
            road_type_enabled=road_type_enabled,
            same_type_max_radius_m=same_type_max_radius_m,
            fallback_enabled=fallback_enabled,
            fallback_max_radius_m=fallback_max_radius_m,
            candidate_limit=routeable_candidate_limit,
            candidate_extra_m=routeable_candidate_extra_m,
        )
        out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna() & out["sumo_endpoint_routeable"]
    else:
        out["sumo_endpoint_routeable"] = pd.NA
        out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    return out


def _endpoint_distance_diagnostics(matched: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    series = {
        "from": pd.to_numeric(matched.get("from_edge_dist_m"), errors="coerce"),
        "to": pd.to_numeric(matched.get("to_edge_dist_m"), errors="coerce"),
    }
    series["max"] = pd.concat([series["from"], series["to"]], axis=1).max(axis=1)

    out: dict[str, dict[str, float | int]] = {}
    for label, values in series.items():
        valid = values.dropna().astype(float)
        row: dict[str, float | int] = {"count": int(valid.size)}
        if valid.empty:
            for pct in (50, 90, 95, 99, 100):
                row[f"p{pct}_m"] = float("nan")
        else:
            for pct in (50, 90, 95, 99, 100):
                row[f"p{pct}_m"] = float(np.percentile(valid.to_numpy(dtype=float), pct))
        for threshold in DISTANCE_DIAGNOSTIC_THRESHOLDS_M:
            row[f"gt_{int(threshold)}m"] = int(valid.gt(threshold).sum())
        out[label] = row
    return out


def _format_distance_diagnostics(diagnostics: dict[str, dict[str, float | int]]) -> str:
    parts = []
    for label in ("from", "to", "max"):
        row = diagnostics.get(label, {})
        parts.append(
            f"{label}: p50={row.get('p50_m', float('nan')):.1f}m "
            f"p90={row.get('p90_m', float('nan')):.1f}m "
            f"p95={row.get('p95_m', float('nan')):.1f}m "
            f"p99={row.get('p99_m', float('nan')):.1f}m "
            f"max={row.get('p100_m', float('nan')):.1f}m"
        )
    return "; ".join(parts)


def _apply_endpoint_distance_guardrail(
    matched: pd.DataFrame,
    max_distance_m: float | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = matched.copy()
    from_dist = pd.to_numeric(out["from_edge_dist_m"], errors="coerce")
    to_dist = pd.to_numeric(out["to_edge_dist_m"], errors="coerce")
    out["sumo_endpoint_max_dist_m"] = pd.concat([from_dist, to_dist], axis=1).max(axis=1)
    routeable = out.get("sumo_endpoint_routeable", pd.Series(True, index=out.index)).fillna(True).astype(bool)
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna() & routeable
    out["sumo_local_activity_endpoint_fallback"] = _local_activity_endpoint_fallback_mask(out)

    keep = out["match_ok"] & ~out["sumo_local_activity_endpoint_fallback"]
    if max_distance_m is not None:
        keep &= from_dist.le(max_distance_m) & to_dist.le(max_distance_m)

    return out.loc[keep].copy(), out.loc[~keep].copy()


def _local_activity_endpoint_fallback_mask(matched: pd.DataFrame) -> pd.Series:
    crossing = matched.get("crossing_type", pd.Series("", index=matched.index)).astype(str)
    false = pd.Series(False, index=matched.index)

    from_class = matched.get("from_matsim_road_class", pd.Series(pd.NA, index=matched.index)).astype(str)
    to_class = matched.get("to_matsim_road_class", pd.Series(pd.NA, index=matched.index)).astype(str)
    from_policy = matched.get("from_match_policy", pd.Series("", index=matched.index)).astype(str)
    to_policy = matched.get("to_match_policy", pd.Series("", index=matched.index)).astype(str)

    bad_from = (
        crossing.isin({"inside", "leaving"})
        & from_policy.eq("fallback_any")
        & from_class.isin(LOCAL_ACTIVITY_ENDPOINT_CLASSES)
    )
    bad_to = (
        crossing.isin({"inside", "entering"})
        & to_policy.eq("fallback_any")
        & to_class.isin(LOCAL_ACTIVITY_ENDPOINT_CLASSES)
    )
    return false | bad_from | bad_to


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
    out_neighbors, in_neighbors = _edge_neighbors(state, valid_edge_ids)

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


def _edge_neighbors(state: PipelineState, valid_edge_ids: set[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
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
    return out_neighbors, in_neighbors


def _match_sumo_edges_deps(state: PipelineState):
    deps = [
        file_dep(state.plans_path),
        file_dep(state.trips_path),
        file_dep(state.events_path),
        file_dep(state.extract_events_script_path),
        file_dep(state.network_path),
        file_dep(state.boundary_path),
        file_dep(state.sumo_network_path),
        file_dep(state.scaled_trips_path),
        param_dep("modes_keep", tuple(state.config["modes_keep"])),
        param_dep("network_crs", state.config.get("network_crs", "EPSG:2056")),
        param_dep("scale_factor", float(state.config["scale_factor"])),
        param_dep("dep_time_jitter_seconds", int(state.config["dep_time_jitter_seconds"])),
        param_dep("coord_jitter_radius_m", float(state.config["coord_jitter_radius_m"])),
        param_dep("demand_scaling_seed", int(state.config["demand_scaling_seed"])),
        param_dep("amenity_kdtree_enabled", bool(state.config["amenity_kdtree_enabled"])),
        param_dep("sumo_vclass_map", state.config["sumo_vclass_map"]),
        param_dep("sumo_match_road_type_enabled", bool(state.config.get("sumo_match_road_type_enabled", True))),
        param_dep("sumo_match_same_type_max_radius_m", state.config.get("sumo_match_same_type_max_radius_m")),
        param_dep("sumo_match_fallback_enabled", bool(state.config.get("sumo_match_fallback_enabled", True))),
        param_dep("sumo_match_fallback_max_radius_m", state.config.get("sumo_match_fallback_max_radius_m")),
        param_dep("sumo_match_route_sequence_enabled", bool(state.config.get("sumo_match_route_sequence_enabled", True))),
        param_dep("sumo_match_route_sequence_max_radius_m", state.config.get("sumo_match_route_sequence_max_radius_m")),
        param_dep("sumo_match_route_sequence_min_dot", float(state.config.get("sumo_match_route_sequence_min_dot", 0.3))),
        param_dep("sumo_match_routeable_endpoints_enabled", bool(state.config.get("sumo_match_routeable_endpoints_enabled", True))),
        param_dep(
            "sumo_match_routeable_candidate_limit",
            int(state.config.get("sumo_match_routeable_candidate_limit", DEFAULT_ROUTEABLE_CANDIDATE_LIMIT)),
        ),
        param_dep(
            "sumo_match_routeable_candidate_extra_m",
            float(state.config.get("sumo_match_routeable_candidate_extra_m", DEFAULT_ROUTEABLE_CANDIDATE_EXTRA_M)),
        ),
        param_dep("max_sumo_endpoint_distance_m", state.config.get("max_sumo_endpoint_distance_m")),
        param_dep("match_sumo_edges_logic_version", MATCH_SUMO_EDGES_LOGIC_VERSION),
    ]
    if state.config["amenity_kdtree_enabled"]:
        deps.append(file_dep(state.config["amenities_path"]))
    return deps


@disk_cache(depends_on=lambda state: _match_sumo_edges_deps(state), step_name="match_sumo_edges")
def _compute_match_sumo_edges_cached(
    state: PipelineState,
) -> tuple[pd.DataFrame, dict[str, dict[str, float | int]], int, dict[str, int]]:
    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)
    edges = _load_sumo_edges(state)
    link_vectors = _build_link_vectors(state)
    valid_edge_ids = set(edges["edge_id"].astype(str))
    out_neighbors, in_neighbors = _edge_neighbors(state, valid_edge_ids)
    start_edge_ids, end_edge_ids = _eligible_edge_ids(state, valid_edge_ids)
    road_type_enabled = bool(state.config.get("sumo_match_road_type_enabled", True))
    fallback_enabled = bool(state.config.get("sumo_match_fallback_enabled", True))
    max_endpoint_distance_m = _config_float(state.config, "max_sumo_endpoint_distance_m", 150.0)
    same_type_max_radius_m = float(
        _config_float(state.config, "sumo_match_same_type_max_radius_m", max_endpoint_distance_m or 150.0)
    )
    fallback_max_radius_m = _config_float(state.config, "sumo_match_fallback_max_radius_m", max_endpoint_distance_m)
    route_sequence_enabled = bool(state.config.get("sumo_match_route_sequence_enabled", True))
    route_sequence_max_radius_m = float(
        _config_float(state.config, "sumo_match_route_sequence_max_radius_m", max_endpoint_distance_m or 150.0)
    )
    route_sequence_min_dot = float(state.config.get("sumo_match_route_sequence_min_dot", 0.3))
    sumo_edge_metadata = _build_sumo_edge_metadata(state, valid_edge_ids)
    edges["road_class"] = edges["edge_id"].astype(str).map(
        {edge_id: meta["road_class"] for edge_id, meta in sumo_edge_metadata.items()}
    )
    edges["sumo_name"] = edges["edge_id"].astype(str).map(
        {edge_id: meta["sumo_name"] for edge_id, meta in sumo_edge_metadata.items()}
    )
    edges["from_node"] = edges["edge_id"].astype(str).map(
        {edge_id: meta["from_node"] for edge_id, meta in sumo_edge_metadata.items()}
    )
    edges["to_node"] = edges["edge_id"].astype(str).map(
        {edge_id: meta["to_node"] for edge_id, meta in sumo_edge_metadata.items()}
    )
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
        out_neighbors=out_neighbors,
        in_neighbors=in_neighbors,
        matsim_road_classes=matsim_road_classes,
        matsim_link_metadata=matsim_link_metadata,
        road_type_enabled=road_type_enabled,
        same_type_max_radius_m=same_type_max_radius_m,
        fallback_enabled=fallback_enabled,
        fallback_max_radius_m=fallback_max_radius_m,
        route_sequence_enabled=route_sequence_enabled,
        route_sequence_max_radius_m=route_sequence_max_radius_m,
        route_sequence_min_dot=route_sequence_min_dot,
        routeable_endpoints_enabled=bool(state.config.get("sumo_match_routeable_endpoints_enabled", True)),
        routeable_candidate_limit=int(
            state.config.get("sumo_match_routeable_candidate_limit", DEFAULT_ROUTEABLE_CANDIDATE_LIMIT)
        ),
        routeable_candidate_extra_m=float(
            state.config.get("sumo_match_routeable_candidate_extra_m", DEFAULT_ROUTEABLE_CANDIDATE_EXTRA_M)
        ),
    )
    diagnostics = _endpoint_distance_diagnostics(matched)

    matched, dropped = _apply_endpoint_distance_guardrail(matched, max_endpoint_distance_m)
    dropped_count = int(len(dropped))
    dropped_by_type = (
        dropped["crossing_type"].fillna("<missing>").astype(str).value_counts().sort_index().to_dict()
        if not dropped.empty and "crossing_type" in dropped.columns
        else {}
    )
    if "dep_time" in matched.columns:
        matched = matched.sort_values("dep_time").reset_index(drop=True)
    else:
        matched = matched.reset_index(drop=True)
    return matched, diagnostics, dropped_count, dropped_by_type


def _log_match_sumo_edges_result(state: PipelineState) -> None:
    print(f"SUMO endpoint distances before guardrail: {_format_distance_diagnostics(state.sumo_endpoint_distance_diagnostics)}")
    max_endpoint_distance_m = _config_float(state.config, "max_sumo_endpoint_distance_m", 150.0)
    guardrails = "distance, local activity fallback, or directed routeability"
    if max_endpoint_distance_m is None:
        print("SUMO endpoint distance guardrail disabled; local activity and directed routeability guardrails active.")
    elif state.sumo_endpoint_guardrail_dropped_count:
        dropped_by_type = ", ".join(
            f"{key}={value}" for key, value in state.sumo_endpoint_guardrail_dropped_by_type.items()
        )
        print(
            f"Dropped {state.sumo_endpoint_guardrail_dropped_count} trips by SUMO match guardrails "
            f"({guardrails}; distance>{max_endpoint_distance_m:.1f}m; {dropped_by_type})."
        )
    else:
        print(f"Dropped 0 trips by SUMO match guardrails ({guardrails}; distance>{max_endpoint_distance_m:.1f}m).")


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    (
        state.scaled_trips_matched,
        state.sumo_endpoint_distance_diagnostics,
        state.sumo_endpoint_guardrail_dropped_count,
        state.sumo_endpoint_guardrail_dropped_by_type,
    ) = _compute_match_sumo_edges_cached(state, no_cache=bool(state.config.get("no_cache", False)))
    _log_match_sumo_edges_result(state)
    if output_needs_refresh(_compute_match_sumo_edges_cached, state.scaled_trips_matched_path):
        CsvWriter.write_df(state.scaled_trips_matched, state.scaled_trips_matched_path)
