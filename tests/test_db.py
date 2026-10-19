from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.db import aggregate, fetch_rows, record_routing_event, record_worker_run


class DatabaseTests(unittest.TestCase):
    def legacy_database(self, temporary):
        path = Path(temporary) / 'metrics.sqlite3'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('''CREATE TABLE routing_events (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT,
                turn_id TEXT, parent_model TEXT, tool_name TEXT, decision TEXT NOT NULL,
                reason TEXT, source_files INTEGER DEFAULT 0, source_bytes INTEGER DEFAULT 0,
                source_lines INTEGER DEFAULT 0, estimated_source_tokens INTEGER DEFAULT 0)''')
            connection.execute("INSERT INTO routing_events (id, created_at, decision, estimated_source_tokens) "
                               "VALUES ('legacy', '2026-10-01T00:00:00+00:00', 'deny', 25000)")
        return path

    def test_additive_migration_preserves_history_and_coverage_denominator(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {'CODEX_SHUNT_DATA_DIR': temporary}):
            self.legacy_database(temporary)
            record_routing_event(decision='unsupported', reason='evaluation-or-escape',
                                 read_family='get-content', measurement='unmeasured')
            record_routing_event(decision='would_route', reason='large-read', read_family='batch',
                                 read_features=('batch', 'file-array'), measurement='requested-text',
                                 estimated_source_tokens=10000)
            payload = aggregate('all')
            self.assertEqual(payload['routing']['routed'], 1)
            self.assertEqual(payload['routing']['estimated_source_tokens_intercepted'], 25000)
            self.assertEqual(payload['coverage']['calls'], 2)
            self.assertEqual(payload['coverage']['unsupported'], 1)
            self.assertEqual(payload['coverage']['unmeasured'], 1)
            self.assertEqual(payload['coverage']['estimated_unrouted_tokens'], 10000)
            self.assertAlmostEqual(payload['coverage']['estimated_unrouted_input_credits'], .5)
            self.assertEqual(len(payload['coverage']['by_feature']), 2)
            self.assertEqual(len(fetch_rows('routing_events', 'all')), 3)

    def test_readonly_legacy_reporting_does_not_migrate_stores(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {'CODEX_SHUNT_DATA_DIR': temporary}):
            path = self.legacy_database(temporary)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute('CREATE TABLE worker_runs (id TEXT, created_at TEXT)')
            payload = aggregate('all')
            self.assertEqual(payload['coverage']['calls'], 0)
            self.assertEqual(payload['routing']['routed'], 1)
            with closing(sqlite3.connect(path)) as connection:
                columns = {row[1] for row in connection.execute('PRAGMA table_info(routing_events)')}
            self.assertNotIn('measurement', columns)

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
            self.assertEqual(result["routing"]["observed"], 1)
            self.assertEqual(result["routing"]["estimated_source_tokens_intercepted"], 0)
            self.assertEqual(result["routing"]["routed"], 0)
            self.assertEqual(result["workers"]["input_tokens"], 1000)
            self.assertEqual(result["workers"]["valid_citations"], 2)

    def test_default_stats_merges_home_and_codex_plugin_stores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home_data = home / ".local" / "share" / "codex-shunt"
            plugin_data = root / "codex-home" / "plugins" / "data" / "codex-shunt-personal"

            with patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "CODEX_HOME": str(root / "codex-home"),
                    "PLUGIN_DATA": str(plugin_data),
                },
                clear=True,
            ):
                record_worker_run(
                    {
                        "id": "plugin-run",
                        "worker_model": "gpt-5.6-luna",
                        "task_kind": "hook-routed-read",
                        "question_hash": "plugin-question",
                        "repository_hash": "plugin-repo",
                        "status": "success",
                    }
                )
                with patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": str(home_data)}, clear=False):
                    record_worker_run(
                        {
                            "id": "home-run",
                            "worker_model": "gpt-5.6-luna",
                            "task_kind": "repository-inspection",
                            "question_hash": "home-question",
                            "repository_hash": "home-repo",
                            "status": "success",
                        }
                    )

                result = aggregate("all")

            self.assertEqual(result["workers"]["total"], 2)
            self.assertEqual(result["by_model"][0]["runs"], 2)

    def test_explicit_data_dir_stays_isolated_from_discovered_stores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_data = root / "codex-home" / "plugins" / "data" / "codex-shunt-personal"
            explicit = root / "explicit"
            with patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "USERPROFILE": str(root),
                    "CODEX_HOME": str(root / "codex-home"),
                    "PLUGIN_DATA": str(plugin_data),
                },
                clear=True,
            ):
                record_worker_run(
                    {
                        "id": "plugin-run",
                        "worker_model": "gpt-5.6-luna",
                        "task_kind": "hook-routed-read",
                        "question_hash": "plugin-question",
                        "repository_hash": "plugin-repo",
                        "status": "success",
                    }
                )
                with patch.dict(os.environ, {"CODEX_SHUNT_DATA_DIR": str(explicit)}, clear=False):
                    result = aggregate("all")

            self.assertEqual(result["workers"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
