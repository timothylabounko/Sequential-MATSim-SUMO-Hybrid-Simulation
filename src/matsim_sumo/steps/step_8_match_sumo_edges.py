import argparse
from pathlib import Path
from time import perf_counter

import geopandas as gpd
import matsim
import numpy as np
import pandas as pd
import sumolib
from pyproj import CRS, Transformer
from shapely import LineString, points

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState


def _line_angle_deg(line: LineString) -> float:
    (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
    return float(np.degrees(np.arctan2(y1 - y0, x1 - x0)) % 360.0)


def _angle_diff_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(((a - b + 180.0) % 360.0) - 180.0)


def _lv95_to_sumo_xy(net, x_lv95: np.ndarray, y_lv95: np.ndarray) -> np.ndarray:
    net_crs = CRS.from_string(net.getGeoProj().srs)
    lv95_to_net = Transformer.from_crs("EPSG:2056", net_crs, always_xy=True)
    px, py = lv95_to_net.transform(x_lv95, y_lv95)
    ox, oy = net.getLocationOffset()
    return np.column_stack([px + ox, py + oy])


def _matsim_angles_from_geo(network_geo: pd.DataFrame) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in network_geo.loc[:, ["link_id", "geometry"]].itertuples(index=False):
        coords = row.geometry.coords
        if len(coords) < 2:
            continue
        (x0, y0), (x1, y1) = coords[0], coords[-1]
        out[str(row.link_id)] = float(np.degrees(np.arctan2(y1 - y0, x1 - x0)) % 360.0)
    return out


def _pack_edge_index(ids: list[str], angles: list[float], geoms: list[LineString]) -> dict[str, object]:
    geo = gpd.GeoSeries(geoms, dtype="geometry")
    return {
        "edge_ids": np.asarray(ids, dtype=object),
        "angles": np.asarray(angles, dtype=float),
        "geoms": geo,
        "sindex": geo.sindex,
    }


def build_edge_indexes(net, vclasses: tuple[str, ...]) -> dict[str, dict[str, dict[str, object]]]:
    out: dict[str, dict[str, dict[str, object]]] = {}
    edges = list(net.getEdges(withInternal=False))

    for vc in vclasses:
        ids_from: list[str] = []
        angles_from: list[float] = []
        geoms_from: list[LineString] = []
        ids_to: list[str] = []
        angles_to: list[float] = []
        geoms_to: list[LineString] = []

        for edge in edges:
            if edge.getID().startswith(":") or edge.isSpecial() or not edge.allows(vc):
                continue
            shp = edge.getShape()
            if len(shp) < 2:
                continue

            line = LineString(shp)
            edge_id = edge.getID()
            angle = _line_angle_deg(line)
            if any(n.allows(vc) for n in edge.getOutgoing().keys()):
                ids_from.append(edge_id)
                angles_from.append(angle)
                geoms_from.append(line)
            if any(n.allows(vc) for n in edge.getIncoming().keys()):
                ids_to.append(edge_id)
                angles_to.append(angle)
                geoms_to.append(line)

        out[vc] = {
            "from": _pack_edge_index(ids_from, angles_from, geoms_from),
            "to": _pack_edge_index(ids_to, angles_to, geoms_to),
        }
    return out


def _top2_nearest_edges(
    pts: gpd.GeoSeries,
    edge_index: dict[str, object],
    max_radius: float,
    same_dist_rel: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n = len(pts)
    e1 = np.empty(n, dtype=object)
    e1[:] = None
    d1 = np.full(n, np.nan, dtype=float)
    a1 = np.full(n, np.nan, dtype=float)
    e2 = np.empty(n, dtype=object)
    e2[:] = None
    d2 = np.full(n, np.nan, dtype=float)
    a2 = np.full(n, np.nan, dtype=float)

    edge_ids = edge_index["edge_ids"]
    if n == 0 or edge_ids.size == 0:
        return e1, d1, a1, e2, d2, a2

    edge_angles = edge_index["angles"]
    geoms = edge_index["geoms"]
    sidx = edge_index["sindex"]

    nearest, nearest_dist = sidx.nearest(pts, return_all=False, max_distance=max_radius, return_distance=True)
    if nearest.size == 0:
        return e1, d1, a1, e2, d2, a2

    li = np.asarray(nearest[0], dtype=int)
    ri = np.asarray(nearest[1], dtype=int)
    nearest_dist = np.asarray(nearest_dist, dtype=float)

    e1[li] = edge_ids[ri]
    d1[li] = nearest_dist
    a1[li] = edge_angles[ri]

    qdist = np.minimum(nearest_dist * (1.0 + float(same_dist_rel)) + 1e-6, float(max_radius))
    second_hits = sidx.query(pts.iloc[li], predicate="dwithin", distance=qdist)
    if second_hits.size == 0:
        return e1, d1, a1, e2, d2, a2

    l2 = li[np.asarray(second_hits[0], dtype=int)]
    r2 = np.asarray(second_hits[1], dtype=int)
    d_second = pts.iloc[l2].distance(geoms.iloc[r2], align=False).to_numpy(dtype=float)

    nearest_edge_index = np.full(n, -1, dtype=int)
    nearest_edge_index[li] = ri
    keep = r2 != nearest_edge_index[l2]
    if not keep.any():
        return e1, d1, a1, e2, d2, a2

    cand_pt = l2[keep]
    cand_edge = r2[keep]
    cand_dist = d_second[keep]
    order = np.lexsort((cand_dist, cand_pt))
    cand_pt = cand_pt[order]
    cand_edge = cand_edge[order]
    cand_dist = cand_dist[order]
    first = np.ones(cand_pt.shape[0], dtype=bool)
    first[1:] = cand_pt[1:] != cand_pt[:-1]

    p = cand_pt[first]
    e = cand_edge[first]
    e2[p] = edge_ids[e]
    d2[p] = cand_dist[first]
    a2[p] = edge_angles[e]
    return e1, d1, a1, e2, d2, a2


def _match_role(
    x: np.ndarray,
    y: np.ndarray,
    vclasses: np.ndarray,
    matsim_angles: np.ndarray,
    edge_indexes: dict[str, dict[str, dict[str, object]]],
    role: str,
    max_radius: float,
    same_dist_rel: float,
    angle_margin_deg: float,
) -> tuple[np.ndarray, np.ndarray]:
    n = len(x)
    out_edge = np.empty(n, dtype=object)
    out_edge[:] = None
    out_dist = np.full(n, np.nan, dtype=float)

    finite = np.isfinite(x) & np.isfinite(y)
    for vc, role_map in edge_indexes.items():
        edge_index = role_map[role]
        if edge_index["edge_ids"].size == 0:
            continue

        m = finite & (vclasses == vc)
        if not m.any():
            continue

        idx = np.flatnonzero(m)
        pts = gpd.GeoSeries(points(x[idx], y[idx]))
        e1, d1, a1, e2, d2, a2 = _top2_nearest_edges(
            pts,
            edge_index=edge_index,
            max_radius=max_radius,
            same_dist_rel=same_dist_rel,
        )

        chosen_edge = e1.copy()
        chosen_dist = d1.copy()

        tie = np.isfinite(d1) & np.isfinite(d2) & (d2 <= d1 * (1.0 + float(same_dist_rel)) + 1e-9)
        mat = matsim_angles[idx]
        with_angle = tie & np.isfinite(mat) & np.isfinite(a1) & np.isfinite(a2)
        if with_angle.any():
            k = np.flatnonzero(with_angle)
            diff1 = _angle_diff_deg(a1[k], mat[k])
            diff2 = _angle_diff_deg(a2[k], mat[k])

            in1 = diff1 <= float(angle_margin_deg)
            in2 = diff2 <= float(angle_margin_deg)
            pick2 = (in2 & ~in1) | (in1 & in2 & (diff2 < diff1))
            if pick2.any():
                c = k[pick2]
                chosen_edge[c] = e2[c]
                chosen_dist[c] = d2[c]

        out_edge[idx] = chosen_edge
        out_dist[idx] = chosen_dist

    return out_edge, out_dist


def match_trips_direction_aware(
    df: pd.DataFrame,
    net,
    edge_indexes: dict[str, dict[str, dict[str, object]]],
    matsim_angles: dict[str, float],
    vehicle_col: str,
    vclass_map: dict[str, str],
    max_radius: float,
    assume_xy: bool,
    same_dist_rel: float,
    angle_margin_deg: float,
) -> pd.DataFrame:
    out = df.copy()
    sx = pd.to_numeric(out["start_x"], errors="coerce").to_numpy(dtype=float)
    sy = pd.to_numeric(out["start_y"], errors="coerce").to_numpy(dtype=float)
    ex = pd.to_numeric(out["end_x"], errors="coerce").to_numpy(dtype=float)
    ey = pd.to_numeric(out["end_y"], errors="coerce").to_numpy(dtype=float)

    if assume_xy:
        sxy = np.column_stack([sx, sy])
        exy = np.column_stack([ex, ey])
    else:
        sxy = _lv95_to_sumo_xy(net, sx, sy)
        exy = _lv95_to_sumo_xy(net, ex, ey)

    vc = out[vehicle_col].astype(str).map(vclass_map).to_numpy(dtype=object)
    start_angles = pd.to_numeric(out["start_link"].astype(str).map(matsim_angles), errors="coerce").to_numpy(dtype=float)
    end_angles = pd.to_numeric(out["end_link"].astype(str).map(matsim_angles), errors="coerce").to_numpy(dtype=float)

    fe, fd = _match_role(
        x=sxy[:, 0],
        y=sxy[:, 1],
        vclasses=vc,
        matsim_angles=start_angles,
        edge_indexes=edge_indexes,
        role="from",
        max_radius=max_radius,
        same_dist_rel=same_dist_rel,
        angle_margin_deg=angle_margin_deg,
    )
    te, td = _match_role(
        x=exy[:, 0],
        y=exy[:, 1],
        vclasses=vc,
        matsim_angles=end_angles,
        edge_indexes=edge_indexes,
        role="to",
        max_radius=max_radius,
        same_dist_rel=same_dist_rel,
        angle_margin_deg=angle_margin_deg,
    )

    out["from_edge"] = fe
    out["from_edge_dist_m"] = fd
    out["to_edge"] = te
    out["to_edge_dist_m"] = td
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    return out


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    out = Path(state.scaled_trips_matched_path)
    if out.exists():
        state.scaled_trips_matched = pd.read_csv(out, sep=";", low_memory=False)
        return

    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)
    if state.network_geo is None:
        raise ValueError("network_geo must be available from step 1 before step 8.")

    if state.matsim_link_angles is None:
        state.matsim_link_angles = _matsim_angles_from_geo(state.network_geo)

    if state.sumo_net is None:
        state.sumo_net = sumolib.net.readNet(str(state.sumo_network_path), withInternal=False)

    vclass_map = state.config["sumo_vclass_map"]
    if state.sumo_edge_indexes is None:
        vclasses = tuple(dict.fromkeys(vclass_map.values()))
        state.sumo_edge_indexes = build_edge_indexes(state.sumo_net, vclasses=vclasses)

    state.scaled_trips_matched = match_trips_direction_aware(
        state.scaled_trips,
        state.sumo_net,
        edge_indexes=state.sumo_edge_indexes,
        matsim_angles=state.matsim_link_angles,
        vehicle_col=state.config["sumo_match_vehicle_col"],
        vclass_map=vclass_map,
        max_radius=float(state.config.get("sumo_match_max_radius_m", 500.0)),
        assume_xy=bool(state.config.get("sumo_match_assume_xy", False)),
        same_dist_rel=float(state.config.get("sumo_match_same_dist_rel", 0.05)),
        angle_margin_deg=float(state.config.get("sumo_match_angle_margin_deg", 10.0)),
    )
    CsvWriter.write_df(state.scaled_trips_matched, out)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Match scaled trips to SUMO edges (direction-aware tie-break).")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--sumo-net", required=True)
    p.add_argument("--matsim-net", required=True)
    p.add_argument("--sep", default=";")
    p.add_argument("--chunksize", type=int, default=200_000)
    p.add_argument("--vehicle-col", default="mode")
    p.add_argument("--max-radius", type=float, default=500.0)
    p.add_argument("--same-dist-rel", type=float, default=0.05)
    p.add_argument("--angle-margin-deg", type=float, default=10.0)
    p.add_argument("--assume-xy", action="store_true")
    return p.parse_args()


