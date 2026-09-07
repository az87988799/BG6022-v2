[CmdletBinding()]
param(
    [switch]$EnableReal,
    [switch]$ConfirmWaterGate,
    [Parameter(Mandatory = $true)][string]$OrcaExecutable,
    [Parameter(Mandatory = $true)][string]$OrcaVersion,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $repoRoot ".venv\Scripts\python.exe"
}
Push-Location $repoRoot
try {
    $gateArgs = @(
        (Join-Path $PSScriptRoot "verify_p5_real_orca.py"),
        "--state-root", $StateRoot,
        "--orca-executable", $OrcaExecutable,
        "--orca-version", $OrcaVersion
    )
    # Preview freezes all five tasks. Both switches require prior owner consent.
    if ($EnableReal -and $ConfirmWaterGate) {
        $gateArgs += @("--execute", "--confirm-water-gate")
    }
    & $Python @gateArgs
    if ($LASTEXITCODE -ne 0) { throw "Water gate failed; preserve evidence, do not retry." }
}
finally {
    Pop-Location
}
