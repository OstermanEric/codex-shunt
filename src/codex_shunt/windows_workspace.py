"""Check Windows sandbox reads and repair only an owned temporary source copy."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path


def powershell_command(script: str) -> list[str]:
    # Encoding keeps spaces, apostrophes, Unicode, and shell metacharacters literal.
    script = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run(command: list[str], environment: dict[str, str], *, deadline: float | None = None,
         **kwargs) -> subprocess.CompletedProcess:
    timeout = 30 if deadline is None else min(30, deadline - time.monotonic())
    if timeout <= 0:
        raise RuntimeError("The Windows sandbox access check timed out.")
    return subprocess.run(command, env=environment, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False, **kwargs)


def sandbox_command(codex: list[str], sandbox_args: list[str], state: Path,
                    environment: dict[str, str], *, deadline: float | None = None) -> list[str]:
    # Older CLIs expose `sandbox windows`; current CLIs select the host themselves.
    help_result = _run([*codex, "sandbox", "--help"], environment, deadline=deadline)
    if help_result.returncode:
        raise RuntimeError("Codex sandbox command is unavailable; update the standalone Codex CLI.")
    platform = ["windows"] if re.search(r"^\s+windows\s", help_result.stdout, re.MULTILINE) else []
    return [*codex, *sandbox_args, "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"', "--disable", "hooks",
            "-c", f"sqlite_home={json.dumps(str(state))}", "sandbox", *platform, "--"]


def _read_probe(command: list[str], workspace: Path,
                environment: dict[str, str], *, deadline: float | None = None) -> tuple[bool, str]:
    marker = "SHUNT_READ_OK_" + uuid.uuid4().hex
    script = f"""$ErrorActionPreference = 'Stop'
try {{
    $root = {_literal(str(workspace))}
    $files = Get-Content -LiteralPath (Join-Path $root '.codex-shunt-files.txt') -Encoding UTF8
    foreach ($file in $files) {{
        Get-Content -LiteralPath (Join-Path $root $file) -Encoding UTF8 | Out-Null
    }}
    Write-Output '{marker}'
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 43
}}
"""
    result = _run([*command, *powershell_command(script)], environment, cwd=workspace, deadline=deadline)
    return (result.returncode == 0 and marker in result.stdout.splitlines(),
            (result.stderr.strip() or result.stdout.strip())[-1200:])


def _offline_sandbox_sid(environment: dict[str, str], *, deadline: float | None = None) -> str:
    # The marker contains account names, not passwords. Never read sandbox-secrets.
    home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    marker = json.loads((home / ".sandbox" / "setup_marker.json").read_text(encoding="utf-8"))
    username = marker.get("offline_username")
    if not isinstance(username, str) or not username or any(c in username for c in "\\/@\r\n"):
        raise RuntimeError("Codex has no valid local offline sandbox account in its setup marker.")
    script = ("$ErrorActionPreference = 'Stop'; "
              f"$account = [System.Security.Principal.NTAccount]::new($env:COMPUTERNAME, {_literal(username)}); "
              "$account.Translate([System.Security.Principal.SecurityIdentifier]).Value")
    result = _run(powershell_command(script), environment, deadline=deadline)
    sid = result.stdout.strip()
    # Accept only a resolved account SID, never Everyone/Users/Authenticated Users.
    if result.returncode or not re.fullmatch(r"S-1-5-21-(?:\d+-){3}\d+", sid):
        raise RuntimeError("Could not resolve Codex's local offline sandbox account.")
    return sid


def _grant_source_read(root: Path, workspace: Path, sid: str,
                       environment: dict[str, str], *, deadline: float | None = None) -> None:
    # No inherited grant on the parent: sibling worker state stays private.
    # Copied sources receive read/execute only; originals and ancestors are untouched.
    for path, access, extra in [(root, "(X)", []), (workspace, "(OI)(CI)(RX)", ["/T"])]:
        result = _run(["icacls.exe", str(path), "/grant", f"*{sid}:{access}", *extra, "/Q"],
                      environment, deadline=deadline)
        if result.returncode:
            raise RuntimeError("Could not grant the Codex sandbox read access to the temporary copy: "
                               + (result.stderr.strip() or result.stdout.strip())[-600:])


def prepare_windows_workspace(codex: list[str], root: Path, workspace: Path, state: Path,
                              environment: dict[str, str], sandbox_args: list[str], *,
                              deadline: float | None = None) -> None:
    """MXC/current-user reads need no ACL changes; repair the legacy account if needed."""
    detail = ""
    deadline = min(deadline or float("inf"), time.monotonic() + 30)
    try:
        command = sandbox_command(codex, sandbox_args, state, environment, deadline=deadline)
        readable, detail = _read_probe(command, workspace, environment, deadline=deadline)
        if readable:
            return
        sid = _offline_sandbox_sid(environment, deadline=deadline)
        _grant_source_read(root, workspace, sid, environment, deadline=deadline)
        readable, detail = _read_probe(command, workspace, environment, deadline=deadline)
        if readable:
            return
        raise RuntimeError(detail or "The sandbox read check failed after granting source read access.")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Windows sandbox cannot access Shunt's temporary workspace. "
            f"{exc} {detail if detail and detail not in str(exc) else ''} "
            "Complete or repair the selected Codex Windows sandbox setup, "
            "then run `shunt setup` again. Routing remains off after failed setup."
        ) from exc