def _run_file_match(args: argparse.Namespace) -> None:
    in_path = Path(args.input)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = perf_counter()
    sumo_net = sumolib.net.readNet(str(args.sumo_net), withInternal=False)
    vclass_map = {"car": "passenger", "truck": "truck"}
    edge_indexes = build_edge_indexes(sumo_net, vclasses=tuple(dict.fromkeys(vclass_map.values())))

    matsim_net = matsim.read_network(str(args.matsim_net))
    matsim_angles = _matsim_angles_from_geo(matsim_net.as_geo(projection="EPSG:2056"))

    first = True
    rows_total = 0
    ok_total = 0
    reader = pd.read_csv(in_path, sep=args.sep, low_memory=False, chunksize=max(1, args.chunksize))
    for i, chunk in enumerate(reader, start=1):
        out = match_trips_direction_aware(
            chunk,
            sumo_net,
            edge_indexes=edge_indexes,
            matsim_angles=matsim_angles,
            vehicle_col=args.vehicle_col,
            vclass_map=vclass_map,
            max_radius=args.max_radius,
            assume_xy=args.assume_xy,
            same_dist_rel=args.same_dist_rel,
            angle_margin_deg=args.angle_margin_deg,
        )
        out.to_csv(
            out_path,
            sep=args.sep,
            index=False,
            mode="w" if first else "a",
            header=first,
            compression="infer",
        )
        first = False
        rows_total += len(out)
        ok_total += int(out["match_ok"].sum())
        print(f"chunk={i} rows={rows_total} match_ok={ok_total}")

    print(f"done rows={rows_total} match_ok={ok_total} time_s={perf_counter() - t0:.2f}")


def main() -> None:
    _run_file_match(_parse_args())


if __name__ == "__main__":
    main()
