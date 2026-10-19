"""Acceptance regressions for the October 2026 user reports."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_shunt import db, hooks, worker
from codex_shunt.cli import _failures_payload, _render_failures, _render_stats
from codex_shunt.config import load_config
from codex_shunt.reads import parse_read, read_question, requested_line_ranges
from codex_shunt.sources import collect_sources


class PipelineReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("a.txt", "b.txt"):
            (self.root / name).write_text("".join(f"line {i}\n" for i in range(1, 601)))
        self.selection = collect_sources(self.root, ["a.txt", "b.txt"], max_files=2, max_bytes=100000)

    def ranges(self, command, shell="powershell"):
        read = parse_read(command, self.root, 10, shell)
        self.assertIsNotNone(read)
        return requested_line_ranges(read, self.selection)

    def result(self, *spans):
        return {"summary": "Selected text", "needs_escalation": False,
                "limitations": [], "recommended_reads": [], "findings": [
                    {"file": file, "line_start": start, "line_end": end,
                     "claim": "Evidence", "confidence": 1.0} for file, start, end in spans]}

    def test_skip_first_is_explicitly_lines_51_through_550(self):
        read = parse_read("Get-Content a.txt | Select-Object -Skip 50 -First 500", self.root, 10, "powershell")
        ranges = requested_line_ranges(read, self.selection)
        self.assertEqual(ranges, [[("a.txt", 51, 550)]])
        question = read_question(read, self.root, ranges)
        self.assertIn("LINE OBJECTS", question)
        self.assertIn('["a.txt", 51, 550]', question)
        outcome = SimpleNamespace(run_id="test", result=self.result(("a.txt", 100, 120)))
        context = hooks._format_worker_context(outcome, ranges)
        self.assertIn("a.txt:51-550 (500 lines)", context)

    def test_resolved_ranges_preserve_counts_order_duplicates_and_boundaries(self):
        for command, expected in [
            ("Get-Content a.txt,b.txt -TotalCount 100 | Select-Object -Skip 50 -First 120",
             [[("a.txt", 51, 100), ("b.txt", 1, 70)]]),
            ("Get-Content a.txt,a.txt | Select-Object -Skip 590 -First 30",
             [[("a.txt", 591, 600), ("a.txt", 1, 20)]]),
            ("Get-Content a.txt -Tail 20 | Select-Object -Skip 5", [[("a.txt", 586, 600)]]),
            ("Get-Content a.txt | Select-Object -Skip 700", [[]]),
            ("Get-Content a.txt | Select-Object -First 0", [[]]),
            ("Get-Content a.txt -TotalCount 300\nGet-Content b.txt -Tail 300",
             [[("a.txt", 1, 300)], [("b.txt", 301, 600)]]),
        ]:
            with self.subTest(command=command):
                self.assertEqual(self.ranges(command), expected)
        self.assertEqual(self.ranges("head -n -500 a.txt", "bash"), [[("a.txt", 1, 100)]])
        self.assertEqual(self.ranges("tail -n +500 a.txt", "bash"), [[("a.txt", 500, 600)]])
        self.assertIsNone(self.ranges("head -c 500 a.txt", "bash"))

    def test_existing_but_out_of_selection_citations_are_rejected(self):
        ranges = [[("a.txt", 51, 550)]]
        for span in [("a.txt", 1, 10), ("a.txt", 50, 51), ("a.txt", 550, 551), ("b.txt", 51, 550)]:
            with self.subTest(span=span), self.assertRaises(worker.WorkerError):
                worker._validate_result(self.result(span), self.root, 10000, {"a.txt", "b.txt"}, ranges)
        self.assertEqual(worker._validate_result(self.result(("a.txt", 51, 550)), self.root,
                                               10000, {"a.txt"}, ranges), (1, 1))

    def test_omitted_batch_statement_or_file_requires_fallback(self):
        ranges = [[("a.txt", 1, 300)], [("b.txt", 301, 600)]]
        for requested in (ranges, [[*ranges[0], *ranges[1]]]):
            with self.assertRaisesRegex(worker.WorkerError, "omitted evidence"):
                worker._validate_result(self.result(("a.txt", 1, 100)), self.root, 10000,
                                        {"a.txt", "b.txt"}, requested)
        self.assertEqual(worker._validate_result(self.result(("a.txt", 1, 100), ("b.txt", 500, 600)),
                                               self.root, 10000, {"a.txt", "b.txt"}, ranges), (2, 2))

    def test_multiline_escalation_allows_the_entire_original_call(self):
        event = {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {
            "cmd": "Get-Content a.txt -TotalCount 300\nGet-Content b.txt -Tail 300", "shell": "powershell"}}
        outcome = SimpleNamespace(result={"needs_escalation": True})
        config = load_config()
        config.update(strict_routing=True, source_sharing_acknowledged=True)
        output = io.StringIO()
        with patch.object(hooks, "_read_event", return_value=event), \
                patch.object(hooks, "load_config", return_value=config), \
                patch.object(hooks, "run_worker", return_value=outcome) as run, \
                patch.object(hooks, "_record_route") as record, redirect_stdout(output):
            self.assertEqual(hooks.hook_pre(), 0)
        self.assertEqual(output.getvalue(), "")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["requested_ranges"],
                         [[("a.txt", 1, 300)], [("b.txt", 301, 600)]])
        self.assertEqual(record.call_args.args[1:3], ("route_failed", "worker-needs-primary-inspection"))


class WorkdirReportTests(unittest.TestCase):
    def test_command_only_hook_projection_reproduces_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            other = root / "shunt-synthetic"
            other.mkdir()
            (other / "fixture-a.txt").write_text("line\n" * 600)
            config = load_config()
            config.update(strict_routing=False)
            event = {"tool_name": "Bash", "cwd": str(root), "tool_input": {
                "command": "Get-Content -LiteralPath 'fixture-a.txt'"}}
            # Codex's command-only projection loses exec_command's workdir.
            for supplied, expected in ((False, "unsupported"), (True, "would_route")):
                if supplied:
                    event["tool_input"]["workdir"] = str(other)
                with patch.object(hooks, "_read_event", return_value=event), \
                        patch.object(hooks, "load_config", return_value=config), \
                        patch("codex_shunt.reads._shell", return_value=(True, "powershell")), \
                        patch.object(hooks, "_record_route") as record:
                    self.assertEqual(hooks.hook_pre(), 0)
                self.assertEqual(record.call_args.args[1], expected)
                if not supplied:
                    self.assertEqual(record.call_args.args[2], "missing-or-nonfile-path")

    def test_absolute_relative_and_separate_roots_resolve_from_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            project = base / "project"
            other = base / "other"
            project.mkdir()
            other.mkdir()
            (other / "local.txt").write_text("line\n" * 600)
            config = load_config()
            config.update(strict_routing=False)
            for directory in (other, "../other"):
                event = {"tool_name": "Bash", "cwd": str(project), "tool_input": {
                    "cmd": "Get-Content local.txt", "shell": "powershell", "workdir": str(directory)}}
                with self.subTest(directory=directory), \
                        patch.object(hooks, "_read_event", return_value=event), \
                        patch.object(hooks, "load_config", return_value=config), \
                        patch.object(hooks, "_record_route") as record:
                    self.assertEqual(hooks.hook_pre(), 0)
                    self.assertEqual(record.call_args.args[1], "would_route")
                event["tool_input"]["cmd"] = "Get-Content missing.txt"
                errors = io.StringIO()
                with patch.object(hooks, "_read_event", return_value=event), \
                        patch.object(hooks, "load_config", return_value=config), \
                        patch.object(hooks, "_record_route"), \
                        patch.dict(os.environ, {"CODEX_SHUNT_DEBUG": "1"}), redirect_stderr(errors):
                    self.assertEqual(hooks.hook_pre(), 0)
                self.assertIn("missing-or-nonfile-path", errors.getvalue())
                self.assertIn(f"effective workdir={other}", errors.getvalue())


class MetricsVisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)

    def record(self):
        db.record_worker_run({"id": "visible", "worker_model": "gpt-6-luna", "task_kind": "test",
                              "question_hash": "q", "repository_hash": "r", "status": "success"})

    def test_no_visible_store_is_unavailable_not_zero_activity(self):
        payload = db.aggregate("all")
        self.assertEqual(payload["availability"]["status"], "unavailable")
        self.assertEqual(payload["availability"]["scope"], "explicit")
        for width in (20, 40, 80):
            rendered = _render_stats(payload, "all", width=width)
            self.assertIn("unavailable", rendered)
            self.assertNotIn("No worker runs yet", rendered)
            self.assertTrue(all(len(line) <= width for line in rendered.splitlines()))
        failures = _render_failures(_failures_payload("all"), "all")
        self.assertIn("unavailable", failures)
        self.assertNotIn("No failed worker runs", failures)

    def test_unreadable_store_is_reported_and_readonly_does_not_create_data(self):
        self.record()
        with patch.object(db, "_connect_readonly", side_effect=sqlite3.OperationalError("private detail")):
            payload = db.aggregate("all")
        self.assertEqual(payload["availability"]["status"], "unavailable")
        self.assertEqual(payload["availability"]["issues"][0]["reason"], "store-unavailable")
        self.assertNotIn("private detail", json.dumps(payload))
        with patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": str(self.root / "absent")}):
            db.aggregate("all")
        self.assertFalse((self.root / "absent").exists())

    def test_empty_readable_store_and_unavailable_store_are_distinct(self):
        db.record_routing_event(decision="below_threshold")
        payload = db.aggregate("all")
        self.assertEqual(payload["availability"]["status"], "complete")
        self.assertIn("No worker runs yet", _render_stats(payload, "all"))

    def test_plugin_discovery_permission_failure_keeps_visible_data_partial(self):
        self.record()
        plugin_root = self.root / "codex" / "plugins" / "data"
        original = Path.iterdir

        def iterdir(path):
            if path == plugin_root:
                raise PermissionError("hidden by sandbox")
            return original(path)

        with patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": "", "PLUGIN_DATA": str(self.root / "data"),
                                     "CODEX_HOME": str(self.root / "codex")}), \
                patch.object(db, "_default_data_dir", return_value=self.root / "data"), \
                patch.object(Path, "iterdir", iterdir):
            payload = db.aggregate("all")
        self.assertEqual(payload["workers"]["total"], 1)
        self.assertEqual(payload["availability"]["status"], "partial")
        self.assertIn({"path": str(plugin_root), "reason": "discovery-unavailable"}, payload["availability"]["issues"])
        self.assertIn("Incomplete metrics", _render_stats(payload, "all"))

    def test_permission_error_from_stat_is_not_silently_treated_as_missing(self):
        path = self.root / "data" / "metrics.sqlite3"
        original = Path.stat

        def stat_path(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("hidden")
            return original(candidate, *args, **kwargs)

        with patch.object(Path, "stat", stat_path):
            payload = db.aggregate("all")
        self.assertEqual(payload["availability"]["status"], "unavailable")
        self.assertTrue(payload["availability"]["issues"])


if __name__ == "__main__":
    unittest.main()
