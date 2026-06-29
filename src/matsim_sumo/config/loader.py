from pathlib import Path
from typing import Any

import yaml

from matsim_sumo.config.defaults import default_extract_events_script_path
from matsim_sumo.config.schema import validate_config


def load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a YAML mapping: {config_path}")
    return data


def apply_defaults(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    out.setdefault("csv_gzip_compresslevel", 1)
    out.setdefault("extract_events_script", str(default_extract_events_script_path()))
    out.setdefault("sumo_match_road_type_enabled", True)
    out.setdefault("sumo_match_corridor_radius_m", 80.0)
    out.setdefault("sumo_match_min_dot", 0.3)
    out.setdefault("sumo_match_inside_max_endpoint_distance_m", 150.0)
    out.setdefault("sumo_match_pass_through_min_route_distance_m", 0.0)
    return out


def load_config(path: str | Path, *, no_cache: bool = False) -> dict[str, Any]:
    config = apply_defaults(load_yaml(path))
    config["no_cache"] = bool(no_cache)
    validate_config(config)
    return config


def write_config_copy(config: dict[str, Any], output_dir: str | Path) -> Path:
    path = Path(output_dir) / "pipeline_config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(config, f, sort_keys=False)
    return path
