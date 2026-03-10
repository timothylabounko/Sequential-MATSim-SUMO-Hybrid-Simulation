def normalize_modes(modes_raw: object) -> list[str]:
    """Normalize config/CLI mode definitions into a clean list."""
    if modes_raw is None:
        return ["car"]

    if isinstance(modes_raw, str):
        items = [modes_raw]
    else:
        try:
            items = list(modes_raw)
        except TypeError:
            items = [str(modes_raw)]

    modes = []
    for item in items:
        for part in str(item).split(","):
            mode = part.strip()
            if mode:
                modes.append(mode)

    return modes or ["car"]
