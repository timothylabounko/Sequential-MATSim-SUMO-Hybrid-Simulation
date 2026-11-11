import unittest

from matsim_sumo.network.tram_access import (
    classify_way,
    decide_lane,
    exclusive_tram_crossing,
    orig_way_ids,
    rewrite_lane_attributes,
)


class TramAccessRulesTest(unittest.TestCase):
    def test_orig_ids_ignore_lane_suffixes(self) -> None:
        self.assertEqual(orig_way_ids("4440330 1458517639#3"), ["4440330", "1458517639"])
        self.assertEqual(orig_way_ids(""), [])

    def test_reserved_track_is_not_a_car_lane(self) -> None:
        self.assertEqual(classify_way({"railway": "tram", "oneway": "yes"}), "exclusive_tram")
        self.assertEqual(
            decide_lane(["exclusive_tram"], allows_passenger=True, allows_tram=True),
            "remove_cars",
        )
        self.assertEqual(
            decide_lane(["exclusive_tram"], allows_passenger=False, allows_tram=True),
            "keep",
        )

    def test_embedded_street_keeps_cars_and_can_share_the_lane(self) -> None:
        tags = {"highway": "tertiary", "embedded_rails": "tram", "name": "Stauffacherstrasse"}
        self.assertEqual(classify_way(tags), "shared_road")
        self.assertEqual(
            decide_lane(["shared_road"], allows_passenger=True, allows_tram=False),
            "keep",
        )
        self.assertEqual(
            decide_lane(["shared_road", "exclusive_tram"], allows_passenger=True, allows_tram=True),
            "keep",
        )
        self.assertEqual(
            decide_lane(["shared_road", "exclusive_tram"], allows_passenger=True, allows_tram=False),
            "add_tram",
        )
        self.assertEqual(
            decide_lane(["shared_road", "exclusive_tram"], allows_passenger=False, allows_tram=True),
            "add_cars",
        )

    def test_unmarked_join_is_not_edited_when_it_is_the_only_lane(self) -> None:
        self.assertEqual(
            decide_lane(
                ["car_road", "exclusive_tram"],
                allows_passenger=True,
                allows_tram=True,
                other_lane_keeps_cars=False,
            ),
            "suspect",
        )
        self.assertEqual(
            decide_lane(
                ["car_road", "exclusive_tram"],
                allows_passenger=True,
                allows_tram=True,
                other_lane_keeps_cars=True,
            ),
            "remove_cars",
        )

    def test_restricted_access_is_left_unchanged(self) -> None:
        self.assertEqual(
            classify_way({"highway": "residential", "embedded_rails": "tram", "motor_vehicle": "destination"}),
            "restricted",
        )
        self.assertEqual(
            decide_lane(["restricted", "exclusive_tram"], allows_passenger=True, allows_tram=True),
            "suspect",
        )
        self.assertEqual(classify_way({"highway": "service", "embedded_rails": "tram", "access": "no"}), "restricted")

    def test_ordinary_road_is_not_a_tram_edit(self) -> None:
        self.assertEqual(classify_way({"highway": "residential"}), "car_road")
        self.assertEqual(decide_lane(["car_road"], allows_passenger=True, allows_tram=False), "keep")

    def test_car_only_lane_cannot_turn_onto_a_tram_only_lane(self) -> None:
        self.assertTrue(exclusive_tram_crossing(True, False, False, True))
        self.assertTrue(exclusive_tram_crossing(False, True, True, False))
        self.assertFalse(exclusive_tram_crossing(True, True, False, True))
        self.assertFalse(exclusive_tram_crossing(True, False, True, False))

    def test_permission_rewrite_removes_cars_and_keeps_the_tram(self) -> None:
        allow, disallow = rewrite_lane_attributes("tram passenger", None, "remove_cars")
        self.assertEqual(allow, "tram")
        self.assertIsNone(disallow)
        allow, disallow = rewrite_lane_attributes(
            None,
            "pedestrian tram rail_urban rail rail_electric rail_fast ship",
            "add_tram",
        )
        self.assertIsNone(allow)
        self.assertNotIn("tram", disallow.split())
        self.assertIn("pedestrian", disallow.split())
        allow, disallow = rewrite_lane_attributes("tram", None, "add_cars")
        self.assertEqual(set(allow.split()), {"tram", "passenger"})


if __name__ == "__main__":
    unittest.main()
