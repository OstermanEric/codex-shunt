"""Real PowerShell CLM and opt-in credential-free Codex Windows sandbox checks."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_shunt import windows_workspace as windows


@unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell")
class ConstrainedLanguageTests(unittest.TestCase):
    def test_real_clm_reads_literal_unicode_paths_and_reports_missing_files(self):
        with tempfile.TemporaryDirectory(prefix="shunt ' [literal] 雪 ") as temporary:
            workspace = Path(temporary)
            source = workspace / "source ' [literal] 雪.txt"
            source.write_text("private source content\n", encoding="utf-8")
            manifest = workspace / ".codex-shunt-files.txt"
            manifest.write_text(source.name + "\n", encoding="utf-8")
            marker = "SHUNT_REAL_CLM"
            script = ("$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'\n"
                      + windows._probe_script(workspace, marker))
            for missing in (False, True):
                if missing:
                    source.unlink()
                result = subprocess.run(windows.powershell_command(script), capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=15)
                self.assertIn(marker + ":START", result.stdout)
                self.assertNotIn("private source content", result.stdout + result.stderr)
                self.assertNotIn("ConstrainedLanguage", result.stderr)
                if missing:
                    self.assertEqual(result.returncode, 43, result.stderr)
                    self.assertIn(":CATEGORY=ObjectNotFound", result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(marker + ":OK", result.stdout)

    def test_junction_is_rejected_before_recursive_acl_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, outside = root / "workspace", root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / ".codex-shunt-files.txt").write_text("source.txt\n")
            (workspace / "source.txt").write_text("copy\n")
            command = [str(windows._system_directory() / "cmd.exe"), "/d", "/c", "mklink", "/J",
                       str(workspace / "redirect"), str(outside)]
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaisesRegex(windows.WorkspaceError, "reparse point"):
                    windows._validate_staging(root, workspace)
            finally:
                (workspace / "redirect").rmdir()


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("CODEX_SHUNT_TEST_CODEX_PATH"),
                     "real Codex Windows backend not provisioned; set CODEX_SHUNT_TEST_CODEX_PATH")
class CodexWindowsSandboxTests(unittest.TestCase):
    def test_real_sandbox_reads_private_copy_denies_writes_and_cleans_up(self):
        codex = Path(os.environ["CODEX_SHUNT_TEST_CODEX_PATH"])
        mode = os.environ.get("CODEX_SHUNT_TEST_SANDBOX_MODE", "unelevated")
        self.assertIn(mode, {"unelevated", "elevated", "mxc"})
        environment = dict(os.environ)
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("CODEX_API_KEY", None)
        sandbox_args = ["-c", f'windows.sandbox="{mode}"', "-c", "features.prefer_mxc=false"]
        with tempfile.TemporaryDirectory(prefix="codex-shunt-real ' 雪 ") as temporary:
            root = Path(temporary)
            workspace, state, codex_home = root / "workspace", root / "state", root / "codex-home"
            for path in (workspace, state, codex_home):
                path.mkdir()
            # Ambient full access must not influence the explicit read-only probe.
            (codex_home / "config.toml").write_text('default_permissions=":danger-full-access"\n')
            environment["CODEX_HOME"] = str(codex_home)
            source = workspace / "source ' [literal] 雪.txt"
            source.write_text("approved copy\n", encoding="utf-8")
            (workspace / ".codex-shunt-files.txt").write_text(source.name + "\n", encoding="utf-8")
            original = root / "original.txt"
            original.write_text("original\n")
            original_acl = subprocess.check_output([str(windows._system_directory() / "icacls.exe"), str(original)])
            windows.prepare_windows_workspace([str(codex)], root, workspace, state, environment, sandbox_args)
            launcher = windows.sandbox_command([str(codex)], sandbox_args, state, environment)
            self.assertEqual(windows._read_probe(launcher, workspace, environment).status, "readable")
            script = ("$ErrorActionPreference = 'Stop'; try { Set-Content -LiteralPath "
                      + windows._literal(str(source)) + " -Value 'changed'; exit 99 } "
                      "catch { Write-Output ('SHUNT_WRITE_CODE=' + $_.FullyQualifiedErrorId); "
                      "Write-Output ('SHUNT_WRITE_CATEGORY=' + $_.CategoryInfo.Category); exit 43 }")
            result = windows._run(launcher.command(workspace, windows.powershell_command(script)),
                                  environment, cwd=workspace)
            self.assertEqual(result.returncode, 43, windows._diagnostics(result.stderr, result.stdout))
            self.assertIn("SHUNT_WRITE_CATEGORY=PermissionDenied", result.stdout)
            self.assertIn("SetContentCommand", result.stdout)
            self.assertEqual(source.read_text(), "approved copy\n")
            self.assertEqual(original.read_text(), "original\n")
            self.assertEqual(original_acl, subprocess.check_output([
                str(windows._system_directory() / "icacls.exe"), str(original)]))
            self.assertFalse((codex_home / "auth.json").exists())
            print(f"\nReal Codex backend verified: {mode}; no model call or authentication.", flush=True)
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
