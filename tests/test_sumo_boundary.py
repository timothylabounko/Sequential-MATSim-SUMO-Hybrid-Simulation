import sys
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.network_preprocessing.sumo_boundary import build_sumo_boundary


def _gdf(lines: list[LineString]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"edge_id": [str(i) for i in range(len(lines))]}, geometry=lines, crs="EPSG:2056")


class SumoBoundaryTest(unittest.TestCase):
    def test_boundary_uses_modest_final_expansion(self) -> None:
        boundary = build_sumo_boundary(
            _gdf([LineString([(0, 0), (1000, 0)])]),
            network_buffer_m=50,
            gap_close_m=0,
            inward_buffer_m=0,
            simplify_m=0,
            outward_buffer_m=100,
        )

        minx, miny, maxx, maxy = boundary.bounds
        self.assertGreaterEqual(minx, -151)
        self.assertLessEqual(maxx, 1151)
        self.assertGreaterEqual(miny, -151)
        self.assertLessEqual(maxy, 151)

    def test_internal_holes_are_removed_by_shell(self) -> None:
        ring = LineString([(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)])
        boundary = build_sumo_boundary(
            _gdf([ring]),
            network_buffer_m=20,
            gap_close_m=0,
            inward_buffer_m=0,
            simplify_m=0,
            outward_buffer_m=0,
        )

        self.assertEqual(len(boundary.interiors), 0)
        self.assertTrue(boundary.covers(Point(50, 50)))

    def test_inward_buffer_cuts_narrow_road_tails(self) -> None:
        main = LineString([(0, 0), (0, 500), (500, 500), (500, 0), (0, 0)])
        tail = LineString([(500, 250), (900, 250)])
        boundary = build_sumo_boundary(
            _gdf([main, tail]),
            network_buffer_m=20,
            gap_close_m=0,
            inward_buffer_m=50,
            simplify_m=0,
            outward_buffer_m=50,
        )

        self.assertFalse(boundary.covers(Point(875, 250)))


if __name__ == "__main__":
    unittest.main()
