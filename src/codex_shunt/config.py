"""Configuration loading for Codex Shunt."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11
    raise RuntimeError("Codex Shunt requires Python 3.11 or newer") from exc


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "config" / "defaults.toml"
ALLOWED_MODES = {"off", "shadow", "enforce"}


def get_data_dir() -> Path:
    override = os.environ.get("CODEX_SHUNT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".local" / "share" / "codex-shunt"


def user_config_path() -> Path:
    return get_data_dir() / "config.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Configuration at {path} must contain a TOML table")
    return payload


def load_config() -> dict[str, Any]:
    config = _read_toml(DEFAULT_CONFIG_PATH)
    config.update(_read_toml(user_config_path()))
    if os.environ.get("CODEX_SHUNT_MODE"):
        config["mode"] = os.environ["CODEX_SHUNT_MODE"]
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if config.get("mode") not in ALLOWED_MODES:
        raise ValueError("mode must be one of: off, shadow, enforce")
    for key in (
        "min_file_lines",
        "min_source_bytes",
        "max_source_files",
        "max_source_bytes",
        "max_output_chars",
        "post_tool_min_bytes",
        "worker_timeout_seconds",
    ):
        value = config.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("post_tool_compression", "retain_raw_worker_events"):
        if not isinstance(config.get(key), bool):
            raise ValueError(f"{key} must be true or false")
    for key in ("worker_model", "reasoning_effort"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"{key} must be a non-empty string")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Unsupported configuration value: {value!r}")


def save_user_config(config: dict[str, Any]) -> Path:
    validate_config(config)
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{key} = {_toml_value(value)}\n" for key, value in config.items())
    path.write_text(body, encoding="utf-8")
    return path


def parse_config_value(key: str, raw: str, current: dict[str, Any]) -> Any:
    if key not in current:
        raise KeyError(f"Unknown configuration key: {key}")
    existing = current[key]
    if isinstance(existing, bool):
        normalized = raw.strip().lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{key} expects true or false")
        return normalized == "true"
    if isinstance(existing, int) and not isinstance(existing, bool):
        return int(raw)
    return raw
