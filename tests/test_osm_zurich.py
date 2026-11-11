import tempfile
import unittest
from pathlib import Path

from matsim_sumo.network.osm_zurich import (
    ZURICH_BBOX,
    edges_outside_bbox,
    merge_missing_osm_elements,
    strip_newer_vclasses,
    zurich_overpass_query,
)


class _Edge:
    def __init__(self, edge_id: str, shape: list[tuple[float, float]], function: str = "") -> None:
        self._id = edge_id
        self._shape = shape
        self._function = function

    def getID(self) -> str:
        return self._id

    def getShape(self) -> list[tuple[float, float]]:
        return self._shape

    def getFunction(self) -> str:
        return self._function


class _Net:
    def __init__(self, edges: list[_Edge]) -> None:
        self._edges = edges

    def getEdges(self) -> list[_Edge]:
        return self._edges

    def convertXY2LonLat(self, x: float, y: float) -> tuple[float, float]:
        return x, y


class ZurichOsmExtractTest(unittest.TestCase):
    def test_bbox_covers_the_city(self) -> None:
        self.assertLess(ZURICH_BBOX["west"], ZURICH_BBOX["east"])
        self.assertLess(ZURICH_BBOX["south"], ZURICH_BBOX["north"])
        self.assertLess(ZURICH_BBOX["west"], 8.54)
        self.assertGreater(ZURICH_BBOX["east"], 8.54)
        self.assertLess(ZURICH_BBOX["south"], 47.37)
        self.assertGreater(ZURICH_BBOX["north"], 47.37)

    def test_overpass_query_uses_the_zurich_box(self) -> None:
        query = zurich_overpass_query()
        self.assertIn(f"{ZURICH_BBOX['south']},{ZURICH_BBOX['west']}", query)
        self.assertIn("footway", query)
        self.assertIn('way["railway"="tram"]', query)
        self.assertIn('relation["type"="restriction"]', query)

    def test_edges_mostly_outside_the_box_are_removed(self) -> None:
        inside = _Edge("inside", [(8.50, 47.37), (8.52, 47.38)])
        outside = _Edge("outside", [(8.70, 47.50), (8.80, 47.55)])
        internal = _Edge("internal", [(8.70, 47.50), (8.80, 47.55)], function="internal")
        dropped = edges_outside_bbox(_Net([inside, outside, internal]), ZURICH_BBOX)
        self.assertEqual(dropped, ["outside"])

    def test_merged_tram_way_keeps_its_nodes_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.osm.xml"
            extra = root / "extra.osm.xml"
            merged = root / "merged.osm.xml"
            base.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="47.37" lon="8.54"/>
  <way id="10"><nd ref="1"/><tag k="highway" v="residential"/></way>
</osm>
""",
                encoding="utf-8",
            )
            extra.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="47.37" lon="8.54"/>
  <node id="2" lat="47.38" lon="8.55"/>
  <way id="20"><nd ref="1"/><nd ref="2"/><tag k="railway" v="tram"/></way>
</osm>
""",
                encoding="utf-8",
            )
            nodes, ways = merge_missing_osm_elements(base, extra, merged)
            text = merged.read_text(encoding="utf-8")
        self.assertEqual((nodes, ways), (1, 1))
        self.assertIn('<node id="2" lat="47.38" lon="8.55" />', text)
        self.assertIn('<nd ref="2" />', text)
        self.assertIn('<tag k="railway" v="tram" />', text)
        self.assertEqual(text.count('id="1"'), 1)

    def test_newer_vehicle_classes_are_removed_for_sumo_118(self) -> None:
        cleaned = strip_newer_vclasses('<lane allow="passenger scooter drone" disallow="pedestrian wheelchair"/>')
        self.assertEqual(cleaned, '<lane allow="passenger" disallow="pedestrian"/>')


if __name__ == "__main__":
    unittest.main()
