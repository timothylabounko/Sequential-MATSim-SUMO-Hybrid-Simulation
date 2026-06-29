import subprocess
from pathlib import Path

import pandas as pd


def _is_current(trips_path, output_path):
    if not Path(output_path).exists():
        return False
    try:
        trips = set(pd.read_csv(trips_path, usecols=["identifier"])["identifier"].astype(str))
        routed = set(pd.read_csv(output_path, usecols=["identifier"])["identifier"].astype(str))
        return trips == routed
    except Exception:
        return False


def route_csv(config_path, trips_path, events_path, output_path, matsim_dir, jar_path, threads=4, return_links=True, java_xmx="8g"):
    for path in map(Path, [config_path, trips_path, events_path, jar_path]):
        if not path.exists():
            raise FileNotFoundError(f"Missing MATSim router input: {path}")
    if _is_current(trips_path, output_path):
        return Path(output_path)
    Path(output_path).unlink(missing_ok=True)

    subprocess.run([
        "java", f"-Xmx{java_xmx}", "-cp", str(Path(jar_path).resolve()),
        "org.eqasim.core.tools.routing.TripsRouter",
        "--config-path", str(Path(config_path).resolve()),
        "--trips-path", str(Path(trips_path).resolve()),
        "--events-path", str(Path(events_path).resolve()),
        "--output-path", str(Path(output_path).resolve()),
        "--threads", str(min(int(threads), 6)),
        "--return-links", str(return_links).lower(),
    ], cwd=Path(matsim_dir).resolve(), check=True)
    return Path(output_path)
