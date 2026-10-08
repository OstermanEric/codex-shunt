"""SQLite telemetry storage for Codex Shunt."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import get_data_dir
from .pricing import DEFAULT_COMPARISON_MODEL, MODEL_RATES, RATE_DATE, credits_for


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS routing_events (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    parent_model TEXT,
    tool_name TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    source_files INTEGER NOT NULL DEFAULT 0,
    source_bytes INTEGER NOT NULL DEFAULT 0,
    source_lines INTEGER NOT NULL DEFAULT 0,
    estimated_source_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_routing_created ON routing_events(created_at);
CREATE INDEX IF NOT EXISTS idx_routing_decision ON routing_events(decision);

CREATE TABLE IF NOT EXISTS worker_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    parent_model TEXT,
    worker_model TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    repository_hash TEXT NOT NULL,
    source_files INTEGER NOT NULL,
    excluded_files INTEGER NOT NULL,
    source_bytes INTEGER NOT NULL,
    source_lines INTEGER NOT NULL,
    estimated_source_tokens INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    actual_worker_credits REAL NOT NULL DEFAULT 0,
    sol_equivalent_credits REAL NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_type TEXT,
    needs_escalation INTEGER NOT NULL DEFAULT 0,
    citation_count INTEGER NOT NULL DEFAULT 0,
    valid_citation_count INTEGER NOT NULL DEFAULT 0,
    result_chars INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_worker_created ON worker_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_worker_model ON worker_runs(worker_model);

"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path() -> Path:
    return get_data_dir() / "metrics.sqlite3"


def _default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "codex-shunt"


def metrics_paths() -> list[Path]:
    """Return unique telemetry stores available to reporting commands.

    Hooks may receive a writable PLUGIN_DATA directory while terminal commands
    do not. Keep explicit overrides isolated, but discover both the normal
    terminal store and Codex plugin-data stores for the default reporting path.
    """
    override = os.environ.get("CODEX_SHUNT_DATA_DIR")
    if override:
        candidates = [Path(override).expanduser().resolve()]
    else:
        candidates = [get_data_dir(), _default_data_dir()]
        for key in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
            value = os.environ.get(key)
            if value:
                candidates.append(Path(value).expanduser().resolve())

        codex_home = Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        ).expanduser()
        plugin_data_root = codex_home / "plugins" / "data"
        try:
            candidates.extend(
                path.parent
                for path in plugin_data_root.glob("*/metrics.sqlite3")
                if path.is_file()
            )
        except OSError:
            pass

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "metrics.sqlite3").is_file() or override:
            unique.append(resolved)
    return unique


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def _connect_readonly(data_dir: Path) -> sqlite3.Connection | None:
    path = data_dir / "metrics.sqlite3"
    if not path.is_file():
        return None
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"routing_events", "worker_runs"}.issubset(tables):
            connection.close()
            return None
        return connection
    except sqlite3.Error:
        return None


def record_routing_event(**values: Any) -> str:
    event_id = values.pop("id", str(uuid.uuid4()))
    row = {
        "id": event_id,
        "created_at": values.pop("created_at", utc_now()),
        "session_id": values.pop("session_id", None),
        "turn_id": values.pop("turn_id", None),
        "parent_model": values.pop("parent_model", None),
        "tool_name": values.pop("tool_name", None),
        "decision": values.pop("decision"),
        "reason": values.pop("reason", None),
        "source_files": values.pop("source_files", 0),
        "source_bytes": values.pop("source_bytes", 0),
        "source_lines": values.pop("source_lines", 0),
        "estimated_source_tokens": values.pop("estimated_source_tokens", 0),
    }
    if values:
        raise TypeError(f"Unknown routing fields: {', '.join(values)}")
    with closing(connect()) as connection, connection:
        connection.execute(
            """INSERT INTO routing_events
            (id, created_at, session_id, turn_id, parent_model, tool_name,
             decision, reason, source_files, source_bytes, source_lines,
             estimated_source_tokens)
            VALUES (:id, :created_at, :session_id, :turn_id, :parent_model,
                    :tool_name, :decision, :reason, :source_files, :source_bytes,
                    :source_lines, :estimated_source_tokens)""",
            row,
        )
    return event_id


def record_worker_run(values: dict[str, Any]) -> str:
    run_id = values.get("id") or str(uuid.uuid4())
    row = {
        "id": run_id,
        "created_at": values.get("created_at", utc_now()),
        "session_id": values.get("session_id"),
        "turn_id": values.get("turn_id"),
        "parent_model": values.get("parent_model"),
        "worker_model": values["worker_model"],
        "task_kind": values["task_kind"],
        "question_hash": values["question_hash"],
        "repository_hash": values["repository_hash"],
        "source_files": values.get("source_files", 0),
        "excluded_files": values.get("excluded_files", 0),
        "source_bytes": values.get("source_bytes", 0),
        "source_lines": values.get("source_lines", 0),
        "estimated_source_tokens": values.get("estimated_source_tokens", 0),
        "input_tokens": values.get("input_tokens", 0),
        "cached_input_tokens": values.get("cached_input_tokens", 0),
        "output_tokens": values.get("output_tokens", 0),
        "reasoning_output_tokens": values.get("reasoning_output_tokens", 0),
        "actual_worker_credits": values.get("actual_worker_credits", 0.0),
        "sol_equivalent_credits": values.get("sol_equivalent_credits", 0.0),
        "duration_ms": values.get("duration_ms", 0),
        "status": values["status"],
        "error_type": values.get("error_type"),
        "needs_escalation": int(bool(values.get("needs_escalation", False))),
        "citation_count": values.get("citation_count", 0),
        "valid_citation_count": values.get("valid_citation_count", 0),
        "result_chars": values.get("result_chars", 0),
    }
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    with closing(connect()) as connection, connection:
        connection.execute(
            f"INSERT INTO worker_runs ({columns}) VALUES ({placeholders})", row
        )
    return run_id




def since_timestamp(raw: str) -> str | None:
    normalized = raw.strip().lower()
    if normalized == "all":
        return None
    if normalized.endswith("d") and normalized[:-1].isdigit():
        delta = timedelta(days=int(normalized[:-1]))
    elif normalized.endswith("h") and normalized[:-1].isdigit():
        delta = timedelta(hours=int(normalized[:-1]))
    else:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("--since expects all, <N>d, <N>h, or an ISO timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return (datetime.now(timezone.utc) - delta).isoformat()


def fetch_rows(table: str, since: str) -> list[dict[str, Any]]:
    if table not in {"routing_events", "worker_runs"}:
        raise ValueError(f"Unsupported table: {table}")
    start = since_timestamp(since)
    query = f"SELECT * FROM {table}"
    parameters: tuple[Any, ...] = (start,) if start else ()
    if start:
        query += " WHERE created_at >= ?"
    query += " ORDER BY created_at DESC"

    rows_by_id: dict[str, dict[str, Any]] = {}
    for data_dir in metrics_paths():
        connection = _connect_readonly(data_dir)
        if connection is None:
            continue
        try:
            for row in connection.execute(query, parameters).fetchall():
                rendered = dict(row)
                rows_by_id.setdefault(str(rendered["id"]), rendered)
        except sqlite3.Error:
            pass
        finally:
            connection.close()
    return sorted(rows_by_id.values(), key=lambda row: row["created_at"], reverse=True)


def aggregate(since: str, comparison_model: str = DEFAULT_COMPARISON_MODEL) -> dict[str, Any]:
    if comparison_model not in MODEL_RATES:
        raise ValueError(f"Unknown comparison model: {comparison_model}")
    routing_rows = fetch_rows("routing_events", since)
    all_workers = fetch_rows("worker_runs", since)
    worker_rows = [row for row in all_workers if row["task_kind"] != "setup-smoke"]
    successful = [row for row in worker_rows if row["status"] == "success"
                  and not row.get("error_type") and not row.get("needs_escalation")]
    enforced = [row for row in routing_rows if row["decision"] == "deny"]
    routing = {
        "eligible": sum(row["decision"] in {"deny", "would_route", "route_failed"} for row in routing_rows),
        "routed": len(enforced),
        "observed": sum(row["decision"] == "would_route" for row in routing_rows),
        "fallbacks": sum(row["decision"] == "route_failed" for row in routing_rows),
        "estimated_source_tokens_intercepted": sum(int(row["estimated_source_tokens"] or 0) for row in enforced),
    }
    workers = {
        "total": len(worker_rows), "succeeded": len(successful),
        "failed": sum(row["status"] == "failed" or bool(row.get("error_type")) for row in worker_rows),
        "escalations": sum(bool(row.get("needs_escalation")) for row in worker_rows),
        "average_duration_ms": sum(int(row["duration_ms"] or 0) for row in worker_rows) / len(worker_rows)
        if worker_rows else 0,
        "estimated_credits": sum(float(row["actual_worker_credits"] or 0) for row in worker_rows),
        "citations": sum(int(row["citation_count"] or 0) for row in worker_rows),
        "valid_citations": sum(int(row["valid_citation_count"] or 0) for row in worker_rows),
    }
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
        workers[key] = sum(int(row[key] or 0) for row in worker_rows)
    equivalent = sum(credits_for(row, MODEL_RATES[comparison_model]) for row in successful)
    savings = equivalent - workers["estimated_credits"]
    models: dict[str, dict[str, Any]] = {}
    for row in worker_rows:
        model = row["worker_model"]
        summary = models.setdefault(model, {"worker_model": model, "runs": 0, "tokens": 0, "estimated_credits": 0.0})
        summary["runs"] += 1
        # Reasoning tokens are a subset of output tokens, not an additional charge.
        summary["tokens"] += int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
        summary["estimated_credits"] += float(row["actual_worker_credits"] or 0)
    return {
        "since": since, "routing": routing, "workers": workers,
        "by_model": sorted(models.values(), key=lambda row: row["runs"], reverse=True),
        "setup_runs_excluded": len(all_workers) - len(worker_rows),
        "comparison": {
            "model": comparison_model, "rate_date": RATE_DATE,
            "estimated_equivalent_credits": equivalent, "estimated_savings_credits": savings,
            "estimated_savings_rate": savings / equivalent if equivalent else None,
            "basis": "Successful worker tokens repriced at standard rates; all real worker costs included. "
                     "Setup excluded. Not measured account charges or end-to-end savings.",
        },
    }
