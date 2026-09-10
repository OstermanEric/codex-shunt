from __future__ import annotations

import sys
import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.worker import (
    LUNA_RATES,
    SOL_RATES,
    _with_outer_sandbox_hint,
    credits_for,
    find_codex,
    parse_usage,
)


class WorkerUsageTests(unittest.TestCase):
    def test_finds_codex_from_explicit_override_when_not_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "codex"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with patch.dict(
                environ, {"CODEX_SHUNT_CODEX_PATH": str(executable)}, clear=False
            ), patch("codex_shunt.worker.shutil.which", return_value=None):
                self.assertEqual(find_codex(), str(executable))

    def test_parses_final_turn_usage(self) -> None:
        stream = "\n".join(
            [
                '{"type":"thread.started","thread_id":"abc"}',
                '{"type":"turn.completed","usage":{"input_tokens":1200000,"cached_input_tokens":200000,"output_tokens":90000,"reasoning_output_tokens":10000}}',
            ]
        )
        usage = parse_usage(stream)
        self.assertEqual(usage["input_tokens"], 1_200_000)
        self.assertEqual(usage["cached_input_tokens"], 200_000)
        self.assertEqual(usage["output_tokens"], 90_000)
        self.assertEqual(usage["reasoning_output_tokens"], 10_000)

    def test_calculates_luna_and_sol_counterfactual(self) -> None:
        usage = {
            "input_tokens": 1_200_000,
            "cached_input_tokens": 200_000,
            "output_tokens": 90_000,
            "reasoning_output_tokens": 10_000,
        }
        self.assertAlmostEqual(credits_for(usage, LUNA_RATES), 7.8)
        self.assertAlmostEqual(credits_for(usage, SOL_RATES), 147.0)

    def test_nested_codex_failure_explains_scoped_outer_approval(self) -> None:
        detail = _with_outer_sandbox_hint(
            "Error: failed to initialize in-process app-server client: Operation not permitted"
        )
        self.assertIn("narrowly scoped elevated/unsandboxed", detail)
        self.assertIn("--sandbox read-only", detail)

    def test_unrelated_worker_failure_is_not_rewritten(self) -> None:
        detail = "Model is unavailable"
        self.assertEqual(_with_outer_sandbox_hint(detail), detail)


if __name__ == "__main__":
    unittest.main()
