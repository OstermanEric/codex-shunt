"""Windows preflight recovery and real kernel-enforced temporary-copy ACLs."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_shunt import windows_workspace as windows


class WindowsWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shunt ' space ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.state = self.root / "state"
        self.workspace.mkdir()
        self.state.mkdir()
        self.environment = {"CODEX_HOME": str(self.root / "custom codex home")}

    def prepare(self):
        windows.prepare_windows_workspace(["codex.exe"], self.root, self.workspace,
                                          self.state, self.environment, ["-c", 'windows.sandbox="elevated"'])

    def test_readable_sandbox_needs_no_identity_lookup_or_acl_changes(self):
        with patch.object(windows, "sandbox_command", return_value=["sandbox"]), \
                patch.object(windows, "_read_probe", return_value=(True, "")), \
                patch.object(windows, "_offline_sandbox_sid") as identity, \
                patch.object(windows, "_grant_source_read") as grant:
            self.prepare()
        identity.assert_not_called()
        grant.assert_not_called()

    def test_denied_read_gets_scoped_repair_and_must_pass_a_second_probe(self):
        with patch.object(windows, "sandbox_command", return_value=["sandbox"]), \
                patch.object(windows, "_read_probe", side_effect=[(False, "Access denied"), (True, "")]) as probe, \
                patch.object(windows, "_offline_sandbox_sid", return_value="resolved-sid"), \
                patch.object(windows, "_grant_source_read") as grant:
            self.prepare()
        grant.assert_called_once()
        self.assertEqual(grant.call_args.args, (self.root, self.workspace, "resolved-sid", self.environment))
        self.assertEqual(probe.call_count, 2)

    def test_blocked_managed_policy_still_fails_after_read_grant(self):
        with patch.object(windows, "sandbox_command", return_value=["sandbox"]), \
                patch.object(windows, "_read_probe", return_value=(False, "Managed restriction denies reads")), \
                patch.object(windows, "_offline_sandbox_sid", return_value="resolved-sid"), \
                patch.object(windows, "_grant_source_read"):
            with self.assertRaisesRegex(RuntimeError, "Managed restriction denies reads"):
                self.prepare()

    def test_missing_identity_preserves_actual_read_failure(self):
        with patch.object(windows, "sandbox_command", return_value=["sandbox"]), \
                patch.object(windows, "_read_probe", return_value=(False, "Access denied")), \
                patch.object(windows, "_grant_source_read") as grant:
            with self.assertRaisesRegex(RuntimeError, "Access denied"):
                self.prepare()
        grant.assert_not_called()

    def test_preflight_timeout_is_actionable(self):
        with patch.object(windows, "sandbox_command", side_effect=subprocess.TimeoutExpired("codex", 30)):
            with self.assertRaisesRegex(RuntimeError, "Windows sandbox cannot access.*shunt setup"):
                self.prepare()

    def test_old_and_current_cli_keep_read_only_and_saved_backend(self):
        for help_text, platform in [("Commands:\n  windows  Run native sandbox\n", ["windows"]),
                                    ("Usage: codex sandbox [COMMAND]...", [])]:
            with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, help_text, "")):
                command = windows.sandbox_command(["codex.exe"], ["-c", 'windows.sandbox="elevated"'],
                                                  self.state, self.environment)
            self.assertEqual(command[command.index("sandbox") + 1:], [*platform, "--"])
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertIn('approval_policy="never"', command)
            self.assertIn('windows.sandbox="elevated"', command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_identity_uses_custom_codex_home_and_resolves_the_local_account(self):
        marker = Path(self.environment["CODEX_HOME"]) / ".sandbox" / "setup_marker.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"offline_username": "dynamic_offline_user"}))
        sid = "S-1-5-21-123-456-789-1001"
        with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, sid, "")) as run:
            self.assertEqual(windows._offline_sandbox_sid(self.environment), sid)
        script = base64.b64decode(run.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("$env:COMPUTERNAME", script)
        self.assertIn("'dynamic_offline_user'", script)
        for broad_sid in ["S-1-1-0", "S-1-5-11", "S-1-5-32-545"]:
            with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, broad_sid, "")):
                with self.assertRaises(RuntimeError):
                    windows._offline_sandbox_sid(self.environment)

    def test_acl_grants_touch_only_temporary_copy_and_do_not_inherit_to_state(self):
        sid = "S-1-5-21-123-456-789-1001"
        with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            windows._grant_source_read(self.root, self.workspace, sid, self.environment)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [
            ["icacls.exe", str(self.root), "/grant", f"*{sid}:(X)", "/Q"],
            ["icacls.exe", str(self.workspace), "/grant", f"*{sid}:(OI)(CI)(RX)", "/T", "/Q"],
        ])

    def test_probe_uses_literal_absolute_paths_and_does_not_return_source_text(self):
        def run(command, environment, **kwargs):
            script = base64.b64decode(command[-1]).decode("utf-16-le")
            self.assertIn(windows._literal(str(self.workspace)), script)
            self.assertIn("-LiteralPath", script)
            self.assertIn("| Out-Null", script)
            self.assertEqual(kwargs["cwd"], self.workspace)
            marker = script.split("Write-Output '")[1].split("'")[0]
            return subprocess.CompletedProcess(command, 0, marker + "\n", "")
        with patch.object(windows, "_run", side_effect=run):
            self.assertTrue(windows._read_probe(["sandbox", "--"], self.workspace, self.environment)[0])


@unittest.skipUnless(sys.platform == "win32", "real Windows ACL and restricted-token checks")
class NativeWindowsAclTests(unittest.TestCase):
    def test_private_copy_becomes_readable_but_writes_state_and_originals_stay_denied(self):
        import ctypes
        from ctypes import wintypes as wt

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wt.HANDLE
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        advapi.CreateRestrictedToken.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                                wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                                ctypes.POINTER(SidAndAttributes), ctypes.POINTER(wt.HANDLE)]
        advapi.ImpersonateLoggedOnUser.argtypes = [wt.HANDLE]
        kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                      wt.DWORD, wt.DWORD, wt.HANDLE]
        kernel.CreateFileW.restype = wt.HANDLE
        token, restricted, sid_pointer = wt.HANDLE(), wt.HANDLE(), ctypes.c_void_p()
        sid = "S-1-5-21-123-456-789-1001"  # Synthetic capability; no Windows account is created.
        try:
            self.assertTrue(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0xE, ctypes.byref(token)))
            self.assertTrue(advapi.ConvertStringSidToSidW(sid, ctypes.byref(sid_pointer)))
            entry = SidAndAttributes(sid_pointer, 0)
            self.assertTrue(advapi.CreateRestrictedToken(token, 0, 0, None, 0, None, 1,
                                                        ctypes.byref(entry), ctypes.byref(restricted)))

            def access(path, rights):
                self.assertTrue(advapi.ImpersonateLoggedOnUser(restricted))
                try:
                    handle = kernel.CreateFileW(str(path), rights, 7, None, 3, 0, None)
                    error = ctypes.get_last_error()
                    if handle == ctypes.c_void_p(-1).value:
                        return False, error
                    kernel.CloseHandle(handle)
                    return True, 0
                finally:
                    advapi.RevertToSelf()

            with tempfile.TemporaryDirectory(prefix="shunt acl ' space ") as temporary:
                root = Path(temporary)
                workspace, state = root / "workspace", root / "state"
                workspace.mkdir()
                state.mkdir()
                source = workspace / "source.txt"
                source.write_text("approved copy\n")
                manifest = workspace / ".codex-shunt-files.txt"
                manifest.write_text("source.txt\n")
                private_state = state / "private.txt"
                private_state.write_text("private state\n")
                original = root / "original.txt"
                original.write_text("original\n")
                self.assertEqual(access(source, 0x80000000), (False, 5))
                windows._grant_source_read(root, workspace, sid, dict(os.environ))
                self.assertEqual(access(source, 0x80000000), (True, 0))
                self.assertEqual(access(manifest, 0x80000000), (True, 0))
                self.assertEqual(access(source, 0x40000000), (False, 5))
                self.assertEqual(access(private_state, 0x80000000), (False, 5))
                self.assertEqual(access(original, 0x80000000), (False, 5))
                self.assertEqual(source.read_text(), "approved copy\n")
            self.assertFalse(root.exists())
        finally:
            advapi.RevertToSelf()
            if restricted:
                kernel.CloseHandle(restricted)
            if token:
                kernel.CloseHandle(token)
            if sid_pointer:
                kernel.LocalFree(sid_pointer)


if __name__ == "__main__":
    unittest.main()
