# check.ps1 - Verification gate for each incremental change.
#
# NOTE: This file is intentionally ASCII-ONLY.
#   Windows PowerShell 5.1 reads BOM-less script files using the system ANSI code
#   page (936/GBK on this machine), so UTF-8 Chinese text inside a .ps1 without a
#   BOM is mis-decoded and causes ParserError. Same root cause as the documented
#   alembic.ini pitfall. Keeping scripts ASCII-only is the established project
#   convention and works regardless of encoding/BOM.
#
# Three levels, progressively stricter:
#   L1 static : byte-compile / import self-check (seconds, no infra needed)
#   L2 unit   : full pytest suite (seconds, hermetic, no external services)
#   L3 e2e    : retrieval eval + PER-QUESTION baseline comparison
#               (minutes, needs full Docker stack + indexed Qdrant)
#
# Usage:
#   .\scripts\check.ps1                    # L1 + L2  (fast, use while coding)
#   .\scripts\check.ps1 -Full              # L1 + L2 + L3 (REQUIRED after any
#                                          #   change to retrieval/chunking/ingest/eval)
#
# Exit code: 0 = all passed; non-zero = failure (do NOT proceed to next step)

param(
    [switch]$Full
)

# NOTE: ErrorActionPreference is deliberately "Continue", NOT "Stop".
#   Windows PowerShell 5.1 wraps a native command's stderr output into an
#   ErrorRecord; with ErrorActionPreference="Stop" that turns into a TERMINATING
#   error and aborts this script even when the command succeeded. Our Python
#   tools legitimately write progress to stderr (logging), so "Stop" would make
#   the gate unusable. We therefore rely on $LASTEXITCODE for pass/fail, which is
#   the correct signal for native commands anyway.
$ErrorActionPreference = "Continue"

# Repo root = parent of the directory containing this script (scripts/).
# $PSScriptRoot (not $MyInvocation.MyCommand.Path) because PowerShell 7.3+
# returns a RELATIVE path from the latter, which broke $Root/$Py resolution.
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
# The project venv lives in backend/.venv (dependencies are installed there).
# Exactly ONE venv exists in this repo; a root .venv no longer exists, so never
# resolve the interpreter from the repo root.
$Py = Join-Path $Backend ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "[FAIL] venv python not found: $Py" -ForegroundColor Red
    exit 1
}

# ---------- keep the source tree free of __pycache__ ----------
# PYTHONDONTWRITEBYTECODE=1 stops the import machinery (pytest, eval, uvicorn)
# from writing .pyc files. `python -m compileall` in L1 writes bytecode
# EXPLICITLY, bypassing that switch, so PYTHONPYCACHEPREFIX redirects any
# explicit output to %TEMP% instead of the repo. Both are restored in finally.
$prevNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$prevPycPrefix = $env:PYTHONPYCACHEPREFIX
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPYCACHEPREFIX = Join-Path ([System.IO.Path]::GetTempPath()) "personal_rag_pycache"
# pytest writes its own cache outside CPython's control; point it at %TEMP% too
# so `backend/.pytest_cache` never appears. Passed to pytest via `-o cache_dir`.
$PytestCache = Join-Path ([System.IO.Path]::GetTempPath()) "personal_rag_pytest_cache"

$script:Failed = @()

function Invoke-Gate {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    Write-Host ">> $Name" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    $global:LASTEXITCODE = 0
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name (exit=$LASTEXITCODE)" -ForegroundColor Red
        $script:Failed += $Name
        return
    }
    Write-Host "[PASS] $Name" -ForegroundColor Green
}

Push-Location $Backend
try {
    # ---------- L1: syntax / compile self-check ----------
    Invoke-Gate "L1 compileall (syntax self-check)" {
        & $Py -m compileall -q app eval tests
        if ($LASTEXITCODE -eq 0) { Write-Host "  all modules compiled (bytecode -> %TEMP%)" }
    }

    # ---------- L2: full unit test suite ----------
    Invoke-Gate "L2 pytest (full suite)" {
        & $Py -m pytest -q --no-header -o "cache_dir=$PytestCache" 2>&1 | Select-Object -Last 6 | Out-String | Write-Host
    }

    # ---------- L3: retrieval eval + per-question baseline diff ----------
    if ($Full) {
        # eval exits non-zero when any case fails; one baseline case is a known
        # miss, so a non-zero exit here is EXPECTED and not itself a gate failure.
        # Do NOT use `exit 0` inside the gate block -- that would terminate this
        # whole script. We simply run the eval and ignore its exit code, then let
        # the real gate be the per-question comparison below.
        Invoke-Gate "L3a retrieval eval (retrieval-only)" {
            & $Py -m eval --retrieval-only *>&1 | Select-Object -Last 18 | Out-String | Write-Host
            $global:LASTEXITCODE = 0
        }

        Write-Host ""
        Write-Host ("=" * 64) -ForegroundColor DarkGray
        Write-Host ">> L3b per-question baseline comparison" -ForegroundColor Cyan
        Write-Host ("=" * 64) -ForegroundColor DarkGray
        Invoke-Gate "L3b baseline comparison" {
            & $Py -m eval.compare --baseline baseline_v1 --current (Join-Path $Root "output\eval_result.json") *>&1 | Out-String | Write-Host
        }
    }
    else {
        Write-Host ""
        Write-Host "[SKIP] L3 end-to-end eval (run with -Full)" -ForegroundColor Yellow
        Write-Host "       Required after changing retrieval/chunking/ingest/eval." -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
    # Restore the caller's environment (these two only affect this run).
    if ($null -eq $prevNoBytecode) { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue }
    else { $env:PYTHONDONTWRITEBYTECODE = $prevNoBytecode }
    if ($null -eq $prevPycPrefix) { Remove-Item Env:PYTHONPYCACHEPREFIX -ErrorAction SilentlyContinue }
    else { $env:PYTHONPYCACHEPREFIX = $prevPycPrefix }
}

# ---------- summary ----------
Write-Host ""
Write-Host ("=" * 64) -ForegroundColor DarkGray
if ($script:Failed.Count -eq 0) {
    Write-Host "OK - all gates passed, safe to continue" -ForegroundColor Green
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    exit 0
}
else {
    Write-Host "FAILED ($($script:Failed.Count)): $($script:Failed -join ', ')" -ForegroundColor Red
    Write-Host "  -> Do NOT proceed. Fix, or roll back with git checkout." -ForegroundColor Red
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    exit 1
}
