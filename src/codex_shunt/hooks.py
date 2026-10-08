"""Fail-open automatic routing for supported read-only shell commands."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .db import record_routing_event
from .reads import parse_read, read_metrics, read_question
from .sources import collect_sources
from .worker import run_worker


def _debug(message: str) -> None:
    if os.environ.get("CODEX_SHUNT_DEBUG") == "1":
        print(f"codex-shunt: {message}", file=sys.stderr)


def _read_event() -> dict[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Hook input must be a JSON object")
    return payload


def _shell_command(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _record_route(event: dict[str, Any], decision: str, reason: str, metrics: dict[str, int]) -> None:
    try:
        record_routing_event(
            session_id=event.get("session_id"), turn_id=event.get("turn_id"),
            parent_model=event.get("model"), tool_name=event.get("tool_name"),
            decision=decision, reason=reason, **metrics,
        )
    except Exception as exc:
        _debug(f"telemetry unavailable: {exc}")


def hook_pre() -> int:
    if os.environ.get("CODEX_SHUNT_WORKER") == "1":
        return 0
    try:
        event = _read_event()
        config = load_config()
        if event.get("tool_name") != "Bash":
            return 0
        tool_input = event.get("tool_input")
        command = _shell_command(tool_input)
        if "CODEX_SHUNT_BYPASS=1" in command:
            return 0
        base = Path(str(event.get("cwd") or os.getcwd())).resolve()
        workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
        cwd = (base / Path(str(workdir)).expanduser()).resolve() if workdir else base
        read = parse_read(command, cwd, int(config["max_source_files"]))
        if read is None:
            _debug("unsupported operation; allowing original tool")
            return 0
        selection = collect_sources(
            cwd, [str(path) for path in read.paths],
            max_files=int(config["max_source_files"]), max_bytes=int(config["max_source_bytes"]),
        )
        if len(selection.files) != len(read.paths):
            return 0
        metrics = read_metrics(read, selection)
    except Exception as exc:
        _debug(f"read recognition failed open: {exc}")
        return 0

    if (metrics["source_lines"] < config["min_file_lines"]
            and metrics["source_bytes"] < config["min_source_bytes"]):
        return 0
    if not config["strict_routing"]:
        _record_route(event, "would_route", "large-read", metrics)
        return 0
    if not config["source_sharing_acknowledged"]:
        _record_route(event, "route_failed", "source-sharing-not-acknowledged", metrics)
        return 0
    try:
        outcome = run_worker(
            question=read_question(read, selection.root), selection=selection, config=config,
            task_kind="hook-routed-read", session_id=event.get("session_id"),
            turn_id=event.get("turn_id"), parent_model=event.get("model"),
        )
        if (outcome.result.get("needs_escalation")
                or outcome.citation_count < 1
                or outcome.valid_citation_count != outcome.citation_count):
            _record_route(event, "route_failed", "worker-needs-primary-inspection", metrics)
            return 0
        context = _format_worker_context(outcome)
    except Exception as exc:
        _record_route(event, "route_failed", type(exc).__name__, metrics)
        _debug(f"worker failed open: {exc}")
        return 0
    _record_route(event, "deny", "large-read", metrics)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": context,
    }}))
    return 0


def _format_worker_context(outcome: Any) -> str:
    result = outcome.result
    lines = [
        "Codex Shunt routed this read through its subscription-authenticated Luna worker. "
        "The original read was not executed.",
        f"Run ID: {outcome.run_id}", "", result["summary"],
    ]
    for finding in result["findings"][:20]:
        lines.append(
            f"- {finding['file']}:{finding['line_start']}-{finding['line_end']}: {finding['claim']}"
        )
    lines.extend(f"- {item}" for item in result.get("limitations", []))
    lines.append("Verify cited ranges. If needed, retry once with CODEX_SHUNT_BYPASS=1.")
    return "\n".join(lines)
