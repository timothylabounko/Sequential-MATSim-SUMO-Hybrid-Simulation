import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "scale_trips", Path(__file__).resolve().parents[1] / "scripts" / "scale_trips.py"
)
scale_trips = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scale_trips)

SOURCE = """<routes>
    <vType id="car" vClass="passenger"/>
    <trip id="a" type="car" depart="100.00" from="e1" to="e2"/>
    <trip id="b" type="car" depart="200.00" from="e3" to="e4"/>
</routes>"""


def test_ten_copies_with_unique_ids_and_original_kept():
    types, trips, _counts = scale_trips.scale_trips(ET.fromstring(SOURCE), 10, 30, 3600)
    assert [t.get("id") for t in types] == ["car"]
    assert len(trips) == 20
    ids = [t.get("id") for t in trips]
    assert len(set(ids)) == 20
    assert {"a", "b", "a_x9", "b_x9"} <= set(ids)
    original = next(t for t in trips if t.get("id") == "b")
    assert original.get("depart") == "200.00"


def test_copies_stay_within_the_spread_and_sorted():
    _, trips, _counts = scale_trips.scale_trips(ET.fromstring(SOURCE), 10, 30, 3600)
    departs = [float(t.get("depart")) for t in trips]
    assert departs == sorted(departs)
    for trip in trips:
        base = 100.0 if trip.get("id").startswith("a") else 200.0
        assert base - 30.0 <= float(trip.get("depart")) <= base + 30.0
        assert trip.get("from") == ("e1" if base == 100.0 else "e3")


def test_depart_lane_and_speed_and_fractional_scale():
    _, trips, _counts = scale_trips.scale_trips(ET.fromstring(SOURCE), 1.5, 30, 3600, seed=3)
    assert 2 <= len(trips) <= 4
    assert all(t.get("departLane") == "best" and t.get("departSpeed") == "avg" for t in trips)
    _, unscaled, _counts = scale_trips.scale_trips(ET.fromstring(SOURCE), 1, 30, 3600)
    assert [t.get("id") for t in unscaled] == ["a", "b"]
