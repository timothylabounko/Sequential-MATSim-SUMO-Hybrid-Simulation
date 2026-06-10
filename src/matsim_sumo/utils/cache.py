from __future__ import annotations

import functools
import hashlib
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


CACHE_DIR = Path(".cache")


@dataclass(frozen=True)
class FileDep:
    path: Path


@dataclass(frozen=True)
class ParamDep:
    name: str
    value: Any


def file_dep(path: str | Path) -> FileDep:
    return FileDep(Path(path))


def param_dep(name: str, value: Any) -> ParamDep:
    return ParamDep(name, value)


def _file_fingerprint(path: Path) -> tuple[str, str, int | None, str | None]:
    path = path.resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ("file", str(path), None, None)
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return ("file", str(path), int(stat.st_size), hasher.hexdigest())


def _normalize_dep(dep: Any) -> tuple[Any, ...]:
    if isinstance(dep, FileDep):
        return _file_fingerprint(dep.path)
    if isinstance(dep, ParamDep):
        return ("param", dep.name, repr(dep.value))
    if isinstance(dep, Path):
        return _file_fingerprint(dep)
    return ("param", repr(dep))


def _safe_step_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _config_disables_cache(values: Iterable[Any]) -> bool:
    for value in values:
        config = getattr(value, "config", None)
        if isinstance(config, dict) and (config.get("no_cache") or config.get("cache_enabled") is False):
            return True
    return False


def disk_cache(
    *,
    depends_on: Callable[..., Iterable[Any]],
    step_name: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        name = step_name or func.__name__
        safe_name = _safe_step_name(name)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            call_kwargs = dict(kwargs)
            explicit_no_cache = bool(call_kwargs.pop("no_cache", False))
            explicit_cache_enabled = call_kwargs.pop("cache_enabled", None)
            disabled = (
                explicit_no_cache
                or explicit_cache_enabled is False
                or _config_disables_cache([*args, *call_kwargs.values()])
            )

            deps = tuple(_normalize_dep(dep) for dep in depends_on(*args, **call_kwargs))
            key = hashlib.sha256(repr(deps).encode("utf-8")).hexdigest()
            short_key = key[:12]
            cache_path = CACHE_DIR / f"{safe_name}-{key}.pkl"

            if disabled:
                wrapper.last_cache_status = "bypass"
                print(f"CACHE BYPASS {name} {short_key}")
                return func(*args, **call_kwargs)

            if cache_path.exists():
                try:
                    with cache_path.open("rb") as f:
                        result = pickle.load(f)
                    wrapper.last_cache_status = "hit"
                    print(f"CACHE HIT {name} {short_key}")
                    return result
                except Exception:
                    pass

            wrapper.last_cache_status = "miss"
            print(f"CACHE MISS {name} {short_key}")
            result = func(*args, **call_kwargs)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(".tmp")
            with tmp_path.open("wb") as f:
                pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp_path.replace(cache_path)
            return result

        wrapper.last_cache_status = None
        return wrapper

    return decorate


def output_needs_refresh(cached_func: Callable[..., Any], path: str | Path) -> bool:
    return getattr(cached_func, "last_cache_status", None) != "hit" or not Path(path).exists()
