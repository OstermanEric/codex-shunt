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
    if (($args -join ' ') -match 'from codex_shunt.worker import find_codex') {
        $env:SHUNT_TEST_PLUGIN
        $global:LASTEXITCODE = 0
    } else {
        $arguments = @($args | Select-Object -Skip 1)
        if ($args[-1] -eq 'setup') { $arguments += '--accept-source-sharing' }
        & $env:SHUNT_TEST_PYTHON @arguments
    }
}
. (Join-Path $env:SHUNT_TEST_ROOT 'install.ps1')
Install-Shunt
& (Join-Path $HOME '.local\bin\shunt.cmd') stats --since all --json
if ($LASTEXITCODE -ne 0) { throw 'Launcher failed' }
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
            self.assertEqual(calls.count("plugin marketplace add"), 2)
            self.assertIn('"setup_runs_excluded": 2', completed.stdout)
