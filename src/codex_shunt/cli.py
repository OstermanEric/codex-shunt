"""Command-line interface for Codex Shunt."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import platform
import sys
import tempfile
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
from .db import aggregate, database_path, fetch_rows, record_feedback
from .hooks import hook_post, hook_pre
from .sources import collect_sources
from .worker import WorkerError, auth_status, find_codex, run_worker


def _human_number(value: int | float) -> str:
    number = float(value)
    for suffix in ("", "K", "M", "B"):
        if abs(number) < 1000 or suffix == "B":
            if suffix:
                return f"{number:.2f}{suffix}"
            return f"{int(number):,}"
        number /= 1000
    return f"{number:.2f}B"


_ANSI = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "cyan": "36",
}


def _color_enabled(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never" or "NO_COLOR" in os.environ:
        return False
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _paint(text: str, *styles: str, enabled: bool) -> str:
    if not enabled:
        return text
    codes = ";".join(_ANSI[style] for style in styles)
    return f"\033[{codes}m{text}\033[0m"


def _rate_style(rate: float, total: int) -> str:
    if total == 0:
        return "dim"
    if rate >= 0.9:
        return "green"
    if rate >= 0.6:
        return "yellow"
    return "red"


def _period_label(since: str) -> str:
    normalized = since.strip().lower()
    if normalized == "all":
        return "all time"
    if normalized[:-1].isdigit() and normalized.endswith(("h", "d")):
        return f"last {normalized}"
    return f"since {since}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def _metric_line(
    label: str,
    value: str,
    detail: str,
    *,
    value_style: str,
    color: bool,
) -> str:
    label_cell = _paint(f"{label:<18}", "bold", "cyan", enabled=color)
    value_cell = _paint(f"{value:<18}", "bold", value_style, enabled=color)
    return f"{label_cell} {value_cell} {detail}"


def _detail_line(label: str, detail: str, *, color: bool) -> str:
    label_cell = _paint(f"{label:<12}", "bold", enabled=color)
    return f"{label_cell} {detail}"


def _render_outcome(outcome: Any, as_json: bool) -> None:
    payload = {
        "run_id": outcome.run_id,
        "result": outcome.result,
        "usage": outcome.usage,
        "duration_ms": outcome.duration_ms,
        "actual_worker_credits": outcome.actual_credits,
        "sol_equivalent_credits": outcome.sol_equivalent_credits,
        "citation_count": outcome.citation_count,
        "valid_citation_count": outcome.valid_citation_count,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"Run ID: {outcome.run_id}")
    print(f"Model: {outcome.worker_model} | Duration: {outcome.duration_ms / 1000:.1f}s")
    print(
        "Usage: "
        f"{outcome.usage['input_tokens']:,} input, "
        f"{outcome.usage['cached_input_tokens']:,} cached input, "
        f"{outcome.usage['output_tokens']:,} output"
    )
    print(
        f"Credits: {outcome.actual_credits:.4f} actual worker; "
        f"{outcome.sol_equivalent_credits:.4f} Sol-equivalent estimate"
    )
    print()
    print(outcome.result["summary"])
    findings = outcome.result.get("findings", [])
    if findings:
        print("\nEvidence:")
        for finding in findings:
            print(
                f"- {finding['file']}:{finding['line_start']}-{finding['line_end']} "
                f"({finding['confidence']:.0%}): {finding['claim']}"
            )
    if outcome.result.get("limitations"):
        print("\nLimitations:")
        for limitation in outcome.result["limitations"]:
            print(f"- {limitation}")
    if outcome.result.get("needs_escalation"):
        print("\nWorker requested primary-model escalation.")


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


def command_summarize_log(args: argparse.Namespace) -> int:
    if not args.question:
        args.question = (
            "Summarize failures in this log. Group repeated errors, identify the earliest useful "
            "failure, cite exact line ranges, and distinguish evidence from hypotheses."
        )
    args.paths = [args.path]
    args.task_kind = "command-output"
    return command_inspect(args)


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
        PLUGIN_ROOT / "scripts" / "codex-shunt-hook",
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

    accepted = bool(args.accept_source_sharing)
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

    config = load_config()
    config["source_sharing_acknowledged"] = True
    if args.enable_strict_routing:
        # Enable only after the optional (and implied) worker test succeeds.
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
            outcome = run_worker(
                question=(
                    "Confirm the safety mode stated in setup-check.txt. Return one concise "
                    "finding with an exact citation."
                ),
                selection=selection,
                config=config,
                task_kind="setup-smoke",
            )
        if outcome.citation_count < 1 or outcome.valid_citation_count != outcome.citation_count:
            raise WorkerError(
                "The Luna setup check returned without a locally valid citation; "
                "strict routing was not enabled"
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


def _stats_payload(since: str) -> dict[str, Any]:
    payload = aggregate(since)
    routing = payload["routing"]
    workers = payload["workers"]
    total_routes = int(routing["total"] or 0)
    routed = int(routing["routed"] or 0)
    enforced = int(routing["enforced"] or 0)
    worker_total = int(workers["total"] or 0)
    succeeded = int(workers["succeeded"] or 0)
    citations = int(workers["citations"] or 0)
    valid = int(workers["valid_citations"] or 0)
    worker_credits = float(workers["worker_credits"] or 0)
    sol_credits = float(workers["sol_credits"] or 0)
    credit_difference = sol_credits - worker_credits
    payload["derived"] = {
        "routing_share": routed / total_routes if total_routes else 0.0,
        "enforced_share": enforced / routed if routed else 0.0,
        "worker_success_rate": succeeded / worker_total if worker_total else 0.0,
        "citation_validity_rate": valid / citations if citations else 0.0,
        "worker_stage_credit_difference": credit_difference,
        "worker_stage_credit_savings_rate": (
            credit_difference / sol_credits if sol_credits else 0.0
        ),
    }
    return payload


def _render_stats(payload: dict[str, Any], since: str, *, color: bool) -> str:
    routing = payload["routing"]
    workers = payload["workers"]
    derived = payload["derived"]
    routing_total = int(routing["total"] or 0)
    routed = int(routing["routed"] or 0)
    enforced = int(routing["enforced"] or 0)
    worker_total = int(workers["total"] or 0)
    succeeded = int(workers["succeeded"] or 0)
    failed = worker_total - succeeded
    citations = int(workers["citations"] or 0)
    valid_citations = int(workers["valid_citations"] or 0)
    escalations = int(workers["escalations"] or 0)
    savings = float(derived["worker_stage_credit_difference"])
    savings_rate = float(derived["worker_stage_credit_savings_rate"])

    lines = [
        f"{_paint('Codex Shunt', 'bold', 'cyan', enabled=color)}"
        f"  {_paint('·', 'dim', enabled=color)}  {_period_label(since)}",
        "",
    ]

    if worker_total:
        if savings_rate >= 0:
            savings_detail = f"estimated · {savings_rate:.1%} less than Sol-equivalent"
        else:
            savings_detail = f"estimated · {abs(savings_rate):.1%} more than Sol-equivalent"
        savings_value = f"{savings:.4f} credits"
        savings_style = "green" if savings > 0 else ("red" if savings < 0 else "dim")
    else:
        savings_value = "—"
        savings_detail = "no worker usage yet"
        savings_style = "dim"
    lines.append(
        _metric_line(
            "CREDITS SAVED",
            savings_value,
            savings_detail,
            value_style=savings_style,
            color=color,
        )
    )

    routable_context = int(routing["estimated_tokens"] or 0)
    context_tokens = int(routing["estimated_intercepted_tokens"] or 0)
    if routable_context > context_tokens:
        context_detail = (
            f"estimated · {_human_number(routable_context - context_tokens)} more identified"
        )
    elif context_tokens:
        context_detail = "estimated primary-model context"
    else:
        context_detail = "no intercepted context"
    lines.append(
        _metric_line(
            "CONTEXT AVOIDED",
            f"{_human_number(context_tokens)} tokens",
            context_detail,
            value_style="green" if context_tokens else "dim",
            color=color,
        )
    )

    success_value = f"{derived['worker_success_rate']:.1%}" if worker_total else "—"
    success_detail = (
        f"{succeeded:,} of {worker_total:,} worker {_plural(worker_total, 'run')}"
        if worker_total
        else "no worker runs"
    )
    lines.append(
        _metric_line(
            "SUCCESS RATE",
            success_value,
            success_detail,
            value_style=_rate_style(derived["worker_success_rate"], worker_total),
            color=color,
        )
    )

    citation_value = f"{derived['citation_validity_rate']:.1%}" if citations else "—"
    citation_detail = (
        f"{valid_citations:,} of {citations:,} citations valid"
        if citations
        else "no citations checked"
    )
    lines.append(
        _metric_line(
            "CITATION VALIDITY",
            citation_value,
            citation_detail,
            value_style=_rate_style(derived["citation_validity_rate"], citations),
            color=color,
        )
    )

    lines.extend(["", _paint("Activity", "bold", enabled=color)])
    run_parts = [
        f"{worker_total:,} total",
        f"{succeeded:,} succeeded",
        f"{failed:,} failed",
        f"{escalations:,} {_plural(escalations, 'escalation')}",
    ]
    lines.append(_detail_line("Runs", " · ".join(run_parts), color=color))
    if routing_total:
        shadowed = routed - enforced
        routing_detail = (
            f"{routed:,} of {routing_total:,} eligible operations · "
            f"{enforced:,} enforced · {shadowed:,} observed with strict routing off"
        )
    else:
        routing_detail = "No eligible operations observed"
    lines.append(_detail_line("Routing", routing_detail, color=color))
    lines.append(
        _detail_line(
            "Usage",
            f"{int(workers['input_tokens'] or 0):,} input · "
            f"{int(workers['cached_input_tokens'] or 0):,} cached · "
            f"{int(workers['output_tokens'] or 0):,} output",
            color=color,
        )
    )
    lines.append(
        _detail_line(
            "Worker cost",
            f"{float(workers['worker_credits'] or 0):.4f} credits exact · "
            f"{float(workers['sol_credits'] or 0):.4f} Sol-equivalent estimated",
            color=color,
        )
    )
    lines.append(
        _detail_line(
            "Latency",
            f"{float(workers['average_duration_ms'] or 0) / 1000:.2f}s average",
            color=color,
        )
    )

    if payload["by_model"]:
        lines.extend(["", _paint("Models", "bold", enabled=color)])
        for row in payload["by_model"]:
            lines.append(
                f"- {row['worker_model']}: {row['runs']} runs, "
                f"{int(row['tokens']):,} tokens · {float(row['credits']):.4f} credits"
            )
    lines.extend(
        [
            "",
            _paint(
                "Exact: worker usage and credits. Estimated: context avoided, "
                "Sol-equivalent cost, and savings.",
                "dim",
                enabled=color,
            ),
        ]
    )
    return "\n".join(lines)


def command_stats(args: argparse.Namespace) -> int:
    payload = _stats_payload(args.since)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(
        _render_stats(
            payload,
            args.since,
            color=_color_enabled(getattr(args, "color", "auto")),
        )
    )
    return 0


def _export_payload(since: str) -> dict[str, Any]:
    return {
        "generated_by": f"codex-shunt {__version__}",
        "summary": _stats_payload(since),
        "routing_events": fetch_rows("routing_events", since),
        "worker_runs": fetch_rows("worker_runs", since),
        "feedback": fetch_rows("feedback", since),
    }


def command_export(args: argparse.Namespace) -> int:
    payload = _export_payload(args.since)
    if args.format == "json":
        rendered = json.dumps(payload, indent=2)
    else:
        records: list[dict[str, Any]] = []
        for table in ("routing_events", "worker_runs", "feedback"):
            for row in payload[table]:
                records.append({"record_type": table, **row})
        fieldnames = sorted({key for row in records for key in row}) if records else ["record_type"]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
        rendered = stream.getvalue()
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(output)
    else:
        print(rendered)
    return 0


def _report_html(payload: dict[str, Any], since: str) -> str:
    routing = payload["routing"]
    workers = payload["workers"]
    derived = payload["derived"]
    model_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['worker_model']))}</td>"
        f"<td>{int(row['runs']):,}</td>"
        f"<td>{int(row['tokens']):,}</td>"
        f"<td>{float(row['credits']):.4f}</td>"
        "</tr>"
        for row in payload["by_model"]
    ) or '<tr><td colspan="4">No worker runs yet.</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex Shunt report</title>
<style>
body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 1050px; padding: 32px; color: #172033; background: #f5f7fb; }}
h1 {{ margin-bottom: 4px; }} .muted {{ color: #607089; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin: 24px 0; }}
.metric {{ background: white; border: 1px solid #dce3ee; border-radius: 12px; padding: 16px; }}
.metric strong {{ display: block; font-size: 26px; }}
table {{ width: 100%; border-collapse: collapse; background: white; }}
th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e4e9f1; }}
.note {{ border-left: 4px solid #7c5cff; padding: 10px 14px; background: #eeeaff; margin-top: 24px; }}
@media (prefers-color-scheme: dark) {{ body {{ color: #eef2fb; background: #111620; }} .metric, table {{ background: #19202d; border-color: #303b4f; }} th, td {{ border-color: #303b4f; }} .muted {{ color: #aab6c8; }} .note {{ background: #28213f; }} }}
</style>
</head>
<body>
<h1>Codex Shunt</h1>
<div class="muted">Local routing report for {html.escape(since)}</div>
<div class="grid">
  <div class="metric"><span>Routed or would route</span><strong>{int(routing['routed'] or 0):,}</strong><span>{derived['routing_share']:.1%} of observed eligible operations</span></div>
  <div class="metric"><span>Estimated context intercepted</span><strong>{_human_number(int(routing['estimated_tokens'] or 0))}</strong><span>estimated source tokens</span></div>
  <div class="metric"><span>Exact worker credits</span><strong>{float(workers['worker_credits'] or 0):.4f}</strong><span>subscription credit accounting</span></div>
  <div class="metric"><span>Worker success</span><strong>{derived['worker_success_rate']:.1%}</strong><span>{int(workers['total'] or 0):,} worker runs</span></div>
  <div class="metric"><span>Citation validity</span><strong>{derived['citation_validity_rate']:.1%}</strong><span>local path and line validation</span></div>
  <div class="metric"><span>Worker-stage difference</span><strong>{derived['worker_stage_credit_difference']:.4f}</strong><span>estimated vs the same tokens on Sol</span></div>
</div>
<h2>Worker models</h2>
<table><thead><tr><th>Model</th><th>Runs</th><th>Exact tokens</th><th>Exact credits</th></tr></thead><tbody>{model_rows}</tbody></table>
<div class="note"><strong>Measurement boundary.</strong> Worker token counts and worker credits are exact. Source-token interception, Sol-equivalent credits, and primary-context savings are estimates because current Codex hooks do not expose stable parent-turn token totals.</div>
</body>
</html>
"""


