from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.db import aggregate, record_routing_event, record_worker_run


class DatabaseTests(unittest.TestCase):
    def test_aggregate_keeps_exact_and_estimated_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}, clear=False
        ):
            record_routing_event(
                decision="would_route",
                reason="large-read",
                estimated_source_tokens=25000,
            )
            record_worker_run(
                {
                    "id": "run-test",
                    "worker_model": "gpt-5.6-luna",
                    "task_kind": "test",
                    "question_hash": "question",
                    "repository_hash": "repo",
                    "source_files": 1,
                    "excluded_files": 0,
                    "source_bytes": 100000,
                    "source_lines": 600,
                    "estimated_source_tokens": 25000,
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "actual_worker_credits": 0.008,
                    "sol_equivalent_credits": 0.15,
                    "status": "success",
                    "citation_count": 2,
                    "valid_citation_count": 2,
                }
            )
            result = aggregate("all")
            self.assertEqual(result["routing"]["estimated_tokens"], 25000)
            self.assertEqual(result["routing"]["estimated_intercepted_tokens"], 0)
            self.assertEqual(result["routing"]["enforced"], 0)
            self.assertEqual(result["workers"]["input_tokens"], 1000)
            self.assertEqual(result["workers"]["valid_citations"], 2)


if __name__ == "__main__":
    unittest.main()
