import xml.etree.ElementTree as ET

from matsim_sumo.network import zero_teleport_final as zf


def _logic(*states):
    logic = ET.Element("tlLogic", {"id": "t", "programID": "webster", "type": "actuated"})
    for duration, state, *bounds in states:
        attributes = {"duration": str(duration), "state": state}
        if bounds:
            attributes.update({"minDur": str(bounds[0]), "maxDur": str(bounds[1])})
        ET.SubElement(logic, "phase", attributes)
    return logic


def test_every_lane_gets_the_movement_next_to_the_lanes_that_have_it():
    assert zf.fill_lanes({0: [0]}, [0, 1, 2]) == {1: 0, 2: 0}
    assert zf.fill_lanes({2: [1, 2]}, [0, 1, 2]) == {0: 1, 1: 1}
    assert zf.fill_lanes({0: [0], 2: [1]}, [0, 1, 2]) == {1: 0}
    assert zf.fill_lanes({}, [0, 1]) == {}


def test_a_stuck_movement_gets_a_protected_green_after_its_green():
    logic = _logic((20, "GgG", 5, 30), (3, "yyy"), (20, "rrr", 5, 30))
    assert zf.protect(logic, stuck={1}, foes={2}) == 1
    states = [(phase.get("state"), phase.get("minDur")) for phase in logic.findall("phase")]
    assert states == [("GgG", "5"), ("Ggy", None), ("GGr", str(zf.PROTECTED_MIN_S)), ("yyy", None), ("rrr", "5")]


def test_no_protected_green_without_a_conflicting_movement():
    logic = _logic((20, "Ggr", 5, 30), (3, "yyr"))
    assert zf.protect(logic, stuck={1}, foes={2}) == 0
    assert len(logic.findall("phase")) == 2


def test_a_jammed_lane_gets_a_longer_maximum_green():
    logic = _logic((20, "Gr", 5, 30), (3, "yr"), (20, "rG", 5, 30), (10, "Gr"))
    assert zf.raise_green(logic, {0}) == 2
    phases = logic.findall("phase")
    assert phases[0].get("maxDur") == "45" and phases[2].get("maxDur") == "30" and phases[3].get("duration") == "15"


def test_final_options_keep_the_driver_and_switch_teleports_to_blockers_and_warnings():
    options = zf.final_options({"time-to-impatience": 120, "ignore-junction-blocker": 0})
    pairs = dict(zip(options[0::2], options[1::2]))
    assert pairs["--time-to-impatience"] == "120"
    assert pairs["--ignore-junction-blocker"] == str(zf.BLOCKER_S)
    assert pairs["--collision.action"] == "warn"


def test_a_vehicle_counts_once_and_at_the_lane_front_if_it_ever_stood_there():
    stuck = {}
    zf.record_stuck(stuck, "v", 300, "a_0", False, 330, 23)
    zf.record_stuck(stuck, "v", 360, "b_0", True, 390, 23)
    assert len(stuck) == 1
    assert stuck["v"]["front"] and stuck["v"]["lane"] == "b_0" and stuck["v"]["longest_s"] == 360
    assert stuck["v"]["first_s"] == 330


def test_stuck_events_include_collisions_and_emergency_stops():
    log = (
        "Warning: Teleporting vehicle 'a'; waited too long (yield), lane='e_0', time=10.00.\n"
        "Warning: Teleporting vehicle 'b'; collision with vehicle 'c', lane='f_0', gap=-0.16, time=651.00, stage=move.\n"
        "Warning: Vehicle 'd' performs emergency stop at the end of lane 'g_1' because there is no connection"
        " to the next edge (decel=-13.35, offset=0.94), time=2970.00.\n"
    )
    assert [(e["vehicle"], e["kind"], e["lane"]) for e in zf.stuck_events(log)] == [
        ("a", "yield", "e_0"),
        ("b", "collision", "f_0"),
        ("d", "no connection", "g_1"),
    ]
