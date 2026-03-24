import re
import subprocess
from pathlib import Path

from matsim_sumo.pipeline_state import PipelineState

_TRIP_RE = re.compile(r"<trip\b")
_ROUTED_RE = re.compile(r"<(?:vehicle|trip|flow)\b")


def _count_xml(path: Path, pattern: re.Pattern[str]) -> int:
    n = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            n += int(bool(pattern.search(line)))
    return n


def run_duarouter_stage(state: PipelineState) -> None:
    state.sumo_routes_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(state.config.get("duarouter_bin", "duarouter")),
            "-n",
            str(state.sumo_network_path),
            "-r",
            str(state.sumo_trips_path),
            "-o",
            str(state.sumo_routes_path),
            "--ignore-errors",
            "true",
            "--no-step-log",
            "true",
            "--no-warnings",
            "true",
        ],
        check=True,
    )
    total = int(state.sumo_trips_count) if state.sumo_trips_count is not None else _count_xml(state.sumo_trips_path, _TRIP_RE)
    routed = _count_xml(state.sumo_routes_path, _ROUTED_RE)
    pct = (100.0 * routed / total) if total else 0.0
    state.sumo_routes_input_count = total
    state.sumo_routes_count = routed
    state.sumo_routes_pct = pct
    print(f"INFO Step 10 duarouter routed {routed}/{total} trips ({pct:.2f}%)")
