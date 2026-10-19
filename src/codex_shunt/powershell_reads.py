"""Fixed PowerShell helpers; requests are literal data, never executable input."""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

from .read_process import run_read_process


# Numeric UTF-16 code units make the response independent of Windows console
# encodings, including Unicode paths, without non-core .NET calls under CLM.
_HELPER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$request = Get-Content -LiteralPath '__REQUEST__' -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    if ($request.action -eq 'expand') {
        $items = @(Get-Item -Path $request.path -ErrorAction Stop |
            Select-Object -First ($request.limit + 1))
        if ($items.Count -gt $request.limit) { throw 'file-limit' }
        $result = @(foreach ($item in $items) {
            if ($item.PSIsContainer -or $item.PSProvider.Name -ne 'FileSystem') {
                throw 'not-a-file'
            }
            $item.FullName
        })
    } elseif ($request.action -eq 'search') {
        $count = 0
        $result = @(for ($i = 0; $i -lt $request.paths.Count; $i++) {
            $params = @{
                LiteralPath = $request.paths[$i]
                Pattern = $request.pattern
                SimpleMatch = [bool]$request.simple
                CaseSensitive = [bool]$request.case_sensitive
                ErrorAction = 'Stop'
            }
            if ($request.encoding) { $params.Encoding = $request.encoding }
            $remaining = $request.limit - $count
            $hits = @(Select-String @params | Select-Object -First ($remaining + 1))
            if ($hits.Count -gt $remaining) { throw 'match-limit' }
            foreach ($hit in $hits) { @{file = $i; line = $hit.LineNumber} }
            $count += $hits.Count
        })
        if ($result.Count -gt $request.limit) { throw 'match-limit' }
    } else { throw 'unsupported-action' }
    $json = ConvertTo-Json -InputObject @($result) -Compress -Depth 5
    'SHUNT_READ_V1:' + (($json.ToCharArray() | ForEach-Object { [int]$_ }) -join ',')
} catch { exit 2 }
"""


def powershell_request(request: dict[str, object], cwd: Path, shell: str) -> object:
    # Keep path arrays outside EncodedCommand's Windows command-line length cap.
    with tempfile.TemporaryDirectory(prefix="codex-shunt-read-") as temporary:
        path = Path(temporary) / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=True), encoding="utf-8")
        script = _HELPER.replace("__REQUEST__", str(path).replace("'", "''"))
        if os.name == "nt" and shell == "powershell":
            from .windows_workspace import powershell_command
            command = powershell_command(script)
        else:
            executable = shutil.which(shell)
            if not executable:
                raise ValueError("powershell-unavailable")
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            command = [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        output = run_read_process(command, cwd, 2_000_000)
    marker = b"SHUNT_READ_V1:"
    if not output.startswith(marker):
        raise ValueError("powershell-helper-failed")
    units = output[len(marker):].strip().split(b",")
    try:
        encoded = b"".join(int(unit).to_bytes(2, "little") for unit in units)
        return json.loads(encoded.decode("utf-16-le"))
    except (ValueError, OverflowError, UnicodeError) as exc:
        raise ValueError("powershell-helper-failed") from exc
