import unittest

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from matsim_sumo.pipeline.stages.step_2_demand_scaling import scale_demand


class DemandScalingTest(unittest.TestCase):
    def test_only_jittered_endpoints_get_reference_links_rematched(self) -> None:
        combined = pd.DataFrame(
            {
                "new_trip_id": ["pass_0", "enter_0", "leave_0", "inside_0"],
                "dep_time": ["07:00:00"] * 4,
                "crossing_type": ["pass_through", "entering", "leaving", "inside"],
                "start_link": ["cut_start", "cut_start", "old_activity_start", "old_activity_start"],
                "end_link": ["cut_end", "old_activity_end", "cut_end", "old_activity_end"],
                "start_x": [0.0, 0.0, 20.0, 20.0],
                "start_y": [0.0, 0.0, 0.0, 0.0],
                "end_x": [10.0, 30.0, 10.0, 30.0],
                "end_y": [0.0, 0.0, 0.0, 0.0],
            }
        )
        network_geo = gpd.GeoDataFrame(
            {
                "link_id": ["nearest_boundary_start", "nearest_boundary_end", "nearest_activity_start", "nearest_activity_end"],
            },
            geometry=[
                LineString([(0.0, -1.0), (0.0, 1.0)]),
                LineString([(10.0, -1.0), (10.0, 1.0)]),
                LineString([(20.0, -1.0), (20.0, 1.0)]),
                LineString([(30.0, -1.0), (30.0, 1.0)]),
            ],
            crs="EPSG:2056",
        )

        scaled = scale_demand(
            combined,
            scale_factor=2,
            dep_time_jitter_s=0,
            coord_radius_m=0.0,
            seed=1,
            network_geo=network_geo,
        ).set_index("new_trip_id")

        clones = scaled.loc[["pass_1", "enter_1", "leave_1", "inside_1"]]

        self.assertEqual(clones.loc["pass_1", "new_start_link"], "cut_start")
        self.assertEqual(clones.loc["pass_1", "new_end_link"], "cut_end")
        self.assertEqual(clones.loc["enter_1", "new_start_link"], "cut_start")
        self.assertEqual(clones.loc["enter_1", "new_end_link"], "nearest_activity_end")
        self.assertEqual(clones.loc["leave_1", "new_start_link"], "nearest_activity_start")
        self.assertEqual(clones.loc["leave_1", "new_end_link"], "cut_end")
        self.assertEqual(clones.loc["inside_1", "new_start_link"], "nearest_activity_start")
        self.assertEqual(clones.loc["inside_1", "new_end_link"], "nearest_activity_end")


if __name__ == "__main__":
    unittest.main()
