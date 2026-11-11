import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

from matsim_sumo.network import behaviour_calibration as bc

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scale_trips.py"
spec = importlib.util.spec_from_file_location("scale_trips", SCRIPT)
scale_trips = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scale_trips)


def _root(departs):
    trips = "".join(f'<trip id="t{i}" type="car" depart="{d}" from="a" to="b"/>' for i, d in enumerate(departs))
    return ET.fromstring(f'<routes><vType id="car"/>{trips}</routes>')


def test_originals_are_kept_and_clones_stay_in_the_window():
    _types, trips, counts = scale_trips.scale_trips(_root([-100.0, 10.0, 1800.0, 3700.0]), 10, 1800, 3600)
    ids = {trip.get("id") for trip in trips}
    assert {"t1", "t2"} <= ids and "t0" not in ids and "t3" not in ids
    assert all(0 <= float(trip.get("depart")) < 3600 for trip in trips)
    assert counts["original"] == 2
    assert counts["clone"] + counts["clone outside the window"] == 4 * 9
    assert all(trip.get("departLane") == "best" and trip.get("departSpeed") == "avg" for trip in trips)


def test_clone_offsets_are_within_thirty_minutes():
    _types, trips, _counts = scale_trips.scale_trips(_root([1800.0]), 50, 1800, 3600)
    assert all(abs(float(trip.get("depart")) - 1800) <= 1800 for trip in trips)
    assert len({trip.get("id") for trip in trips}) == len(trips)


def test_sample_share_from_eqasim_config(tmp_path):
    config = tmp_path / "output_config.xml"
    config.write_text('<config><module name="eqasim"><param name="sampleSize" value="0.25" /></module></config>')
    assert scale_trips.sample_share(config) == (0.25, "sampleSize")
    config.write_text('<config><module name="qsim"><param name="flowCapacityFactor" value="0.1" /></module></config>')
    assert scale_trips.sample_share(config) == (0.1, "flowCapacityFactor")


def test_behaviour_is_set_on_every_vehicle_type_only():
    text = '<routes><vType id="car" vClass="passenger"/><vType id="truck" length="12"/><trip id="x"/></routes>'
    individual = tuple(0.9 if gene.name == "tau" else gene.default for gene in bc.GENES)
    out = bc.with_behaviour(text, individual)
    assert out.count('tau="0.9"') == 2
    assert 'length="12"' in out and '<trip id="x"/>' in out
    assert "time-to-impatience" not in out


def test_options_never_touch_traffic_lights():
    for individual in (bc.default_individual(), bc.baggenstos_individual()):
        assert not any("tls" in option or "red" in option.lower() for option in bc.sumo_options(individual))
    assert bc.sumo_options(bc.default_individual())[-1] == "-1"


def test_fitness_penalises_travel_time_bias_beyond_tolerance():
    error = bc.travel_time_error({"a": 120.0, "b": 240.0}, {"a": 100.0, "b": 200.0})
    assert error["bias_percent"] == 20.0
    assert bc.score(1.5, 0.5, error["bias_percent"], 10.0) == 12.0
    assert bc.score(1.5, 0.0, -5.0, 10.0) == 1.5
    assert bc.score(None, 0.0, 0.0, 10.0) == float("inf")


def test_zero_means_never_for_keep_clear_and_blocker_genes():
    individual = bc.default_individual()
    out = bc.with_behaviour('<routes><vType id="car"/></routes>', individual)
    assert 'jmIgnoreKeepClearTime="-1"' in out and 'lcLookaheadLeft="2"' in out
    options = bc.sumo_options(individual)
    assert options[options.index("--ignore-junction-blocker") + 1] == "-1"
