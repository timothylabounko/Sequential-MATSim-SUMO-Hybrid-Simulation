import unittest

import pandas as pd
from shapely.geometry import box

from matsim_sumo.pipeline.stages.step_04_segment_timing import drop_segments_outside_boundary


class SegmentBoundaryFilterTest(unittest.TestCase):
    def test_drops_segments_with_endpoint_outside_boundary_tolerance(self) -> None:
        segments = pd.DataFrame(
            {
                "start_x": [1.0, 1.0, 1.0],
                "start_y": [1.0, 1.0, 1.0],
                "end_x": [9.0, 10.5, 12.0],
                "end_y": [9.0, 5.0, 5.0],
            }
        )

        kept, dropped = drop_segments_outside_boundary(segments, box(0, 0, 10, 10), tolerance_m=1.0)

        self.assertEqual(dropped, 1)
        self.assertEqual(kept.index.to_list(), [0, 1])
        self.assertEqual(kept["end_x"].to_list(), [9.0, 10.5])


if __name__ == "__main__":
    unittest.main()
