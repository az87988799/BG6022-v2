import json
import runpy
import sys

from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.interfaces.cli import main


def test_module_entrypoint_runs_the_same_offline_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orca_agent",
            "--state-root",
            str(tmp_path / "state"),
            "start",
            "--fixture",
            "water_sp_v1",
            "--new-conversation",
            "--json",
        ],
    )
    try:
        runpy.run_module("orca_agent.__main__", run_name="__main__")
    except SystemExit as error:
        assert error.code == 0


def test_cli_start_and_replay_request_are_json_and_offline(tmp_path, capsys) -> None:
    state_root = tmp_path / "state"
    request = tmp_path / "start.json"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "start",
                "--fixture",
                "water_sp_v1",
                "--new-conversation",
                "--save-request",
                str(request),
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["phase"] == "awaiting_approval"
    assert request.exists()
    assert (
        main(["--state-root", str(state_root), "replay-request", "--file", str(request), "--json"])
        == 0
    )
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["run_id"] == output["run_id"]


def test_cli_rejects_an_unknown_fixture(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "--state-root",
                str(tmp_path / "state"),
                "start",
                "--fixture",
                "not_a_fixture",
                "--new-conversation",
                "--json",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().err)["error"] == "ValueError"


def test_cli_runs_and_verifies_the_persisted_report_chain(tmp_path, capsys) -> None:
    state_root = tmp_path / "state"
    approval_file = tmp_path / "approve.json"

    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "start",
                "--fixture",
                "water_sp_v1",
                "--new-conversation",
                "--json",
            ]
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)
    approval = started["approval"]
    state = approval["state"]

    assert (
        main(
            ["--state-root", str(state_root), "worker", "--drain", "--max-effects", "20", "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["reports"] == []
    assert P3ApplicationService(state_root).backend.execution_count() == 0

    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "approve",
                "--run",
                started["run_id"],
                "--conversation-id",
                started["conversation_id"],
                "--interrupt-id",
                state["approval_interrupt_id"],
                "--action-id",
                approval["action"]["action_id"],
                "--action-hash",
                approval["action"]["action_hash"],
                "--envelope-hash",
                state["envelope_hash"],
                "--budget-hash",
                state["budget_hash"],
                "--expected-revision",
                str(approval["revision"]),
                "--save-request",
                str(approval_file),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["phase"] == "dispatch_pending"

    assert (
        main(
            ["--state-root", str(state_root), "worker", "--drain", "--max-effects", "20", "--json"]
        )
        == 0
    )
    worker_output = json.loads(capsys.readouterr().out)
    assert [item["outcome"] for item in worker_output["reports"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]

    report_file = tmp_path / "report.json"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "report",
                "--run",
                started["run_id"],
                "--format",
                "json",
                "--output",
                str(report_file),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "verify-report",
                "--run",
                started["run_id"],
                "--report",
                str(report_file),
                "--json",
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] is True
    assert verified["exported_report"]["valid"] is True
    assert json.loads(report_file.read_text(encoding="utf-8"))["data_origin"] == "fake_fixture"

    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "replay-request",
                "--file",
                str(approval_file),
                "--json",
            ]
        )
        == 0
    )
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["run_id"] == started["run_id"]
    assert replayed["phase"] == "dispatch_pending"
