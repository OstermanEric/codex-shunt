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
ALLOWED_LEGACY_MODES = {"off", "shadow", "enforce"}

SOURCE_SHARING_NOTICE = """Codex Shunt sends the contents of selected eligible text files to a
separate GPT-5.6 Luna Codex invocation using your existing ChatGPT/Codex login.
No API key is used, but the worker consumes your subscription allowance or credits.
Common secret-bearing paths and generated files are excluded, and the worker is
ephemeral and read-only. Filtering is not a guarantee: do not select credentials,
private keys, regulated data, or source you are not permitted to process with Codex.
Local telemetry stores counts and hashes, not source text, prompts, or worker answers."""


def get_data_dir() -> Path:
    override = os.environ.get("CODEX_SHUNT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).expanduser().resolve()
    return Path.home() / ".local" / "share" / "codex-shunt"


def user_config_path() -> Path:
    override = os.environ.get("CODEX_SHUNT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve() / "config.toml"
    # Keep the operator-facing config stable whether the command is invoked by
    # a plugin hook (which receives PLUGIN_DATA) or from an ordinary terminal.
    return Path.home() / ".local" / "share" / "codex-shunt" / "config.toml"


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
    plugin_config: dict[str, Any] = {}
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data and not os.environ.get("CODEX_SHUNT_DATA_DIR"):
        plugin_config = _read_toml(Path(plugin_data).expanduser().resolve() / "config.toml")
    user_config = {**plugin_config, **_read_toml(user_config_path())}
    config.update(user_config)

    # Migrate the original tri-state setting without requiring users to edit or
    # delete their preserved plugin data after an update.
    legacy_mode = user_config.get("mode")
    if "strict_routing" not in user_config and legacy_mode in ALLOWED_LEGACY_MODES:
        config["strict_routing"] = legacy_mode == "enforce"
    config.pop("mode", None)

    strict_override = os.environ.get("CODEX_SHUNT_STRICT_ROUTING")
    if strict_override is not None:
        normalized = strict_override.strip().lower()
        if normalized not in {"true", "false", "1", "0"}:
            raise ValueError("CODEX_SHUNT_STRICT_ROUTING expects true, false, 1, or 0")
        config["strict_routing"] = normalized in {"true", "1"}
    elif os.environ.get("CODEX_SHUNT_MODE"):
        mode = os.environ["CODEX_SHUNT_MODE"].strip().lower()
        if mode not in ALLOWED_LEGACY_MODES:
            raise ValueError("CODEX_SHUNT_MODE must be one of: off, shadow, enforce")
        config["strict_routing"] = mode == "enforce"

    sharing_override = os.environ.get("CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED")
    if sharing_override is not None:
        normalized = sharing_override.strip().lower()
        if normalized not in {"true", "false", "1", "0"}:
            raise ValueError(
                "CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED expects true, false, 1, or 0"
            )
        config["source_sharing_acknowledged"] = normalized in {"true", "1"}
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    for key in ("strict_routing", "source_sharing_acknowledged"):
        if not isinstance(config.get(key), bool):
            raise ValueError(f"{key} must be true or false")
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
