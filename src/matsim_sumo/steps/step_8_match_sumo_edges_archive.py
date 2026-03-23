import argparse
import os
import subprocess
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd
import sumolib
from pyproj import CRS, Transformer
from shapely import LineString, STRtree, points

from matsim_sumo.pipeline_state import PipelineState


def build_edge_caches(net, vclasses=("passenger", "truck")):
    edge_by_id = {e.getID(): e for e in net.getEdges(withInternal=False)}
    eligible = {}
    for vc in vclasses:
        base = [
            e
            for e in net.getEdges(withInternal=False)
            if not e.getID().startswith(":") and not e.isSpecial() and e.allows(vc)
        ]
        eligible[vc] = {
            "from": {e.getID() for e in base if any(n.allows(vc) for n in e.getOutgoing().keys())},
            "to": {e.getID() for e in base if any(n.allows(vc) for n in e.getIncoming().keys())},
        }
    return edge_by_id, eligible


def _quantize(arr: np.ndarray, step_m: float) -> np.ndarray:
    if step_m <= 0:
        return arr
    return np.round(arr / step_m) * step_m


def _lv95_to_sumo_xy(net, x_lv95: np.ndarray, y_lv95: np.ndarray) -> np.ndarray:
    net_crs = CRS.from_string(net.getGeoProj().srs)
    lv95_to_net = Transformer.from_crs("EPSG:2056", net_crs, always_xy=True)
    px, py = lv95_to_net.transform(x_lv95, y_lv95)
    ox, oy = net.getLocationOffset()
    return np.column_stack([px + ox, py + oy])


def _build_strtree_caches(edge_by_id: dict, eligible: dict) -> dict:
    caches = {}
    for vc, roles in eligible.items():
        role_cache = {}
        for role in ("from", "to"):
            geoms = []
            ids = []
            for eid in roles[role]:
                edge = edge_by_id.get(eid)
                if edge is None:
                    continue
                shp = edge.getShape()
                if len(shp) < 2:
                    continue
                geoms.append(LineString(shp))
                ids.append(eid)
            role_cache[role] = (STRtree(geoms), np.asarray(ids, dtype=object)) if geoms else None
        caches[vc] = role_cache
    return caches


def _radius_from_dist(dist: np.ndarray, base_r: float, max_r: float) -> np.ndarray:
    out = np.full(dist.shape, base_r, dtype=float)
    m = dist > base_r
    if m.any():
        out[m] = base_r * np.power(2.0, np.ceil(np.log2(dist[m] / base_r)))
    return np.minimum(out, max_r)


