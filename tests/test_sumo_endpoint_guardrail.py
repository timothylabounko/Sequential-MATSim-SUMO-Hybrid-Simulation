import unittest

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from matsim_sumo.pipeline.stages.step_3_map_matching import _apply_endpoint_distance_guardrail, _match_trips


class SumoEndpointGuardrailTest(unittest.TestCase):
    def test_guardrail_keeps_only_matched_rows_with_both_endpoint_distances_under_limit(self) -> None:
        matched = pd.DataFrame(
            {
                "from_edge": ["a", "b", "c", None],
                "to_edge": ["x", "y", "z", "w"],
                "from_edge_dist_m": [50.0, 151.0, 10.0, 20.0],
                "to_edge_dist_m": [149.0, 10.0, None, 10.0],
                "crossing_type": ["inside", "entering", "leaving", "pass_through"],
            }
        )

        kept, dropped = _apply_endpoint_distance_guardrail(matched, 150.0)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept.iloc[0]["from_edge"], "a")
        self.assertEqual(kept.iloc[0]["sumo_endpoint_max_dist_m"], 149.0)
        self.assertEqual(len(dropped), 3)

    def test_guardrail_drops_local_activity_endpoint_fallbacks_only(self) -> None:
        matched = pd.DataFrame(
            {
                "from_edge": ["a", "b", "c"],
                "to_edge": ["x", "y", "z"],
                "from_edge_dist_m": [10.0, 10.0, 10.0],
                "to_edge_dist_m": [10.0, 10.0, 10.0],
                "crossing_type": ["leaving", "pass_through", "entering"],
                "from_matsim_road_class": ["residential", "residential", "secondary"],
                "to_matsim_road_class": ["secondary", "residential", "residential"],
                "from_match_policy": ["fallback_any", "fallback_any", "same_type"],
                "to_match_policy": ["same_type", "fallback_any", "fallback_any"],
            }
        )

        kept, dropped = _apply_endpoint_distance_guardrail(matched, 150.0)

        self.assertEqual(kept["from_edge"].to_list(), ["b"])
        self.assertEqual(dropped["from_edge"].to_list(), ["a", "c"])
        self.assertTrue(dropped["sumo_local_activity_endpoint_fallback"].all())

    def test_route_sequence_can_override_pruned_boundary_start_edge(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [0.0],
                "start_y": [0.0],
                "end_x": [10.0],
                "end_y": [0.0],
                "crossing_type": ["pass_through"],
                "route_links": ["252309 2292149 2087056 2087041"],
                "new_start_link": ["252309"],
                "new_end_link": ["2087041"],
            }
        )
        edges = gpd.GeoDataFrame(
            {
                "edge_id": ["135081939", "15091874", "536676260", "536676266#3"],
                "road_class": ["motorway", "motorway", "motorway", "secondary"],
                "sumo_name": ["", "", "", ""],
                "from_node": ["567351494", "567351494", "256030294", "28499471"],
                "to_node": ["other", "256030294", "28499471", "exit"],
            },
            geometry=[
                LineString([(0.0, 0.0), (1.0, 0.0)]),
                LineString([(0.0, 1.0), (2.0, 1.0)]),
                LineString([(2.0, 1.0), (8.0, 1.0)]),
                LineString([(10.0, 0.0), (11.0, 0.0)]),
            ],
            crs="EPSG:2056",
        )
        metadata = {
            "252309": {"road_class": "motorway", "osm_id": None, "name_norm": None, "from_node": "entry", "to_node": "567351494", "vx": 1.0, "vy": 0.0},
            "2292149": {"road_class": "motorway", "osm_id": None, "name_norm": None, "from_node": "567351494", "to_node": "256030294", "vx": 1.0, "vy": 0.0},
            "2087041": {"road_class": "secondary", "osm_id": None, "name_norm": None, "from_node": "28499471", "to_node": "exit", "vx": 1.0, "vy": 0.0},
        }

        matched = _match_trips(
            trips,
            edges=edges,
            link_vectors={"252309": (1.0, 0.0), "2087041": (1.0, 0.0)},
            start_edge_ids={"135081939"},
            end_edge_ids={"536676266#3"},
            out_neighbors={
                "135081939": set(),
                "15091874": {"536676260"},
                "536676260": {"536676266#3"},
                "536676266#3": set(),
            },
            in_neighbors={
                "135081939": set(),
                "15091874": set(),
                "536676260": {"15091874"},
                "536676266#3": {"536676260"},
            },
            matsim_road_classes={"252309": "motorway", "2087041": "secondary"},
            matsim_link_metadata=metadata,
            road_type_enabled=True,
            same_type_max_radius_m=150.0,
            fallback_enabled=True,
            fallback_max_radius_m=150.0,
            route_sequence_enabled=True,
            route_sequence_max_radius_m=150.0,
            route_sequence_min_dot=0.3,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "15091874")
        self.assertEqual(matched.iloc[0]["from_match_policy"], "route_sequence")
        self.assertEqual(matched.iloc[0]["from_route_match_link"], "2292149")
        self.assertEqual(matched.iloc[0]["to_edge"], "536676266#3")

    def test_entering_trip_matches_destination_only_from_reachable_edges(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [0.0],
                "start_y": [0.0],
                "end_x": [10.0],
                "end_y": [0.0],
                "crossing_type": ["entering"],
                "route_links": [""],
                "new_start_link": ["start_link"],
                "new_end_link": ["end_link"],
            }
        )
        edges = gpd.GeoDataFrame(
            {
                "edge_id": ["start", "bad_end", "good_end"],
                "road_class": ["residential", "residential", "residential"],
                "sumo_name": ["", "", ""],
                "from_node": ["a", "x", "b"],
                "to_node": ["b", "y", "c"],
            },
            geometry=[
                LineString([(0.0, 0.0), (1.0, 0.0)]),
                LineString([(10.0, 0.0), (11.0, 0.0)]),
                LineString([(12.0, 0.0), (13.0, 0.0)]),
            ],
            crs="EPSG:2056",
        )

        matched = _match_trips(
            trips,
            edges=edges,
            link_vectors={"start_link": (1.0, 0.0), "end_link": (1.0, 0.0)},
            start_edge_ids={"start"},
            end_edge_ids={"bad_end", "good_end"},
            out_neighbors={"start": {"good_end"}, "bad_end": set(), "good_end": set()},
            in_neighbors={"start": set(), "bad_end": set(), "good_end": {"start"}},
            matsim_road_classes={"start_link": "residential", "end_link": "residential"},
            matsim_link_metadata={},
            road_type_enabled=True,
            same_type_max_radius_m=150.0,
            fallback_enabled=True,
            fallback_max_radius_m=150.0,
            route_sequence_enabled=False,
            route_sequence_max_radius_m=150.0,
            route_sequence_min_dot=0.3,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "start")
        self.assertEqual(matched.iloc[0]["to_edge"], "good_end")
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_routeable_endpoint_match_can_be_disabled(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [0.0],
                "start_y": [0.0],
                "end_x": [10.0],
                "end_y": [0.0],
                "crossing_type": ["entering"],
                "route_links": [""],
                "new_start_link": ["start_link"],
                "new_end_link": ["end_link"],
            }
        )
        edges = gpd.GeoDataFrame(
            {
                "edge_id": ["start", "bad_end", "good_end"],
                "road_class": ["residential", "residential", "residential"],
                "sumo_name": ["", "", ""],
                "from_node": ["a", "x", "b"],
                "to_node": ["b", "y", "c"],
            },
            geometry=[
                LineString([(0.0, 0.0), (1.0, 0.0)]),
                LineString([(10.0, 0.0), (11.0, 0.0)]),
                LineString([(12.0, 0.0), (13.0, 0.0)]),
            ],
            crs="EPSG:2056",
        )

        matched = _match_trips(
            trips,
            edges=edges,
            link_vectors={"start_link": (1.0, 0.0), "end_link": (1.0, 0.0)},
            start_edge_ids={"start"},
            end_edge_ids={"bad_end", "good_end"},
            out_neighbors={"start": {"good_end"}, "bad_end": set(), "good_end": set()},
            in_neighbors={"start": set(), "bad_end": set(), "good_end": {"start"}},
            matsim_road_classes={"start_link": "residential", "end_link": "residential"},
            matsim_link_metadata={},
            road_type_enabled=True,
            same_type_max_radius_m=150.0,
            fallback_enabled=True,
            fallback_max_radius_m=150.0,
            route_sequence_enabled=False,
            route_sequence_max_radius_m=150.0,
            route_sequence_min_dot=0.3,
            routeable_endpoints_enabled=False,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "start")
        self.assertEqual(matched.iloc[0]["to_edge"], "bad_end")
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_leaving_trip_matches_start_only_from_edges_reaching_destination(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [0.0],
                "start_y": [0.0],
                "end_x": [10.0],
                "end_y": [0.0],
                "crossing_type": ["leaving"],
                "route_links": [""],
                "new_start_link": ["start_link"],
                "new_end_link": ["end_link"],
            }
        )
        edges = gpd.GeoDataFrame(
            {
                "edge_id": ["bad_start", "good_start", "end"],
                "road_class": ["residential", "residential", "residential"],
                "sumo_name": ["", "", ""],
                "from_node": ["x", "a", "b"],
                "to_node": ["y", "b", "c"],
            },
            geometry=[
                LineString([(0.0, 0.0), (1.0, 0.0)]),
                LineString([(2.0, 0.0), (3.0, 0.0)]),
                LineString([(10.0, 0.0), (11.0, 0.0)]),
            ],
            crs="EPSG:2056",
        )

        matched = _match_trips(
            trips,
            edges=edges,
            link_vectors={"start_link": (1.0, 0.0), "end_link": (1.0, 0.0)},
            start_edge_ids={"bad_start", "good_start"},
            end_edge_ids={"end"},
            out_neighbors={"bad_start": set(), "good_start": {"end"}, "end": set()},
            in_neighbors={"bad_start": set(), "good_start": set(), "end": {"good_start"}},
            matsim_road_classes={"start_link": "residential", "end_link": "residential"},
            matsim_link_metadata={},
            road_type_enabled=True,
            same_type_max_radius_m=150.0,
            fallback_enabled=True,
            fallback_max_radius_m=150.0,
            route_sequence_enabled=False,
            route_sequence_max_radius_m=150.0,
            route_sequence_min_dot=0.3,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "good_start")
        self.assertEqual(matched.iloc[0]["to_edge"], "end")
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_inside_trip_matches_destination_only_from_reachable_edges(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [0.0],
                "start_y": [0.0],
                "end_x": [10.0],
                "end_y": [0.0],
                "crossing_type": ["inside"],
                "route_links": [""],
                "new_start_link": ["start_link"],
                "new_end_link": ["end_link"],
            }
        )
        edges = gpd.GeoDataFrame(
            {
                "edge_id": ["start", "bad_end", "good_end"],
                "road_class": ["residential", "residential", "residential"],
                "sumo_name": ["", "", ""],
                "from_node": ["a", "x", "b"],
                "to_node": ["b", "y", "c"],
            },
            geometry=[
                LineString([(0.0, 0.0), (1.0, 0.0)]),
                LineString([(10.0, 0.0), (11.0, 0.0)]),
                LineString([(12.0, 0.0), (13.0, 0.0)]),
            ],
            crs="EPSG:2056",
        )

        matched = _match_trips(
            trips,
            edges=edges,
            link_vectors={"start_link": (1.0, 0.0), "end_link": (1.0, 0.0)},
            start_edge_ids={"start"},
            end_edge_ids={"bad_end", "good_end"},
            out_neighbors={"start": {"good_end"}, "bad_end": set(), "good_end": set()},
            in_neighbors={"start": set(), "bad_end": set(), "good_end": {"start"}},
            matsim_road_classes={"start_link": "residential", "end_link": "residential"},
            matsim_link_metadata={},
            road_type_enabled=True,
            same_type_max_radius_m=150.0,
            fallback_enabled=True,
            fallback_max_radius_m=150.0,
            route_sequence_enabled=False,
            route_sequence_max_radius_m=150.0,
            route_sequence_min_dot=0.3,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "start")
        self.assertEqual(matched.iloc[0]["to_edge"], "good_end")
        self.assertTrue(bool(matched.iloc[0]["match_ok"]))

    def test_routeable_endpoint_match_leaves_trip_unmatched_when_no_candidate_exists(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [0.0],
                "start_y": [0.0],
                "end_x": [10.0],
                "end_y": [0.0],
                "crossing_type": ["entering"],
                "route_links": [""],
                "new_start_link": ["start_link"],
                "new_end_link": ["end_link"],
            }
        )
        edges = gpd.GeoDataFrame(
            {
                "edge_id": ["start", "bad_end"],
                "road_class": ["residential", "residential"],
                "sumo_name": ["", ""],
                "from_node": ["a", "x"],
                "to_node": ["b", "y"],
            },
            geometry=[
                LineString([(0.0, 0.0), (1.0, 0.0)]),
                LineString([(10.0, 0.0), (11.0, 0.0)]),
            ],
            crs="EPSG:2056",
        )

        matched = _match_trips(
            trips,
            edges=edges,
            link_vectors={"start_link": (1.0, 0.0), "end_link": (1.0, 0.0)},
            start_edge_ids={"start"},
            end_edge_ids={"bad_end"},
            out_neighbors={"start": set(), "bad_end": set()},
            in_neighbors={"start": set(), "bad_end": set()},
            matsim_road_classes={"start_link": "residential", "end_link": "residential"},
            matsim_link_metadata={},
            road_type_enabled=True,
            same_type_max_radius_m=150.0,
            fallback_enabled=True,
            fallback_max_radius_m=150.0,
            route_sequence_enabled=False,
            route_sequence_max_radius_m=150.0,
            route_sequence_min_dot=0.3,
        )

        self.assertEqual(matched.iloc[0]["from_edge"], "start")
        self.assertTrue(pd.isna(matched.iloc[0]["to_edge"]))
        self.assertFalse(bool(matched.iloc[0]["match_ok"]))


if __name__ == "__main__":
    unittest.main()
