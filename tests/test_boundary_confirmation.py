import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import LineString, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matsim_sumo.plot.boundary_confirmation import (
    BOUNDARY_DECLINED_MESSAGE,
    confirm_boundary_preview,
)


def _sumo_geo() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"edge_id": ["a", "b"]},
        geometry=[
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (10, 10)]),
        ],
        crs="EPSG:2056",
    )


def _write_boundary(path: Path) -> None:
    gpd.GeoDataFrame(
        {"name": ["test"]},
        geometry=[Polygon([(-1, -1), (11, -1), (11, 11), (-1, 11), (-1, -1)])],
        crs="EPSG:2056",
    ).to_file(path, driver="GeoJSON")


class BoundaryConfirmationTest(unittest.TestCase):
    def test_yes_creates_preview_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            boundary_path = tmp_path / "boundary.geojson"
            _write_boundary(boundary_path)

            with patch("builtins.input", return_value="yes"), patch(
                "matsim_sumo.plot.boundary_confirmation.webbrowser.open", return_value=True
            ) as open_mock:
                with redirect_stdout(StringIO()):
                    plot_path = confirm_boundary_preview(_sumo_geo(), boundary_path, tmp_path / "output")

            self.assertTrue(plot_path.exists())
            self.assertGreater(plot_path.stat().st_size, 0)
            open_mock.assert_called_once()

    def test_no_aborts_before_pipeline_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            boundary_path = tmp_path / "boundary.geojson"
            _write_boundary(boundary_path)

            with patch("builtins.input", return_value="n"), patch(
                "matsim_sumo.plot.boundary_confirmation.webbrowser.open", return_value=True
            ):
                with self.assertRaises(SystemExit) as ctx:
                    with redirect_stdout(StringIO()):
                        confirm_boundary_preview(_sumo_geo(), boundary_path, tmp_path / "output")

            self.assertEqual(str(ctx.exception), BOUNDARY_DECLINED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
