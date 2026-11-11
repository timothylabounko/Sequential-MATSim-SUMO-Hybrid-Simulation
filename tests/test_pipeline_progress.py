import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from matsim_sumo.network import pipeline_progress as pp


def test_each_stage_folder_gives_its_teleports():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        search = root / "round1" / "behaviour"
        search.mkdir(parents=True)
        (search / "summary.json").write_text(json.dumps({"best": {"teleports": 161.5}}))
        assignment = root / "assignment"
        assignment.mkdir()
        (assignment / "assignment.json").write_text(json.dumps({"check": {"23": {"teleports": 261}, "42": {"teleports": 300}}}))
        final = root / "final"
        final.mkdir()
        (final / "final.json").write_text(json.dumps({"results": {}}))
        assert pp.teleports(root / "round1") == 161.5
        assert pp.teleports(assignment) == 280.5
        assert pp.teleports(final) == 0.0


def test_the_sample_demand_has_its_own_lowest_share():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stage1, stage2 = root / "stage1", root / "stage2"
        stage1.mkdir()
        stage2.mkdir()
        (stage1 / "summary.json").write_text(json.dumps({"rounds": [{"round": 1, "stuck": 119}, {"round": 2, "stuck": 0}]}))
        (stage1 / "final.rou.xml").write_text("<routes>" + "<vehicle id='a'/>" * 8131 + "</routes>")
        (stage2 / "summary.json").write_text(json.dumps({"rounds": [
            {"round": 1, "stuck": 4352, "inserted": 65343}, {"round": 2, "stuck": 3611, "inserted": 65692}]}))
        args = SimpleNamespace(stage1=stage1, stage2=stage2, signals=None, behaviour=None, junctions=None,
                               rounds=[], assignment=None, final=None)
        rows = pp.table(args)
    assert [row["teleported_percent"] for row in rows] == [1.46, 0.0, 6.66, 5.5]
    assert [row["lowest_so_far_percent"] for row in rows] == [1.46, 0.0, 6.66, 5.5]
    assert [row["iteration"] for row in rows] == [1, 2, 3, 4]


def test_every_demand_level_has_its_own_lowest_share():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        levels = []
        for name, stuck, inserted in (("level-010", [119, 0], 8131), ("level-025", [300, 90], 20000), ("level-100", [3611], 65692)):
            work = root / name
            work.mkdir()
            rounds = [{"round": i + 1, "stuck": s, "inserted": inserted} for i, s in enumerate(stuck)]
            (work / "summary.json").write_text(json.dumps({"rounds": rounds}))
            levels.append(work)
        args = SimpleNamespace(levels=levels, stage1=None, stage2=None, signals=None, behaviour=None, junctions=None,
                               rounds=[], assignment=None, final=None)
        rows = pp.table(args)
    assert [row["demand_percent"] for row in rows] == [10, 10, 25, 25, 100]
    assert [row["stage"] for row in rows] == [1, 1, 2, 2, 2]
    assert [row["lowest_so_far_percent"] for row in rows] == [1.46, 0.0, 1.5, 0.45, 5.5]
    assert rows[2]["step"] == "25 % demand, round 1"
