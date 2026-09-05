"""Thin offline CLI for the P3 Water fake vertical slice."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.domain.ids import (
    ActionId,
    ConversationId,
    InterruptId,
    RunId,
)
from orca_agent.domain.p3 import ReportManifestV1, WorkflowPhase
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository, P3RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.p3_versions import P3_FIXTURE_ID
from orca_agent.reporting.renderer import P3ReportRenderer

from ..execution.commands import ApproveAction, CancelWaterRun, StartWaterRun


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orca-agent")
    parser.add_argument("--state-root", required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--fixture", default=P3_FIXTURE_ID)
    start.add_argument("--new-conversation", action="store_true")
    start.add_argument("--save-request")
    start.add_argument("--json", action="store_true")

    approve = subparsers.add_parser("approve")
    for name, value_type, required in (
        ("run-id", RunId, True),
        ("conversation-id", ConversationId, True),
        ("interrupt-id", InterruptId, True),
        ("action-id", ActionId, True),
    ):
        option_names = (f"--{name}", "--run" if name == "run-id" else f"--{name}")
        approve.add_argument(
            *dict.fromkeys(option_names),
            dest=name.replace("-", "_"),
            type=value_type,
            required=required,
        )
    approve.add_argument("--action-hash", required=True)
    approve.add_argument("--envelope-hash", required=True)
    approve.add_argument("--budget-hash", required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    approve.add_argument("--command-id", type=_command_id)
    approve.add_argument("--save-request")
    approve.add_argument("--json", action="store_true")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    inspect.add_argument("--json", action="store_true")

    worker = subparsers.add_parser("worker")
    worker.add_argument("--limit", "--max-effects", dest="limit", type=int, default=1)
    worker.add_argument("--drain", action="store_true")
    worker.add_argument("--json", action="store_true")

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    cancel.add_argument("--conversation-id", type=ConversationId, required=True)
    cancel.add_argument("--expected-revision", type=int, required=True)
    cancel.add_argument("--reason-code", default="user_cancelled")
    cancel.add_argument("--json", action="store_true")

    report = subparsers.add_parser("report")
    report.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    report.add_argument("--format", choices=("md", "json"), default="md")
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify-report")
    verify.add_argument("--run-id", "--run", dest="run_id", type=RunId)
    verify.add_argument("--report", type=Path)
    verify.add_argument("--json", action="store_true")

    replay = subparsers.add_parser("replay-request")
    replay.add_argument("--file", type=Path, required=True)
    replay.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        service = P3ApplicationService(args.state_root)
        if args.operation == "start":
            if args.fixture != P3_FIXTURE_ID:
                raise ValueError("unsupported fixture")
            command = StartWaterRun.create(
                requested_at_utc=service.clock.now_utc(),
                new_conversation=args.new_conversation,
            )
            _save_request(command, args.save_request)
            result = service.start(command)
            output = result.model_dump(mode="json")
            if result.accepted:
                output["approval"] = service.inspect(result.run_id).model_dump(mode="json")
            return _emit(output, result.accepted, args.json)
        if args.operation == "approve":
            command = ApproveAction.create(
                run_id=args.run_id,
                conversation_id=args.conversation_id,
                interrupt_id=args.interrupt_id,
                action_id=args.action_id,
                action_hash=args.action_hash,
                envelope_hash=args.envelope_hash,
                budget_hash=args.budget_hash,
                expected_revision=args.expected_revision,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.approve(command)
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "inspect":
            return _emit(service.inspect(args.run_id).model_dump(mode="json"), True, args.json)
        if args.operation == "worker":
            worker = service.create_worker()
            reports = []
            while True:
                batch = worker.run_once(limit=max(args.limit, 1))
                reports.extend(asdict(item) for item in batch)
                if not args.drain or not batch:
                    break
            return _emit(
                {"reports": reports, "backend_execution_count": service.backend.execution_count()},
                True,
                args.json,
            )
        if args.operation == "cancel":
            result = service.cancel(
                CancelWaterRun.create(
                    run_id=args.run_id,
                    conversation_id=args.conversation_id,
                    expected_revision=args.expected_revision,
                    reason_code=args.reason_code,
                    requested_at_utc=service.clock.now_utc(),
                )
            )
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "report":
            output = _export_report(service, args.run_id, args.format, args.output)
            return _emit(output, True, args.json)
        if args.operation == "verify-report":
            if args.run_id is None and args.report is None:
                raise ValueError("verify-report requires --run or --report")
            if args.run_id is not None:
                output = P3ReportRenderer(
                    service.database_path,
                    service.state_root,
                    clock=service.clock,
                ).verify(args.run_id)
                if args.report is not None:
                    output["exported_report"] = _verify_report(args.report)
            else:
                output = _verify_report(args.report)
            return _emit(output, bool(output["valid"]), args.json)
        if args.operation == "replay-request":
            command = _load_request(args.file)
            if isinstance(command, ApproveAction):
                result = service.approve(command)
            elif isinstance(command, StartWaterRun):
                result = service.start(command)
            else:
                raise ValueError("unsupported replay request")
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        raise ValueError("unsupported operation")
    except SystemExit:
        raise
    except Exception as error:
        print(
            json.dumps({"accepted": False, "code": "cli_error", "error": type(error).__name__}),
            file=sys.stderr,
        )
        return 3


def _command_id(value: str):
    from orca_agent.domain.ids import CommandId

    return CommandId(value)


def _save_request(command: object, path: str | None) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(command.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_request(path: Path):
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("request file must contain a JSON object")
    if "fixture_id" in payload:
        return StartWaterRun.model_validate_json(raw, strict=True)
    return ApproveAction.model_validate_json(raw, strict=True)


def _export_report(service: P3ApplicationService, run_id: RunId, format_name: str, output: Path):
    view = service.inspect(run_id)
    if view.state.phase is not WorkflowPhase.COMPLETED:
        raise ValueError("report is not available until the workflow is completed")
    P3ReportRenderer(service.database_path, service.state_root, clock=service.clock).verify(run_id)
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        manifest_entry = P3RecordRepository(uow.connection).latest(
            run_id=run_id,
            record_type="report_manifest",
            model_type=ReportManifestV1,
        )
        if manifest_entry is None:
            raise ValueError("report manifest is missing")
        manifest = manifest_entry[1]
        artifact_id = (
            manifest.markdown_artifact_id if format_name == "md" else manifest.json_artifact_id
        )
        artifact = ArtifactRecordRepository(uow.connection).get(artifact_id)
        if artifact is None:
            raise ValueError("report artifact is missing")
        content = ArtifactStore(service.state_root).read(artifact)
        uow.commit()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return {"valid": True, "path": str(output), "artifact_id": str(artifact_id)}


def _verify_report(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"valid": False, "path": str(path)}
    if "FAKE FIXTURE ONLY" in text:
        return {"valid": True, "path": str(path)}
    try:
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError):
        payload = None
    valid = (
        isinstance(payload, dict)
        and payload.get("fake_marker") == "fake_fixture_only"
        and payload.get("data_origin") == "fake_fixture"
        and payload.get("real_scientific_result") is False
        and payload.get("backend") == "fake"
    )
    return {"valid": valid, "path": str(path)}


def _emit(value: object, accepted: bool, json_requested: bool) -> int:
    del json_requested
    print(json.dumps(value, ensure_ascii=False, default=str))
    return 0 if accepted else 2


__all__ = ["build_parser", "main"]
