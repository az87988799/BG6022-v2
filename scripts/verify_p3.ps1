param(
    [Parameter(Mandatory = $true)]
    [string]$StateRoot,

    [switch]$AutoApprove
)

$ErrorActionPreference = "Stop"
$resolvedRoot = [IO.Path]::GetFullPath($StateRoot)
if (Test-Path -LiteralPath $resolvedRoot) {
    throw "Refusing to overwrite an existing P3 verification directory: $resolvedRoot"
}
New-Item -ItemType Directory -Path $resolvedRoot | Out-Null

function Invoke-P3Cli {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & python -m uv run --offline --no-sync python -m orca_agent `
        --state-root $resolvedRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "P3 CLI failed (exit $LASTEXITCODE): $($output -join "`n")"
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Invoke-P3CliExpectedFailure {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & python -m uv run --offline --no-sync python -m orca_agent `
        --state-root $resolvedRoot @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        throw "P3 CLI unexpectedly succeeded: $($output -join "`n")"
    }
    return [pscustomobject]@{ exit_code = $exitCode; output = ($output -join "`n") }
}

$start = Invoke-P3Cli @("start", "--fixture", "water_sp_v1", "--new-conversation", "--json")
$approval = $start.approval
$state = $approval.state
$action = $approval.action

if ($action.execution_envelope.backend_kind -ne "fake") {
    throw "P3 action is not restricted to FakeBackend"
}
if ($state.phase -ne "awaiting_approval" -or $state.pending_interrupt_id -ne $state.approval_interrupt_id) {
    throw "P3 start did not leave one pending approval interrupt"
}

$safeWorker = Invoke-P3Cli @("worker", "--drain", "--max-effects", "20", "--json")
if ($safeWorker.reports.Count -ne 0 -or $safeWorker.backend_execution_count -ne 0) {
    throw "SAFE worker unexpectedly dispatched before approval"
}

$beforeApproval = Invoke-P3Cli @("inspect", "--run", $start.run_id, "--json")
if ($beforeApproval.state.phase -ne "awaiting_approval") {
    throw "workflow changed before approval"
}

Write-Output "P3 fake action: $($action.action_id)"
Write-Output "P3 budget: cores=$($action.budget.cores), memory_mb=$($action.budget.memory_mb), wall_time_seconds=$($action.budget.wall_time_seconds)"
if (-not $AutoApprove) {
    $confirmation = Read-Host "Approve this FakeBackend-only action? Type APPROVE to continue"
    if ($confirmation -cne "APPROVE") {
        throw "Owner did not approve the displayed action"
    }
}

$approvalFile = Join-Path $resolvedRoot "approve.json"
$approved = Invoke-P3Cli @(
    "approve",
    "--run", $start.run_id,
    "--conversation-id", $start.conversation_id,
    "--interrupt-id", $state.approval_interrupt_id,
    "--action-id", $action.action_id,
    "--action-hash", $action.action_hash,
    "--envelope-hash", $state.envelope_hash,
    "--budget-hash", $state.budget_hash,
    "--expected-revision", [string]$approval.revision,
    "--save-request", $approvalFile,
    "--json"
)
if ($approved.phase -ne "dispatch_pending") {
    throw "approval did not create the dispatch stage"
}

$completed = Invoke-P3Cli @("worker", "--drain", "--max-effects", "20", "--json")
if ($completed.reports.Count -ne 3) {
    throw "expected three P3 stage completions, got $($completed.reports.Count)"
}
if (($completed.reports | Where-Object { $_.outcome -ne "succeeded" }).Count -ne 0) {
    throw "a P3 stage did not succeed"
}
if ($completed.backend_execution_count -ne 1) {
    throw "expected exactly one persistent fake execution"
}

$replayed = Invoke-P3Cli @("replay-request", "--file", $approvalFile, "--json")
if (($replayed | ConvertTo-Json -Depth 30 -Compress) -ne ($approved | ConvertTo-Json -Depth 30 -Compress)) {
    throw "approval replay differs from its original public result"
}
$afterReplay = Invoke-P3Cli @("worker", "--drain", "--max-effects", "20", "--json")
if ($afterReplay.backend_execution_count -ne 1 -or $afterReplay.reports.Count -ne 0) {
    throw "replay created new work"
}

$sameRun = Invoke-P3Cli @("inspect", "--run", $start.run_id, "--json")
if ($sameRun.state.phase -ne "completed") {
    throw "cross-process inspect did not observe completion"
}

$markdownFile = Join-Path $resolvedRoot "report.md"
$jsonFile = Join-Path $resolvedRoot "report.json"
$null = Invoke-P3Cli @("report", "--run", $start.run_id, "--format", "md", "--output", $markdownFile, "--json")
$null = Invoke-P3Cli @("report", "--run", $start.run_id, "--format", "json", "--output", $jsonFile, "--json")
$verified = Invoke-P3Cli @("verify-report", "--run", $start.run_id, "--report", $jsonFile, "--json")
if (-not $verified.valid -or -not $verified.exported_report.valid) {
    throw "durable report verification failed"
}

$startA = Invoke-P3Cli @("start", "--fixture", "water_sp_v1", "--new-conversation", "--json")
$startB = Invoke-P3Cli @("start", "--fixture", "water_sp_v1", "--new-conversation", "--json")
if ($startA.conversation_id -eq $startB.conversation_id) {
    throw "new conversations were not isolated"
}
$oldApprovalAgainstB = Invoke-P3CliExpectedFailure @(
    "approve",
    "--run", $startB.run_id,
    "--conversation-id", $startB.conversation_id,
    "--interrupt-id", $startA.approval.state.approval_interrupt_id,
    "--action-id", $startA.approval.action.action_id,
    "--action-hash", $startA.approval.action.action_hash,
    "--envelope-hash", $startA.approval.state.envelope_hash,
    "--budget-hash", $startA.approval.state.budget_hash,
    "--expected-revision", [string]$startB.approval.revision,
    "--json"
)
$bAfterWrongApproval = Invoke-P3Cli @("inspect", "--run", $startB.run_id, "--json")
if ($bAfterWrongApproval.state.phase -ne "awaiting_approval") {
    throw "conversation B accepted conversation A's approval"
}

$summary = [ordered]@{
    status = "PASS"
    state_root = $resolvedRoot
    run_id = $start.run_id
    approval_file = $approvalFile
    markdown_report = $markdownFile
    json_report = $jsonFile
    execution_count = $afterReplay.backend_execution_count
    stage_count = $completed.reports.Count
    report_verified = $verified.valid
    stale_approval_exit_code = $oldApprovalAgainstB.exit_code
    conversation_isolation = $true
}
$summary | ConvertTo-Json -Depth 10
