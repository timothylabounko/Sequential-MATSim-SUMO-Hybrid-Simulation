import sys
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.steps.step_1_boundary_cut_links import compute_cut_link_ids_from_geo


class BoundaryCutLinksTest(unittest.TestCase):
    def test_links_that_touch_boundary_geometry_are_cut_even_without_endpoint_side_change(self) -> None:
        geo = gpd.GeoDataFrame(
            {"link_id": ["1", "2", "3", "not_numeric"]},
            geometry=[
                LineString([(-1, 5), (5, 5)]),
                LineString([(-1, 1), (11, 1)]),
                LineString([(2, 2), (8, 2)]),
                LineString([(-1, 9), (11, 9)]),
            ],
            crs="EPSG:2056",
        )

        out = compute_cut_link_ids_from_geo(geo, box(0, 0, 10, 10))

        self.assertEqual(out.to_list(), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
