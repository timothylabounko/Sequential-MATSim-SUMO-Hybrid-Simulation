import os
from pathlib import Path

from matsim_sumo.network import route_assignment as ra
from matsim_sumo.network import simulation


def test_the_iteration_with_fewest_teleports_is_kept_but_never_the_empty_network_one():
    def row(iteration, teleports, not_inserted):
        return {"iteration": iteration, "teleports": teleports, "not_inserted_percent": not_inserted, "routes": __file__}

    rows = [row(0, 0, 4.0), row(1, 120, 9.0), row(2, 80, 9.5), row(3, 80, 9.1)]
    assert ra.best_iteration(rows)["iteration"] == 3
    assert ra.best_iteration([row(0, 0, 4.0)]) is None


def test_duaiterate_gets_the_simulation_settings_without_rerouting():
    os.environ.setdefault("SUMO_HOME", "/sumo")
    command = ra.dua_command(
        Path("n.net.xml"), Path("s.add.xml"), Path("demand.trips.xml"), 10, 3600, 300, ("--ignore-junction-blocker", "-1")
    )
    assert "sumo--ignore-junction-blocker=-1" in command
    assert "sumo--tls.actuated.jam-threshold=30" in command
    assert not any("rerouting" in part for part in command)
    assert command[command.index("-l") + 1] == "10"


def test_simulation_without_rerouting_keeps_the_other_settings():
    with_device = simulation.sumo_options(3600, 300)
    without = simulation.sumo_options(3600, 300, rerouting=False)
    assert "--device.rerouting.probability" in with_device
    assert "--device.rerouting.probability" not in without
    assert "--tls.actuated.jam-threshold" in without and "--ignore-route-errors" in without
