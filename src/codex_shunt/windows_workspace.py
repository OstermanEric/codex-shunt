"""Verify Windows sandbox reads and repair only the owned temporary source copy."""

from __future__ import annotations

import base64
import csv
import errno
import io
import json
import os
import re
import stat
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


@dataclass(frozen=True)
class ProbeResult:
    status: str
    exit_code: int | None = None
    error_code: str = ""
    detail: str = ""


class WorkspaceError(RuntimeError):
    def __init__(self, category: str, detail: str):
        self.category = category
        advice = {
            "probe_error": "Shunt's PowerShell probe is incompatible with this environment.",
            "staging_error": "Shunt could not verify its staged source files.",
            "startup_error": "Check the selected Codex sandbox's startup or policy error.",
            "timeout": "The Windows workspace check exceeded its deadline.",
            "identity_error": "Shunt could not identify the sandbox's user safely.",
            "access_denied": "The sandbox still cannot read the temporary source copy.",
            "workspace_entry_denied": "The sandbox cannot enter the temporary source copy.",
            "grant_error": "Shunt could not grant scoped access to the temporary source copy.",
        }.get(category, "Windows workspace verification failed.")
        super().__init__(f"Windows workspace verification failed ({category}). {advice} "
                         f"{detail} Luna was not started. Run shunt setup after resolving this issue.")


def _system_directory() -> Path:
    # Ask Windows for its trusted system directory instead of searching PATH.
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise WorkspaceError("startup_error", "Windows did not return a system directory.")
    return Path(buffer.value)


def powershell_command(script: str) -> list[str]:
    # EncodedCommand is UTF-16LE, independent of redirected output encoding.
    # Only ASCII markers cross stdout; do not construct .NET encoders in CLM.
    executable = _system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def read_only_config_args(sandbox_args: list[str], state: Path) -> list[str]:
    """Resolve the worker and probe's intended restrictions in one place."""
    return [*sandbox_args, "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"', "--disable", "hooks",
            "-c", f"sqlite_home={json.dumps(str(state))}",
            # Override personal shell customizations in the standalone probe,
            # matching the worker which ignores personal config. Default secret
            # environment exclusions remain enabled in both invocations.
            "-c", 'shell_environment_policy={inherit="all",ignore_default_excludes=false,exclude=[],set={},include_only=[]}']


def _run(command: list[str], environment: dict[str, str], *, deadline: float | None = None,
         **kwargs) -> subprocess.CompletedProcess:
    timeout = 30 if deadline is None else min(30, deadline - time.monotonic())
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    return subprocess.run(command, env=environment, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False, **kwargs)


@dataclass(frozen=True)
class SandboxLauncher:
    arguments: tuple[str, ...]
    supports_cd: bool

    def command(self, cwd: Path, program: list[str]) -> list[str]:
        cd = ["-C", str(cwd)] if self.supports_cd else []
        return [*self.arguments, *cd, "--", *program]


def sandbox_command(codex: list[str], sandbox_args: list[str], state: Path,
                    environment: dict[str, str], *, deadline: float | None = None) -> SandboxLauncher:
    help_result = _run([*codex, "sandbox", "--help"], environment, deadline=deadline)
    if help_result.returncode:
        raise WorkspaceError("startup_error", "The sandbox CLI is unavailable; update the standalone Codex CLI.")
    help_text = help_result.stdout
    platform = ["windows"] if re.search(r"^\s+windows\s", help_text, re.MULTILINE) else []
    if platform:
        help_result = _run([*codex, "sandbox", *platform, "--help"], environment, deadline=deadline)
        if help_result.returncode:
            raise WorkspaceError("startup_error", "The Windows sandbox CLI is unavailable.")
        help_text = help_result.stdout
    options: list[str] = []
    if "--permission-profile" in help_text:
        # Explicit profiles otherwise ignore managed requirements in current
        # sandbox CLIs. Never silently omit them to obtain a passing probe.
        if "--include-managed-config" not in help_text:
            raise WorkspaceError("startup_error", "Update Codex: this sandbox CLI cannot include managed requirements.")
        options = ["--permission-profile", ":read-only", "--include-managed-config"]
    return SandboxLauncher(tuple([*codex, *read_only_config_args(sandbox_args, state),
                                  "sandbox", *platform, *options]), "--cd" in help_text)


