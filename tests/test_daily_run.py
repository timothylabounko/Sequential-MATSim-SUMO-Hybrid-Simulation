import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from matsim_sumo.network import daily_run as dr

FINAL = """<configuration>
    <input>
        <net-file value="/n.net.xml"/>
        <route-files value="/morning.rou.xml"/>
        <additional-files value="/signals.add.xml"/>
    </input>
    <begin value="0"/>
    <end value="3600"/>
    <time-to-teleport value="-1"/>
    <ignore-junction-blocker value="60"/>
</configuration>"""


def test_vtypes_are_copied_by_id_and_others_kept():
    routes = '<routes>\n    <vType id="car" vClass="passenger"/>\n    <vType id="bus" vClass="bus"/>\n    <vehicle id="v" type="car"/>\n</routes>'
    source = '<routes>\n    <vType id="car" vClass="passenger" tau="1" sigma="0.5"/>\n</routes>'
    text, replaced = dr.copy_vtypes(routes, source)
    assert replaced == 1
    assert '<vType id="car" vClass="passenger" tau="1" sigma="0.5"/>' in text
    assert '<vType id="bus" vClass="bus"/>' in text
    assert '<vehicle id="v" type="car"/>' in text


def test_daily_config_keeps_the_final_settings():
    with tempfile.TemporaryDirectory() as tmp:
        final = Path(tmp) / "final.sumocfg"
        final.write_text(FINAL, encoding="utf-8")
        target = dr.daily_config(final, Path(tmp) / "day.rou.xml", Path(tmp) / "run")
        root = ET.parse(target).getroot()
        additional = (Path(tmp) / "run" / "edgedata.add.xml").read_text(encoding="utf-8")
    assert root.find("end").get("value") == "86400"
    assert root.find("time-to-teleport").get("value") == "-1"
    assert root.find("ignore-junction-blocker").get("value") == "60"
    assert root.find("input/route-files").get("value").endswith("day.rou.xml")
    assert root.find("input/additional-files").get("value").startswith("/signals.add.xml,")
    assert 'id="whole"' in additional and 'end="86400"' in additional
    assert 'period="3600"' in additional
