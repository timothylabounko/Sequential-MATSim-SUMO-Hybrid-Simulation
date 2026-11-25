import datetime as dt
import tempfile
import unittest
from pathlib import Path

from matsim_sumo.network.bike_correction import repair, unroutable
from matsim_sumo.network.bike_counts import geh, reference_hour, typical_weekday
from matsim_sumo.network.bike_network import BikeEdge, BikeGraph, build_graph, connections, successors
from matsim_sumo.network.bike_reference import (
    BikeLink,
    BikeReference,
    CatalogOnly,
    TurnBan,
    assert_geometry_file,
    clip_parts,
    directions,
)


def _edge(edge_id: str, origin: str, dest: str, ox: float, oy: float, dx: float, dy: float) -> BikeEdge:
    return BikeEdge(edge_id, origin, dest, "", 4.2, 1, "zurich", edge_id, 10.0, 1.0, ((ox, oy), (dx, dy)))


class DirectionTest(unittest.TestCase):
    def test_oneway_and_a_ban_close_a_direction(self) -> None:
        self.assertEqual(directions("0", 0, 0), (True, True))
        self.assertEqual(directions("FT", 0, 0), (True, False))
        self.assertEqual(directions("TF", 0, 1), (False, False))

    def test_a_corridor_keeps_only_the_part_next_to_the_city(self) -> None:
        parts = [[(0, 0), (10, 0), (30, 0)], [(100, 0), (110, 0)]]
        self.assertEqual(clip_parts(parts, (0, -1, 12, 1), margin=0), [[(0, 0), (10, 0)]])

    def test_a_catalog_xml_is_not_a_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "velonetz1.xml"
            path.write_text('<rdf:RDF xmlns:dcat="http://www.w3.org/ns/dcat#"><dcat:Dataset/></rdf:RDF>', encoding="utf-8")
            with self.assertRaises(CatalogOnly):
                assert_geometry_file(path)


class GraphTest(unittest.TestCase):
    def test_a_banned_turn_is_left_unconnected(self) -> None:
        link = lambda fid, x0, x1, y1: BikeLink(
            fid, "street", (((x0, 1_248_000.0), (x1, y1)),), True, True, False, False, 4.2, 4.2, 1, 1, "zurich"
        )
        reference = BikeReference(
            links=[link("1", 2_683_000.0, 2_683_100.0, 1_248_000.0), link("2", 2_683_100.0, 2_683_200.0, 1_248_100.0)],
            bans=[TurnBan("1", True, "2")],
        )
        graph = build_graph(reference)
        pairs = set(connections(graph))
        self.assertNotIn(("v1", "v2"), pairs)
        self.assertIn(("-v2", "-v1"), pairs)
        self.assertEqual(graph.dropped_bans, 0)


class SearchTest(unittest.TestCase):
    def test_the_search_joins_a_gap_that_blocks_a_trip(self) -> None:
        graph = BikeGraph(
            edges=[
                _edge("v1", "a", "b", 0, 0, 100, 0),
                _edge("v2", "c", "d", 105, 0, 200, 0),
            ],
            nodes={"a": (0, 0), "b": (100, 0), "c": (105, 0), "d": (200, 0)},
        )
        self.assertEqual(unroutable([("v1", "v2")], successors(graph.edges)), 1)
        result = repair(graph, [("v1", "v2")], rounds=5, patience=3)
        self.assertEqual(result["unroutable"], 0)
        self.assertEqual(len(result["connectors"]), 1)
        self.assertLess(result["score"], 10)

    def test_an_already_connected_trip_gains_no_connector(self) -> None:
        graph = BikeGraph(
            edges=[_edge("v1", "a", "b", 0, 0, 100, 0), _edge("v2", "b", "c", 100, 0, 200, 0)],
            nodes={"a": (0, 0), "b": (100, 0), "c": (200, 0)},
        )
        result = repair(graph, [("v1", "v2")], rounds=5, patience=2)
        self.assertEqual(result["connectors"], [])
        self.assertEqual(result["unroutable"], 0)


class CountTest(unittest.TestCase):
    def test_geh_is_zero_when_the_volumes_match(self) -> None:
        self.assertEqual(geh(10, 10), 0)

    def test_the_morning_median_skips_july_and_a_missing_quarter(self) -> None:
        self.assertTrue(typical_weekday(dt.date(2025, 1, 7)))
        self.assertFalse(typical_weekday(dt.date(2025, 7, 8)))
        stamps = []
        incoming = []
        outgoing = []
        for day, velo_in in (("2025-01-07", 1), ("2025-01-08", 5), ("2025-07-08", 100)):
            for minute in (0, 15, 30, 45):
                stamps.append(f"{day}T07:{minute:02d}")
                incoming.append(velo_in)
                outgoing.append(1)
        stamps.append("2025-01-09T07:00")
        incoming.append(9)
        outgoing.append(9)
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table({
            "FK_STANDORT": [7] * len(stamps),
            "DATUM": stamps,
            "VELO_IN": incoming,
            "VELO_OUT": outgoing,
            "FUSS_IN": [None] * len(stamps),
            "FUSS_OUT": [None] * len(stamps),
            "OST": [2_683_000] * len(stamps),
            "NORD": [1_248_000] * len(stamps),
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counts.parquet"
            pq.write_table(table, path)
            rows = reference_hour(path, year=2025)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["days"], 2)
        self.assertEqual(rows[0]["count"], 16)


class DemandCutTest(unittest.TestCase):
    def test_a_bicycle_lane_is_invisible_to_cars(self) -> None:
        import importlib.util

        script = Path(__file__).resolve().parents[1] / "scripts" / "plans_to_sumo.py"
        spec = importlib.util.spec_from_file_location("plans_to_sumo", script)
        plans = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plans)
        with tempfile.TemporaryDirectory() as directory:
            net = Path(directory) / "net.xml"
            net.write_text(
                """<net>
                <location netOffset="0.00,0.00" convBoundary="0.00,0.00,100.00,1.00" origBoundary="0,0,0,0" projParameter="!"/>
                <edge id="bike" from="a" to="b"><lane id="bike_0" index="0" allow="bicycle" speed="5" length="100" shape="0,0 100,0"/></edge>
                <edge id="car" from="a" to="b"><lane id="car_0" index="0" disallow="bicycle" speed="10" length="100" shape="0,1 100,1"/></edge>
                <connection from="bike" to="bike" fromLane="0" toLane="0"/>
                <connection from="car" to="car" fromLane="0" toLane="0"/>
                </net>""",
                encoding="utf-8",
            )
            self.assertEqual(set(plans.SumoNet(net).shapes), {"car"})
            self.assertEqual(set(plans.SumoNet(net, vclass="bicycle").shapes), {"bike"})
        modes = {mode.strip() for mode in "car,truck".split(",")}
        self.assertIn('vClass="passenger"', "".join(plans.vtype_lines(modes)))
        self.assertIn('vClass="bicycle"', "".join(plans.vtype_lines({"bike"})))
        with self.assertRaises(SystemExit):
            plans.main(["--modes", "car,bike", "--net", "net.xml", "--output", "out.xml"])


if __name__ == "__main__":
    unittest.main()
