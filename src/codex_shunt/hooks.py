"""Codex hook handlers for routing and oversized-output compression."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .db import record_routing_event
from .sources import collect_sources, count_lines, is_probably_text, is_sensitive
from .worker import WorkerError, run_worker, run_worker_for_content


READ_COMMANDS = {"awk", "bat", "cat", "head", "less", "more", "sed", "tail"}
OUTPUT_COMMAND_MARKERS = (
    " test",
    "pytest",
    "jest",
    "vitest",
    "xcodebuild",
    "gradle",
    "cargo test",
    "go test",
    "eslint",
    "lint",
    "build",
    "tsc",
)


def _debug(message: str) -> None:
    if os.environ.get("CODEX_SHUNT_DEBUG") == "1":
        print(f"codex-shunt: {message}", file=sys.stderr)


def _read_event() -> dict[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Hook input must be a JSON object")
    return payload


def _shell_command(tool_input: Any) -> str:
    """Return the command from legacy Bash or current exec_command input."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _command_paths(command: str, cwd: Path) -> list[Path]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens:
        return []

    executable_names = [Path(token).name for token in tokens if not token.startswith("-")]
    if not any(name in READ_COMMANDS for name in executable_names[:3]):
        return []

    paths: list[Path] = []
    for token in tokens:
        if token.startswith("-") or token in {"|", ";", "&&", "||"}:
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate.is_file() and candidate not in paths:
            paths.append(candidate)
    return paths


def _mcp_paths(tool_input: Any, cwd: Path) -> list[Path]:
    if not isinstance(tool_input, dict):
        return []
    raw_values: list[str] = []
    for key in ("path", "file", "file_path", "filename"):
        if isinstance(tool_input.get(key), str):
            raw_values.append(tool_input[key])
    if isinstance(tool_input.get("paths"), list):
        raw_values.extend(value for value in tool_input["paths"] if isinstance(value, str))
    paths: list[Path] = []
    for raw in raw_values:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate.is_file() and candidate not in paths:
            paths.append(candidate)
    return paths


