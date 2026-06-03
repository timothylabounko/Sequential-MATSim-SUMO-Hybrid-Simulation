import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from matsim_sumo.cli import main
from matsim_sumo.config.loader import load_config
from matsim_sumo.pipeline.runner import build_pipeline_state, resolve_sumo_network_path


def _minimal_config() -> dict:
    return {
        "matsim_output_dir": "data/inputs/matsim",
        "boundary_file": "data/generated/boundaries/test_boundary.geojson",
        "network_file": "output_network.xml.gz",
        "plans_file": "output_plans.xml.gz",
        "trips_file": "output_trips.csv.gz",
        "events_file": "output_events.xml.gz",
        "output_dir": "results/test",
        "modes_keep": ["car"],
        "write_intermediates": False,
        "write_review_csv": False,
        "scale_factor": 1.0,
        "dep_time_jitter_seconds": 0,
        "coord_jitter_radius_m": 0.0,
        "demand_scaling_seed": 1,
        "amenity_kdtree_enabled": False,
        "amenities_path": "data/inputs/amenities/locations.csv",
        "sumo_input_dir": "data/inputs/sumo",
        "sumo_network_path": "scenario.net.xml",
        "sumo_network_lv95_tmp_file": "data/tmp/sumo_network_lv95.geojson",
        "sumo_vclass_map": {"car": "passenger"},
        "run_duarouter": False,
        "duarouter_bin": "duarouter",
        "sumo_routes_file": "4_2_sumo_routes.rou.xml",
        "sumo_unrouted_trips_file": "4_2_unrouted_trips.xml",
        "duarouter_log_file": "4_2_duarouter.log",
    }


class ConfigCliTest(unittest.TestCase):
    def test_load_config_applies_runtime_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(_minimal_config()))

            cfg = load_config(path, no_cache=True)

        self.assertTrue(cfg["no_cache"])
        self.assertEqual(cfg["csv_gzip_compresslevel"], 1)
        self.assertTrue(Path(cfg["extract_events_script"]).name.endswith("extract_entered_link_events.sh"))

    def test_path_resolution_matches_config(self) -> None:
        cfg = _minimal_config()
        state = build_pipeline_state(cfg)

        self.assertEqual(state.network_path, Path("data/inputs/matsim/output_network.xml.gz"))
        self.assertEqual(resolve_sumo_network_path(cfg), Path("data/inputs/sumo/scenario.net.xml"))
        self.assertEqual(state.scaled_trips_path, Path("results/test/2_scaled_trips.csv.gz"))
        self.assertEqual(state.scaled_trips_matched_path, Path("results/test/3_matched_trips.csv.gz"))
        self.assertEqual(state.sumo_trips_path, Path("results/test/4_1_sumo_trips.xml"))

    def test_run_help_is_available(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["run", "--help"])

        self.assertEqual(rc, 0)
        self.assertIn("Run the MATSim-SUMO conversion pipeline", out.getvalue())

    def test_root_help_lists_commands(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["--help"])

        self.assertEqual(rc, 0)
        self.assertIn("matsim-sumo <command>", out.getvalue())
        self.assertIn("export-routes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
