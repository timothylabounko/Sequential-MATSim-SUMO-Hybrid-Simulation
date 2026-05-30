import csv
import tempfile
import unittest
from pathlib import Path

from matsim_sumo.io.sumo_trips_xml import write_sumo_trips_xml


class WriteSumoTripsXmlTest(unittest.TestCase):
    def test_skips_missing_or_zero_departures(self) -> None:
        rows = [
            {"new_trip_id": "valid", "dep_time": "08:15:00", "from_edge": "a", "to_edge": "b"},
            {"new_trip_id": "blank", "dep_time": "", "from_edge": "a", "to_edge": "b"},
            {"new_trip_id": "nan", "dep_time": "nan", "from_edge": "a", "to_edge": "b"},
            {"new_trip_id": "zero", "dep_time": "0", "from_edge": "a", "to_edge": "b"},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "matched.csv"
            xml_path = tmp_path / "trips.xml"
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["new_trip_id", "dep_time", "from_edge", "to_edge"], delimiter=";")
                writer.writeheader()
                writer.writerows(rows)

            total, written = write_sumo_trips_xml(csv_path, xml_path)
            xml = xml_path.read_text(encoding="utf-8")

        self.assertEqual(total, 4)
        self.assertEqual(written, 1)
        self.assertIn('id="valid" depart="08:15:00"', xml)
        self.assertNotIn('depart="0"', xml)
        self.assertNotIn('id="blank"', xml)
        self.assertNotIn('id="nan"', xml)
        self.assertNotIn('id="zero"', xml)

    def test_keeps_explicit_midnight_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "matched.csv"
            xml_path = tmp_path / "trips.xml"
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["new_trip_id", "dep_time", "from_edge", "to_edge"], delimiter=";")
                writer.writeheader()
                writer.writerow({"new_trip_id": "midnight", "dep_time": "00:00:00", "from_edge": "a", "to_edge": "b"})

            total, written = write_sumo_trips_xml(csv_path, xml_path)
            xml = xml_path.read_text(encoding="utf-8")

        self.assertEqual(total, 1)
        self.assertEqual(written, 1)
        self.assertIn('id="midnight" depart="00:00:00"', xml)


if __name__ == "__main__":
    unittest.main()
