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

    # Independent CLI, fake outputs and controlled Python child processes.
    # These test process control, not real ORCA scientific acceptance.
    & $Python -m pytest -q tests/p5
    if ($LASTEXITCODE -ne 0) { throw "P5 offline verification failed" }
}
finally {
    Pop-Location
}
