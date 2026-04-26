from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir


APP_NAME = "VoidfarersClient"
APP_AUTHOR = "Voidfarers"


def default_config_path() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR)) / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or default_config_path()

    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)