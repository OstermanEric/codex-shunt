"""Native paths and conservative PowerShell reads, plus Windows integration."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codex_shunt.reads import parse_read, read_metrics
from codex_shunt.sources import collect_sources
from codex_shunt.worker import find_codex
from codex_shunt import worker
import test_packaged_plugin as packaged


class WindowsReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "src").mkdir()
        self.source = self.root / "src" / "space name.txt"
        self.source.write_bytes(b"line\r\n" * 600)
        self.selection = collect_sources(self.root, [str(self.source)], max_files=1, max_bytes=100000)
        platform = patch("codex_shunt.reads.sys.platform", "win32")
        platform.start()
        self.addCleanup(platform.stop)

    def test_explicit_paths_aliases_and_line_limits(self):
        for command, lines in [
            ('Get-Content -LiteralPath "src\\space name.txt"', 600),
            ('get-content "src\\space name.txt" -Raw', 600),
            ('GC -Path "src\\space name.txt" -TotalCount 40', 40),
            ('cat "src\\space name.txt" -Tail 20', 20),
            ('type "src\\space name.txt" -TotalCount 0', 0),
            (f"Get-Content -LiteralPath '{self.source}'", 600),
        ]:
            with self.subTest(command=command):
                read = parse_read(command, self.root, 1)
                self.assertIsNotNone(read)
                self.assertEqual(read.paths, (self.source.resolve(),))
                self.assertEqual(read_metrics(read, self.selection)["source_lines"], lines)

    def test_shell_evaluation_wildcards_and_unsupported_flags_stay_raw(self):
        for suffix in [
            '-Encoding UTF8', '-Raw -Tail 2', '-TotalCount -1', '-Tail 1 -TotalCount 2',
            '| Select-Object -First 2', '; Set-Content out.txt changed',
            '> out.txt', '$(Get-Location)', '-Path other.txt', '"-Raw"',
        ]:
            with self.subTest(suffix=suffix):
                self.assertIsNone(parse_read(f'Get-Content "src\\space name.txt" {suffix}', self.root, 1))
        for command in ["Get-Content src\\*.txt", "Get-Content 'src\\*.txt'",
                        "Get-Content @('file.txt')", "Get-Content a.txt,b.txt",
                        'Get-Content "$env:SOURCE"', 'Get-Content FileSystem::file.txt',
                        'Get-Content file.txt:stream', '"Get-Content" file.txt']:
            self.assertIsNone(parse_read(command, self.root, 1))

    def test_literal_path_does_not_expand_brackets(self):
        source = self.root / "[fixture].txt"
        source.write_text("fixture", encoding="utf-8")
        self.assertIsNone(parse_read("Get-Content '[fixture].txt'", self.root, 1))
        self.assertEqual(parse_read("Get-Content -LiteralPath '[fixture].txt'", self.root, 1).paths,
                         (source.resolve(),))

    def test_discovers_native_executable_and_skips_batch_shims(self):
        executable = self.root / "codex.exe"
        executable.write_bytes(b"fixture")
        executable.chmod(0o755)
        shim = self.root / "codex.cmd"
        shim.write_text("fixture")
        with patch.dict(os.environ, {"CODEX_SHUNT_CODEX_PATH": str(shim)}), \
                patch("codex_shunt.worker.shutil.which", return_value=str(executable)) as which:
            self.assertEqual(find_codex(), str(executable))
            which.assert_called_once_with("codex.exe")

    def test_discovers_reported_versioned_install_without_path_override(self):
        home = self.root / "home"
        local = home / "AppData" / "Local"
        older = local / "OpenAI" / "Codex" / "bin" / "older" / "codex.exe"
        newer = local / "OpenAI" / "Codex" / "bin" / "9691020b546a15b2" / "codex.exe"
        for executable in (older, newer):
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
            executable.chmod(0o755)
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=True), \
                patch.object(Path, "home", return_value=home), \
                patch.object(worker.shutil, "which", return_value=None):
            self.assertEqual(find_codex(), str(newer))

    def test_prefers_active_standalone_pointer_and_honors_custom_codex_home(self):
        codex_home = self.root / "custom codex home"
        current = codex_home / "packages" / "standalone" / "current" / "bin" / "codex.exe"
        current.parent.mkdir(parents=True)
        current.write_bytes(b"fixture")
        current.chmod(0o755)
        local = self.root / "local"
        stale = local / "OpenAI" / "Codex" / "bin" / "old" / "codex.exe"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"fixture")
        stale.chmod(0o755)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home), "LOCALAPPDATA": str(local),
                                     "USERPROFILE": str(self.root), "HOME": str(self.root)}, clear=True), \
                patch.object(worker.shutil, "which", return_value=None):
            self.assertEqual(find_codex(), str(current))

    def test_preserves_windows_sandbox_selection_without_other_user_settings(self):
        codex_home = self.root / "codex home"
        codex_home.mkdir()
        path = codex_home / "config.toml"
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            self.assertEqual(worker._windows_sandbox_args(), [])
            for mode in ("mxc", "elevated", "unelevated"):
                path.write_text(f'model="different-model"\napproval_policy="never"\n[windows]\nsandbox="{mode}"\n[features]\nprefer_mxc=true\n', encoding="utf-8")
                self.assertEqual(worker._windows_sandbox_args(),
                                 ["-c", f'windows.sandbox="{mode}"', "-c", "features.prefer_mxc=true"])
            path.write_text('[windows]\nsandbox="unknown"\n[features]\nprefer_mxc=false\n')
            self.assertEqual(worker._windows_sandbox_args(), ["-c", "features.prefer_mxc=false"])

    def test_windows_worker_launch_keeps_read_only_and_sandbox_selection(self):
        codex_home = self.root / "codex home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('[windows]\nsandbox="elevated"\n')
        def execute(command, **kwargs):
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn('--ignore-user-config', command)
            self.assertIn('windows.sandbox="elevated"', command)
            self.assertIn('Get-Content -LiteralPath', command[-1])
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
            result = {"summary": "A file read was blocked", "findings": [], "limitations": ["Sandbox not provisioned"],
                      "needs_escalation": True, "recommended_reads": []}
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(result))
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
                patch.object(worker, "find_codex", return_value="fixture.exe"), \
                patch.object(worker, "ensure_chatgpt_auth"), \
                patch.object(worker.subprocess, "run", side_effect=execute), \
                patch.object(worker, "record_worker_run") as record:
            config = {"source_sharing_acknowledged": True, "worker_model": "gpt-6-luna",
                      "reasoning_effort": "low", "worker_timeout_seconds": 20, "max_output_chars": 5000}
            outcome = worker.run_worker(question="read the file", selection=self.selection, config=config, task_kind="setup-smoke")
        self.assertTrue(outcome.result["needs_escalation"])
        self.assertEqual(outcome.valid_citation_count, 0)
        self.assertEqual(record.call_args.args[0]["status"], "escalated")
        self.assertNotIn("Sandbox not provisioned", json.dumps(record.call_args.args[0]))

    def test_python_hook_fails_open_without_runtime(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        bridge = scripts / "codex-shunt-hook.py"
        shutil.copyfile(ROOT / "scripts/codex-shunt-hook.py", bridge)
        completed = subprocess.run([sys.executable, "-I", str(bridge), "pre"],
                                   input="{}", text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_python_hook_routes_a_real_event_with_shared_runtime(self):
        fake, log = packaged.PackagedPluginTests()._fake_codex(self.root)
        command = ('Get-Content -LiteralPath "src\\space name.txt"' if os.name == "nt"
                   else 'cat "src/space name.txt"')
        event = {"cwd": str(self.root), "tool_name": "Bash", "tool_input": {"cmd": command}}
        environment = {**os.environ, "CODEX_SHUNT_CODEX_PATH": str(fake),
                       "CODEX_SHUNT_DATA_DIR": str(self.root / "data"),
                       "CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED": "true", "FAKE_CODEX_LOG": str(log)}
        completed = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/codex-shunt-hook.py"), "pre"],
                                   input=json.dumps(event), text=True, capture_output=True,
                                   env=environment, check=False, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(log.is_file())


@unittest.skipUnless(os.name == "nt", "requires native Windows PowerShell")
class WindowsIntegrationTests(unittest.TestCase):
    def test_manifest_hook_executes_in_powershell_with_spaced_plugin_path(self):
        with tempfile.TemporaryDirectory(prefix="shunt hook ") as temporary:
            helper = packaged.PackagedPluginTests()
            cache = helper._copy_to_fresh_cache(temporary)
            fake, log = helper._fake_codex(Path(temporary))
            fixture = Path(temporary) / "fixture"
            fixture.mkdir()
            (fixture / "large.txt").write_bytes(b"line\r\n" * 600)
            command = json.loads((cache / "hooks/hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["commandWindows"]
            environment = {**os.environ, "PLUGIN_ROOT": str(cache),
                           "CODEX_SHUNT_CODEX_PATH": str(fake), "FAKE_CODEX_LOG": str(log),
                           "CODEX_SHUNT_DATA_DIR": str(Path(temporary) / "data"),
                           "CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED": "true"}
            event = {"cwd": str(fixture), "tool_name": "Bash",
                     "tool_input": {"cmd": "Get-Content -LiteralPath large.txt"}}
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                       input=json.dumps(event), text=True, capture_output=True,
                                       env=environment, check=False, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertTrue(log.is_file())

    def test_powershell_installer_setup_launcher_and_repeat_install(self):
        with tempfile.TemporaryDirectory(prefix="shunt install ") as temporary:
            root = Path(temporary)
            home = root / "home with spaces"
            home.mkdir()
            helper = packaged.PackagedPluginTests()
            fake, log = helper._fake_codex(root)
            plugin = root / "fake-plugin.ps1"
            plugin.write_text("Add-Content -LiteralPath $env:SHUNT_PLUGIN_LOG -Encoding UTF8 -Value ($args -join ' ')\n$global:LASTEXITCODE = 0\n")
            harness = root / "install-test.ps1"
            harness.write_text(r'''
$ErrorActionPreference = 'Stop'
if ($HOME -ne $env:SHUNT_TEST_HOME) { throw 'Test HOME was not isolated' }
function git {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'clone') {
        $destination = $args[-1]
        New-Item -ItemType Directory -Force $destination | Out-Null
        Get-ChildItem -LiteralPath $env:SHUNT_TEST_ROOT -Force | Where-Object Name -ne '.git' | Copy-Item -Destination $destination -Recurse -Force
        New-Item -ItemType Directory (Join-Path $destination '.git') | Out-Null
    } elseif ($args -contains 'get-url') { 'https://github.com/OstermanEric/codex-shunt.git' }
}
function py {
    $arguments = @($args | Select-Object -Skip 1)
    if (($args -join ' ') -match 'from codex_shunt.worker import find_codex') {
        $discovered = & $env:SHUNT_TEST_PYTHON @arguments
        if ($LASTEXITCODE -ne 0 -or $discovered -ne $env:CODEX_SHUNT_CODEX_PATH) { throw 'Codex discovery failed' }
        $env:SHUNT_TEST_PLUGIN
        $global:LASTEXITCODE = 0
    } else {
        if ($args[-1] -eq 'setup' -and $env:SHUNT_TEST_FAIL_SETUP -eq '1') {
            Write-Host 'Worker could not verify the fixture.'
            $global:LASTEXITCODE = 1
            return
        }
        if ($args[-1] -eq 'setup') { $arguments += '--accept-source-sharing' }
        & $env:SHUNT_TEST_PYTHON @arguments
    }
}
. (Join-Path $env:SHUNT_TEST_ROOT 'install.ps1')
Install-Shunt
& (Join-Path $HOME '.local\bin\shunt.cmd') stats --since all --json
if ($LASTEXITCODE -ne 0) { throw 'Launcher failed' }
$env:SHUNT_TEST_FAIL_SETUP = '1'
Install-Shunt
''', encoding="utf-8")
            environment = {**os.environ, "USERPROFILE": str(home), "HOME": str(home),
                           "SHUNT_TEST_HOME": str(home), "SHUNT_TEST_ROOT": str(ROOT),
                           "SHUNT_TEST_PYTHON": sys.executable, "SHUNT_TEST_PLUGIN": str(plugin),
                           "SHUNT_PLUGIN_LOG": str(root / "plugin.log"),
                           "CODEX_SHUNT_CODEX_PATH": str(fake), "FAKE_CODEX_LOG": str(log),
                           "CODEX_SHUNT_DATA_DIR": str(root / "data")}
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                                       capture_output=True, text=True, env=environment, check=False, timeout=90)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            config = (root / "data/config.toml").read_text()
            self.assertIn("strict_routing = true", config)
            self.assertIn("source_sharing_acknowledged = true", config)
            self.assertTrue((home / ".local/bin/shunt.cmd").is_file())
            calls = (root / "plugin.log").read_text(encoding="utf-8-sig")
            self.assertEqual(calls.count("plugin marketplace add"), 3)
            self.assertIn('"setup_runs_excluded": 2', completed.stdout)
            self.assertIn('Shunt is installed; setup still needs attention.', completed.stdout)
            self.assertNotIn('RuntimeException', completed.stderr)