def _bounded_read_lines(command: str, path_count: int) -> int | None:
    """Estimate an explicit head/tail/sed line range; None means unbounded."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    for index, token in enumerate(tokens):
        executable = Path(token).name
        if executable in {"head", "tail"}:
            arguments = tokens[index + 1 :]
            if any(argument == "-c" or argument.startswith("--bytes") for argument in arguments):
                return None
            lines = 10
            for position, argument in enumerate(arguments):
                if argument in {"-n", "--lines"} and position + 1 < len(arguments):
                    try:
                        lines = abs(int(arguments[position + 1]))
                    except ValueError:
                        return None
                elif argument.startswith("--lines="):
                    try:
                        lines = abs(int(argument.split("=", 1)[1]))
                    except ValueError:
                        return None
                elif re.fullmatch(r"-\d+", argument):
                    lines = int(argument[1:])
            return lines * max(path_count, 1)

        if executable == "sed":
            arguments = tokens[index + 1 :]
            if "-n" not in arguments and "--quiet" not in arguments and "--silent" not in arguments:
                return None
            for argument in arguments:
                match = re.fullmatch(r"(\d+)(?:,(\d+))?p", argument)
                if not match:
                    continue
                start = int(match.group(1))
                end = int(match.group(2) or match.group(1))
                return max(end - start + 1, 0) * max(path_count, 1)
            return None
    return None


def _source_metrics(paths: list[Path]) -> dict[str, int]:
    source_files = 0
    source_bytes = 0
    source_lines = 0
    for path in paths:
        if is_sensitive(path) or not is_probably_text(path):
            continue
        try:
            source_files += 1
            source_bytes += path.stat().st_size
            source_lines += count_lines(path)
        except OSError:
            continue
    return {
        "source_files": source_files,
        "source_bytes": source_bytes,
        "source_lines": source_lines,
        "estimated_source_tokens": (source_bytes + 3) // 4,
    }


def _record_route(event: dict[str, Any], decision: str, reason: str, metrics: dict[str, int]) -> None:
    try:
        record_routing_event(
            session_id=event.get("session_id"),
            turn_id=event.get("turn_id"),
            parent_model=event.get("model"),
            tool_name=event.get("tool_name"),
            decision=decision,
            reason=reason,
            **metrics,
        )
    except Exception as exc:  # Hooks should fail open when telemetry is unavailable.
        _debug(f"failed to record routing event: {exc}")


def hook_pre() -> int:
    if os.environ.get("CODEX_SHUNT_WORKER") == "1":
        return 0
    try:
        event = _read_event()
        config = load_config()
    except Exception as exc:
        _debug(f"pre-hook initialization failed: {exc}")
        return 0
    tool_name = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input")
    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve()
    command = _shell_command(tool_input)
    if "CODEX_SHUNT_BYPASS=1" in command or "codex-shunt" in command:
        return 0

    paths = _command_paths(command, cwd) if tool_name == "Bash" else _mcp_paths(tool_input, cwd)
    metrics = _source_metrics(paths)
    bounded_lines = _bounded_read_lines(command, len(paths)) if tool_name == "Bash" else None
    if bounded_lines is not None and bounded_lines < int(config["min_file_lines"]):
        return 0
    is_large = (
        metrics["source_lines"] >= int(config["min_file_lines"])
        or metrics["source_bytes"] >= int(config["min_source_bytes"])
    )
    if not paths or not is_large:
        return 0

    reason = "large-predictable-read"
    if not config["strict_routing"]:
        _record_route(event, "would_route", reason, metrics)
        return 0
    if not config["source_sharing_acknowledged"]:
        _record_route(event, "route_failed", "source-sharing-not-acknowledged", metrics)
        _debug("strict routing skipped: run `shunt setup` to acknowledge source sharing")
        return 0

    relative_paths: list[str] = []
    for path in paths:
        try:
            relative_paths.append(path.relative_to(cwd).as_posix())
        except ValueError:
            # Strict routing never copies files from outside the active workspace.
            _record_route(event, "route_failed", "path-outside-workspace", metrics)
            return 0

    operation = f"The parent attempted a {tool_name or 'file-read'} operation."
    question = (
        "Summarize the selected files for a primary coding agent before they enter its context. "
        "Identify their purpose, important symbols or behavior, and any evidence likely to matter "
        "to the attempted read. Do not make architecture or implementation decisions. "
        f"{operation}"
    )

    try:
        selection = collect_sources(
            cwd,
            relative_paths,
            max_files=int(config["max_source_files"]),
            max_bytes=int(config["max_source_bytes"]),
        )
        outcome = run_worker(
            question=question,
            selection=selection,
            config=config,
            task_kind="hook-routed-read",
            session_id=event.get("session_id"),
            turn_id=event.get("turn_id"),
            parent_model=event.get("model"),
        )
    except Exception as exc:
        # Strict routing is intentionally fail-open. If the bundled worker or
        # telemetry is unavailable, Codex receives the original tool result.
        _record_route(event, "route_failed", type(exc).__name__, metrics)
        _debug(f"strict routing failed open: {exc}")
        return 0

    _record_route(event, "deny", reason, metrics)
    routed_result = _format_worker_context(
        outcome,
        introduction=(
            "Codex Shunt routed this large read through its bundled, "
            "subscription-authenticated Luna worker. The original read was not executed."
        ),
        retry_note=(
            "Use targeted reads to verify cited ranges. If the worker summary is unsuitable, "
            "retry the original operation once with CODEX_SHUNT_BYPASS=1."
        ),
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": routed_result,
                }
            }
        )
    )
    return 0


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False, indent=2)


def _format_worker_context(
    outcome: Any,
    *,
    introduction: str = (
        "Codex Shunt compressed oversized command output with a "
        "subscription-authenticated Luna worker."
    ),
    retry_note: str = (
        "The original output was not retained by Codex Shunt. Rerun the command with "
        "CODEX_SHUNT_BYPASS=1 if exact raw lines are needed."
    ),
) -> str:
    result = outcome.result
    lines = [
        introduction,
        f"Run ID: {outcome.run_id}",
        "",
        result.get("summary", "No summary returned."),
    ]
    findings = result.get("findings", [])
    if findings:
        lines.extend(["", "Evidence:"])
        for finding in findings[:20]:
            lines.append(
                f"- {finding.get('file')}:{finding.get('line_start')}-{finding.get('line_end')}: "
                f"{finding.get('claim')}"
            )
    limitations = result.get("limitations", [])
    if limitations:
        lines.extend(["", "Limitations:"])
        lines.extend(f"- {item}" for item in limitations)
    lines.extend(["", retry_note])
    return "\n".join(lines)


def hook_post() -> int:
    if os.environ.get("CODEX_SHUNT_WORKER") == "1":
        return 0
    try:
        event = _read_event()
        config = load_config()
    except Exception as exc:
        _debug(f"post-hook initialization failed: {exc}")
        return 0
    tool_input = event.get("tool_input")
    command = _shell_command(tool_input)
    if not command or "CODEX_SHUNT_BYPASS=1" in command or "codex-shunt" in command:
        return 0
    lowered = f" {command.lower()}"
    if not any(marker in lowered for marker in OUTPUT_COMMAND_MARKERS):
        return 0

    content = _response_text(event.get("tool_response"))
    content_bytes = len(content.encode("utf-8", errors="replace"))
    if content_bytes < int(config["post_tool_min_bytes"]):
        return 0
    metrics = {
        "source_files": 1,
        "source_bytes": content_bytes,
        "source_lines": content.count("\n") + 1,
        "estimated_source_tokens": (content_bytes + 3) // 4,
    }
    sharing_allowed = bool(config["source_sharing_acknowledged"])
    decision = (
        "compress"
        if config["strict_routing"] and config["post_tool_compression"] and sharing_allowed
        else "would_compress"
    )
    _record_route(event, decision, "oversized-command-output", metrics)
    if decision != "compress":
        return 0

    worker_content = content
    maximum = int(config["max_source_bytes"])
    if content_bytes > maximum:
        encoded = content.encode("utf-8", errors="replace")
        head_size = int(maximum * 0.7)
        tail_size = maximum - head_size
        worker_content = (
            encoded[:head_size].decode("utf-8", errors="replace")
            + "\n\n[Codex Shunt truncated the middle of oversized output]\n\n"
            + encoded[-tail_size:].decode("utf-8", errors="replace")
        )

    try:
        outcome = run_worker_for_content(
            question=(
                "Summarize this command output for the primary coding agent. Group repeated failures, "
                "identify the earliest useful error, cite exact line ranges in tool-output.txt, and "
                "distinguish evidence from hypotheses."
            ),
            content=worker_content,
            config=config,
            task_kind="command-output",
            session_id=event.get("session_id"),
            turn_id=event.get("turn_id"),
            parent_model=event.get("model"),
        )
    except WorkerError as exc:
        _record_route(event, "compress_failed", type(exc).__name__, metrics)
        _debug(str(exc))
        return 0

    context = _format_worker_context(outcome)
    print(
        json.dumps(
            {
                "continue": False,
                "stopReason": "Oversized command output was summarized by Codex Shunt.",
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                },
            }
        )
    )
    return 0
