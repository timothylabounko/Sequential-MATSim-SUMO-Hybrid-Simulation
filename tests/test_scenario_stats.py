import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from matsim_sumo.pipeline.scenario_stats import build_scenario_stats, write_scenario_stats


class _FakeSumoNet:
    def getEdges(self, withInternal=False):  # noqa: N803
        return [object(), object(), object()]

    def getNodes(self):
        return [object(), object()]

    def getTrafficLights(self):
        return [object()]


def _state(tmp: Path) -> SimpleNamespace:
    boundary_path = tmp / "boundary.geojson"
    gpd.GeoDataFrame(
        geometry=[Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])],
        crs="EPSG:2056",
    ).to_file(boundary_path, driver="GeoJSON")

    matched_path = tmp / "3_matched_trips.csv.gz"
    pd.DataFrame({"match_ok": [True, True, False]}).to_csv(matched_path, sep=";", index=False)

    return SimpleNamespace(
        config={"run_duarouter": True},
        boundary=None,
        boundary_path=boundary_path,
        sumo_net=_FakeSumoNet(),
        sumo_network_path=tmp / "network.net.xml",
        sumo_network_geo=pd.DataFrame({"edge_id": ["a", "b"]}),
        sumo_network_lv95_path=tmp / "network.geojson",
        car_legs=pd.DataFrame(index=range(10)),
        car_legs_path=tmp / "missing.csv.gz",
        car_legs_cut=pd.DataFrame(index=range(8)),
        car_legs_cut_path=tmp / "missing.csv.gz",
        crossing_segments=pd.DataFrame(index=range(6)),
        crossing_segments_path=tmp / "missing.csv.gz",
        crossing_segments_timed=pd.DataFrame(index=range(5)),
        crossing_segments_timed_path=tmp / "missing.csv.gz",
        crossing_segments_outside_boundary_dropped_count=1,
        trips_inside=pd.DataFrame(index=range(4)),
        trips_inside_path=tmp / "missing.csv.gz",
        combined_trips=pd.DataFrame(index=range(9)),
        combined_trips_path=tmp / "missing.csv.gz",
        scaled_trips=pd.DataFrame(index=range(18)),
        scaled_trips_path=tmp / "missing.csv.gz",
        scaled_trips_matched=None,
        scaled_trips_matched_path=matched_path,
        sumo_trips_count=2,
        sumo_trips_path=tmp / "4_1_sumo_trips.xml",
        sumo_endpoint_guardrail_dropped_count=15,
        sumo_endpoint_guardrail_dropped_by_type={
            "short_pass_through_distance": 12,
            "not_routeable": 3,
        },
        sumo_routes_input_count=2,
        sumo_routes_count=1,
        sumo_routes_path=tmp / "4_2_sumo_routes.rou.xml",
        sumo_unrouted_count=1,
        sumo_unrouted_trips_path=tmp / "4_2_unrouted_trips.xml",
        sumo_unrouted_pct=50.0,
        scenario_stats_path=tmp / "scenario_stats.csv",
        scenario_stats=None,
    )


class ScenarioStatsTest(unittest.TestCase):
    def test_build_scenario_stats_has_sections_and_required_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            stats = build_scenario_stats(state)

        sections = set(stats["section"])
        self.assertTrue({"Scenario", "SUMO network", "Demand pipeline", "Matching drops", "Routing"} <= sections)

        values = {(row.section, row.metric): row.value for row in stats.itertuples(index=False)}
        self.assertEqual(values[("Scenario", "boundary_area")], 1.0)
        self.assertEqual(values[("SUMO network", "edges_total")], 3)
        self.assertEqual(values[("SUMO network", "nodes_total")], 2)
        self.assertEqual(values[("SUMO network", "tls_total")], 1)
        self.assertEqual(values[("SUMO network", "vclass_edges")], 2)
        self.assertEqual(values[("Demand pipeline", "plan_legs_extracted")], 10)
        self.assertEqual(values[("Demand pipeline", "match_ok_trips")], 2)

    def test_matching_drop_reasons_are_dynamic_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stats = build_scenario_stats(_state(Path(tmp_dir)))

        drops = stats[stats["section"] == "Matching drops"].set_index("metric")["value"].to_dict()
        self.assertEqual(drops["total"], 15)
        self.assertEqual(drops["short_pass_through_distance"], 12)
        self.assertEqual(drops["not_routeable"], 3)

    def test_write_scenario_stats_sets_state_and_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            path = write_scenario_stats(state)

            self.assertEqual(path, state.scenario_stats_path)
            self.assertTrue(path.exists())
            self.assertIsNotNone(state.scenario_stats)
            written = pd.read_csv(path)

        self.assertEqual(list(written.columns), ["section", "metric", "value", "unit"])


if __name__ == "__main__":
    unittest.main()
