import unittest

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from matsim_sumo.pipeline.stages.step_3_map_matching import (
    _apply_endpoint_distance_guardrail,
    _build_corridor_candidates,
    _match_trips,
    _unit_vector_xy,
)


def _edges(rows: list[tuple[str, str, str, str, str, LineString]]) -> gpd.GeoDataFrame:
    gdf = gpd.GeoDataFrame(
        {
            "edge_id": [r[0] for r in rows],
            "road_class": [r[1] for r in rows],
            "sumo_name": [r[2] for r in rows],
            "from_node": [r[3] for r in rows],
            "to_node": [r[4] for r in rows],
        },
        geometry=[r[5] for r in rows],
        crs="EPSG:2056",
    )
    vec = gdf.geometry.map(lambda g: _unit_vector_xy(g.coords[0][0], g.coords[0][1], g.coords[-1][0], g.coords[-1][1]))
    gdf["edge_dx"] = vec.map(lambda v: v[0]).to_numpy(dtype=float)
    gdf["edge_dy"] = vec.map(lambda v: v[1]).to_numpy(dtype=float)
    return gdf


class RouteCorridorStep3Test(unittest.TestCase):
    def test_coarse_matsim_link_maps_to_forward_sumo_split_edges(self) -> None:
        edges = _edges(
            [
                ("548701320", "secondary", "manessestrasse", "n0", "n1", LineString([(0.0, 0.0), (45.0, 0.0)])),
                ("39013712#1", "secondary", "manessestrasse", "n1", "n2", LineString([(45.0, 0.0), (100.0, 0.0)])),
                ("39013713", "secondary", "manessestrasse", "n2", "n1", LineString([(100.0, 2.0), (45.0, 2.0)])),
                ("24043220", "secondary", "manessestrasse", "n1", "n0", LineString([(45.0, 5.0), (0.0, 5.0)])),
                ("284105541", "secondary", "manessestrasse", "far0", "far1", LineString([(0.0, 100.0), (100.0, 100.0)])),
            ]
        )
        route_metadata = {
            "2047316": {
                "geometry": LineString([(0.0, 0.0), (100.0, 0.0)]),
                "vx": 1.0,
                "vy": 0.0,
                "road_class": "secondary",
                "osm_id": None,
                "name_norm": "manessestrasse",
            }
        }

        candidates = _build_corridor_candidates(route_metadata, edges, radius_m=80.0, min_dot=0.3, road_type_enabled=True)
        candidate_ids = {c["edge_id"] for c in candidates["2047316"]}

        self.assertEqual(candidate_ids, {"548701320", "39013712#1"})
        self.assertTrue(all(c["dot"] >= 0.3 for c in candidates["2047316"]))

    def test_boundary_pass_through_emits_junction_endpoints(self) -> None:
        edges = _edges(
            [
                ("548701320", "secondary", "manessestrasse", "cluster_entry", "mid", LineString([(0.0, 0.0), (45.0, 0.0)])),
                ("39013712#1", "secondary", "manessestrasse", "mid", "cluster_exit", LineString([(45.0, 0.0), (100.0, 0.0)])),
            ]
        )
        candidates = _build_corridor_candidates(
            {
                "2047316": {
                    "geometry": LineString([(0.0, 0.0), (100.0, 0.0)]),
                    "vx": 1.0,
                    "vy": 0.0,
                    "road_class": "secondary",
                    "osm_id": None,
                    "name_norm": "manessestrasse",
                }
            },
            edges,
            radius_m=80.0,
            min_dot=0.3,
            road_type_enabled=True,
        )
        trips = pd.DataFrame({"trip_id": ["t1"], "crossing_type": ["pass_through"], "route_links": ["2047316"]})

        matched = _match_trips(
            trips,
            edges,
            candidates,
            start_edge_ids={"548701320", "39013712#1"},
            end_edge_ids={"548701320", "39013712#1"},
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "548701320")
        self.assertEqual(matched.iloc[0]["to_edge"], "39013712#1")
        self.assertEqual(matched.iloc[0]["from_junction"], "cluster_entry")
        self.assertEqual(matched.iloc[0]["to_junction"], "cluster_exit")
        self.assertEqual(matched.iloc[0]["from_match_policy"], "route_corridor")
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_inside_trip_keeps_exact_edge_endpoints(self) -> None:
        edges = _edges(
            [
                ("a", "secondary", "main", "n0", "n1", LineString([(0.0, 0.0), (45.0, 0.0)])),
                ("b", "secondary", "main", "n1", "n2", LineString([(45.0, 0.0), (100.0, 0.0)])),
            ]
        )
        candidates = _build_corridor_candidates(
            {
                "route": {
                    "geometry": LineString([(0.0, 0.0), (100.0, 0.0)]),
                    "vx": 1.0,
                    "vy": 0.0,
                    "road_class": "secondary",
                    "osm_id": None,
                    "name_norm": "main",
                }
            },
            edges,
            radius_m=80.0,
            min_dot=0.3,
            road_type_enabled=True,
        )
        trips = pd.DataFrame({"trip_id": ["t1"], "crossing_type": ["inside"], "route_links": ["route"]})

        matched = _match_trips(trips, edges, candidates, start_edge_ids={"a", "b"}, end_edge_ids={"a", "b"})

        self.assertEqual(matched.iloc[0]["from_edge"], "a")
        self.assertEqual(matched.iloc[0]["to_edge"], "b")
        self.assertTrue(pd.isna(matched.iloc[0]["from_junction"]))
        self.assertTrue(pd.isna(matched.iloc[0]["to_junction"]))
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_unmatched_corridor_is_dropped_not_nearest_fallback_matched(self) -> None:
        edges = _edges([("near", "secondary", "main", "n0", "n1", LineString([(0.0, 0.0), (10.0, 0.0)]))])
        trips = pd.DataFrame({"trip_id": ["t1"], "crossing_type": ["pass_through"], "route_links": ["missing"]})

        matched = _match_trips(trips, edges, candidates_by_link={}, start_edge_ids={"near"}, end_edge_ids={"near"})
        kept, dropped = _apply_endpoint_distance_guardrail(matched)

        self.assertEqual(len(kept), 0)
        self.assertEqual(len(dropped), 1)
        self.assertFalse(bool(dropped.iloc[0]["match_ok"]))
        self.assertTrue(pd.isna(dropped.iloc[0]["from_edge"]))
        self.assertEqual(dropped.iloc[0]["sumo_match_drop_reason"], "no_start_and_end_corridor_edge")

    def test_inside_trip_without_route_uses_nearest_edges(self) -> None:
        edges = _edges(
            [
                ("start", "residential", "local", "a", "b", LineString([(0.0, 0.0), (10.0, 0.0)])),
                ("wrong_type", "primary", "main", "x", "y", LineString([(0.0, 0.5), (10.0, 0.5)])),
                ("wrong_dir", "residential", "local", "b", "a", LineString([(10.0, 1.0), (0.0, 1.0)])),
                ("end", "residential", "local", "c", "d", LineString([(100.0, 0.0), (110.0, 0.0)])),
            ]
        )
        trips = pd.DataFrame(
            {
                "trip_id": ["t1"],
                "crossing_type": ["inside"],
                "route_links": [np.nan],
                "start_x": [1.0],
                "start_y": [1.0],
                "end_x": [101.0],
                "end_y": [1.0],
                "start_link": ["m_start"],
                "end_link": ["m_end"],
            }
        )
        metadata = {
            "m_start": {"vx": 1.0, "vy": 0.0, "road_class": "residential"},
            "m_end": {"vx": 1.0, "vy": 0.0, "road_class": "residential"},
        }

        matched = _match_trips(
            trips,
            edges,
            candidates_by_link={},
            start_edge_ids={"start", "wrong_type", "wrong_dir", "end"},
            end_edge_ids={"start", "wrong_type", "wrong_dir", "end"},
            link_metadata=metadata,
            inside_max_endpoint_distance_m=20.0,
            min_dot=0.3,
            road_type_enabled=True,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "start")
        self.assertEqual(matched.iloc[0]["to_edge"], "end")
        self.assertEqual(matched.iloc[0]["from_match_policy"], "inside_nearest")
        self.assertEqual(matched.iloc[0]["to_match_policy"], "inside_nearest")
        self.assertTrue(pd.isna(matched.iloc[0]["from_junction"]))
        self.assertTrue(pd.isna(matched.iloc[0]["to_junction"]))
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_inside_trip_without_usable_points_is_dropped_with_clear_reason(self) -> None:
        edges = _edges([("near", "secondary", "main", "n0", "n1", LineString([(0.0, 0.0), (10.0, 0.0)]))])
        trips = pd.DataFrame({"trip_id": ["t1"], "crossing_type": ["inside"], "route_links": [np.nan]})

        matched = _match_trips(trips, edges, candidates_by_link={}, start_edge_ids={"near"}, end_edge_ids={"near"})

        self.assertFalse(bool(matched.iloc[0]["match_ok"]))
        self.assertEqual(matched.iloc[0]["sumo_match_drop_reason"], "inside_nearest_unmatched")

    def test_short_pass_through_filter_drops_by_distance_only(self) -> None:
        matched = pd.DataFrame(
            {
                "from_edge": ["a", "b", "c"],
                "to_edge": ["x", "y", "z"],
                "from_match_dist_m": [0.0, 0.0, 0.0],
                "to_match_dist_m": [0.0, 0.0, 0.0],
                "crossing_type": ["pass_through", "pass_through", "entering"],
                "route_link_count": [3, 8, 3],
                "route_length_m": [900.0, 2000.0, 900.0],
            }
        )

        kept, dropped = _apply_endpoint_distance_guardrail(
            matched,
            min_pass_through_route_distance_m=1000.0,
        )

        self.assertEqual(kept["from_edge"].to_list(), ["b", "c"])
        self.assertEqual(dropped.iloc[0]["sumo_match_drop_reason"], "short_pass_through_distance")

    def test_same_junction_pass_through_is_dropped(self) -> None:
        matched = pd.DataFrame(
            {
                "from_edge": ["a", "b"],
                "to_edge": ["x", "y"],
                "from_junction": ["j1", "j2"],
                "to_junction": ["j1", "j3"],
                "from_match_dist_m": [0.0, 0.0],
                "to_match_dist_m": [0.0, 0.0],
                "crossing_type": ["pass_through", "pass_through"],
                "route_length_m": [1000.0, 1000.0],
            }
        )

        kept, dropped = _apply_endpoint_distance_guardrail(matched)

        self.assertEqual(kept["from_edge"].to_list(), ["b"])
        self.assertEqual(dropped.iloc[0]["sumo_match_drop_reason"], "same_boundary_junction_pass_through")


if __name__ == "__main__":
    unittest.main()
