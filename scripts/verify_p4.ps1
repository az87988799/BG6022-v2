[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StateRoot,

    [ValidateSet("fake", "pubchem")]
    [string]$Provider = "fake",

    [switch]$AutoConfirmIdentity,

    [string]$ManifestPath
)

$ErrorActionPreference = "Stop"

if ($AutoConfirmIdentity -and $Provider -ne "fake") {
    throw "-AutoConfirmIdentity is permitted only with the local fake provider."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$stateRoot = [IO.Path]::GetFullPath($StateRoot)
$databasePath = Join-Path $stateRoot "state.sqlite3"
if (Test-Path -LiteralPath $databasePath) {
    throw "StateRoot must be fresh; refusing to reuse $databasePath"
}
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null

if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $stateRoot "p4-verification-manifest.json"
} else {
    $ManifestPath = [IO.Path]::GetFullPath($ManifestPath)
}

$python = (Get-Command python -ErrorAction Stop).Source

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-AgentJson {
    param(
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    Push-Location $repoRoot
    try {
        $raw = @(& $python -m uv run --offline --no-sync python -m orca_agent `
            --state-root $stateRoot @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $jsonLine = $raw |
        Where-Object { $_ -is [string] -and $_.Trim().Length -gt 0 } |
        Select-Object -Last 1
    if ($null -eq $jsonLine) {
        throw "CLI produced no JSON output: $($Arguments -join ' ')"
    }
    try {
        $data = $jsonLine | ConvertFrom-Json -Depth 50
    } catch {
        throw "CLI produced invalid JSON: $jsonLine"
    }
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "CLI failed with exit code $($exitCode): $jsonLine"
    }
    [pscustomobject]@{
        ExitCode = $exitCode
        Data = $data
    }
}

$manifest = [ordered]@{
    manifest_version = 1
    workflow = "p4"
    provider = $Provider
    started_at_utc = [DateTime]::UtcNow.ToString("o")
    state_root = $stateRoot
    auto_confirm_identity = [bool]$AutoConfirmIdentity
    cases = @()
    cancellation = $null
    wrong_binding = $null
    backend_counts = $null
}

$awaitingForNegativeCase = $null
foreach ($name in @("water", "ethanol", "benzene")) {
    $prepareFile = Join-Path $stateRoot "prepare-$name.json"
    $prepare = Invoke-AgentJson @(
        "prepare", "--name", $name, "--charge", "0", "--multiplicity", "1",
        "--provider", $Provider, "--protocol", "ground_state_baseline_r2scan3c_v1",
        "--new-conversation", "--save-request", $prepareFile, "--json"
    )
    Assert-Condition ($prepare.Data.accepted -eq $true) "prepare $name was not accepted"

    $workerArguments = @("worker", "--workflow", "p4", "--drain", "--json")
    if ($Provider -eq "pubchem") {
        $workerArguments += "--allow-network"
    }
    $worker = Invoke-AgentJson $workerArguments
    $runId = [string]$prepare.Data.run_id
    $case = [ordered]@{
        name = $name
        run_id = $runId
        prepare = $prepare.Data
        worker = $worker.Data
    }

    if ($AutoConfirmIdentity) {
        $awaiting = Invoke-AgentJson @("inspect", "--run", $runId, "--json")
        Assert-Condition ($awaiting.Data.state.phase -eq "awaiting_identity") "$name did not await identity"
        if ($null -eq $awaitingForNegativeCase) {
            $awaitingForNegativeCase = $awaiting.Data
        }
        $candidate = $awaiting.Data.candidate_bundle.candidates[0]
        $confirmFile = Join-Path $stateRoot "confirm-$name.json"
        $confirm = Invoke-AgentJson @(
            "confirm-identity", "--run", $runId,
            "--conversation-id", $awaiting.Data.conversation_id,
            "--expected-revision", ([string]$awaiting.Data.revision),
            "--interrupt-id", $awaiting.Data.interrupt.interrupt_id,
            "--query-hash", $awaiting.Data.query.query_hash,
            "--candidate-set-hash", $awaiting.Data.candidate_bundle.candidate_set_hash,
            "--candidate-id", $candidate.candidate_id,
            "--candidate-hash", $candidate.candidate_hash,
            "--decision", "accept", "--save-request", $confirmFile, "--json"
        )
        Assert-Condition ($confirm.Data.accepted -eq $true) "$name identity confirmation failed"
        $planFile = Join-Path $stateRoot "$name-plan.md"
        $plan = Invoke-AgentJson @(
            "export-plan", "--run", $runId, "--format", "md",
            "--output", $planFile, "--json"
        )
        Assert-Condition (Test-Path -LiteralPath $planFile) "$name plan export is missing"
        $replay = Invoke-AgentJson @("replay-request", "--file", $confirmFile, "--json")
        Assert-Condition ($replay.Data.revision -eq $confirm.Data.revision) "$name replay changed the result"
        $case.confirm = $confirm.Data
        $case.replay = $replay.Data
        $case.export = $plan.Data
    } else {
        $case.status = "identity_confirmation_not_requested"
    }
    $manifest.cases += [pscustomobject]$case
}

if ($AutoConfirmIdentity -and $null -ne $awaitingForNegativeCase) {
    $negative = Invoke-AgentJson @(
        "confirm-identity", "--run", $awaitingForNegativeCase.run_id,
        "--conversation-id", $awaitingForNegativeCase.conversation_id,
        "--expected-revision", ([string]$awaitingForNegativeCase.revision),
        "--interrupt-id", $awaitingForNegativeCase.interrupt.interrupt_id,
        "--query-hash", $awaitingForNegativeCase.query.query_hash,
        "--candidate-set-hash", $awaitingForNegativeCase.candidate_bundle.candidate_set_hash,
        "--candidate-id", $awaitingForNegativeCase.candidate_bundle.candidates[0].candidate_id,
        "--candidate-hash", ("0" * 64), "--decision", "accept", "--json"
    ) -AllowFailure
    Assert-Condition ($negative.Data.accepted -eq $false) "wrong candidate binding was accepted"
    $manifest.wrong_binding = $negative.Data
}

$cancelPrepare = Invoke-AgentJson @(
    "prepare", "--name", "water", "--charge", "0", "--multiplicity", "1",
    "--provider", $Provider, "--new-conversation", "--json"
)
$cancel = Invoke-AgentJson @(
    "cancel", "--workflow", "p4", "--run", $cancelPrepare.Data.run_id,
    "--conversation-id", $cancelPrepare.Data.conversation_id,
    "--expected-revision", ([string]$cancelPrepare.Data.revision),
    "--reason-code", "user_cancelled", "--json"
)
Assert-Condition ($cancel.Data.accepted -eq $true) "P4 cancellation failed"
$manifest.cancellation = $cancel.Data

$countsCode = 'import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps({"actions": c.execute("select count(*) from actions").fetchone()[0], "jobs": c.execute("select count(*) from jobs").fetchone()[0]}))'
$countsRaw = & $python -c $countsCode $databasePath
$counts = ($countsRaw | Select-Object -Last 1) | ConvertFrom-Json
Assert-Condition ($counts.actions -eq 0 -and $counts.jobs -eq 0) "P4 created execution records"
$manifest.backend_counts = $counts
$manifest.completed_at_utc = [DateTime]::UtcNow.ToString("o")

$manifestParent = Split-Path -Parent $ManifestPath
if (-not [string]::IsNullOrWhiteSpace($manifestParent)) {
    New-Item -ItemType Directory -Force -Path $manifestParent | Out-Null
}
$manifest | ConvertTo-Json -Depth 50 | Set-Content -LiteralPath $ManifestPath -Encoding utf8
Write-Output ($manifest | ConvertTo-Json -Depth 50)
