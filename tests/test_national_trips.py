import unittest

from matsim_sumo.demand.national_trips import (
    car_trip_in_window,
    lv95_to_sumo,
    parse_clock,
    parse_depart,
    shift_depart,
    to_sumo_xy,
)


class NationalTripsTest(unittest.TestCase):
    def test_clock_and_numeric_departures(self) -> None:
        self.assertEqual(parse_depart("07:15:00"), 26100)
        self.assertEqual(parse_depart("26100"), 26100)
        self.assertIsNone(parse_depart(""))

    def test_only_a_car_inside_the_window_is_kept(self) -> None:
        self.assertEqual(car_trip_in_window({"main_mode": "car", "dep_time": "07:30:00"}, 25200, 28800), 27000)
        self.assertIsNone(car_trip_in_window({"main_mode": "walk", "dep_time": "07:30:00"}, 25200, 28800))
        self.assertIsNone(car_trip_in_window({"main_mode": "car", "dep_time": "09:00:00"}, 25200, 28800))

    def test_morning_car_is_kept_and_shifted_to_zero(self) -> None:
        start_s = parse_clock("07:00:00")
        end_s = parse_clock("08:00:00")
        self.assertEqual((start_s, end_s), (25200, 28800))
        depart = car_trip_in_window({"main_mode": "car", "dep_time": "07:30:00"}, start_s, end_s)
        self.assertEqual(depart, 27000)
        self.assertEqual(27000 - 25200, 1800)
        self.assertEqual(shift_depart(depart, start_s), 1800)
        self.assertEqual(shift_depart(parse_depart("07:00:00"), start_s), 0)
        self.assertEqual(shift_depart(parse_depart("07:00:01"), start_s), 1)

    def test_zurich_hb_lands_inside_the_sumo_network(self) -> None:
        x, y = lv95_to_sumo(2_683_108, 1_247_948, (-456786.67, -5238228.33))
        self.assertAlmostEqual(x, 8421, delta=30)
        self.assertAlmostEqual(y, 8948, delta=30)

    def test_eqasim_column_names_and_lv03_match_the_matsim_names(self) -> None:
        offset = (-456786.67, -5238228.33)
        row = {"mode": "car", "departure_time": "07:10:00", "origin_x": "683108", "origin_y": "247948"}
        self.assertEqual(car_trip_in_window(row, 25200, 28800), 25800)
        lv95 = to_sumo_xy(2_683_108, 1_247_948, offset)
        lv03 = to_sumo_xy(683_108, 247_948, offset)
        self.assertAlmostEqual(lv95[0], lv03[0], delta=1)
        self.assertAlmostEqual(lv95[1], lv03[1], delta=1)


if __name__ == "__main__":
    unittest.main()
