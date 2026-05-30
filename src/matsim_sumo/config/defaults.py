from importlib.resources import files
from pathlib import Path


def default_extract_events_script_path() -> Path:
    """Return the packaged shell helper used for MATSim event extraction."""
    return Path(str(files("matsim_sumo.resources.shell").joinpath("extract_entered_link_events.sh")))
