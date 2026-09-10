"""Run an isolated, subscription-authenticated Codex worker."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PLUGIN_ROOT
from .db import record_worker_run
from .sources import SourceSelection, collect_sources, copy_to_workspace


LUNA_RATES = {"input": 5.0, "cached": 0.5, "output": 30.0}
SOL_RATES = {"input": 100.0, "cached": 10.0, "output": 500.0}
MODEL_RATES = {
    "gpt-6-astra": {"input": 250.0, "cached": 25.0, "output": 1250.0},
    "gpt-5.6-sol": SOL_RATES,
    "gpt-5.6-terra": {"input": 50.0, "cached": 5.0, "output": 300.0},
    "gpt-5.6-luna": LUNA_RATES,
}


class WorkerError(RuntimeError):
    """Raised when a Luna worker cannot provide a valid result."""


@dataclass(frozen=True)
class WorkerOutcome:
    run_id: str
    worker_model: str
    result: dict[str, Any]
    usage: dict[str, int]
    duration_ms: int
    actual_credits: float
    sol_equivalent_credits: float
    citation_count: int
    valid_citation_count: int


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def find_codex() -> str:
    candidates: list[Path] = []
    override = os.environ.get("CODEX_SHUNT_CODEX_PATH")
    if override:
        candidates.append(Path(override).expanduser())

    executable = shutil.which("codex")
    if executable:
        candidates.append(Path(executable))

    home = Path.home()
    candidates.extend(
        [
            home / ".local" / "bin" / "codex",
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            home / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex",
            home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return normalized

    raise WorkerError(
        "The Codex CLI was not found on PATH or in a standard standalone/desktop location. "
        "Set CODEX_SHUNT_CODEX_PATH to the executable path or install the Codex CLI."
    )


def auth_status(codex: str | None = None) -> tuple[bool, str]:
    executable = codex or find_codex()
    environment = os.environ.copy()
    environment.pop("CODEX_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    completed = subprocess.run(
        [executable, "login", "status"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
        check=False,
    )
    message = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return completed.returncode == 0 and "chatgpt" in message.lower(), message


def ensure_chatgpt_auth(codex: str) -> None:
    if os.environ.get("CODEX_SHUNT_REQUIRE_CHATGPT_AUTH", "1") == "0":
        return
    valid, message = auth_status(codex)
    if not valid:
        detail = message or "No authentication status returned"
        raise WorkerError(
            "Codex Shunt requires `codex login` with ChatGPT subscription access. "
            f"Current status: {detail}"
        )


def parse_usage(jsonl: str) -> dict[str, int]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for raw_line in jsonl.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        incoming = event["usage"]
        usage = {
            "input_tokens": int(incoming.get("input_tokens", 0) or 0),
            "cached_input_tokens": int(incoming.get("cached_input_tokens", 0) or 0),
            "output_tokens": int(incoming.get("output_tokens", 0) or 0),
            "reasoning_output_tokens": int(
                incoming.get("reasoning_output_tokens", incoming.get("reasoning_tokens", 0)) or 0
            ),
        }
    return usage


def credits_for(usage: dict[str, int], rates: dict[str, float]) -> float:
    cached = min(usage["cached_input_tokens"], usage["input_tokens"])
    uncached = max(usage["input_tokens"] - cached, 0)
    total = (
        uncached * rates["input"]
        + cached * rates["cached"]
        + usage["output_tokens"] * rates["output"]
    )
    return total / 1_000_000


def _worker_prompt(question: str, task_kind: str, source_count: int) -> str:
    return f"""You are the read-only evidence worker for Codex Shunt.

Task kind: {task_kind}
Question: {question}

The current directory is an isolated copy containing {source_count} approved text files.
`.codex-shunt-files.txt` lists every approved source file. Analyze only those files.

Rules:
- Collect evidence; do not make architecture, security, migration, or final product decisions.
- Never edit files.
- Cite every material claim using a repository-relative file and exact line range.
- Prefer concise synthesis over reproducing source text.
- If the question is ambiguous, evidence is incomplete, or judgment should remain with the parent,
  set `needs_escalation` to true and explain the limitation.
