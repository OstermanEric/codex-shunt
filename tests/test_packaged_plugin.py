from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class PackagedPluginTests(unittest.TestCase):
    def test_public_marketplace_manifest_points_to_github_plugin(self) -> None:
        marketplace_path = PLUGIN_ROOT / ".agents" / "plugins" / "marketplace.json"
        payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], "codex-shunt")
        self.assertEqual(payload["plugins"][0]["name"], "codex-shunt")
        self.assertEqual(payload["plugins"][0]["source"]["source"], "url")
        self.assertEqual(
            payload["plugins"][0]["source"]["url"],
            "https://github.com/OstermanEric/codex-shunt",
        )
        self.assertEqual(payload["plugins"][0]["source"]["ref"], "main")

    def _copy_to_fresh_cache(self, temporary: str) -> Path:
        cache_root = (
            Path(temporary)
            / "cache"
            / "personal"
            / "codex-shunt"
            / "0.1.0+codex.clean-install"
        )
        shutil.copytree(
            PLUGIN_ROOT,
            cache_root,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        return cache_root

    def _fake_codex(self, root: Path) -> tuple[Path, Path]:
        executable = root / "codex"
        log_path = root / "worker-invocation.json"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "if sys.argv[1:3] == ['login', 'status']:\n"
            "    print('Logged in using ChatGPT')\n"
            "    raise SystemExit(0)\n"
            "args = sys.argv[1:]\n"
            "result_path = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
            "result_path.write_text(json.dumps({\n"
            "    'summary': 'Fresh-cache worker result.',\n"
            "    'findings': [{'claim': 'The fixture has two lines.', "
            "'file': 'sample.txt', 'line_start': 1, 'line_end': 2, "
            "'confidence': 1.0}],\n"
            "    'limitations': [],\n"
            "    'needs_escalation': False,\n"
            "}), encoding='utf-8')\n"
            "pathlib.Path(os.environ['FAKE_CODEX_LOG']).write_text(json.dumps({\n"
            "    'args': args, 'worker': os.environ.get('CODEX_SHUNT_WORKER')\n"
            "}), encoding='utf-8')\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {\n"
            "    'input_tokens': 100, 'cached_input_tokens': 10, "
            "'output_tokens': 20, 'reasoning_output_tokens': 5\n"
            "}}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable, log_path

    def test_fresh_cache_copy_runs_bundled_direct_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = self._copy_to_fresh_cache(temporary)
            fixture = Path(temporary) / "fixture"
            fixture.mkdir()
            (fixture / "sample.txt").write_text("first\nsecond\n", encoding="utf-8")
            fake_codex, log_path = self._fake_codex(Path(temporary))
            data_dir = Path(temporary) / "plugin-data"
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_SHUNT_CODEX_PATH": str(fake_codex),
                    "CODEX_SHUNT_DATA_DIR": str(data_dir),
                    "CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED": "true",
                    "FAKE_CODEX_LOG": str(log_path),
                }
            )

            completed = subprocess.run(
                [
                    str(cache_root / "scripts" / "codex-shunt"),
                    "inspect",
                    "--root",
                    str(fixture),
                    "--question",
                    "What is in the fixture?",
                    "--json",
                    "sample.txt",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["result"]["summary"], "Fresh-cache worker result.")
            invocation = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(invocation["worker"], "1")
            disable_index = invocation["args"].index("--disable")
            self.assertEqual(invocation["args"][disable_index + 1], "hooks")
            schema_argument = invocation["args"][invocation["args"].index("--output-schema") + 1]
            self.assertEqual(
                Path(schema_argument).resolve(),
                (cache_root / "schemas" / "worker-result.schema.json").resolve(),
            )
            self.assertTrue((data_dir / "metrics.sqlite3").is_file())

    def test_missing_bundled_runner_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = self._copy_to_fresh_cache(temporary)
            (cache_root / "scripts" / "codex-shunt").unlink()
            completed = subprocess.run(
                [str(cache_root / "scripts" / "codex-shunt-hook"), "pre"],
                input="{}",
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PLUGIN_ROOT": str(cache_root)},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")

    def test_hook_runs_from_fresh_cache_and_uses_plugin_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = self._copy_to_fresh_cache(temporary)
            fixture = Path(temporary) / "fixture"
            fixture.mkdir()
            source = fixture / "large.txt"
            source.write_text("line\n" * 600, encoding="utf-8")
            plugin_data = Path(temporary) / "plugin-data"
            event = {
                "session_id": "fresh-cache-session",
                "turn_id": "fresh-cache-turn",
                "cwd": str(fixture),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"cmd": "rtk cat large.txt"},
                "model": "gpt-5.6-sol",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": temporary,
                    "PLUGIN_ROOT": str(cache_root),
                    "PLUGIN_DATA": str(plugin_data),
                }
            )

            completed = subprocess.run(
                [str(cache_root / "scripts" / "codex-shunt-hook"), "pre"],
                input=json.dumps(event),
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertTrue((plugin_data / "metrics.sqlite3").is_file())

    def test_strict_hook_routes_directly_from_fresh_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = self._copy_to_fresh_cache(temporary)
            fixture = Path(temporary) / "fixture"
            fixture.mkdir()
            source = fixture / "large.txt"
            source.write_text("line\n" * 600, encoding="utf-8")
            fake_codex, log_path = self._fake_codex(Path(temporary))
            plugin_data = Path(temporary) / "plugin-data"
            event = {
                "session_id": "strict-cache-session",
                "turn_id": "strict-cache-turn",
                "cwd": str(fixture),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"cmd": "rtk cat large.txt"},
                "model": "gpt-5.6-sol",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": temporary,
                    "PLUGIN_ROOT": str(cache_root),
                    "PLUGIN_DATA": str(plugin_data),
                    "CODEX_SHUNT_CODEX_PATH": str(fake_codex),
                    "CODEX_SHUNT_STRICT_ROUTING": "true",
                    "CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED": "true",
                    "FAKE_CODEX_LOG": str(log_path),
                }
            )

            completed = subprocess.run(
                [str(cache_root / "scripts" / "codex-shunt-hook"), "pre"],
                input=json.dumps(event),
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            response = json.loads(completed.stdout)
            hook_output = response["hookSpecificOutput"]
            self.assertEqual(hook_output["permissionDecision"], "deny")
            self.assertIn("Fresh-cache worker result", hook_output["permissionDecisionReason"])
            self.assertTrue(log_path.is_file())


if __name__ == "__main__":
    unittest.main()
