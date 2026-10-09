"""Windows preflight regressions and real kernel-enforced temporary-copy ACLs."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
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
        self.workspace, self.state = self.root / "workspace", self.root / "state"
        self.workspace.mkdir()
        self.state.mkdir()
        (self.workspace / "source.txt").write_text("do not expose this source\n")
        (self.workspace / ".codex-shunt-files.txt").write_text("source.txt\n")
        self.environment = {"CODEX_HOME": str(self.root / "custom codex home")}
        self.launcher = windows.SandboxLauncher(("sandbox",), False)
        self.system = self.root / "system"
        self.system.mkdir()
        self.system_patch = patch.object(windows, "_system_directory", return_value=self.system)
        self.system_patch.start()
        self.addCleanup(self.system_patch.stop)

    def prepare(self):
        windows.prepare_windows_workspace(["codex.exe"], self.root, self.workspace,
                                          self.state, self.environment, ["-c", 'windows.sandbox="unelevated"'])

    def test_readable_sandbox_needs_no_identity_lookup_or_acl_changes(self):
        with patch.object(windows, "sandbox_command", return_value=self.launcher), \
                patch.object(windows, "_read_probe", return_value=windows.ProbeResult("readable")), \
                patch.object(windows, "_sandbox_user_sid") as identity, \
                patch.object(windows, "_grant_source_read") as grant:
            self.prepare()
        identity.assert_not_called()
        grant.assert_not_called()

    def test_unelevated_denial_uses_observed_identity_without_setup_marker(self):
        with patch.object(windows, "sandbox_command", return_value=self.launcher), \
                patch.object(windows, "_read_probe", side_effect=[windows.ProbeResult("access_denied"),
                                                                 windows.ProbeResult("readable")]) as probe, \
                patch.object(windows, "_sandbox_user_sid", return_value="S-1-12-1-1-2-3-4") as identity, \
                patch.object(windows, "_grant_source_read") as grant:
            self.prepare()
        self.assertFalse((Path(self.environment["CODEX_HOME"]) / ".sandbox/setup_marker.json").exists())
        identity.assert_called_once()
        self.assertEqual(identity.call_args.args[1], self.system)
        self.assertEqual(grant.call_args.args, (self.root, self.workspace, "S-1-12-1-1-2-3-4", self.environment))
        self.assertEqual(probe.call_count, 2)

    def test_workspace_entry_failure_bootstraps_and_rechecks_actual_workspace(self):
        for bootstrap_status in ("readable", "access_denied"):
            with self.subTest(bootstrap_status=bootstrap_status), \
                    patch.object(windows, "sandbox_command", return_value=self.launcher), \
                    patch.object(windows, "_read_probe", side_effect=[
                        windows.ProbeResult("workspace_entry_denied"), windows.ProbeResult(bootstrap_status),
                        windows.ProbeResult("readable")]) as probe, \
                    patch.object(windows, "_sandbox_user_sid", return_value="S-1-5-21-1-2-3-4"), \
                    patch.object(windows, "_grant_source_read") as grant:
                self.prepare()
            self.assertEqual(probe.call_args_list[1].kwargs["cwd"], self.system)
            self.assertNotIn("cwd", probe.call_args_list[2].kwargs)
            grant.assert_called_once()

    def test_probe_staging_timeout_and_policy_failures_never_request_acl_repair(self):
        for status in ("probe_error", "staging_error", "timeout", "startup_error"):
            with self.subTest(status=status), \
                    patch.object(windows, "sandbox_command", return_value=self.launcher), \
                    patch.object(windows, "_read_probe", return_value=windows.ProbeResult(status)), \
                    patch.object(windows, "_sandbox_user_sid") as identity, \
                    patch.object(windows, "_grant_source_read") as grant:
                with self.assertRaises(windows.WorkspaceError) as raised:
                    self.prepare()
                self.assertEqual(raised.exception.category, status)
                identity.assert_not_called()
                grant.assert_not_called()

    def test_generic_startup_failure_never_enters_permission_repair(self):
        with patch.object(windows, "sandbox_command", return_value=self.launcher), \
                patch.object(windows, "_read_probe", side_effect=[windows.ProbeResult("startup_error"),
                                                                 windows.ProbeResult("readable")]) as probe, \
                patch.object(windows, "_sandbox_user_sid") as identity, \
                patch.object(windows, "_grant_source_read") as grant:
            with self.assertRaises(windows.WorkspaceError):
                self.prepare()
        self.assertEqual(probe.call_count, 1)
        identity.assert_not_called()
        grant.assert_not_called()

    def test_bootstrap_script_or_policy_failure_never_changes_permissions(self):
        for status in ("startup_error", "probe_error", "staging_error", "timeout"):
            with self.subTest(status=status), \
                    patch.object(windows, "sandbox_command", return_value=self.launcher), \
                    patch.object(windows, "_read_probe", side_effect=[
                        windows.ProbeResult("workspace_entry_denied"), windows.ProbeResult(status)]), \
                    patch.object(windows, "_sandbox_user_sid") as identity, \
                    patch.object(windows, "_grant_source_read") as grant:
                with self.assertRaises(windows.WorkspaceError) as raised:
                    self.prepare()
                self.assertEqual(raised.exception.category, status)
                identity.assert_not_called()
                grant.assert_not_called()

    def test_repair_must_pass_a_second_probe(self):
        with patch.object(windows, "sandbox_command", return_value=self.launcher), \
                patch.object(windows, "_read_probe", return_value=windows.ProbeResult("access_denied")), \
                patch.object(windows, "_sandbox_user_sid", return_value="S-1-5-21-1-2-3-4"), \
                patch.object(windows, "_grant_source_read"):
            with self.assertRaises(windows.WorkspaceError) as raised:
                self.prepare()
        self.assertEqual(raised.exception.category, "access_denied")

    def test_missing_manifest_and_escaping_or_linked_files_never_change_acl(self):
        manifest = self.workspace / ".codex-shunt-files.txt"
        for name in ("../state/private.txt", "C:/original.txt", "/original.txt", "missing.txt", ""):
            with self.subTest(name=name), patch.object(windows, "_run") as run:
                manifest.write_text(name)
                with self.assertRaises(windows.WorkspaceError):
                    windows._grant_source_read(self.root, self.workspace, "S-1-5-21-1-2-3-4", self.environment)
                run.assert_not_called()
        manifest.unlink()
        with patch.object(windows, "_run") as run, self.assertRaises(windows.WorkspaceError):
            self.prepare()
        run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Windows reparse-point coverage is in the native sandbox suite")
    def test_staging_link_cannot_redirect_recursive_grant(self):
        (self.workspace / "redirect").symlink_to(self.state, target_is_directory=True)
        with patch.object(windows, "_run") as run, self.assertRaises(windows.WorkspaceError):
            windows._grant_source_read(self.root, self.workspace, "S-1-5-21-1-2-3-4", self.environment)
        run.assert_not_called()

    def test_shared_deadline_stops_before_launching_a_command(self):
        with patch.object(windows.subprocess, "run") as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                windows._run(["sandbox"], self.environment, deadline=time.monotonic() - 1)
        run.assert_not_called()
        with patch.object(windows, "sandbox_command", side_effect=subprocess.TimeoutExpired("codex", 30)):
            with self.assertRaises(windows.WorkspaceError) as raised:
                self.prepare()
        self.assertEqual(raised.exception.category, "timeout")

    def test_old_and_current_cli_keep_read_only_and_managed_requirements(self):
        cases = [
            (["Commands:\n  windows  Run native sandbox\n", "Usage: codex sandbox windows [COMMAND]..."], ["windows"]),
            (["Usage: codex sandbox [COMMAND]... --permission-profile --include-managed-config --cd"], []),
        ]
        for help_texts, platform in cases:
            with self.subTest(platform=platform), patch.object(windows, "_run", side_effect=[
                    subprocess.CompletedProcess([], 0, text, "") for text in help_texts]):
                launcher = windows.sandbox_command(["codex.exe"], ["-c", 'windows.sandbox="elevated"'],
                                                   self.state, self.environment)
            command = launcher.command(self.workspace, ["whoami.exe"])
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertIn('approval_policy="never"', command)
            self.assertIn('windows.sandbox="elevated"', command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertEqual(command[command.index("sandbox") + 1:][:len(platform)], platform)
            if not platform:
                self.assertIn("--include-managed-config", command)
                self.assertEqual(command[command.index("--permission-profile") + 1], ":read-only")
                self.assertEqual(command[command.index("-C") + 1], str(self.workspace))

    def test_profile_cli_without_managed_support_fails_closed(self):
        with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, "--permission-profile", "")):
            with self.assertRaisesRegex(windows.WorkspaceError, "managed requirements"):
                windows.sandbox_command(["codex.exe"], [], self.state, self.environment)

    def test_observed_identity_accepts_local_domain_and_cloud_users_only(self):
        for sid in ("S-1-5-21-123-456-789-1001", "S-1-12-1-123-456-789-1001"):
            with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, f'"domain,user","{sid}"\n', "")) as run:
                self.assertEqual(windows._sandbox_user_sid(self.launcher, self.system, self.environment), sid)
            command = run.call_args.args[0]
            self.assertEqual(command[-5:], [str(self.system / "whoami.exe"), "/user", "/fo", "csv", "/nh"])
            self.assertEqual(run.call_args.kwargs["cwd"], self.system)
        for output in ('"user","S-1-1-0"', '"user","S-1-5-11"', '"user","S-1-5-32-545"',
                       '"user","S-1-5-21-1-2-3-4"\n"other","S-1-5-21-1-2-3-5"', "garbage"):
            with self.subTest(output=output), patch.object(windows, "_run",
                    return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaises(windows.WorkspaceError):
                    windows._sandbox_user_sid(self.launcher, self.system, self.environment)

    def test_acl_grants_touch_only_temporary_copy_and_do_not_inherit_to_state(self):
        sid = "S-1-5-21-123-456-789-1001"
        with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            windows._grant_source_read(self.root, self.workspace, sid, self.environment)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            [str(self.system / "icacls.exe"), str(self.root), "/grant", f"*{sid}:(X)", "/Q"],
            [str(self.system / "icacls.exe"), str(self.workspace), "/grant", f"*{sid}:(OI)(CI)(RX)", "/T", "/Q"],
        ])

    def test_probe_preserves_literal_unicode_paths_and_never_prints_source_text(self):
        workspace = self.root / "space ' [literal] 雪"
        def run(command, environment, **kwargs):
            script = base64.b64decode(command[-1]).decode("utf-16-le")
            self.assertIn(windows._literal(str(workspace)), script)
            self.assertIn("-LiteralPath", script)
            self.assertIn("| Out-Null", script)
            self.assertNotIn("[Console]", script)
            self.assertNotIn("::new", script)
            marker = script.split("Write-Output '")[1].split(":START'")[0]
            return subprocess.CompletedProcess(command, 0, marker + ":START\n" + marker + ":OK\n", "")
        with patch.object(windows, "_run", side_effect=run):
            self.assertEqual(windows._read_probe(self.launcher, workspace, self.environment).status, "readable")

    def test_probe_classifies_nonce_bound_errors_without_english_message_matching(self):
        for code, category, status in [
            ("UnauthorizedAccess,Microsoft.PowerShell.Commands.GetContentCommand", "PermissionDenied", "access_denied"),
            ("System.UnauthorizedAccessException,Microsoft.PowerShell.Commands.GetContentCommand", "NotSpecified", "access_denied"),
            ("PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand", "ObjectNotFound", "staging_error"),
            ("MethodInvocationNotSupportedInConstrainedLanguage", "InvalidOperation", "probe_error"),
        ]:
            def run(command, environment, **kwargs):
                script = base64.b64decode(command[-1]).decode("utf-16-le")
                marker = script.split("Write-Output '")[1].split(":START'")[0]
                output = f"{marker}:START\n{marker}:CODE={code}\n{marker}:CATEGORY={category}\n"
                return subprocess.CompletedProcess(command, 43, output, "")
            with self.subTest(code=code), patch.object(windows, "_run", side_effect=run):
                result = windows._read_probe(self.launcher, self.workspace, self.environment)
                self.assertEqual(result.status, status)
                self.assertEqual(result.error_code, code)

    def test_unrelated_success_marker_is_not_accepted(self):
        with patch.object(windows, "_run", return_value=subprocess.CompletedProcess([], 0, "SHUNT_other:START\nSHUNT_other:OK", "")):
            self.assertEqual(windows._read_probe(self.launcher, self.workspace, self.environment).status, "startup_error")

    def test_pre_marker_cwd_exception_requires_bootstrap_confirmation(self):
        for diagnostic, status in [
            ("Native launch failed (os error 5)", "workspace_entry_denied"),
            ("Localized message\n    + FullyQualifiedErrorId : System.UnauthorizedAccessException\n",
             "workspace_entry_denied"),
            ('#< CLIXML\n<Objs><S S="Error">System.UnauthorizedAccessException_x000D__x000A_</S></Objs>',
             "workspace_entry_denied"),
            ("Access denied by startup policy", "startup_error"),
        ]:
            with self.subTest(diagnostic=diagnostic), patch.object(windows, "_run",
                    return_value=subprocess.CompletedProcess([], 1, "", diagnostic)):
                self.assertEqual(windows._read_probe(self.launcher, self.workspace, self.environment).status, status)

    def test_clixml_preserves_errors_discards_progress_and_bounds_malformed_data(self):
        output = '#< CLIXML\n<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04"><Obj S="progress"><S>noise</S></Obj><S S="Error">CannotCreateTypeConstrainedLanguage_x000D__x000A_failure</S></Objs>'
        self.assertEqual(windows._diagnostics(output), "CannotCreateTypeConstrainedLanguage\r\nfailure")
        self.assertNotIn("<Objs", windows._diagnostics("<Objs broken"))
        self.assertLessEqual(len(windows._diagnostics("x" * 2000)), 1200)
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
        # icacls requires a registered principal. Use the existing Guests group
        # only as this synthetic token's restricting SID; no account is created
        # or logged into, and only disposable fixture files receive its read ACE.
        sid = "S-1-5-32-546"
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
