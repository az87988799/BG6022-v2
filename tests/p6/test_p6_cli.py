from __future__ import annotations

import json

from orca_agent.interfaces import cli
from tests.p6.test_p6_workflow import _fake_sp_source


def _last_json(capsys) -> dict[str, object]:
    output = capsys.readouterr().out.strip().splitlines()
    return json.loads(output[-1])


def test_p6_cli_assess_worker_report_verify_and_replay(tmp_path, capsys) -> None:
    p5_service, _clock, source = _fake_sp_source(tmp_path)
    state_root = str(p5_service.state_root)
    request = tmp_path / "p6-assess-request.json"
    assert (
        cli.main(
            [
                "--state-root",
                state_root,
                "assess",
                "--source-run-id",
                str(source.run_id),
                "--save-request",
                str(request),
                "--json",
            ]
        )
        == 0
    )
    created = _last_json(capsys)
    run_id = str(created["run_id"])
    assert json.loads(request.read_text(encoding="utf-8"))["command_type"] == "p6.assess"

    assert (
        cli.main(
            [
                "--state-root",
                state_root,
                "worker",
                "--workflow",
                "p6",
                "--run-id",
                run_id,
                "--drain",
                "--json",
            ]
        )
        == 0
    )
    worker = _last_json(capsys)
    assert [item["outcome"] for item in worker["reports"]] == ["succeeded", "succeeded"]

    assert (
        cli.main(
            [
                "--state-root",
                state_root,
                "inspect",
                "--workflow",
                "p6",
                "--run-id",
                run_id,
                "--json",
            ]
        )
        == 0
    )
    inspected = _last_json(capsys)
    assert inspected["state"]["phase"] == "completed"

    exported = tmp_path / "p6-report.md"
    assert (
        cli.main(
            [
                "--state-root",
                state_root,
                "report",
                "--run-id",
                run_id,
                "--format",
                "md",
                "--output",
                str(exported),
                "--json",
            ]
        )
        == 0
    )
    assert _last_json(capsys)["valid"] is True
    assert exported.exists()
    assert (
        cli.main(
            [
                "--state-root",
                state_root,
                "verify-report",
                "--run-id",
                run_id,
                "--report",
                str(exported),
                "--json",
            ]
        )
        == 0
    )
    verified = _last_json(capsys)
    assert verified["valid"] is True and verified["exported_report"]["valid"] is True

    assert (
        cli.main(
            [
                "--state-root",
                state_root,
                "replay-request",
                "--file",
                str(request),
                "--json",
            ]
        )
        == 0
    )
    assert _last_json(capsys)["accepted"] is True
