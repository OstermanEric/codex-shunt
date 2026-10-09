from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.cli import _color_enabled, _render_stats, build_parser, command_setup
from codex_shunt.config import load_config, save_user_config
from codex_shunt.worker import WorkerError


class SetupTests(unittest.TestCase):
    def _args(self, **overrides: bool) -> argparse.Namespace:
        values = {
            "accept_source_sharing": False,
            "test_worker": False,
            "enable_strict_routing": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_noninteractive_setup_requires_explicit_acceptance(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}), \
                patch("sys.stdin.isatty", return_value=False), redirect_stdout(output), redirect_stderr(errors):
            status = command_setup(self._args())
        self.assertEqual(status, 2)
        self.assertIn("separate GPT-6 Luna Codex invocation", output.getvalue())
        self.assertIn("No configuration was changed", errors.getvalue())

    def test_setup_records_consent_tests_worker_then_enables_strict_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"CODEX_SHUNT_DATA_DIR": temporary}
            outcome = SimpleNamespace(
                worker_model="gpt-5.6-luna",
                result={"needs_escalation": False},
                duration_ms=100,
                valid_citation_count=1,
                citation_count=1,
            )
            checks = [{"name": "ready", "ok": True, "detail": "ready"}]
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "codex_shunt.cli._doctor_checks", return_value=checks
            ), patch("codex_shunt.cli.run_worker", return_value=outcome) as worker, redirect_stdout(
                output
            ):
                status = command_setup(
                    self._args(
                        accept_source_sharing=True,
                        enable_strict_routing=True,
                    )
                )
                config = load_config()
            self.assertEqual(status, 0)
            self.assertTrue(config["source_sharing_acknowledged"])
            self.assertTrue(config["strict_routing"])
            worker.assert_called_once()
            self.assertIn("enabled after successful worker verification", output.getvalue())

    def test_failed_setup_disables_default_routing_before_worker_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"CODEX_SHUNT_DATA_DIR": temporary}
            checks = [{"name": "runtime", "ok": False, "detail": "unavailable"}]
            with patch.dict(os.environ, environment, clear=True), patch(
                "codex_shunt.cli._doctor_checks", return_value=checks
            ), patch("codex_shunt.cli.run_worker") as worker, redirect_stdout(io.StringIO()):
                self.assertTrue(load_config()["strict_routing"])
                status = command_setup(build_parser().parse_args(["setup", "--accept-source-sharing"]))
                config = load_config()
            self.assertEqual(status, 1)
            self.assertTrue(config["source_sharing_acknowledged"])
            self.assertFalse(config["strict_routing"])
            worker.assert_not_called()

    def test_retry_reuses_saved_consent_and_reports_worker_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}):
            config = load_config()
            config["source_sharing_acknowledged"] = True
            save_user_config(config)
            outcome = SimpleNamespace(result={"needs_escalation": True, "summary": "Cannot read the fixture",
                                               "limitations": ["Sandbox setup required"]},
                                      citation_count=0, valid_citation_count=0)
            with patch("builtins.input", side_effect=AssertionError("Asked for consent again")), \
                    patch("codex_shunt.cli._doctor_checks", return_value=[{"name": "ready", "ok": True, "detail": "ready"}]), \
                    patch("codex_shunt.cli.run_worker", return_value=outcome), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(WorkerError, "Sandbox setup required"):
                    command_setup(build_parser().parse_args(["setup"]))
            self.assertFalse(load_config()["strict_routing"])

    def test_verification_failure_explains_installed_state_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}), \
                patch("codex_shunt.cli._doctor_checks", return_value=[{"name": "ready", "ok": True, "detail": "ready"}]), \
                patch("codex_shunt.cli.run_worker", side_effect=WorkerError("No cited evidence")), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(WorkerError, "Shunt is installed.*Luna verification failed") as raised:
                command_setup(build_parser().parse_args(["setup", "--accept-source-sharing"]))
            self.assertIn("reinstalling is unnecessary", str(raised.exception))
            self.assertIn("No cited evidence", str(raised.exception))
            self.assertFalse(load_config()["strict_routing"])




if __name__ == "__main__":
    unittest.main()
