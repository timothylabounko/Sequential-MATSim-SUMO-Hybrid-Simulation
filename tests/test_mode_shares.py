import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from shapely.geometry import box

from analysis.mode_shares.common import (
    COMPARISON_MODES,
    comparison_table,
    endpoint_mask,
    read_boundary_geom,
    share_table,
)
from analysis.mode_shares.matsim import endpoint_mode_counts as matsim_endpoint_mode_counts
from analysis.mode_shares.microcensus import clean_trips as clean_mz_trips
from analysis.mode_shares.microcensus import endpoint_mode_counts as mz_endpoint_mode_counts


class ModeSharesTest(unittest.TestCase):
    def test_share_table_orders_and_normalizes_comparison_modes(self) -> None:
        out = share_table(Counter({"walk": 3, "car": 1}), "trips", "share", modes=COMPARISON_MODES)

        self.assertEqual(out["mode"].to_list(), COMPARISON_MODES)
        self.assertAlmostEqual(out.loc[out["mode"].eq("walk"), "share"].iloc[0], 0.75)
        self.assertAlmostEqual(out.loc[out["mode"].eq("car"), "share"].iloc[0], 0.25)
        self.assertAlmostEqual(out["share"].sum(), 1.0)

    def test_share_table_keeps_all_modes_without_mode_list(self) -> None:
        out = share_table(Counter({"truck": 4, "walk_loop": 2, "car": 1}), "trips", "share")

        self.assertEqual(out["mode"].to_list(), ["truck", "walk_loop", "car"])
        self.assertAlmostEqual(out["share"].sum(), 1.0)

    def test_endpoint_mask_counts_origin_or_destination(self) -> None:
        trips = pd.DataFrame(
            {
                "start_x": [1.0, 12.0, 12.0],
                "start_y": [1.0, 12.0, 12.0],
                "end_x": [12.0, 2.0, 12.0],
                "end_y": [12.0, 2.0, 12.0],
            }
        )
        geom = box(0, 0, 10, 10)

        origin = endpoint_mask(trips, "start_x", "start_y", geom)
        destination = endpoint_mask(trips, "end_x", "end_y", geom)

        self.assertEqual(origin.to_list(), [True, False, False])
        self.assertEqual(destination.to_list(), [False, True, False])

    def test_comparison_table_uses_mode_order_and_diff_pp(self) -> None:
        mz = pd.DataFrame({"mode": ["car", "walk"], "Microcensus_share": [0.4, 0.6]})
        matsim = pd.DataFrame({"mode": ["car", "walk"], "MATSim_share": [0.5, 0.5]})

        out = comparison_table(mz, matsim)

        self.assertEqual(out["mode"].to_list(), COMPARISON_MODES)
        self.assertAlmostEqual(out.loc[out["mode"].eq("car"), "diff_pp_MATSim_minus_Microcensus"].iloc[0], 10.0)
        self.assertAlmostEqual(out.loc[out["mode"].eq("walk"), "diff_pp_MATSim_minus_Microcensus"].iloc[0], -10.0)

    def test_matsim_endpoint_counts_filter_persons_distance_and_modes(self) -> None:
        trips = pd.DataFrame(
            {
                "person": ["p1", "p1", "p2", "p3", "p1"],
                "main_mode": ["car", "truck", "walk", "pt", "bike_loop"],
                "euclidean_distance": [100.0, 200.0, 300.0, 1.0, 400.0],
                "start_x": [1.0, 1.0, 1.0, 1.0, 1.0],
                "start_y": [1.0, 1.0, 1.0, 1.0, 1.0],
                "end_x": [12.0, 2.0, 2.0, 2.0, 2.0],
                "end_y": [12.0, 2.0, 2.0, 2.0, 2.0],
            }
        )
        persons = pd.DataFrame(
            {
                "person": ["p1", "p2", "p3"],
                "subpopulation": ["", "", "freight"],
            }
        )
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            trips.to_csv(base / "output_trips.csv.gz", sep=";", index=False, compression="gzip")
            persons.to_csv(base / "output_persons.csv.gz", sep=";", index=False, compression="gzip")

            counts = matsim_endpoint_mode_counts(base, box(0, 0, 10, 10))

        self.assertEqual(counts["origin"], Counter({"car": 1, "walk": 1}))
        self.assertEqual(counts["destination"], Counter({"walk": 1}))

    def test_mz_aggregation_defaults_to_workday_persons(self) -> None:
        persons = pd.DataFrame(
            {
                "person_id": ["p1", "p2", "p3", "p4", "p5", "p6"],
                "person_weight": [2.0, 10.0, 5.0, 6.0, None, 7.0],
                "household_weight": [1.0, 1.0, 1.0, None, 1.0, 1.0],
                "weekend": [False, True, False, False, False, False],
            }
        )
        trips = pd.DataFrame(
            {
                "person_id": ["p1", "p2", "p3", "p4", "p5", "p6"],
                "mode": ["pt", "car_passenger", "car", "walk", "bike", "bike"],
                "crowfly_distance": [100.0, 100.0, 100.0, 100.0, 100.0, 1.0],
                "origin_x": [0.5] * 6,
                "origin_y": [0.5] * 6,
                "destination_x": [1.0] * 6,
                "destination_y": [1.0] * 6,
            }
        )

        with patch("analysis.mode_shares.microcensus.load_data", return_value=(persons, trips, {"p3"})):
            counts = mz_endpoint_mode_counts(Path("."), box(0, 0, 10, 10))
            cleaned = clean_mz_trips(Path("."))

        self.assertEqual(counts["origin"], Counter({"pt": 2.0}))
        self.assertEqual(counts["destination"], Counter({"pt": 2.0}))
        self.assertEqual(cleaned["person_id"].to_list(), ["p1"])

    def test_boundary_csv_wkt_is_reprojected_to_lv95(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "boundary.csv"
            path.write_text('WKT,Name\n"POLYGON ((8.52 47.36, 8.53 47.36, 8.53 47.37, 8.52 47.37, 8.52 47.36))",test\n')

            geom = read_boundary_geom(path)

        minx, miny, maxx, maxy = geom.bounds
        self.assertGreater(minx, 2_600_000)
        self.assertGreater(miny, 1_200_000)
        self.assertLess(maxx, 2_800_000)
        self.assertLess(maxy, 1_300_000)


if __name__ == "__main__":
    unittest.main()
