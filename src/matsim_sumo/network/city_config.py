"""City configuration of the zero-teleport pipeline.

One TOML file per city (``cities/<city>.toml``) holds everything that differs
between cities: where the MATSim scenario and the SUMO network are, the area
and time zone, the simulated hour, and the demand levels. The algorithm
itself is the same for every city. Paths may start with ``~``; relative paths
are read from the folder of the configuration file.

``--env`` prints the configuration as shell ``export`` lines for
``scripts/mac_zero_teleport_pipeline.sh``.
"""

from __future__ import annotations

import argparse
import shlex
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULTS = {
    "city": {"name": "", "timezone": "", "lefthand": False, "german_urban_speeds": False},
    "area": {},
    "inputs": {
        "work_dir": "",
        "matsim_dir": "",
        "matsim_plans": "output_plans.xml.gz",
        "matsim_network": "output_network.xml.gz",
        "matsim_config": "",
        "sumo_net": "",
        "osm": "",
    },
    "demand": {"from": "07:00:00", "to": "08:00:00", "modes": "car,truck", "levels": [0.25, 0.5, 1.0], "sample_share": 0.0},
    "validation": {"counts": "", "count_year": 2024},
}
AREA_KEYS = ("west", "south", "east", "north")


def _path(value: str, base: Path) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def load(path: Path) -> dict:
    """The configuration with defaults filled in and paths made absolute; raises ValueError when it is incomplete."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    config = {section: {**values, **raw.get(section, {})} for section, values in DEFAULTS.items()}
    base = path.parent
    for key in ("work_dir", "matsim_dir", "sumo_net", "osm"):
        config["inputs"][key] = _path(config["inputs"][key], base)
    matsim_dir = Path(config["inputs"]["matsim_dir"]) if config["inputs"]["matsim_dir"] else base
    for key in ("matsim_plans", "matsim_network", "matsim_config"):
        config["inputs"][key] = _path(config["inputs"][key], matsim_dir)

    problems = []
    if not config["city"]["name"]:
        problems.append("city.name is missing")
    try:
        ZoneInfo(config["city"]["timezone"])
    except Exception:
        problems.append(f"city.timezone {config['city']['timezone']!r} is not a time zone such as Europe/Zurich")
    for key in ("work_dir", "matsim_dir"):
        if not config["inputs"][key]:
            problems.append(f"inputs.{key} is missing")
    if not config["inputs"]["sumo_net"] and not all(key in config["area"] for key in AREA_KEYS):
        problems.append("give inputs.sumo_net, or area.west/south/east/north to build the network from OpenStreetMap")
    levels = config["demand"]["levels"]
    if not levels or any(not 0 < float(level) <= 1 for level in levels) or float(levels[-1]) != 1.0:
        problems.append("demand.levels must be shares of the population between 0 and 1 that end with 1.0")
    if sorted(levels) != list(levels):
        problems.append("demand.levels must be in increasing order")
    if not 0 <= float(config["demand"]["sample_share"]) <= 1:
        problems.append("demand.sample_share must be between 0 and 1 (0: read it from the MATSim config)")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return config


def shell_exports(config: dict) -> list[str]:
    area = config["area"]
    values = {
        "CITY_NAME": config["city"]["name"],
        "CITY_TIMEZONE": config["city"]["timezone"],
        "CITY_LEFTHAND": "1" if config["city"]["lefthand"] else "0",
        "CITY_GERMAN_URBAN_SPEEDS": "1" if config["city"]["german_urban_speeds"] else "0",
        "CITY_AREA": ",".join(str(area[key]) for key in AREA_KEYS) if all(key in area for key in AREA_KEYS) else "",
        "WORK_DIR": config["inputs"]["work_dir"],
        "MATSIM_PLANS": config["inputs"]["matsim_plans"],
        "MATSIM_NETWORK": config["inputs"]["matsim_network"],
        "MATSIM_CONFIG": config["inputs"]["matsim_config"],
        "MATSIM_DIR": config["inputs"]["matsim_dir"],
        "SUMO_NET": config["inputs"]["sumo_net"],
        "OSM_FILE": config["inputs"]["osm"],
        "DEMAND_FROM": config["demand"]["from"],
        "DEMAND_TO": config["demand"]["to"],
        "DEMAND_MODES": config["demand"]["modes"],
        "DEMAND_LEVELS": " ".join(f"{float(level):g}" for level in config["demand"]["levels"]),
        "SAMPLE_SHARE": f"{float(config['demand']['sample_share']):g}",
        "COUNTS": config["validation"]["counts"],
        "COUNT_YEAR": str(config["validation"]["count_year"]),
    }
    return [f"export {key}={shlex.quote(value)}" for key, value in values.items()]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path)
    parser.add_argument("--env", action="store_true", help="Print shell export lines")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.env:
        print("\n".join(shell_exports(config)))
    else:
        for section, values in config.items():
            for key, value in values.items():
                print(f"{section}.{key} = {value}")


if __name__ == "__main__":
    main()
