"""Command-line interface for Codex Shunt."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    PLUGIN_ROOT,
    SOURCE_SHARING_NOTICE,
    get_data_dir,
    load_config,
    parse_config_value,
    save_user_config,
    user_config_path,
)
from .db import aggregate, database_path, fetch_rows
from .hooks import hook_pre
from .pricing import MODEL_RATES
from .sources import collect_sources
from .worker import WorkerError, auth_status, find_codex, run_worker, worker_limitation


def _color_enabled(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never" or "NO_COLOR" in os.environ:
        return False
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _period_label(since: str) -> str:
    normalized = since.strip().lower()
    if normalized == "all":
        return "all time"
    if normalized[:-1].isdigit() and normalized.endswith(("h", "d")):
        return f"last {normalized}"
    return f"since {since}"


def command_inspect(args: argparse.Namespace) -> int:
    config = load_config()
    root = Path(args.root or os.getcwd())
    selection = collect_sources(
        root,
        args.paths,
        max_files=int(config["max_source_files"]),
        max_bytes=int(config["max_source_bytes"]),
    )
    outcome = run_worker(
        question=args.question,
        selection=selection,
        config=config,
        task_kind=args.task_kind,
        parent_model=args.parent_model,
    )
    _render_outcome(outcome, args.json)
    return 0


def _doctor_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "python",
            "ok": sys.version_info >= (3, 11),
            "detail": platform.python_version(),
        }
    )
    try:
        codex = find_codex()
        checks.append({"name": "codex_cli", "ok": True, "detail": codex})
        authenticated, status = auth_status(codex)
        checks.append({"name": "chatgpt_auth", "ok": authenticated, "detail": status})
    except Exception as exc:
        checks.append({"name": "codex_cli", "ok": False, "detail": str(exc)})
    required = [
        PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
        PLUGIN_ROOT / "hooks" / "hooks.json",
        PLUGIN_ROOT / "scripts" / "codex-shunt",
        PLUGIN_ROOT / "scripts" / ("codex-shunt-hook.py" if sys.platform == "win32" else "codex-shunt-hook"),
        PLUGIN_ROOT / "schemas" / "worker-result.schema.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    checks.append(
        {
            "name": "plugin_files",
            "ok": not missing,
            "detail": "complete" if not missing else f"missing: {', '.join(missing)}",
        }
    )
    try:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        database_path()
        checks.append({"name": "data_directory", "ok": True, "detail": str(data_dir)})
    except Exception as exc:
        checks.append({"name": "data_directory", "ok": False, "detail": str(exc)})
    try:
        config = load_config()
        sharing_allowed = bool(config["source_sharing_acknowledged"])
        checks.append(
            {
                "name": "source_sharing",
                "ok": sharing_allowed,
                "detail": (
                    "acknowledged"
                    if sharing_allowed
                    else "not acknowledged; run `shunt setup`"
                ),
            }
        )
        checks.append(
            {
                "name": "configuration",
                "ok": True,
                "detail": (
                    f"strict_routing={str(config['strict_routing']).lower()}, "
                    f"worker={config['worker_model']}"
                ),
            }
        )
    except Exception as exc:
        checks.append({"name": "configuration", "ok": False, "detail": str(exc)})
    return checks


def _print_checks(checks: list[dict[str, Any]]) -> None:
    for item in checks:
        marker = "PASS" if item["ok"] else "FAIL"
        print(f"[{marker}] {item['name']}: {item['detail']}")


def command_setup(args: argparse.Namespace) -> int:
    print("Codex Shunt source-sharing disclosure\n")
    print(SOURCE_SHARING_NOTICE)
    print()

    config = load_config()
    accepted = bool(args.accept_source_sharing or config["source_sharing_acknowledged"])
    if config["source_sharing_acknowledged"]:
        print("Source-sharing acknowledgement is already recorded.\n")
    if not accepted and sys.stdin.isatty():
        response = input("Type 'accept' to continue, or press Enter to cancel: ").strip().lower()
        accepted = response == "accept"
    if not accepted:
        print(
            "No configuration was changed. Run again interactively, or pass "
            "--accept-source-sharing after reviewing the disclosure.",
            file=sys.stderr,
        )
        return 2

    config["source_sharing_acknowledged"] = True
    if args.enable_strict_routing:
        # Enable only after the worker test succeeds.
        config["strict_routing"] = False
    path = save_user_config(config)
    print(f"\nSaved source-sharing acknowledgement to {path}")

    checks = _doctor_checks()
    _print_checks(checks)
    if not all(item["ok"] for item in checks):
        return 1

    should_test_worker = bool(args.test_worker or args.enable_strict_routing)
    if should_test_worker:
        print("\nTesting the subscription-authenticated read-only Luna worker...")
        with tempfile.TemporaryDirectory(prefix="codex-shunt-setup-") as temporary:
            root = Path(temporary)
            sample = root / "setup-check.txt"
            sample.write_text(
                "Codex Shunt setup check.\n"
                "The expected safety mode is read-only.\n",
                encoding="utf-8",
            )
            selection = collect_sources(
                root,
                [sample.name],
                max_files=int(config["max_source_files"]),
                max_bytes=int(config["max_source_bytes"]),
            )
            try:
                outcome = run_worker(
                    question=(
                        "Read setup-check.txt from the current directory. Confirm the safety mode "
                        "stated on line 2. Return one concise finding citing setup-check.txt line 2. "
                        "If you cannot read it, report the read error and request escalation."
                    ),
                    selection=selection,
                    config=config,
                    task_kind="setup-smoke",
                )
            except WorkerError as exc:
                raise WorkerError(
                    "Shunt is installed, but Luna verification failed. Automatic routing remains off.\n"
                    f"{exc}\nResolve the reported issue, then run `shunt setup`; reinstalling is unnecessary."
                ) from exc
        if (outcome.result.get("needs_escalation") or outcome.citation_count < 1
                or outcome.valid_citation_count != outcome.citation_count):
            raise WorkerError(
                "Shunt is installed, but Luna could not verify the sample file. "
                "Automatic routing remains off.\n" + worker_limitation(outcome.result) +
                "\nResolve the reported issue, then run `shunt setup`; reinstalling is unnecessary."
            )
        print(
            f"[PASS] luna_worker: {outcome.worker_model}, "
            f"{outcome.duration_ms / 1000:.1f}s, "
            f"{outcome.valid_citation_count}/{outcome.citation_count} citations valid"
        )

    if args.enable_strict_routing:
        config["strict_routing"] = True
        save_user_config(config)
        print("[PASS] strict_routing: enabled after successful worker verification")
    elif config["strict_routing"]:
        print("\nStrict routing remains enabled.")
    else:
        print("\nStrict routing remains off. Enable it later with:")
        print("  shunt config set strict_routing true")

    print(
        "\nSetup complete. In a sandboxed Codex task, approve only the narrowly scoped "
        "Shunt launcher when prompted; the Luna child remains ephemeral and read-only."
    )
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    checks = _doctor_checks()

    if args.json:
        print(json.dumps({"ok": all(item["ok"] for item in checks), "checks": checks}, indent=2))
    else:
        _print_checks(checks)
    return 0 if all(item["ok"] for item in checks) else 1


def command_config(args: argparse.Namespace) -> int:
    config = load_config()
    if args.config_action == "show":
        print(f"User config: {user_config_path()}")
        for key, value in config.items():
            print(f"{key} = {json.dumps(value)}")
        return 0
    value = parse_config_value(args.key, args.value, config)
    if args.key == "source_sharing_acknowledged" and value is True:
        raise ValueError(
            "Use `shunt setup` to review and acknowledge the source-sharing disclosure"
        )
    if args.key == "strict_routing" and value is True and not config[
        "source_sharing_acknowledged"
    ]:
        raise ValueError("Run `shunt setup` before enabling strict routing")
    config[args.key] = value
    if args.key == "source_sharing_acknowledged" and value is False:
        config["strict_routing"] = False
    path = save_user_config(config)
    print(f"Set {args.key} = {json.dumps(value)}")
    if args.key == "source_sharing_acknowledged" and value is False:
        print("Strict routing was also disabled.")
    print(f"Saved {path}")
    return 0


def _render_outcome(outcome: Any, as_json: bool) -> None:
    payload = {
        "run_id": outcome.run_id, "worker_model": outcome.worker_model,
        "result": outcome.result, "usage": outcome.usage, "duration_ms": outcome.duration_ms,
        "estimated_worker_credits": outcome.estimated_worker_credits,
        "citation_count": outcome.citation_count, "valid_citation_count": outcome.valid_citation_count,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"{outcome.worker_model} · {outcome.duration_ms / 1000:.1f}s · run {outcome.run_id}\n")
    print(outcome.result["summary"])
    for finding in outcome.result["findings"]:
        print(f"- {finding['file']}:{finding['line_start']}-{finding['line_end']}: {finding['claim']}")
    for limitation in outcome.result["limitations"]:
        print(f"- {limitation}")
    if outcome.result["needs_escalation"]:
        print("Worker requested primary-model inspection.")


def _stats_payload(since: str, comparison_model: str | None = None) -> dict[str, Any]:
    return aggregate(since, comparison_model or load_config()["comparison_model"])


def _render_stats(
    payload: dict[str, Any], since: str, *, color: bool = False, width: int | None = None,
) -> str:
    workers, routing, comparison = payload["workers"], payload["routing"], payload["comparison"]
    width = max(20, width if width is not None else shutil.get_terminal_size((80, 24)).columns)
    styles = {
        "title": "1;36", "label": "36", "value": "1", "muted": "90",
        "good": "1;32", "warning": "1;33", "bad": "1;31",
    }
    lines: list[str] = []

    def paint(text: str, tone: str) -> str:
        return f"\033[{styles[tone]}m{text}\033[0m" if color and tone else text

    def line(
        *parts: tuple[str, str], prefix: str = "  ", continuation: str = "  ",
        prefix_tone: str = "",
    ) -> None:
        # Wrap plain words before styling so ANSI escapes never affect alignment.
        rendered, length, has_words = paint(prefix, prefix_tone), len(prefix), False
        for text, tone in parts:
            # Keep short metrics together, including their unit or label.
            if (has_words and len(text) <= width - len(continuation)
                    and length + 1 + len(text) > width):
                lines.append(rendered)
                rendered, length, has_words = continuation, len(continuation), False
            for word in text.split():
                if has_words and length + 1 + len(word) > width:
                    lines.append(rendered)
                    rendered, length, has_words = continuation, len(continuation), False
                gap = " " if has_words else ""
                rendered += gap + paint(word, tone)
                length += len(gap) + len(word)
                has_words = True
        lines.append(rendered)

    def row(label: str, *parts: tuple[str, str]) -> None:
        if width < 48:
            line((label, "label"))
            line(*parts, prefix="    ", continuation="    ")
        else:
            prefix = f"  {label:<10}  "
            line(*parts, prefix=prefix, continuation=" " * len(prefix), prefix_tone="label")

    def credits(amount: float) -> str:
        if 0 < amount < 0.0001:
            return "<0.0001"
        return f"{amount:,.4f}" if 0 < amount < 0.01 else f"{amount:,.2f}"

    def percent(rate: float) -> str:
        return f"{rate:.1%}".replace(".0%", "%")

    total, citations = workers["total"], workers["citations"]
    savings = comparison["estimated_savings_credits"]
    rate = comparison["estimated_savings_rate"]
    model = comparison["model"]
    line(("Codex Shunt", "title"), (f"· {_period_label(since)}", "muted"))
    line(("─" * min(64, width - 4), "muted"))
    lines.append("")

    if total:
        tone = "good" if savings > 0 else "bad" if savings < 0 else "muted"
        headline = "saved" if savings >= 0 else "extra cost"
        difference = f"· {percent(abs(rate))} {'lower' if rate >= 0 else 'higher'} cost" if rate is not None else ""
        line((f"{credits(abs(savings))} credits {headline}", tone), (difference, tone))
        line((f"Estimated vs {model}", "muted"))
    else:
        line(("No worker runs yet", "value"))
        line(("Run a Shunt inspection to start tracking savings.", "muted"))

    lines.append("")
    if total:
        row("Cost", (f"{credits(workers['estimated_credits'])} worker", "value"),
            ("/", "muted"), (f"{credits(comparison['estimated_equivalent_credits'])} comparison", "value"),
            ("credits (est.)", "muted"))
        success = workers["succeeded"] / total
        row("Runs", (f"{workers['succeeded']:,}/{total:,} succeeded ({percent(success)})",
                     "good" if success == 1 else "warning"),
            (f"· {workers['average_duration_ms'] / 1000:.1f}s avg", "muted"))
        problems = []
        if workers["failed"]:
            problems.append((f"{workers['failed']:,} failed", "bad"))
        if workers["escalations"]:
            if problems:
                problems.append(("·", "muted"))
            escalations = workers["escalations"]
            problems.append((f"{escalations:,} needed review", "warning"))
        if problems:
            row("Outcomes", *problems)
        if citations:
            validity = workers["valid_citations"] / citations
            row("Citations", (f"{workers['valid_citations']:,}/{citations:,} ranges valid ({percent(validity)})",
                              "good" if validity == 1 else "warning"))
        else:
            row("Citations", ("— no citation ranges", "muted"))

    if total or routing["routed"] or routing["fallbacks"] or routing["observed"]:
        observed = f"· {routing['observed']:,} observed only" if routing["observed"] else ""
        row("Routing", (f"{routing['routed']:,} routed", "value"), ("·", "muted"),
            (f"{routing['fallbacks']:,} fallbacks", "warning" if routing["fallbacks"] else "muted"),
            (observed, "muted"))
    if total:
        row("Tokens", (f"{workers['input_tokens']:,} input", "value"),
            (f"({workers['cached_input_tokens']:,} cached)", "muted"),
            (f"· {workers['output_tokens']:,} output", "value"))
    if total or routing["estimated_source_tokens_intercepted"]:
        row("Source", (f"{routing['estimated_source_tokens_intercepted']:,} tokens intercepted", "value"),
            ("(gross est.)", "muted"))

    return "\n".join(lines).rstrip()


def _failures_payload(since: str) -> dict[str, Any]:
    failures = [
        row for row in fetch_rows("worker_runs", since)
        if row["task_kind"] != "setup-smoke"
        and (row["status"] == "failed" or row.get("error_type"))
    ]
    return {"since": since, "failures": failures}


def _render_failures(
    payload: dict[str, Any], since: str, *, color: bool = False, width: int | None = None,
) -> str:
    width = max(20, width if width is not None else shutil.get_terminal_size((80, 24)).columns)
    lines: list[str] = []

    def line(text: str, tone: str = "", indent: str = "  ") -> None:
        wrapped = textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)
        lines.extend(f"\033[{tone}m{part}\033[0m" if color and tone else part for part in wrapped)

    line(f"Codex Shunt · failures · {_period_label(since)}", "1;36")
    line("─" * min(64, width - 4), "90")
    lines.append("")
    failures = payload["failures"]
    if not failures:
        line("No failed worker runs in this period.", "1;32")
        return "\n".join(lines)

    count = len(failures)
    limit = 10
    note = f" · newest {limit} shown" if count > limit else ""
    line(f"{count:,} failed worker {'run' if count == 1 else 'runs'}{note}", "1;31")
    for row in failures[:limit]:
        lines.append("")
        try:
            timestamp = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            when = timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except (ValueError, TypeError):
            when = str(row["created_at"])
        line(f"{when} · {row.get('error_type') or 'Unspecified error'}", "1;31")
        line(f"Run {row['id']}", "1", "    ")
        line(f"{row['worker_model']} · {row['task_kind']} · {row['duration_ms'] / 1000:.1f}s", "90", "    ")
        files = row["source_files"]
        line(f"{files:,} {'file' if files == 1 else 'files'} · {row['input_tokens']:,} input · {row['output_tokens']:,} output", "90", "    ")
        if row.get("session_id"):
            line(f"Chat {row['session_id']}", "90", "    ")
        if row.get("turn_id"):
            line(f"Turn {row['turn_id']}", "90", "    ")
    return "\n".join(lines)


def command_stats(args: argparse.Namespace) -> int:
    payload = _failures_payload(args.since) if args.failures else _stats_payload(args.since, args.compare_to)
    render = _render_failures if args.failures else _render_stats
    print(json.dumps(payload, indent=2) if args.json else
          render(payload, args.since, color=_color_enabled(args.color)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shunt", description="Route large reads to Luna using your ChatGPT login.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Acknowledge source sharing, test Luna, and enable routing")
    setup.add_argument("--accept-source-sharing", action="store_true")
    # Preserve existing setup invocations; plain setup now does the complete flow.
    setup.add_argument("--test-worker", action="store_true", default=True, help=argparse.SUPPRESS)
    setup.add_argument("--enable-strict-routing", action="store_true", default=True, help=argparse.SUPPRESS)
    setup.set_defaults(handler=command_setup)
    inspect = commands.add_parser("inspect", help="Ask Luna a narrow question about selected files")
    inspect.add_argument("paths", nargs="+")
    inspect.add_argument("--root")
    inspect.add_argument("--question", required=True)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(handler=command_inspect, task_kind="repository-inspection", parent_model=None)
    status = commands.add_parser("status", aliases=["doctor"], help="Check local prerequisites and configuration")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_doctor)
    config = commands.add_parser("config", help="View or change the TOML configuration")
    actions = config.add_subparsers(dest="config_action", required=True)
    actions.add_parser("show").set_defaults(handler=command_config)
    setting = actions.add_parser("set")
    setting.add_argument("key")
    setting.add_argument("value")
    setting.set_defaults(handler=command_config)
    stats = commands.add_parser("stats", help="Show observed usage and estimated savings")
    stats.add_argument("--since", default="7d", help="all, <N>d, <N>h, or an ISO timestamp")
    stats.add_argument("--compare-to", choices=tuple(MODEL_RATES), help="Override comparison_model for this report")
    stats.add_argument("--failures", action="store_true",
                       help="Show the newest 10 failed worker runs; use --json for all recorded details")
    stats.add_argument("--json", action="store_true")
    stats.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                       help="ANSI colors: auto for terminals (honors NO_COLOR), always, or never")
    stats.set_defaults(handler=command_stats)
    return parser


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"hook", "hook-pre", "hook-post"}:
        # Old loaded PostToolUse definitions become harmless until the plugin refreshes.
        event = sys.argv[2] if sys.argv[1] == "hook" and len(sys.argv) > 2 else sys.argv[1]
        raise SystemExit(hook_pre() if event in {"pre", "hook-pre"} else 0)
    parser = build_parser()
    args = parser.parse_args()
    try:
        status = args.handler(args)
    except (ValueError, KeyError, WorkerError, OSError) as exc:
        print(f"shunt: {exc}", file=sys.stderr)
        status = 1
    raise SystemExit(status)
