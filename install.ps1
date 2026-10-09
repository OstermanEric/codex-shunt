# Native Windows installation. Requires Git, the Python launcher, and standalone Codex.
$ErrorActionPreference = 'Stop'

function Install-Shunt {
    $checkout = Join-Path $HOME '.codex\plugins\codex-shunt'
    $repository = 'https://github.com/OstermanEric/codex-shunt.git'
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Git is missing. Install Git for Windows from https://git-scm.com/download/win, reopen PowerShell, and retry.'
    }
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Python is missing. Install Python 3.11+ with the py launcher from https://www.python.org/downloads/windows/, reopen PowerShell, and retry.'
    }
    & py -3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ with the Python launcher, then retry.' }

    if (Test-Path $checkout) {
        if (-not (Test-Path (Join-Path $checkout '.git'))) { throw "Refusing to replace existing folder: $checkout" }
        $remote = & git -C $checkout remote get-url origin
        if ($LASTEXITCODE -ne 0 -or (($remote -replace '\.git$', '') -ne ($repository -replace '\.git$', ''))) {
            throw "Refusing to update an unrelated repository: $checkout"
        }
        $dirty = & git -C $checkout status --porcelain
        if ($LASTEXITCODE -ne 0 -or $dirty) { throw "Save your local changes in $checkout before updating." }
        & git -C $checkout pull --ff-only
    } else {
        New-Item -ItemType Directory -Force (Split-Path $checkout) | Out-Null
        & git clone --depth 1 $repository $checkout
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not download/update Codex Shunt.' }

    $findCodex = @'
import sys
sys.path.insert(0, sys.argv[1])
from codex_shunt.worker import find_codex
try:
    print(find_codex())
except RuntimeError as error:
    print(error, file=sys.stderr)
    sys.exit(1)
'@
    $codex = & py -3 -X utf8 -c $findCodex (Join-Path $checkout 'src')
    if ($LASTEXITCODE -ne 0) { throw 'Install the standalone Codex CLI and sign in with ChatGPT, then retry.' }
    & $codex plugin marketplace add $checkout
    if ($LASTEXITCODE -ne 0) { throw 'Could not add the Shunt marketplace.' }
    & $codex plugin add 'codex-shunt@codex-shunt'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the Shunt plugin.' }

    $binDir = Join-Path $HOME '.local\bin'
    New-Item -ItemType Directory -Force $binDir | Out-Null
    $command = Join-Path $binDir 'shunt.cmd'
    $marker = '@rem Codex Shunt launcher'
    if ((Test-Path $command) -and (Get-Content -LiteralPath $command -TotalCount 1) -ne $marker) {
        throw "Refusing to overwrite an unrelated command: $command"
    }
    # Relative to this launcher, so non-ASCII home paths need no batch-file encoding.
    @($marker, '@echo off', 'py -3 -X utf8 "%~dp0..\..\.codex\plugins\codex-shunt\scripts\codex-shunt" %*', 'exit /b %errorlevel%') | Set-Content -LiteralPath $command -Encoding ASCII

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (@($userPath -split ';') -notcontains $binDir) {
        [Environment]::SetEnvironmentVariable('Path', "$binDir;$userPath", 'User')
    }
    if (@($env:Path -split ';') -notcontains $binDir) { $env:Path = "$binDir;$env:Path" }
    Write-Host 'Installed shunt. Starting guided setup...'
    & py -3 -X utf8 (Join-Path $checkout 'scripts\codex-shunt') setup
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Shunt is installed; setup still needs attention. Read the message above and run shunt setup when ready.' -ForegroundColor Yellow
        return
    }
    Write-Host 'Review and trust the Shunt hook in Codex, then start a new chat.'
    Write-Host 'Usage: shunt stats --since 7d'
}

Install-Shunt
