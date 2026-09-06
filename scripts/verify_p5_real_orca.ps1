[CmdletBinding()]
param(
    [switch]$EnableReal,
    [switch]$ConfirmWaterGate,
    [Parameter(Mandatory = $true)]
    [string]$OrcaExecutable,
    [string]$OrcaVersion = "",
    [string]$StateRoot = "",
    [int]$MaxPolls = 1800,
    [int]$PollSeconds = 2,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($Python)) {
    $candidate = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $candidate) { $candidate } else { "python" }
}
$orcaPath = (Resolve-Path -LiteralPath $OrcaExecutable).Path
if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    $StateRoot = Join-Path ([IO.Path]::GetTempPath()) ("orca-agent-p5-water-real-" + [guid]::NewGuid().ToString("N"))
}

$preview = [ordered]@{
    mode = if ($EnableReal -and $ConfirmWaterGate) { "execute" } else { "preview" }
    molecule = "Water"
    smiles = "O"
    protocol = "p5.opt_freq_sp.r2scan3c.v1"
    orca_executable = $orcaPath
    orca_version = $OrcaVersion
    state_root = $StateRoot
    cores = 1
    job_budgets_seconds = @(900, 1800, 300)
    run_budget_seconds = 3600
    real_execution_authorized = [bool]($EnableReal -and $ConfirmWaterGate)
    note = "R03/R04 are not implicitly attempted; preserve NOT_EXERCISED when not controlled."
}
$preview | ConvertTo-Json -Depth 6
if (-not ($EnableReal -and $ConfirmWaterGate)) {
    Write-Output "Preview only. Re-run with -EnableReal -ConfirmWaterGate to execute the explicitly scoped Water gate."
    exit 0
}
if ([string]::IsNullOrWhiteSpace($OrcaVersion) -or -not $OrcaVersion.StartsWith("6.1")) {
    throw "-OrcaVersion must explicitly identify ORCA 6.1"
}
if ($MaxPolls -lt 1 -or $PollSeconds -lt 1) { throw "poll limits must be positive" }

Push-Location $repoRoot
try {
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    function Invoke-OrcaAgent([string[]]$Arguments) {
        $output = & $Python -m orca_agent.interfaces.cli --state-root $StateRoot @Arguments 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw ("orca-agent failed: " + ($output -join [Environment]::NewLine))
        }
        return ($output | Select-Object -Last 1 | ConvertFrom-Json)
    }

    $doctor = Invoke-OrcaAgent @("doctor", "--workflow", "p5", "--probe", "--orca-executable", $orcaPath, "--json")
    if (-not $doctor.real_execution) { throw "ORCA doctor did not verify a real ORCA 6.1 executable" }
    if ([string]$doctor.orca_version -ne $OrcaVersion) {
        throw "-OrcaVersion does not match the probed ORCA version"
    }
    $started = Invoke-OrcaAgent @("prepare", "--smiles", "O", "--charge", "0", "--multiplicity", "1", "--provider", "local", "--new-conversation", "--json")
    $p4Run = [string]$started.run_id
    $null = Invoke-OrcaAgent @("worker", "--workflow", "p4", "--json")
    $p4 = Invoke-OrcaAgent @("inspect", "--workflow", "p4", "--run-id", $p4Run, "--json")
    $candidate = $p4.candidate_bundle.candidates[0]
    $null = Invoke-OrcaAgent @(
        "confirm-identity", "--run-id", $p4Run, "--conversation-id", $p4.conversation_id,
        "--expected-revision", $p4.revision, "--interrupt-id", $p4.interrupt.interrupt_id,
        "--query-id", $p4.query.query_id, "--query-hash", $p4.query.query_hash,
        "--candidate-bundle-id", $p4.candidate_bundle.record_id,
        "--candidate-bundle-hash", $p4.candidate_bundle.bundle_hash,
        "--candidate-set-hash", $p4.candidate_bundle.candidate_set_hash,
        "--candidate-id", $candidate.candidate_id, "--candidate-hash", $candidate.candidate_hash,
        "--decision", "accept", "--json"
    )
    $prepared = Invoke-OrcaAgent @(
        "prepare-execution", "--source-run-id", $p4Run, "--protocol", "p5.opt_freq_sp.r2scan3c.v1",
        "--backend", "local_orca", "--orca-executable", $orcaPath, "--orca-version", $OrcaVersion, "--json"
    )
    $runId = [string]$prepared.run_id
    for ($poll = 0; $poll -lt $MaxPolls; $poll++) {
        $view = Invoke-OrcaAgent @("inspect", "--workflow", "p5", "--run-id", $runId, "--json")
        $phase = [string]$view.state.phase
        if ($phase -eq "completed") { break }
        if ($phase -in @("failed", "cancelled")) { throw "Water gate reached terminal phase $phase" }
        if ($phase -eq "awaiting_execution_approval") {
            $null = Invoke-OrcaAgent @(
                "approve", "--workflow", "p5", "--run-id", $runId,
                "--conversation-id", $view.conversation_id, "--action-id", $view.action.action_id,
                "--action-hash", $view.action.action_hash, "--binding-hash", $view.binding.binding_hash,
                "--envelope-hash", $view.action.envelope_hash, "--budget-hash", $view.action.budget_hash,
                "--expected-revision", $view.revision, "--json"
            )
        } else {
            $null = Invoke-OrcaAgent @(
                "worker", "--workflow", "p5", "--backend", "local_orca", "--allow-real-orca",
                "--orca-executable", $orcaPath, "--orca-version", $OrcaVersion, "--limit", "1", "--json"
            )
        }
        Start-Sleep -Seconds $PollSeconds
    }
    $final = Invoke-OrcaAgent @("inspect", "--workflow", "p5", "--run-id", $runId, "--json")
    if ([string]$final.state.phase -ne "completed") { throw "Water gate did not complete within MaxPolls" }
    $final | ConvertTo-Json -Depth 20
}
finally {
    Pop-Location
}
