import csv
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from matsim_sumo.network import trip_ends as te

NODES = """<nodes>
    <node id="A" x="0" y="0"/> <node id="B" x="1000" y="0"/> <node id="C" x="2000" y="0"/>
    <node id="D" x="3000" y="0"/> <node id="E" x="1300" y="-400"/>
</nodes>"""
EDGES = """<edges>
    <edge id="AB" from="A" to="B" numLanes="2" speed="27"/> <edge id="BC" from="B" to="C" numLanes="2" speed="27"/>
    <edge id="CD" from="C" to="D" numLanes="2" speed="27"/> <edge id="DC" from="D" to="C" numLanes="2" speed="27"/>
    <edge id="CB" from="C" to="B" numLanes="2" speed="27"/> <edge id="BA" from="B" to="A" numLanes="2" speed="27"/>
    <edge id="BE" from="B" to="E" numLanes="1" speed="16"/>
</edges>"""


def build(tmp: Path) -> Path:
    (tmp / "n.nod.xml").write_text(NODES)
    (tmp / "e.edg.xml").write_text(EDGES)
    net = tmp / "net.xml"
    subprocess.run(["netconvert", "-n", str(tmp / "n.nod.xml"), "-e", str(tmp / "e.edg.xml"), "-o", str(net)],
                   check=True, capture_output=True)
    return net


def test_start_past_the_exit_moves_before_it_and_activity_starts_stay():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        net = build(tmp)
        trips = tmp / "trips.xml"
        trips.write_text(
            '<routes>\n'
            '    <trip id="t1" depart="0" from="BC" to="BE"/>\n'
            '    <trip id="t1_x2" depart="5" from="BC" to="BE"/>\n'
            '    <trip id="t2" depart="9" from="BC" to="BE"/>\n'
            '    <trip id="t3" depart="9" from="BC" to="CD"/>\n'
            '</routes>\n'
        )
        ends = tmp / "ends.csv"
        with ends.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "start_free", "start_x", "start_y", "end_free", "end_x", "end_y"])
            writer.writerow(["t1", 0, "", "", 1, 0, 0])
            writer.writerow(["t2", 1, 0, 0, 1, 0, 0])
            writer.writerow(["t3", 0, "", "", 1, 0, 0])
        out = tmp / "out.xml"
        counts = te.repair(net, trips, ends, out, tmp / "moves.csv")
        starts = {trip.get("id"): trip.get("from") for trip in ET.parse(out).getroot().iter("trip")}
        assert starts == {"t1": "AB", "t1_x2": "AB", "t2": "BC", "t3": "BC"}
        assert counts["starts_moved"] == 2


def test_the_walk_does_not_turn_onto_the_opposite_carriageway():
    with tempfile.TemporaryDirectory() as tmp:
        import sumolib

        net = sumolib.net.readNet(str(build(Path(tmp))))
        assert [edge for edge, _d in te.upstream_candidates(net.getEdge("AB"))] == []
        assert [edge for edge, _d in te.upstream_candidates(net.getEdge("BC"))] == ["AB"]
