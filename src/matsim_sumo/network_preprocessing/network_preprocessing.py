from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import sumolib
from pyproj import Transformer
from shapely.geometry import LineString

from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep


def _edge_allows_any(edge, vclasses: tuple[str, ...]) -> bool:
    return any(edge.allows(vc) for vc in vclasses)


@disk_cache(
    depends_on=lambda net, network_path, vclasses: [
        file_dep(network_path),
        param_dep("vclasses", tuple(dict.fromkeys(vclasses))),
    ],
    step_name="sumo_network_lv95",
)
def _preprocess_sumo_network_geo_cached(
    net,
    network_path: str | Path,
    vclasses: tuple[str, ...],
) -> gpd.GeoDataFrame:
    import networkx as nx

    to_lv95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True).transform
    vclasses = tuple(dict.fromkeys(vclasses))

    rows = []
    for edge in net.getEdges(withInternal=False):
        if not _edge_allows_any(edge, vclasses):
            continue
        shape = edge.getShape()
        if len(shape) < 2:
            continue
        geom = LineString([to_lv95(*net.convertXY2LonLat(x, y)) for x, y in shape])
        rows.append((edge.getID(), edge, geom))
    if not rows:
        raise ValueError(f"No SUMO edges found for classes {vclasses} in {network_path}")

    keep_ids = {eid for eid, _, _ in rows}
    graph = nx.DiGraph()
    graph.add_nodes_from(keep_ids)
    for eid, edge, _ in rows:
        for nxt in edge.getOutgoing().keys():
            nid = nxt.getID()
            if nid in keep_ids:
                graph.add_edge(eid, nid)
    # Keep the largest weakly connected component (connectivity ignoring direction).
    components = list(nx.weakly_connected_components(graph))
    largest = max(components, key=len)

    # Strong-connectivity alternative (stricter, removes more edge tails):
    # components = list(nx.strongly_connected_components(graph))
    # largest = max(components, key=len)
    
    rows = [(eid, geom) for eid, _, geom in rows if eid in largest]

    geo = gpd.GeoDataFrame(
        {"edge_id": [eid for eid, _ in rows]},
        geometry=[geom for _, geom in rows],
        crs="EPSG:2056",
    )
    return geo


