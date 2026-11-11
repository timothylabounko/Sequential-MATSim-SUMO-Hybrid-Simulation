import tempfile
import unittest
from pathlib import Path

from matsim_sumo.network.osm_zurich import DEFAULT_OUTPUT
from matsim_sumo.network.zurich_correction import (
    DEFAULT_OUTPUT_DIR,
    Edit,
    Restriction,
    assert_distinct_output,
    connection_xml,
    cover_routes,
    cover_routes_on_graph,
    default_osm_path,
    direction_band,
    edited_connections,
    excepts_cars,
    is_opposite_edge,
    node_contains_via,
    only_extra_indexes,
    parse_restrictions,
    prohibited_indexes,
    signed_deflection_degrees,
    single_missing_usual_turn,
    straight_target_lane,
    uturn_indexes_to_remove,
)


class ZurichCorrectionRulesTest(unittest.TestCase):
    def test_defaults_reread_the_unedited_network(self) -> None:
        self.assertEqual(DEFAULT_OUTPUT, Path("networks/zurich/zurich.net.xml"))
        self.assertEqual(DEFAULT_OUTPUT_DIR, Path("results/zurich-correction"))
        self.assertNotEqual(DEFAULT_OUTPUT, DEFAULT_OUTPUT_DIR / "zurich.corrected.net.xml")

    def test_output_cannot_replace_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "zurich.net.xml"
            source.write_text("<net/>", encoding="utf-8")
            with self.assertRaises(SystemExit):
                assert_distinct_output(source, source)

    def test_osm_path_sits_beside_the_network(self) -> None:
        self.assertEqual(
            default_osm_path(Path("networks/zurich/zurich.net.xml")),
            Path("networks/zurich/zurich.osm.xml"),
        )

    def test_exclusive_uturn_stays(self) -> None:
        self.assertEqual(uturn_indexes_to_remove(["t"]), [])
        self.assertEqual(uturn_indexes_to_remove(["t", "t"]), [])

    def test_uturn_with_another_exit_is_removed(self) -> None:
        self.assertEqual(uturn_indexes_to_remove(["r", "s", "l", "t"]), [3])
        self.assertEqual(uturn_indexes_to_remove(["s"]), [])

    def test_straight_merge_clamps_to_the_downstream_lanes(self) -> None:
        self.assertEqual(straight_target_lane(0, 1), 0)
        self.assertEqual(straight_target_lane(1, 1), 0)
        self.assertEqual(straight_target_lane(1, 2), 1)
        self.assertEqual(straight_target_lane(2, 2), 1)

    def test_opposite_edges_are_the_two_directions_of_one_way(self) -> None:
        self.assertTrue(is_opposite_edge("-11005812", "11005812"))
        self.assertTrue(is_opposite_edge("106098860#0", "-106098860#1"))
        self.assertFalse(is_opposite_edge("-1055784511", "-1391317309"))

    def test_cluster_junction_contains_each_member_node(self) -> None:
        self.assertTrue(node_contains_via("cluster_379831737_469805737", "379831737"))
        self.assertFalse(node_contains_via("cluster_379831737_469805737", "37983173"))
        self.assertTrue(node_contains_via("9701204803", "9701204803"))

    def test_prohibition_requires_every_match_to_use_the_banned_direction(self) -> None:
        self.assertEqual(prohibited_indexes(["l"], frozenset({"l", "L"})), [0])
        self.assertIsNone(prohibited_indexes(["l"], frozenset({"t"})))
        self.assertIsNone(prohibited_indexes(["l", "s"], frozenset({"l", "L"})))
        self.assertIsNone(prohibited_indexes(["l"] * 5, frozenset({"l", "L"})))

    def test_only_restriction_keeps_other_turns_when_the_allowed_turn_is_missing(self) -> None:
        outgoing = [("straight-edge", "s"), ("left-edge", "l")]
        self.assertIsNone(only_extra_indexes(outgoing, {"missing"}, frozenset({"s"})))
        self.assertEqual(only_extra_indexes(outgoing, {"straight-edge"}, frozenset({"s"})), [1])
        self.assertEqual(only_extra_indexes(outgoing, {"straight-edge", "left-edge"}, frozenset({"s"})), [])

    def test_car_exceptions_are_not_treated_as_car_bans(self) -> None:
        self.assertFalse(excepts_cars("bicycle;bus"))
        self.assertTrue(excepts_cars("motorcar"))
        self.assertTrue(excepts_cars("bicycle|vehicle"))

    def test_one_missing_usual_turn_is_reported_and_several_are_not(self) -> None:
        exits = [("east", -90.0), ("north", 0.0)]
        self.assertEqual(single_missing_usual_turn(exits, {"s"}), "east")
        self.assertIsNone(single_missing_usual_turn(exits, set()))
        self.assertIsNone(single_missing_usual_turn([("back", 180.0)], set()))

    def test_heading_change_sign_matches_right_hand_turns(self) -> None:
        north = [(0.0, 0.0), (0.0, 10.0)]
        east = [(0.0, 10.0), (10.0, 10.0)]
        delta = signed_deflection_degrees(north, east)
        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertAlmostEqual(delta, -90.0)
        self.assertEqual(direction_band(delta), "r")
        self.assertEqual(direction_band(90.0), "l")
        self.assertEqual(direction_band(0.0), "s")
        self.assertEqual(direction_band(180.0), "t")

    def test_route_cover_chains_maneuvers_and_stops_at_the_limit(self) -> None:
        links = [("a", "b"), ("c", "d"), ("b", "c"), ("d", "e")]
        self.assertEqual(cover_routes(links, max_length=3), [[("a", "b"), ("b", "c"), ("c", "d")], [("d", "e")]])

    def test_route_cover_drives_existing_links_between_suspects(self) -> None:
        successors = {"a": ("b",), "b": ("c",), "c": ("d",), "d": ("e",), "e": ()}
        routes = cover_routes_on_graph([("a", "b"), ("d", "e")], successors, max_length=5, max_hops=4)
        self.assertEqual(
            routes,
            [[("a", "b", "suspect"), ("b", "c", "link"), ("c", "d", "link"), ("d", "e", "suspect")]],
        )

    def test_edit_set_removes_and_adds_without_touching_other_connections(self) -> None:
        existing = {("a", "b", 0, 0), ("a", "c", 0, 0), ("d", "e", 1, 1)}
        edited = edited_connections(existing, {("a", "c", 0, 0)}, {("a", "b", 1, 0)})
        self.assertEqual(edited, {("a", "b", 0, 0), ("a", "b", 1, 0), ("d", "e", 1, 1)})

    def test_connection_file_encodes_deletes_and_adds(self) -> None:
        xml = connection_xml(
            [
                Edit("delete", "from&edge", "to", 0, 0, "remove_uturn", "j", "", "evidence"),
                Edit("add", "from", "to", 1, 0, "reconnect_orphan_lane", "j", "", "evidence"),
            ],
            Path("networks/zurich/zurich.net.xml"),
        )
        self.assertIn('<delete from="from&amp;edge" to="to" fromLane="0" toLane="0"/>', xml)
        self.assertIn('<connection from="from" to="to" fromLane="1" toLane="0"/>', xml)

    def test_restrictions_are_read_from_osm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            osm = Path(directory) / "sample.osm.xml"
            osm.write_text(
                """<?xml version="1.0"?>
<osm>
  <relation id="7">
    <member type="way" ref="10" role="from"/>
    <member type="node" ref="20" role="via"/>
    <member type="way" ref="30" role="to"/>
    <tag k="type" v="restriction"/>
    <tag k="restriction" v="no_left_turn"/>
    <tag k="except" v="bicycle"/>
  </relation>
  <relation id="8">
    <member type="way" ref="1" role="from"/>
    <member type="way" ref="2" role="via"/>
    <member type="way" ref="3" role="to"/>
    <tag k="type" v="restriction"/>
    <tag k="restriction" v="only_straight_on"/>
  </relation>
</osm>
""",
                encoding="utf-8",
            )
            restrictions = parse_restrictions(osm)
        self.assertEqual(
            restrictions,
            [
                Restriction("7", "no_left_turn", "10", "30", "20", (), ("bicycle",)),
                Restriction("8", "only_straight_on", "1", "3", None, ("2",), ()),
            ],
        )


if __name__ == "__main__":
    unittest.main()
