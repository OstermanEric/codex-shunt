"""Command-line interface for Codex Shunt."""

from __future__ import annotations

import argparse
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
from .db import aggregate, database_path
from .hooks import hook_pre
from .pricing import MODEL_RATES
from .sources import collect_sources
from .worker import WorkerError, auth_status, find_codex, run_worker


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
            outcome = run_worker(
                question=(
                    "Confirm the safety mode stated in setup-check.txt. Return one concise "
                    "finding with an exact citation."
                ),
                selection=selection,
                config=config,
                task_kind="setup-smoke",
            )
        if (outcome.result.get("needs_escalation") or outcome.citation_count < 1
                or outcome.valid_citation_count != outcome.citation_count):
            raise WorkerError(
                "The Luna setup check did not provide reliable cited evidence; "
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


def _render_stats(payload: dict[str, Any], since: str, *, color: bool = False) -> str:
    workers, routing, comparison = payload["workers"], payload["routing"], payload["comparison"]
    title = "\033[1;36mCodex Shunt\033[0m" if color else "Codex Shunt"
    total, citations = workers["total"], workers["citations"]
    savings = f"{comparison['estimated_savings_credits']:.4f} credits" if total else "—"
    rate = comparison["estimated_savings_rate"]
    difference = (f" · {abs(rate):.1%} {'lower' if rate >= 0 else 'higher'} estimated cost"
                  if rate is not None else "")
    success = f"{workers['succeeded'] / total:.1%}" if total else "—"
    validity = f"{workers['valid_citations'] / citations:.1%}" if citations else "—"
    return "\n".join([
        f"{title} · {_period_label(since)}",
        f"Comparison: {comparison['model']} (standard rates, {comparison['rate_date']})", "",
        f"ESTIMATED SAVINGS  {savings}{difference}",
        f"WORKER COST       {workers['estimated_credits']:.4f} credits estimated",
        f"COMPARISON COST   {comparison['estimated_equivalent_credits']:.4f} credits estimated",
        f"SOURCE INTERCEPTED {routing['estimated_source_tokens_intercepted']:,} tokens estimated (gross)",
        f"SUCCESS RATE      {success} · {workers['succeeded']} of {total} runs",
        f"CITATION RANGES   {validity} · {workers['valid_citations']} of {citations} valid", "",
        f"Routing: {routing['routed']} routed · {routing['observed']} observed · {routing['fallbacks']} fallbacks",
        f"Workers: {total} total · {workers['failed']} failed · {workers['escalations']} escalations",
        f"Usage: {workers['input_tokens']:,} input · {workers['cached_input_tokens']:,} cached · {workers['output_tokens']:,} output",
        f"Latency: {workers['average_duration_ms'] / 1000:.2f}s average",
        f"Setup checks excluded: {payload['setup_runs_excluded']}", "",
        comparison["basis"],
        "Gross source interception does not subtract summaries, verification, or retries.",
    ])


def command_stats(args: argparse.Namespace) -> int:
    payload = _stats_payload(args.since, args.compare_to)
    print(json.dumps(payload, indent=2) if args.json else
          _render_stats(payload, args.since, color=_color_enabled(args.color)))
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
    stats.add_argument("--json", action="store_true")
    stats.add_argument("--color", choices=("auto", "always", "never"), default="auto")
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