def preprocess_sumo_network_to_lv95(
    network_path: str | Path,
    vclasses: tuple[str, ...],
    out_path: str | Path,
    *,
    no_cache: bool = False,
) -> tuple[object, gpd.GeoDataFrame]:
    net = sumolib.net.readNet(str(network_path), withInternal=False)
    geo = _preprocess_sumo_network_geo_cached(net, network_path, vclasses, no_cache=no_cache)
    out = Path(out_path)
    if output_needs_refresh(_preprocess_sumo_network_geo_cached, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        geo.to_file(out, driver="GeoJSON")
    return net, geo


class SumoNetworkPreprocessor:
    def __init__(self, network_path: str | Path, vclasses: tuple[str, ...]):
        self.network_path = Path(network_path)
        self.vclasses = tuple(dict.fromkeys(vclasses))
        self.network = None
        self.nodes: pd.DataFrame | None = None
        self.links: pd.DataFrame | None = None
        self.graph = None
        self.nx_graph = None
        self.turn_graph = None
        self._node_id_to_idx: dict[str, int] | None = None
        self._node_xy: np.ndarray | None = None

    def load_network(self) -> None:
        if self.network is not None:
            return

        self.network = sumolib.net.readNet(str(self.network_path), withInternal=False)

        nodes = self.network.getNodes()
        node_ids = [n.getID() for n in nodes]
        node_xy = np.asarray([n.getCoord() for n in nodes], dtype=np.float64)
        self._node_id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
        self._node_xy = node_xy
        self.nodes = pd.DataFrame(
            {
                "node_id": node_ids,
                "x": node_xy[:, 0],
                "y": node_xy[:, 1],
            }
        )

        rows = []
        append = rows.append
        for e in self.network.getEdges(withInternal=False):
            if not _edge_allows_any(e, self.vclasses):
                continue
            speed = float(e.getSpeed())
            length = float(e.getLength())
            append(
                {
                    "link_id": e.getID(),
                    "from_node": e.getFromNode().getID(),
                    "to_node": e.getToNode().getID(),
                    "length": length,
                    "travel_time": length / speed if speed > 0 else np.inf,
                    "speed": speed,
                }
            )
        self.links = pd.DataFrame(rows)

    def create_pandana_graph(self):
        import pandana as pdna

        self.load_network()

        assert self.nodes is not None and self.links is not None and self._node_id_to_idx is not None
        node_map = self._node_id_to_idx

        from_nodes = self.links["from_node"].map(node_map).to_numpy(dtype=np.int32)
        to_nodes = self.links["to_node"].map(node_map).to_numpy(dtype=np.int32)
        edge_weights = self.links[["length", "travel_time"]]

        self.graph = pdna.Network(
            self.nodes["x"].to_numpy(),
            self.nodes["y"].to_numpy(),
            from_nodes,
            to_nodes,
            edge_weights=edge_weights,
            twoway=False,
        )
        return self.graph

    def create_nx_graph(self):
        import networkx as nx

        self.load_network()
        assert self.nodes is not None and self.links is not None and self.network is not None

        g = nx.DiGraph()
        g.add_nodes_from(self.nodes.index.to_list())

        node_map = self._node_id_to_idx
        assert node_map is not None

        for row in self.links.itertuples(index=False):
            u = node_map[row.from_node]
            v = node_map[row.to_node]
            g.add_edge(
                u,
                v,
                edge_id=row.link_id,
                length=float(row.length),
                travel_time=float(row.travel_time),
            )
        self.nx_graph = g

        tg = nx.DiGraph()
        mode_edges = [e for e in self.network.getEdges(withInternal=False) if _edge_allows_any(e, self.vclasses)]
        edge_ids = [e.getID() for e in mode_edges]
        tg.add_nodes_from(edge_ids)
        for edge in mode_edges:
            src_id = edge.getID()
            for out_edge in edge.getOutgoing().keys():
                if _edge_allows_any(out_edge, self.vclasses):
                    tg.add_edge(src_id, out_edge.getID())
        self.turn_graph = tg
        return self.nx_graph

    def route_batch_trips_pandana(
        self,
        trips: pd.DataFrame,
        departure_hour: int | None = None,
        congestion: bool = True,
    ) -> pd.DataFrame:
        import networkx as nx

        del departure_hour, congestion

        if self.graph is None:
            self.create_pandana_graph()
        if self.nx_graph is None:
            self.create_nx_graph()

        assert self.graph is not None and self.nx_graph is not None and self._node_xy is not None
        g = self.nx_graph

        ox = pd.to_numeric(trips["origin_x"], errors="coerce").to_numpy(dtype=np.float64)
        oy = pd.to_numeric(trips["origin_y"], errors="coerce").to_numpy(dtype=np.float64)
        dx = pd.to_numeric(trips["destination_x"], errors="coerce").to_numpy(dtype=np.float64)
        dy = pd.to_numeric(trips["destination_y"], errors="coerce").to_numpy(dtype=np.float64)

        origin_idx = self.graph.get_node_ids(ox, oy).astype(np.int64, copy=False)
        dest_idx = self.graph.get_node_ids(dx, dy).astype(np.int64, copy=False)

        node_xy = self._node_xy
        access_euc_distances = np.hypot(ox - node_xy[origin_idx, 0], oy - node_xy[origin_idx, 1])
        egress_euc_distances = np.hypot(dx - node_xy[dest_idx, 0], dy - node_xy[dest_idx, 1])

        tt = self.graph.shortest_path_lengths(origin_idx, dest_idx, imp_name="travel_time")

        od = pd.DataFrame({"o": origin_idx, "d": dest_idx})
        unique_od = od.drop_duplicates(ignore_index=True)

        total_distance_map: dict[tuple[int, int], float] = {}
        from_edge_map: dict[tuple[int, int], str | None] = {}
        to_edge_map: dict[tuple[int, int], str | None] = {}

        for o, d in unique_od.itertuples(index=False):
            key = (int(o), int(d))
            try:
                path = nx.shortest_path(g, source=key[0], target=key[1], weight="travel_time")
            except Exception:
                total_distance_map[key] = np.nan
                from_edge_map[key] = None
                to_edge_map[key] = None
                continue

            if len(path) < 2:
                total_distance_map[key] = 0.0
                from_edge_map[key] = None
                to_edge_map[key] = None
                continue

            dist = 0.0
            first_edge_id = None
            last_edge_id = None
            for u, v in zip(path[:-1], path[1:]):
                data = g[u][v]
                dist += float(data["length"])
                if first_edge_id is None:
                    first_edge_id = data["edge_id"]
                last_edge_id = data["edge_id"]

            total_distance_map[key] = dist
            from_edge_map[key] = first_edge_id
            to_edge_map[key] = last_edge_id

        keys = list(zip(origin_idx.tolist(), dest_idx.tolist()))
        ds = [total_distance_map.get(k, np.nan) for k in keys]
        from_edges = [from_edge_map.get(k) for k in keys]
        to_edges = [to_edge_map.get(k) for k in keys]

        return pd.DataFrame(
            {
                "access_euc_distance": access_euc_distances,
                "egress_euc_distance": egress_euc_distances,
                "total_travel_time": np.asarray(tt),
                "total_distance": np.asarray(ds),
                "from_edge": from_edges,
                "to_edge": to_edges,
            },
            index=trips.index,
        )

    def remove_unconnected_links_with_turns_restrictions(self, df: pd.DataFrame | None = None):
        import networkx as nx

        if self.turn_graph is None:
            self.create_nx_graph()
        assert self.turn_graph is not None and self.links is not None

        if df is None:
            df = self.links.copy()
        else:
            df = df.copy()

        if self.turn_graph.number_of_nodes() == 0:
            return df, 0

        components = list(nx.strongly_connected_components(self.turn_graph))
        largest_scc = max(components, key=len)

        if "modes" in df.columns:
            modes = df["modes"].astype(str)
            car_mask = modes.str.contains(r"\b(car|passenger)\b", regex=True)
        else:
            car_mask = pd.Series(True, index=df.index)

        car_df = df[car_mask]
        kept_car = car_df[car_df["link_id"].astype(str).isin(largest_scc)]
        non_car = df[~car_mask]

        out = pd.concat([kept_car, non_car], ignore_index=True)
        removed_count = int(len(car_df) - len(kept_car))
        return out, removed_count