def _diagnostics(*streams: str) -> str:
    """Decode PowerShell error records; never display progress/CLIXML dumps."""
    parts = []
    for stream in streams:
        stream = stream or ""
        if "<Objs" in stream:
            prefix, xml = stream.split("<Objs", 1)
            prefix = prefix.replace("#< CLIXML", "").strip()
            if prefix:
                parts.append(prefix)
            try:
                root = ET.fromstring("<Objs" + xml)
                parts.extend(node.text or "" for node in root.iter()
                             if node.tag.rsplit("}", 1)[-1] == "S" and node.get("S") == "Error")
            except ET.ParseError:
                parts.append("PowerShell diagnostic records could not be decoded.")
        else:
            parts.append(stream.replace("#< CLIXML", "").strip())
    text = "\n".join(part for part in parts if part)
    return re.sub(r"_x([0-9a-fA-F]{4})_", lambda match: chr(int(match[1], 16)), text)[-1200:]


def _probe_script(workspace: Path, marker: str) -> str:
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Write-Output '{marker}:START'
try {{
    $root = {_literal(str(workspace))}
    $files = Get-Content -LiteralPath (Join-Path $root '.codex-shunt-files.txt') -Encoding UTF8
    foreach ($file in $files) {{
        Get-Content -LiteralPath (Join-Path $root $file) -Encoding UTF8 | Out-Null
    }}
    Write-Output '{marker}:OK'
}} catch {{
    Write-Output ('{marker}:CODE=' + $_.FullyQualifiedErrorId)
    Write-Output ('{marker}:CATEGORY=' + $_.CategoryInfo.Category)
    exit 43
}}
"""


def _read_probe(launcher: SandboxLauncher, workspace: Path, environment: dict[str, str], *,
                cwd: Path | None = None, deadline: float | None = None) -> ProbeResult:
    marker = "SHUNT_" + uuid.uuid4().hex
    cwd = cwd or workspace
    try:
        result = _run(launcher.command(cwd, powershell_command(_probe_script(workspace, marker))),
                      environment, cwd=cwd, deadline=deadline)
    except subprocess.TimeoutExpired:
        return ProbeResult("timeout")
    except OSError as exc:
        denied = getattr(exc, "winerror", None) == 5 or exc.errno == errno.EACCES
        return ProbeResult("workspace_entry_denied" if denied else "startup_error",
                           error_code=str(getattr(exc, "winerror", None) or exc.errno))
    lines = result.stdout.splitlines()
    if result.returncode == 0 and marker + ":START" in lines and marker + ":OK" in lines:
        return ProbeResult("readable", 0)
    code = next((line[len(marker + ":CODE="):] for line in lines if line.startswith(marker + ":CODE=")), "")
    category = next((line[len(marker + ":CATEGORY="):] for line in lines if line.startswith(marker + ":CATEGORY=")), "")
    if marker + ":START" not in lines:
        detail = _diagnostics(result.stderr, result.stdout)
        # PowerShell can resolve its inaccessible cwd on the first cmdlet,
        # before START is printed. Recognize the stable exception identifier
        # as well as native error 5; a bootstrap probe must still confirm a
        # staged-file read outcome before this can request any ACL changes.
        denied = bool(re.search(r"\b(?:os error|win32 error)\s*[:(]?\s*5\b", detail, re.IGNORECASE)
                      or re.search(r"\bSystem\.UnauthorizedAccessException\b", detail))
        return ProbeResult("workspace_entry_denied" if denied else "startup_error",
                           result.returncode, detail=detail)
    # Only a failure in our Get-Content operation can request an ACL repair.
    if result.returncode == 43 and "GetContentCommand" in code:
        if category == "PermissionDenied" or code.split(",")[0] in {"UnauthorizedAccess", "System.UnauthorizedAccessException"}:
            return ProbeResult("access_denied", 43, code, "The staged-file read was denied.")
        if category == "ObjectNotFound" or code.split(",")[0] == "PathNotFound":
            return ProbeResult("staging_error", 43, code, "A staged file or manifest is missing.")
    return ProbeResult("probe_error", result.returncode, code,
                       _diagnostics(result.stderr) or code or "The probe did not complete its read check.")


def _sandbox_user_sid(launcher: SandboxLauncher, bootstrap: Path, environment: dict[str, str], *,
                      deadline: float | None = None) -> str:
    result = _run(launcher.command(bootstrap, [str(_system_directory() / "whoami.exe"),
                                             "/user", "/fo", "csv", "/nh"]),
                  environment, cwd=bootstrap, deadline=deadline)
    try:
        rows = list(csv.reader(io.StringIO(result.stdout.strip())))
        sid = rows[0][1] if len(rows) == 1 and len(rows[0]) == 2 else ""
    except csv.Error:
        sid = ""
    # Local/domain and cloud-backed user accounts; never accept broad groups.
    if result.returncode or not re.fullmatch(r"S-1-(?:5-21|12-1)-(?:\d+-){3}\d+", sid):
        raise WorkspaceError("identity_error", "The sandbox did not return one valid user SID.")
    return sid


def _validate_staging(root: Path, workspace: Path) -> None:
    """Before recursive ACL changes, reject paths outside our unreleased copy."""
    if workspace.absolute().parent != root.absolute() or workspace.name != "workspace":
        raise WorkspaceError("staging_error", "The copied workspace is outside the staging root.")
    for current, directories, files in os.walk(root, followlinks=False):
        for path in [Path(current), *(Path(current) / name for name in [*directories, *files])]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorkspaceError("staging_error", "The staging tree contains a link or reparse point.")
    manifest = workspace / ".codex-shunt-files.txt"
    try:
        names = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise WorkspaceError("staging_error", "The staged manifest is missing or unreadable.") from None
    if not names:
        raise WorkspaceError("staging_error", "The staged manifest is empty.")
    for name in names:
        relative = PureWindowsPath(name)
        if not name or relative.drive or relative.root or ".." in relative.parts:
            raise WorkspaceError("staging_error", "A manifest entry escapes the copied workspace.")
        path = workspace.joinpath(*relative.parts)
        if not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
            raise WorkspaceError("staging_error", "An approved staged file is missing or outside the copy.")


def _grant_source_read(root: Path, workspace: Path, sid: str,
                       environment: dict[str, str], *, deadline: float | None = None) -> None:
    _validate_staging(root, workspace)
    # The parent grant does not inherit to sibling runtime state. Originals and
    # ancestors are untouched. An RX grant never replaces the read-only policy.
    for path, access, extra in [(root, "(X)", []), (workspace, "(OI)(CI)(RX)", ["/T"])]:
        result = _run([str(_system_directory() / "icacls.exe"), str(path), "/grant",
                       f"*{sid}:{access}", *extra, "/Q"], environment, deadline=deadline)
        if result.returncode:
            raise WorkspaceError("grant_error", _diagnostics(result.stderr, result.stdout))


def _require_read(result: ProbeResult) -> None:
    raise WorkspaceError(result.status, result.detail or result.error_code)


def prepare_windows_workspace(codex: list[str], root: Path, workspace: Path, state: Path,
                              environment: dict[str, str], sandbox_args: list[str], *,
                              deadline: float | None = None) -> None:
    """Observe actual sandbox access; only confirmed copy failures request repair."""
    deadline = min(deadline or float("inf"), time.monotonic() + 30)
    try:
        _validate_staging(root, workspace)
        launcher = sandbox_command(codex, sandbox_args, state, environment, deadline=deadline)
        result = _read_probe(launcher, workspace, environment, deadline=deadline)
        if result.status == "readable":
            return
        if result.status not in {"access_denied", "workspace_entry_denied"}:
            _require_read(result)
        bootstrap = _system_directory()
        if result.status == "workspace_entry_denied":
            # An inaccessible cwd can prevent PowerShell from even starting.
            # Change only the launch directory, retaining the sandbox policy.
            bootstrap_result = _read_probe(launcher, workspace, environment,
                                           cwd=bootstrap, deadline=deadline)
            if bootstrap_result.status not in {"readable", "access_denied"}:
                _require_read(bootstrap_result)
        sid = _sandbox_user_sid(launcher, bootstrap, environment, deadline=deadline)
        _grant_source_read(root, workspace, sid, environment, deadline=deadline)
        result = _read_probe(launcher, workspace, environment, deadline=deadline)
        if result.status != "readable":
            _require_read(result)
    except WorkspaceError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError("timeout", "") from exc
    except (OSError, ValueError) as exc:
        raise WorkspaceError("staging_error", "The temporary workspace could not be prepared.") from exc
