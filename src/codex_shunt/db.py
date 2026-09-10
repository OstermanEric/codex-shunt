"""SQLite telemetry storage for Codex Shunt."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import get_data_dir


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

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY(run_id) REFERENCES worker_runs(id)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path() -> Path:
    return get_data_dir() / "metrics.sqlite3"


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


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
    with connect() as connection:
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
    with connect() as connection:
        connection.execute(
            f"INSERT INTO worker_runs ({columns}) VALUES ({placeholders})", row
        )
    return run_id


def record_feedback(run_id: str, verdict: str, note: str | None) -> str:
    feedback_id = str(uuid.uuid4())
    with connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM worker_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not exists:
            raise ValueError(f"Unknown worker run: {run_id}")
        connection.execute(
            "INSERT INTO feedback (id, created_at, run_id, verdict, note) VALUES (?, ?, ?, ?, ?)",
            (feedback_id, utc_now(), run_id, verdict, note),
        )
    return feedback_id


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
    if table not in {"routing_events", "worker_runs", "feedback"}:
        raise ValueError(f"Unsupported table: {table}")
    start = since_timestamp(since)
    query = f"SELECT * FROM {table}"
    parameters: tuple[Any, ...] = ()
    if start:
        query += " WHERE created_at >= ?"
        parameters = (start,)
    query += " ORDER BY created_at DESC"
    with connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def aggregate(since: str) -> dict[str, Any]:
    start = since_timestamp(since)
    where = " WHERE created_at >= ?" if start else ""
    parameters: tuple[Any, ...] = (start,) if start else ()
    with connect() as connection:
        routing = connection.execute(
            f"""SELECT COUNT(*) AS total,
                SUM(CASE WHEN decision IN ('would_route', 'deny', 'would_compress', 'compress') THEN 1 ELSE 0 END) AS routed,
                COALESCE(SUM(estimated_source_tokens), 0) AS estimated_tokens
                FROM routing_events{where}""",
            parameters,
        ).fetchone()
        workers = connection.execute(
            f"""SELECT COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS succeeded,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(reasoning_output_tokens), 0) AS reasoning_output_tokens,
                COALESCE(SUM(actual_worker_credits), 0) AS worker_credits,
                COALESCE(SUM(sol_equivalent_credits), 0) AS sol_credits,
                COALESCE(AVG(duration_ms), 0) AS average_duration_ms,
                COALESCE(SUM(citation_count), 0) AS citations,
                COALESCE(SUM(valid_citation_count), 0) AS valid_citations,
                COALESCE(SUM(needs_escalation), 0) AS escalations
                FROM worker_runs{where}""",
            parameters,
        ).fetchone()
        by_model = connection.execute(
            f"""SELECT worker_model, COUNT(*) AS runs,
                COALESCE(SUM(input_tokens + output_tokens + reasoning_output_tokens), 0) AS tokens,
                COALESCE(SUM(actual_worker_credits), 0) AS credits
                FROM worker_runs{where}
                GROUP BY worker_model ORDER BY runs DESC""",
            parameters,
        ).fetchall()
        feedback = connection.execute(
            f"""SELECT verdict, COUNT(*) AS count FROM feedback{where}
                GROUP BY verdict""",
            parameters,
        ).fetchall()
    return {
        "since": since,
        "routing": dict(routing),
        "workers": dict(workers),
        "by_model": [dict(row) for row in by_model],
        "feedback": [dict(row) for row in feedback],
    }
