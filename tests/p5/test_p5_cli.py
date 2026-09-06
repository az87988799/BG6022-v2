from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(repo_root: Path, state_root: Path, *arguments: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "orca_agent.interfaces.cli",
            "--state-root",
            str(state_root),
            *arguments,
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_p5_cli_prepare_approve_worker_and_export_across_processes(tmp_path):
    repo_root = Path(__file__).parents[2]
    state_root = tmp_path / "state"
    started = _run_cli(
        repo_root,
        state_root,
        "prepare",
        "--smiles",
        "O",
        "--charge",
        "0",
        "--multiplicity",
        "1",
        "--provider",
        "local",
        "--new-conversation",
        "--json",
    )
    p4_run_id = started["run_id"]
    _run_cli(repo_root, state_root, "worker", "--workflow", "p4", "--json")
    p4_view = _run_cli(
        repo_root,
        state_root,
        "inspect",
        "--workflow",
        "p4",
        "--run-id",
        str(p4_run_id),
        "--json",
    )
    candidate = p4_view["candidate_bundle"]["candidates"][0]
    _run_cli(
        repo_root,
        state_root,
        "confirm-identity",
        "--run-id",
        str(p4_run_id),
        "--conversation-id",
        str(p4_view["conversation_id"]),
        "--expected-revision",
        str(p4_view["revision"]),
        "--interrupt-id",
        str(p4_view["interrupt"]["interrupt_id"]),
        "--query-id",
        str(p4_view["query"]["query_id"]),
        "--query-hash",
        str(p4_view["query"]["query_hash"]),
        "--candidate-bundle-id",
        str(p4_view["candidate_bundle"]["record_id"]),
        "--candidate-bundle-hash",
        str(p4_view["candidate_bundle"]["bundle_hash"]),
        "--candidate-set-hash",
        str(p4_view["candidate_bundle"]["candidate_set_hash"]),
        "--candidate-id",
        str(candidate["candidate_id"]),
        "--candidate-hash",
        str(candidate["candidate_hash"]),
        "--decision",
        "accept",
        "--json",
    )
    prepared = _run_cli(
        repo_root,
        state_root,
        "prepare-execution",
        "--source-run-id",
        str(p4_run_id),
        "--protocol",
        "p5.sp_initial.r2scan3c.v1",
        "--json",
    )
    p5_run_id = prepared["run_id"]
    p5_view = _run_cli(
        repo_root,
        state_root,
        "inspect",
        "--workflow",
        "p5",
        "--run-id",
        str(p5_run_id),
        "--json",
    )
    _run_cli(
        repo_root,
        state_root,
        "approve",
        "--workflow",
        "p5",
        "--run-id",
        str(p5_run_id),
        "--conversation-id",
        str(p5_view["conversation_id"]),
        "--action-id",
        str(p5_view["action"]["action_id"]),
        "--action-hash",
        str(p5_view["action"]["action_hash"]),
        "--binding-hash",
        str(p5_view["binding"]["binding_hash"]),
        "--envelope-hash",
        str(p5_view["action"]["envelope_hash"]),
        "--budget-hash",
        str(p5_view["action"]["budget_hash"]),
        "--expected-revision",
        str(p5_view["revision"]),
        "--json",
    )
    _run_cli(repo_root, state_root, "worker", "--workflow", "p5", "--json")
    final = _run_cli(
        repo_root,
        state_root,
        "inspect",
        "--workflow",
        "p5",
        "--run-id",
        str(p5_run_id),
        "--json",
    )
    assert final["state"]["phase"] == "completed"
    output = tmp_path / "execution.json"
    exported = _run_cli(
        repo_root,
        state_root,
        "export-execution",
        "--run-id",
        str(p5_run_id),
        "--format",
        "json",
        "--output",
        str(output),
        "--json",
    )
    assert exported["valid"] is True
    assert json.loads(output.read_text(encoding="utf-8"))["claim_status"] == "not_generated"
