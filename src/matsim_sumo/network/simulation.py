"""One way to simulate the full-traffic stages, shared by every stage after the repair.

The signal stage, the behaviour calibration, the hotspot check and the
``sumo-gui`` configuration all route the trips once with ``duarouter`` and
then call SUMO with the options below. A number reported by one stage can
therefore be reproduced in the GUI.

Stuck vehicles are counted from SUMO's ``--statistic-output`` (``teleports
total``), not from warnings in the log: a collision is one teleport, and a
vehicle that could not enter the network is reported as ``waiting``.

The routes are checked lane by lane after ``duarouter`` (``lane_routes.py``):
a route that needs a lane change the road markings forbid is replaced.

``SIMULATION_OPTIONS``:

* ``--device.rerouting.probability 1`` and ``--device.rerouting.period 300``:
  every vehicle chooses its route at departure and every five minutes from the
  current travel times, instead of all copies of a trip driving the empty-network
  shortest path.
* ``--tls.actuated.jam-threshold 30``: a detector occupied for 30 s or more
  counts as jammed. SUMO then also uses detectors on lanes that are shared by
  movements with different signal phases, so every actuated link is controlled.
* Collisions use SUMO's default action (the vehicle is teleported once).

Routes from an iterative assignment (``route_assignment.py``) are simulated
with ``rerouting=False``: the rerouting device would replace them at departure.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from matsim_sumo.network.lane_routes import fix_routes

REROUTING_PERIOD_S = 300
JAM_THRESHOLD_S = 30
SIMULATION_OPTIONS: tuple[str, ...] = (
    "--device.rerouting.probability",
    "1",
    "--device.rerouting.period",
    str(REROUTING_PERIOD_S),
    "--tls.actuated.jam-threshold",
    str(JAM_THRESHOLD_S),
    "--ignore-route-errors",
    "true",
)
WITHOUT_REROUTING: tuple[str, ...] = SIMULATION_OPTIONS[4:]
STATISTIC_FIELDS = {
    "vehicles": ("loaded", "inserted", "running", "waiting"),
    "teleports": ("total", "jam", "yield", "wrongLane"),
    "safety": ("collisions", "emergencyStops", "emergencyBraking"),
    "vehicleTripStatistics": ("count", "duration", "waitingTime", "timeLoss", "departDelay"),
}


def route_trips(net: Path, trips: Path, output: Path, timeout: int = 7200) -> int:
    """Route the trips on ``net``, then make every route drivable (``lane_routes``).

    Returns the number of trips without any path; they are left out.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "duarouter",
            "-n",
            str(net),
            "-r",
            str(trips),
            "--repair",
            "true",
            "--ignore-errors",
            "true",
            "--no-step-log",
            "true",
            "-o",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    text = completed.stdout + completed.stderr
    output.with_suffix(".routing.txt").write_text(text, encoding="utf-8")
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError(text[-1500:])
    output.with_suffix(".alt.xml").unlink(missing_ok=True)
    lanes = fix_routes(net, output)
    output.with_suffix(".lanes.json").write_text(json.dumps(lanes, indent=1) + "\n", encoding="utf-8")
    print(
        f"  {lanes['undrivable']} of {lanes['routes']} routes need a banned lane change, "
        f"{lanes['rerouted']} replaced by a drivable route; {lanes['fixed_route']} routes use one of "
        f"{lanes['trap_edges']} edges where a router could choose an undrivable route and keep their route",
        flush=True,
    )
    wanted = len(re.findall(r"<(?:trip|vehicle)\b", trips.read_text(encoding="utf-8")))
    return wanted - lanes["routes"]


def sumo_options(
    end_s: int,
    teleport_s: int,
    additional: tuple[Path, ...] = (),
    extra: tuple[str, ...] = (),
    seed: int | None = None,
    rerouting: bool = True,
) -> list[str]:
    options = [
        "-b",
        "0",
        "-e",
        str(end_s),
        "--time-to-teleport",
        str(teleport_s),
        "--time-to-teleport.highways",
        str(teleport_s),
        *(SIMULATION_OPTIONS if rerouting else WITHOUT_REROUTING),
    ]
    if additional:
        options += ["-a", ",".join(str(path.resolve()) for path in additional)]
    if seed is not None:
        options += ["--seed", str(seed)]
    return options + list(extra)


def simulate(
    net: Path,
    routes: Path,
    log: Path,
    end_s: int,
    teleport_s: int,
    additional: tuple[Path, ...] = (),
    extra: tuple[str, ...] = (),
    seed: int | None = None,
    timeout: int = 14400,
    rerouting: bool = True,
) -> dict:
    """Run SUMO and return its statistics; ``log`` keeps the warnings, ``<log>.stats.xml`` the statistics."""
    log.parent.mkdir(parents=True, exist_ok=True)
    stats = log.with_suffix(".stats.xml")
    command = [
        "sumo",
        "-n",
        str(net),
        "-r",
        str(routes),
        *sumo_options(end_s, teleport_s, additional, extra, seed, rerouting),
        "--no-step-log",
        "true",
        "--duration-log.statistics",
        "true",
        "--log",
        str(log),
        "--statistic-output",
        str(stats),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
    return read_statistics(stats)


def read_statistics(path: Path) -> dict:
    """The numbers of a SUMO ``--statistic-output`` file, with the shares used by the stages."""
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    result: dict = {}
    for element, fields in STATISTIC_FIELDS.items():
        match = re.search(rf"<{element}\b([^>]*)>", text)
        attributes = dict(re.findall(r'(\w+)="([^"]*)"', match.group(1))) if match else {}
        for field in fields:
            if element == "teleports":
                key = "teleports" if field == "total" else f"teleports_{field}"
            elif element == "vehicleTripStatistics":
                key = f"trip_{field}"
            else:
                key = field
            value = attributes.get(field)
            result[key] = None if value is None else (float(value) if "." in value else int(value))
    teleports, inserted, loaded = result["teleports"], result["inserted"], result["loaded"]
    if teleports is not None:
        result["teleports_collision"] = teleports - sum(
            result[f"teleports_{kind}"] or 0 for kind in ("jam", "yield", "wrongLane")
        )
    result["teleported_percent"] = round(100.0 * teleports / inserted, 2) if inserted and teleports is not None else None
    waiting = result["waiting"]
    result["not_inserted_percent"] = round(100.0 * waiting / loaded, 2) if loaded and waiting is not None else None
    return result


def write_config(
    path: Path,
    net: Path,
    routes: Path,
    end_s: int,
    teleport_s: int,
    additional: tuple[Path, ...] = (),
    extra: tuple[str, ...] = (),
    seed: int | None = None,
    rerouting: bool = True,
) -> Path:
    """A ``.sumocfg`` that simulates exactly what :func:`simulate` simulates."""
    options = sumo_options(end_s, teleport_s, additional, extra, seed, rerouting)
    pairs = dict(zip(options[0::2], options[1::2]))
    names = {"-b": "begin", "-e": "end", "-a": "additional-files", "--seed": "seed"}
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<configuration>",
        "    <input>",
        f'        <net-file value="{net.resolve()}"/>',
        f'        <route-files value="{routes.resolve()}"/>',
    ]
    if "-a" in pairs:
        lines.append(f'        <additional-files value="{pairs.pop("-a")}"/>')
    lines.append("    </input>")
    for flag, value in pairs.items():
        lines.append(f'    <{names.get(flag, flag.lstrip("-"))} value="{value}"/>')
    lines += ["</configuration>", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
