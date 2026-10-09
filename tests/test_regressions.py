"""Regression coverage for routing correctness and honest comparisons."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_shunt import hooks, worker
from codex_shunt.cli import (
    _color_enabled, _failures_payload, _render_failures, _render_stats,
    _stats_payload, build_parser,
)
from codex_shunt.config import load_config, save_user_config
from codex_shunt.db import aggregate, record_worker_run
from codex_shunt.reads import parse_read, read_metrics
from codex_shunt.sources import collect_sources


class RoutingRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "large.txt").write_text("line\n" * 600)
        (self.root / "wide.txt").write_text(("x" * 6000 + "\n") * 20)
        with patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": str(self.root / "data")}):
            self.config = load_config()
        self.config.update(strict_routing=True, source_sharing_acknowledged=True)

    def invoke(self, command, *, workdir=None, outcome=None, failure=None):
        event = {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"cmd": command}}
        if workdir:
            event["tool_input"]["workdir"] = str(workdir)
        result = {"summary": "Cited summary", "needs_escalation": False, "limitations": [],
                  "findings": [{"file": "large.txt", "line_start": 1, "line_end": 2, "claim": "Lines"}]}
        outcome = outcome or SimpleNamespace(run_id="regression", result=result,
                                            citation_count=1, valid_citation_count=1)
        output = io.StringIO()
        with patch.object(hooks, "_read_event", return_value=event), \
             patch.object(hooks, "load_config", return_value=self.config), \
             patch.object(hooks, "_record_route") as record, \
             patch.object(hooks, "run_worker", return_value=outcome, side_effect=failure) as run, \
             redirect_stdout(output):
            self.assertEqual(hooks.hook_pre(), 0)
        return output.getvalue(), run, record

    def test_edits_and_combined_shell_operations_are_never_replaced(self):
        for command in ["sed -i 's/line/change/g' large.txt", "sed -n '1p;w out' large.txt",
                        "cat large.txt && touch marker", "cat large.txt; touch marker",
                        "cat large.txt > out", "cat large.txt | head", "cat $(echo large.txt)",
                        "awk '{print}' large.txt", "cat large.txt\ntouch marker",
                        'cat "large.txt"large.txt', 'cat "large.txt"$(touch marker)',
                        'cat large.txt # comment', 'cat {large,wide}.txt']:
            with self.subTest(command=command):
                output, run, _ = self.invoke(command)
                self.assertEqual(output, "")
                run.assert_not_called()

    def test_routing_uses_requested_lines_and_bytes(self):
        commands = ["cat large.txt", "tail -n +1 large.txt", "head -n 20 wide.txt",
                    "head -c 100000 wide.txt", "rtk proxy cat large.txt"]
        if sys.platform != "win32":
            commands.append("cat *.txt")  # Windows cat is Get-Content; wildcards stay raw.
        for command in commands:
            with self.subTest(command=command):
                output, run, _ = self.invoke(command)
                self.assertEqual(json.loads(output)["hookSpecificOutput"]["permissionDecision"], "deny")
                run.assert_called_once()
        for command in ["head -n 40 large.txt", "tail -n 40 large.txt", "sed -n '20,40p' large.txt",
                        "head -c 20 wide.txt", "head -n 0 large.txt", "tail -n 0 large.txt"]:
            with self.subTest(command=command):
                output, run, _ = self.invoke(command)
                self.assertEqual(output, "")
                run.assert_not_called()

    def test_combined_head_does_not_mask_a_broad_read(self):
        output, run, _ = self.invoke("cat large.txt && head -n 10 large.txt")
        self.assertEqual(output, "")  # Complete compound commands deliberately stay on the primary path.
        run.assert_not_called()

    def test_exec_workdir_is_used_and_normalized_operation_reaches_worker(self):
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "local.txt").write_text("line\n" * 600)
        output, run, _ = self.invoke("tail -n +1 local.txt", workdir="sub")
        self.assertTrue(output)
        self.assertEqual(run.call_args.kwargs["selection"].root, sub.resolve())
        self.assertIn('"count": "+1"', run.call_args.kwargs["question"])
        self.assertIn('"files": ["local.txt"]', run.call_args.kwargs["question"])

    def test_escalation_invalid_citations_and_worker_failure_allow_original(self):
        for outcome in [
            SimpleNamespace(result={"needs_escalation": True}),
            SimpleNamespace(result={"needs_escalation": False}, citation_count=2, valid_citation_count=1),
            SimpleNamespace(result={"needs_escalation": False}, citation_count=0, valid_citation_count=0),
        ]:
            output, _, record = self.invoke("cat large.txt", outcome=outcome)
            self.assertEqual(output, "")
            self.assertEqual(record.call_args.args[1], "route_failed")
        output, _, _ = self.invoke("cat large.txt", failure=RuntimeError("unavailable"))
        self.assertEqual(output, "")

    def test_quoted_glob_is_literal_and_unknown_flags_fail_open(self):
        for command in ["cat '*.txt'", "head --unknown large.txt", "sed -n '1p' *.txt",
                        "rg --pre malicious line large.txt", "cat --output out large.txt"]:
            self.assertIsNone(parse_read(command, self.root, 500))
        with patch.object(Path, "expanduser", side_effect=AssertionError("quoted tilde expanded")):
            self.assertIsNone(parse_read('cat "~/large.txt"', self.root, 500))

    def test_range_measurement_and_tail_start(self):
        selection = collect_sources(self.root, ["large.txt"], max_files=1, max_bytes=1000000)
        for command, lines in [("tail -n +500 large.txt", 101), ("head -n -500 large.txt", 100),
                               ("head -n 40 large.txt", 40), ("sed -n '2,3p' large.txt", 2)]:
            with self.subTest(command=command):
                read = parse_read(command, self.root, 500)
                self.assertEqual(read_metrics(read, selection)["source_lines"], lines)

    def test_ripgrep_preserves_pattern_and_measures_matches(self):
        with patch("codex_shunt.reads.shutil.which", return_value="/fake/rg"), \
             patch("codex_shunt.reads.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"line\n" * 600)) as rg:
            output, run, _ = self.invoke("rg -n -F 'line' large.txt")
        self.assertTrue(output)
        self.assertIn('"pattern": "line"', run.call_args.kwargs["question"])
        self.assertIn("--no-config", rg.call_args.args[0])


class ComparisonRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def record(self, run_id="real", **overrides):
        row = {"id": run_id, "worker_model": "gpt-6-luna", "task_kind": "hook-routed-read",
               "question_hash": "question", "repository_hash": "repo", "status": "success",
               "input_tokens": 1200000, "cached_input_tokens": 200000, "output_tokens": 90000,
               "reasoning_output_tokens": 10000, "actual_worker_credits": 3.675,
               "sol_equivalent_credits": 147.0, "citation_count": 1, "valid_citation_count": 1}
        record_worker_run({**row, **overrides})

    def test_new_baseline_reprices_historical_tokens_and_changes_with_config(self):
        self.record()
        result = _stats_payload("all")
        self.assertEqual(result["comparison"]["model"], "gpt-6.1-sol")
        self.assertAlmostEqual(result["comparison"]["estimated_equivalent_credits"], 73.0)
        self.assertAlmostEqual(result["comparison"]["estimated_savings_credits"], 69.325)
        config = load_config()
        config["comparison_model"] = "gpt-6-sol"
        save_user_config(config)
        self.assertAlmostEqual(_stats_payload("all")["comparison"]["estimated_equivalent_credits"], 73.5)
        self.assertEqual(_stats_payload("all", "gpt-5.6-sol")["comparison"]["model"], "gpt-5.6-sol")
        self.assertEqual(load_config()["comparison_model"], "gpt-6-sol")
        self.assertEqual(result["by_model"][0]["tokens"], 1290000)

    def test_setup_only_produces_no_work_or_savings(self):
        self.record(task_kind="setup-smoke")
        result = aggregate("all")
        self.assertEqual(result["workers"]["total"], 0)
        self.assertEqual(result["comparison"]["estimated_savings_credits"], 0)
        self.assertEqual(result["setup_runs_excluded"], 1)
        rendered = _render_stats(result, "all")
        self.assertIn("No worker runs yet", rendered)
        self.assertNotIn("setup check", rendered)
        self.assertNotIn("credits saved", rendered)
        self.assertNotIn("0%", rendered)

    def test_failed_and_escalated_work_has_cost_but_no_counterfactual_savings(self):
        self.record()
        self.record("bad", error_type="WorkerError", actual_worker_credits=1.0)
        self.record("escalated", needs_escalation=True, actual_worker_credits=1.0)
        result = aggregate("all")
        self.assertEqual(result["workers"]["succeeded"], 1)
        self.assertEqual(result["workers"]["failed"], 1)
        self.assertEqual(result["workers"]["escalations"], 1)
        self.assertAlmostEqual(result["comparison"]["estimated_equivalent_credits"], 73.0)
        self.assertAlmostEqual(result["comparison"]["estimated_savings_credits"], 67.325)
        rendered = _render_stats(result, "all")
        self.assertIn("1 needed review", rendered)
        self.assertIn("Outcomes", rendered)
        self.assertNotIn("need primary review", rendered)

    def test_failed_history_filters_setup_and_escalations_and_honors_period(self):
        self.record("successful", created_at="2026-01-01T00:00:00+00:00")
        self.record("old-error", error_type="WorkerError", created_at="2026-01-02T00:00:00+00:00")
        self.record("failed", status="failed", created_at="2026-01-03T00:00:00+00:00")
        self.record("review", needs_escalation=True, created_at="2026-01-04T00:00:00+00:00")
        self.record("setup", task_kind="setup-smoke", error_type="WorkerError",
                    created_at="2026-01-05T00:00:00+00:00")
        self.record("flagged-error", error_type="timeout", needs_escalation=True,
                    session_id="chat-id", turn_id="turn-id", created_at="2026-01-06T00:00:00+00:00")
        result = _failures_payload("2026-01-03T00:00:00Z")
        self.assertEqual([row["id"] for row in result["failures"]], ["flagged-error", "failed"])
        rendered = _render_failures(result, result["since"])
        self.assertIn("timeout", rendered)
        self.assertIn("Run flagged-error", rendered)
        self.assertIn("Chat chat-id", rendered)
        self.assertIn("Turn turn-id", rendered)
        self.assertIn("Unspecified error", rendered)

    def test_failure_json_keeps_all_records_and_default_stats_contract(self):
        for number in range(12):
            self.record(f"failure-{number}", status="failed", error_type="WorkerError",
                        created_at=f"2026-01-{number + 1:02d}T00:00:00+00:00")
        parser = build_parser()
        args = parser.parse_args(["stats", "--failures", "--since", "all", "--json", "--color", "always"])
        output = io.StringIO()
        with redirect_stdout(output), patch("codex_shunt.cli._stats_payload", side_effect=AssertionError):
            self.assertEqual(args.handler(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(len(result["failures"]), 12)
        self.assertEqual(result["failures"][0]["id"], "failure-11")
        self.assertNotIn("\033[", output.getvalue())
        rendered = _render_failures(result, "all")
        self.assertIn("newest 10 shown", rendered)
        self.assertEqual(rendered.count("Run failure-"), 10)

        args = parser.parse_args(["stats", "--since", "all", "--json"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(args.handler(args), 0)
        normal = json.loads(output.getvalue())
        self.assertEqual(normal["workers"]["failed"], 12)
        self.assertIn("comparison", normal)
        self.assertNotIn("failures", normal)

    def test_empty_failed_history_does_not_treat_review_as_failure(self):
        self.record(needs_escalation=True)
        result = _failures_payload("all")
        self.assertEqual(result["failures"], [])
        self.assertIn("No failed worker runs", _render_failures(result, "all"))

    def test_stats_label_estimates_and_accept_per_command_comparison(self):
        self.record()
        result = aggregate("all")
        rendered = _render_stats(result, "all", color=False)
        self.assertIn("gpt-6.1-sol", rendered)
        self.assertIn("Estimated vs", rendered)
        self.assertNotIn("Standard rates", rendered)
        self.assertNotIn("Not measured account charges", rendered)
        self.assertIn("Not measured account charges", result["comparison"]["basis"])
        self.assertNotIn("exact", rendered.lower())
        self.assertNotIn("CONTEXT AVOIDED", rendered)
        self.assertIn("\033[", _render_stats(result, "all", color=True))
        args = build_parser().parse_args(["stats", "--compare-to", "gpt-6-sol", "--color", "never"])
        self.assertEqual(args.compare_to, "gpt-6-sol")
        with patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertFalse(_color_enabled("auto"))


class WorkerValidationRegressions(unittest.TestCase):
    def test_invalid_result_is_recorded_as_failure_and_runtime_flags_remain_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("line\n")
            selection = collect_sources(root, ["file.txt"], max_files=1, max_bytes=1000)
            config = load_config()
            config["source_sharing_acknowledged"] = True
            records = []
            def fake_exec(command, **kwargs):
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertIn("--ignore-user-config", command)
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                result_path = Path(command[command.index("--output-last-message") + 1])
                result_path.write_text(json.dumps({"summary": 123, "findings": []}))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(worker, "find_codex", return_value="/fake/codex"), \
                 patch.object(worker, "ensure_chatgpt_auth"), \
                 patch.object(worker, "prepare_windows_workspace"), \
                 patch.object(worker.subprocess, "run", side_effect=fake_exec), \
                 patch.object(worker, "record_worker_run", side_effect=records.append):
                with self.assertRaises(worker.WorkerError):
                    worker.run_worker(question="read", selection=selection, config=config, task_kind="test")
            self.assertEqual(records[0]["status"], "failed")
            self.assertEqual(records[0]["error_type"], "WorkerError")

    def test_only_approved_source_citations_are_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.txt").write_text("line\n")
            (root / ".codex-shunt-result.json").write_text("result\n")
            base = {"summary": "summary", "limitations": [], "recommended_reads": [], "needs_escalation": False}
            for filename in [".codex-shunt-result.json", "../outside.txt", "source.txt"]:
                result = {**base, "findings": [{"file": filename, "line_start": 1, "line_end": 1,
                                               "claim": "claim", "confidence": 1.0}]}
                if filename == "source.txt":
                    self.assertEqual(worker._validate_result(result, root, 10000, {"source.txt"}), (1, 1))
                else:
                    with self.assertRaises(worker.WorkerError):
                        worker._validate_result(result, root, 10000, {"source.txt"})

    def test_empty_findings_require_escalation_and_keep_the_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = {"summary": "Unable to read file", "findings": [],
                      "limitations": ["Access denied by sandbox"], "recommended_reads": [],
                      "needs_escalation": False}
            with self.assertRaisesRegex(worker.WorkerError, "Access denied by sandbox"):
                worker._validate_result(result, Path(temporary), 10000, {"source.txt"})
            result["needs_escalation"] = True
            self.assertEqual(worker._validate_result(result, Path(temporary), 10000, {"source.txt"}), (0, 0))


if __name__ == "__main__":
    unittest.main()
