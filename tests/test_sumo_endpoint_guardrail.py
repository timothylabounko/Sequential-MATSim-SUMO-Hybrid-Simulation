import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from matsim_sumo.steps.step_8_match_sumo_edges_simple import _apply_endpoint_distance_guardrail


class SumoEndpointGuardrailTest(unittest.TestCase):
    def test_guardrail_keeps_only_matched_rows_with_both_endpoint_distances_under_limit(self) -> None:
        matched = pd.DataFrame(
            {
                "from_edge": ["a", "b", "c", None],
                "to_edge": ["x", "y", "z", "w"],
                "from_edge_dist_m": [50.0, 151.0, 10.0, 20.0],
                "to_edge_dist_m": [149.0, 10.0, None, 10.0],
                "crossing_type": ["inside", "entering", "leaving", "pass_through"],
            }
        )

        kept, dropped = _apply_endpoint_distance_guardrail(matched, 150.0)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept.iloc[0]["from_edge"], "a")
        self.assertEqual(kept.iloc[0]["sumo_endpoint_max_dist_m"], 149.0)
        self.assertEqual(len(dropped), 3)


if __name__ == "__main__":
    unittest.main()
