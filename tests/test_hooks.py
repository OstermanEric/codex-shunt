from __future__ import annotations

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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.db import aggregate
from codex_shunt.hooks import hook_post, hook_pre


class PreHookTests(unittest.TestCase):
    def _event(self, root: Path, file: Path) -> dict[str, object]:
        return {
            "session_id": "session-test",
            "turn_id": "turn-test",
            "cwd": str(root),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"cat {file.name}"},
            "model": "gpt-5.6-sol",
        }

    def test_strict_off_records_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.txt"
            source.write_text("line\n" * 600, encoding="utf-8")
            event = self._event(root, source)
            output = io.StringIO()
            environment = {
                "CODEX_SHUNT_DATA_DIR": str(root / "data"),
                "CODEX_SHUNT_STRICT_ROUTING": "false",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "sys.stdin", io.StringIO(json.dumps(event))
            ), redirect_stdout(output):
                self.assertEqual(hook_pre(), 0)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(aggregate("all")["routing"]["routed"], 1)

    def test_strict_mode_routes_large_read_and_denies_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.txt"
            source.write_text("line\n" * 600, encoding="utf-8")
            event = self._event(root, source)
            output = io.StringIO()
            environment = {
                "CODEX_SHUNT_DATA_DIR": str(root / "data"),
                "CODEX_SHUNT_STRICT_ROUTING": "true",
            }
            outcome = SimpleNamespace(
                run_id="run-routed",
                result={
                    "summary": "Compact worker summary.",
                    "findings": [
                        {
                            "file": "large.txt",
                            "line_start": 1,
                            "line_end": 2,
                            "claim": "The file contains repeated lines.",
                        }
                    ],
                    "limitations": [],
                },
            )
            with patch.dict(os.environ, environment, clear=False), patch(
                "sys.stdin", io.StringIO(json.dumps(event))
            ), patch(
                "codex_shunt.hooks.run_worker", return_value=outcome
            ) as worker, redirect_stdout(output):
                self.assertEqual(hook_pre(), 0)
            response = json.loads(output.getvalue())
            hook_output = response["hookSpecificOutput"]
            self.assertEqual(hook_output["permissionDecision"], "deny")
            self.assertIn("Compact worker summary", hook_output["permissionDecisionReason"])
            self.assertIn("original read was not executed", hook_output["permissionDecisionReason"])
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(worker.call_args.kwargs["task_kind"], "hook-routed-read")

    def test_strict_mode_fails_open_when_worker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.txt"
            source.write_text("line\n" * 600, encoding="utf-8")
            event = self._event(root, source)
            output = io.StringIO()
            environment = {
                "CODEX_SHUNT_DATA_DIR": str(root / "data"),
                "CODEX_SHUNT_STRICT_ROUTING": "true",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "sys.stdin", io.StringIO(json.dumps(event))
            ), patch(
                "codex_shunt.hooks.run_worker", side_effect=RuntimeError("worker unavailable")
            ), redirect_stdout(output):
                self.assertEqual(hook_pre(), 0)
                summary = aggregate("all")["routing"]
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(summary["enforced"], 0)

    def test_strict_mode_allows_targeted_range_from_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.txt"
            source.write_text("line\n" * 600, encoding="utf-8")
            event = self._event(root, source)
            event["tool_input"] = {"command": "sed -n '1,40p' large.txt"}
            output = io.StringIO()
            environment = {
                "CODEX_SHUNT_DATA_DIR": str(root / "data"),
                "CODEX_SHUNT_STRICT_ROUTING": "true",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "sys.stdin", io.StringIO(json.dumps(event))
            ), patch("codex_shunt.hooks.run_worker") as worker, redirect_stdout(output):
                self.assertEqual(hook_pre(), 0)
            self.assertEqual(output.getvalue(), "")
            worker.assert_not_called()

    def test_strict_off_post_hook_records_without_replacing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = {
                "session_id": "session-test",
                "turn_id": "turn-test",
                "cwd": str(root),
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "pnpm test"},
                "tool_response": "failure line\n" * 10000,
                "model": "gpt-5.6-sol",
            }
            output = io.StringIO()
            environment = {
                "CODEX_SHUNT_DATA_DIR": str(root / "data"),
                "CODEX_SHUNT_STRICT_ROUTING": "false",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "sys.stdin", io.StringIO(json.dumps(event))
            ), redirect_stdout(output):
                self.assertEqual(hook_post(), 0)
                self.assertEqual(output.getvalue(), "")
                summary = aggregate("all")["routing"]
                self.assertEqual(summary["total"], 1)
                self.assertEqual(summary["routed"], 1)


if __name__ == "__main__":
    unittest.main()