def command_report(args: argparse.Namespace) -> int:
    payload = _stats_payload(args.since)
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else get_data_dir() / "reports" / "codex-shunt-report.html"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_report_html(payload, args.since), encoding="utf-8")
    print(output)
    return 0


def command_feedback(args: argparse.Namespace) -> int:
    feedback_id = record_feedback(args.run_id, args.verdict, args.note)
    print(f"Recorded feedback {feedback_id} for run {args.run_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-shunt",
        description="Route predictable Codex work to a subscription-authenticated Luna worker.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup", help="Review source sharing, verify auth, and optionally test the worker"
    )
    setup.add_argument(
        "--accept-source-sharing",
        action="store_true",
        help="Non-interactively acknowledge the printed source-sharing disclosure",
    )
    setup.add_argument(
        "--test-worker",
        action="store_true",
        help="Send a synthetic two-line fixture through the read-only Luna worker",
    )
    setup.add_argument(
        "--enable-strict-routing",
        action="store_true",
        help="Test the worker and enable automatic routing only after it succeeds",
    )
    setup.set_defaults(handler=command_setup)

    inspect = commands.add_parser("inspect", help="Analyze selected paths with the Luna worker")
    inspect.add_argument("paths", nargs="+", help="Files or directories inside --root")
    inspect.add_argument("--root", help="Repository root; defaults to the current directory")
    inspect.add_argument("--question", required=True, help="Narrow evidence question")
    inspect.add_argument("--task-kind", default="repository-inspection")
    inspect.add_argument("--parent-model", help="Optional parent model label for telemetry")
    inspect.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    inspect.set_defaults(handler=command_inspect)

    log = commands.add_parser("summarize-log", help="Summarize one large text log with Luna")
    log.add_argument("path")
    log.add_argument("--root", help="Root containing the log; defaults to the current directory")
    log.add_argument("--question")
    log.add_argument("--parent-model")
    log.add_argument("--json", action="store_true")
    log.set_defaults(handler=command_summarize_log)

    doctor = commands.add_parser("doctor", help="Check CLI, ChatGPT auth, config, and storage")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    status = commands.add_parser("status", help="Alias for doctor")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_doctor)

    config = commands.add_parser("config", help="View or change Codex Shunt configuration")
    config_actions = config.add_subparsers(dest="config_action", required=True)
    config_show = config_actions.add_parser("show")
    config_show.set_defaults(handler=command_config)
    config_set = config_actions.add_parser("set")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.set_defaults(handler=command_config)

    stats = commands.add_parser("stats", help="Show aggregate local telemetry")
    stats.add_argument("--since", default="7d")
    stats.add_argument("--json", action="store_true")
    stats.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colorize output (default: auto; NO_COLOR disables auto color)",
    )
    stats.set_defaults(handler=command_stats)

    export = commands.add_parser("export", help="Export local telemetry without source content")
    export.add_argument("--since", default="all")
    export.add_argument("--format", choices=("json", "csv"), default="json")
    export.add_argument("--output")
    export.set_defaults(handler=command_export)

    report = commands.add_parser("report", help="Generate a self-contained local HTML report")
    report.add_argument("--since", default="30d")
    report.add_argument("--output")
    report.set_defaults(handler=command_report)

    feedback = commands.add_parser("feedback", help="Mark a worker result accepted or rejected")
    feedback.add_argument("run_id")
    feedback.add_argument("verdict", choices=("accepted", "rejected"))
    feedback.add_argument("--note")
    feedback.set_defaults(handler=command_feedback)

    data_dir = commands.add_parser("data-dir", help="Print the local metrics directory")
    data_dir.set_defaults(handler=lambda _args: (print(get_data_dir()), 0)[1])

    return parser


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "hook" and sys.argv[2] == "pre":
        raise SystemExit(hook_pre())
    if len(sys.argv) > 2 and sys.argv[1] == "hook" and sys.argv[2] == "post":
        raise SystemExit(hook_post())
    # Backward-compatible internal names for already-loaded hook definitions.
    if len(sys.argv) > 1 and sys.argv[1] == "hook-pre":
        raise SystemExit(hook_pre())
    if len(sys.argv) > 1 and sys.argv[1] == "hook-post":
        raise SystemExit(hook_post())
    parser = build_parser()
    args = parser.parse_args()
    try:
        status = args.handler(args)
    except (ValueError, KeyError, WorkerError, OSError) as exc:
        print(f"codex-shunt: {exc}", file=sys.stderr)
        status = 1
    raise SystemExit(status)
