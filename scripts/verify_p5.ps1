[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$StateRoot = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($Python)) {
    $candidate = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $candidate) { $candidate } else { "python" }
}
if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    $StateRoot = Join-Path ([IO.Path]::GetTempPath()) ("orca-agent-p5-offline-" + [guid]::NewGuid().ToString("N"))
}

Push-Location $repoRoot
try {
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    & $Python -m orca_agent.interfaces.cli --state-root $StateRoot doctor --workflow p5 --json
    if ($LASTEXITCODE -ne 0) { throw "P5 doctor failed" }

    # The test invokes several independent CLI processes and uses only the
    # deterministic fake execution backend.  No ORCA path or network flag is
    # supplied here.
    & $Python -m pytest -q tests/p5
    if ($LASTEXITCODE -ne 0) { throw "P5 offline verification failed" }
}
finally {
    Pop-Location
}