def _match_role_strtree(
    x: np.ndarray,
    y: np.ndarray,
    vclasses: np.ndarray,
    tree_caches: dict,
    role: str,
    radius: float,
    max_radius: float,
    cache_round_m: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xq = _quantize(x, cache_round_m)
    yq = _quantize(y, cache_round_m)
    keys = pd.MultiIndex.from_arrays([xq, yq, vclasses])
    codes, uniq = keys.factorize(sort=False)

    ux = np.asarray(uniq.get_level_values(0), dtype=float)
    uy = np.asarray(uniq.get_level_values(1), dtype=float)
    uvc = np.asarray(uniq.get_level_values(2), dtype=object)

    u_edge = np.empty(len(uniq), dtype=object)
    u_dist = np.full(len(uniq), np.nan, dtype=float)
    u_r = np.full(len(uniq), np.nan, dtype=float)
    u_edge[:] = None

    finite = np.isfinite(ux) & np.isfinite(uy)
    for vc, role_map in tree_caches.items():
        tree_entry = role_map.get(role)
        if tree_entry is None:
            continue
        m = finite & (uvc == vc)
        if not m.any():
            continue

        tree, edge_ids = tree_entry
        pos = np.flatnonzero(m)
        pts = points(ux[pos], uy[pos])
        (idx_pts, idx_edges), dist = tree.query_nearest(pts, return_distance=True, all_matches=False)
        hit_pos = pos[idx_pts]
        dist = np.asarray(dist, dtype=float)
        ok = dist <= max_radius
        if not ok.any():
            continue
        p = hit_pos[ok]
        d = dist[ok]
        u_edge[p] = edge_ids[idx_edges[ok]]
        u_dist[p] = d
        u_r[p] = _radius_from_dist(d, float(radius), float(max_radius))

    return u_edge[codes], u_dist[codes], u_r[codes]


def match_trips_fast(
    df,
    net,
    eligible,
    edge_by_id=None,
    vehicle_col="mode",
    vclass_map=None,
    radius=150.0,
    max_radius=2000.0,
    assume_xy=False,
    cache_round_m=1.0,
):
    if vclass_map is None:
        vclass_map = {"car": "passenger", "truck": "truck"}
    if edge_by_id is None:
        edge_by_id = {e.getID(): e for e in net.getEdges(withInternal=False)}

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

    vc = out[vehicle_col].astype(str).map(vclass_map).to_numpy()
    trees = _build_strtree_caches(edge_by_id, eligible)

    fe, fd, fr = _match_role_strtree(
        sxy[:, 0], sxy[:, 1], vc, trees, "from", float(radius), float(max_radius), float(cache_round_m)
    )
    te, td, tr = _match_role_strtree(
        exy[:, 0], exy[:, 1], vc, trees, "to", float(radius), float(max_radius), float(cache_round_m)
    )
    from_node = {eid: e.getFromNode().getID() for eid, e in edge_by_id.items()}
    to_node = {eid: e.getToNode().getID() for eid, e in edge_by_id.items()}

    out["from_edge"] = fe
    out["from_edge_dist_m"] = fd
    out["from_search_radius_m"] = fr
    out["from_edge_from_node"] = pd.Series(fe, index=out.index).map(from_node)
    out["from_edge_to_node"] = pd.Series(fe, index=out.index).map(to_node)
    out["to_edge"] = te
    out["to_edge_dist_m"] = td
    out["to_search_radius_m"] = tr
    out["to_edge_from_node"] = pd.Series(te, index=out.index).map(from_node)
    out["to_edge_to_node"] = pd.Series(te, index=out.index).map(to_node)
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    return out


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    out = Path(state.scaled_trips_matched_path)
    if not out.exists():
        script = Path(state.match_sumo_edges_script_path)
        if not script.exists():
            raise FileNotFoundError(f"SUMO edge matching script not found: {script}")
        env = os.environ.copy()
        env["CHUNK_ROWS"] = str(int(state.config.get("sumo_match_chunksize", 200_000)))
        env["RADIUS_M"] = str(float(state.config.get("sumo_match_radius_m", 150.0)))
        env["MAX_RADIUS_M"] = str(float(state.config.get("sumo_match_max_radius_m", 2000.0)))
        env["CACHE_ROUND_M"] = str(float(state.config.get("sumo_match_cache_round_m", 1.0)))
        env["ASSUME_XY"] = "1" if bool(state.config.get("sumo_match_assume_xy", False)) else "0"
        subprocess.run(
            ["bash", str(script), str(state.scaled_trips_path), str(out), str(state.sumo_network_path)],
            check=True,
            env=env,
        )
    state.scaled_trips_matched = pd.read_csv(out, sep=";", low_memory=False)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Match scaled trips to nearest valid SUMO edges.")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--sumo-net", required=True)
    p.add_argument("--sep", default=";")
    p.add_argument("--chunksize", type=int, default=200_000)
    p.add_argument("--vehicle-col", default="mode")
    p.add_argument("--radius", type=float, default=150.0)
    p.add_argument("--max-radius", type=float, default=2000.0)
    p.add_argument("--cache-round-m", type=float, default=1.0)
    p.add_argument("--assume-xy", action="store_true")
    return p.parse_args()


def _run_file_match(args: argparse.Namespace) -> None:
    in_path = Path(args.input)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = perf_counter()
    net = sumolib.net.readNet(str(args.sumo_net), withInternal=False)
    edge_by_id, eligible = build_edge_caches(net)
    vclass_map = {"car": "passenger", "truck": "truck"}

    first = True
    rows_total = 0
    ok_total = 0
    reader = pd.read_csv(in_path, sep=args.sep, low_memory=False, chunksize=max(1, args.chunksize))
    for i, chunk in enumerate(reader, start=1):
        out = match_trips_fast(
            chunk,
            net,
            eligible,
            edge_by_id=edge_by_id,
            vehicle_col=args.vehicle_col,
            vclass_map=vclass_map,
            radius=args.radius,
            max_radius=args.max_radius,
            assume_xy=args.assume_xy,
            cache_round_m=args.cache_round_m,
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