- Do not cite `.codex-shunt-files.txt`.
- Return only the JSON object required by the output schema.
"""


def _validate_result(
    result: dict[str, Any], workspace: Path, max_output_chars: int
) -> tuple[int, int]:
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > max_output_chars:
        raise WorkerError(
            f"Worker result exceeded max_output_chars ({len(encoded)} > {max_output_chars})"
        )
    if not isinstance(result.get("summary"), str):
        raise WorkerError("Worker result is missing a string summary")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise WorkerError("Worker result is missing a findings array")

    valid = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        raw_path = finding.get("file")
        start = finding.get("line_start")
        end = finding.get("line_end")
        if not isinstance(raw_path, str) or not isinstance(start, int) or not isinstance(end, int):
            continue
        path = (workspace / raw_path).resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError:
            continue
        if not path.is_file() or path.name == ".codex-shunt-files.txt":
            continue
        try:
            line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if 1 <= start <= end <= max(line_count, 1):
            valid += 1

    if valid != len(findings):
        result["needs_escalation"] = True
        limitations = result.setdefault("limitations", [])
        limitations.append(
            f"Codex Shunt validated {valid} of {len(findings)} worker citations."
        )
    return len(findings), valid


def run_worker(
    *,
    question: str,
    selection: SourceSelection,
    config: dict[str, Any],
    task_kind: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    parent_model: str | None = None,
) -> WorkerOutcome:
    run_id = str(uuid.uuid4())
    codex = find_codex()
    worker_model = str(config["worker_model"])
    if worker_model not in MODEL_RATES:
        raise WorkerError(
            f"No bundled subscription credit rate for worker model {worker_model!r}; "
            f"choose one of: {', '.join(sorted(MODEL_RATES))}"
        )
    started = time.monotonic()
    usage = parse_usage("")
    actual_credits = 0.0
    sol_credits = 0.0
    result: dict[str, Any] | None = None
    citation_count = 0
    valid_citations = 0
    error_type: str | None = None

    try:
        ensure_chatgpt_auth(codex)
        with tempfile.TemporaryDirectory(prefix="codex-shunt-") as temporary:
            workspace = Path(temporary)
            copy_to_workspace(selection, workspace)
            manifest = "\n".join(item.relative.as_posix() for item in selection.files) + "\n"
            (workspace / ".codex-shunt-files.txt").write_text(manifest, encoding="utf-8")
            result_path = workspace / ".codex-shunt-result.json"
            schema_path = PLUGIN_ROOT / "schemas" / "worker-result.schema.json"
            command = [
                codex,
                "exec",
                "--json",
                "--ephemeral",
                "--skip-git-repo-check",
                "--disable",
                "hooks",
                "--model",
                worker_model,
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-C",
                str(workspace),
                "-c",
                f'model_reasoning_effort="{config["reasoning_effort"]}"',
                _worker_prompt(question, task_kind, len(selection.files)),
            ]
            environment = os.environ.copy()
            environment.pop("CODEX_API_KEY", None)
            environment.pop("OPENAI_API_KEY", None)
            environment["CODEX_SHUNT_WORKER"] = "1"
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=int(config["worker_timeout_seconds"]),
                env=environment,
                check=False,
            )
            usage = parse_usage(completed.stdout)
            actual_credits = credits_for(usage, MODEL_RATES[worker_model])
            sol_credits = credits_for(usage, SOL_RATES)
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout[-2000:].strip()
                raise WorkerError(f"Codex worker exited {completed.returncode}: {detail}")
            if not result_path.exists():
                raise WorkerError("Codex worker did not create its structured result")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise WorkerError(f"Codex worker returned invalid JSON: {exc}") from exc
            if not isinstance(result, dict):
                raise WorkerError("Codex worker result must be a JSON object")
            citation_count, valid_citations = _validate_result(
                result, workspace, int(config["max_output_chars"])
            )
    except subprocess.TimeoutExpired as exc:
        error_type = "timeout"
        raise WorkerError(
            f"Codex worker timed out after {config['worker_timeout_seconds']} seconds"
        ) from exc
    except Exception as exc:
        error_type = error_type or type(exc).__name__
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        record_worker_run(
            {
                "id": run_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "parent_model": parent_model,
                "worker_model": worker_model,
                "task_kind": task_kind,
                "question_hash": short_hash(question),
                "repository_hash": short_hash(str(selection.root)),
                "source_files": len(selection.files),
                "excluded_files": selection.excluded_files,
                "source_bytes": selection.source_bytes,
                "source_lines": selection.source_lines,
                "estimated_source_tokens": selection.estimated_tokens,
                **usage,
                "actual_worker_credits": actual_credits,
                "sol_equivalent_credits": sol_credits,
                "duration_ms": duration_ms,
                "status": "success" if result is not None else "failed",
                "error_type": error_type,
                "needs_escalation": bool(result and result.get("needs_escalation")),
                "citation_count": citation_count,
                "valid_citation_count": valid_citations,
                "result_chars": len(json.dumps(result)) if result is not None else 0,
            }
        )

    assert result is not None
    return WorkerOutcome(
        run_id=run_id,
        worker_model=worker_model,
        result=result,
        usage=usage,
        duration_ms=duration_ms,
        actual_credits=actual_credits,
        sol_equivalent_credits=sol_credits,
        citation_count=citation_count,
        valid_citation_count=valid_citations,
    )


def run_worker_for_content(
    *,
    question: str,
    content: str,
    config: dict[str, Any],
    task_kind: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    parent_model: str | None = None,
) -> WorkerOutcome:
    with tempfile.TemporaryDirectory(prefix="codex-shunt-input-") as temporary:
        root = Path(temporary)
        (root / "tool-output.txt").write_text(content, encoding="utf-8", errors="replace")
        selection = collect_sources(
            root,
            ["tool-output.txt"],
            max_files=1,
            max_bytes=max(len(content.encode("utf-8", errors="replace")) + 1, 1024),
        )
        return run_worker(
            question=question,
            selection=selection,
            config=config,
            task_kind=task_kind,
            session_id=session_id,
            turn_id=turn_id,
            parent_model=parent_model,
        )
