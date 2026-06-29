import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from matsim_sumo.pipeline.runner import run_pipeline_steps


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        config={
            "sumo_vclass_map": {"car": "passenger"},
            "run_duarouter": False,
            "mode_share_diagnostics_enabled": False,
        },
        output_dir=Path("results/test"),
        cut_link_ids=[1],
        car_legs=pd.DataFrame(index=range(1)),
        car_legs_cut=pd.DataFrame(index=range(1)),
        crossing_segments=pd.DataFrame({"dep_time": [pd.NA, "08:00:00"]}),
        crossing_segments_timed=pd.DataFrame({"dep_time": ["07:00:00", "08:00:00"]}),
        trips_inside=pd.DataFrame(index=range(1)),
        combined_trips=pd.DataFrame(index=range(1)),
        scaled_trips=pd.DataFrame(index=range(1)),
        scaled_trips_matched=pd.DataFrame({"match_ok": [True]}),
        sumo_endpoint_guardrail_dropped_count=0,
        sumo_trips_count=1,
        sumo_routes_count=0,
        sumo_routes_pct=0.0,
        sumo_unrouted_count=0,
        sumo_unrouted_pct=0.0,
        mode_share_outputs=None,
        scenario_stats_path=Path("results/test/scenario_stats.csv"),
    )


class OptionalDiagnosticsTest(unittest.TestCase):
    def test_mode_share_diagnostics_are_skipped_by_default(self) -> None:
        state = _state()
        stage_names = [
            "run_sumo_network_preprocessing_stage",
            "run_sumo_boundary_stage",
            "run_boundary_cut_links_stage",
            "run_plans_car_legs_stage",
            "run_crossing_segments_stage",
            "run_segment_timing_stage",
            "run_inside_trip_filter_stage",
            "run_combine_trips_stage",
            "run_demand_scaling_stage",
            "run_match_sumo_edges_stage",
            "run_write_sumo_trips_xml_stage",
            "run_scenario_stats_helper",
        ]
        patches = [patch(f"matsim_sumo.pipeline.runner.{name}") for name in stage_names]
        patches.append(patch("matsim_sumo.pipeline.runner.run_mode_share_diagnostics"))

        started = [p.start() for p in patches]
        try:
            stats = run_pipeline_steps(state)
        finally:
            for p in reversed(patches):
                p.stop()

        mode_share_mock = started[-1]
        mode_share_mock.assert_not_called()
        self.assertFalse(stats["mode_share_diagnostics_enabled"])
        self.assertEqual(stats["mode_share_outputs"], 0)


if __name__ == "__main__":
    unittest.main()
