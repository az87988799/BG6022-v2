"""Thin offline CLI for the P3 Water fake vertical slice."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.application.p6_service import P6ApplicationService
from orca_agent.domain.ids import (
    ActionId,
    AssessmentId,
    ConversationId,
    InterruptId,
    RunId,
    WorkflowRecordId,
)
from orca_agent.domain.p3 import ReportManifestV1, WorkflowPhase
from orca_agent.domain.p4 import IdentityDecision, IdentityProvider, MoleculeInputKind
from orca_agent.identity.fake_pubchem import FakePubChemAdapter
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository, P3RecordRepository
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.p3_versions import (
    P3_ENGINE_VERSION,
    P3_FIXTURE_ID,
    P3_SCHEMA_VERSION,
)
from orca_agent.orchestration.p4_commands import (
    CancelPlanningRun,
    ConfirmMoleculeIdentity,
    StartPlanningRun,
)
from orca_agent.orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from orca_agent.orchestration.p5_commands import (
    ApproveP5Action,
    CancelP5Execution,
    PrepareExecution,
    ReconcileP5Execution,
)
from orca_agent.orchestration.p5_versions import P5_ENGINE_VERSION, P5_SCHEMA_VERSION
from orca_agent.orchestration.p6_commands import AssessP6Run, CancelP6Run
from orca_agent.orchestration.p6_versions import P6_ENGINE_VERSION, P6_SCHEMA_VERSION
from orca_agent.reporting.p6_renderer import P6ReportRenderer
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

    prepare = subparsers.add_parser("prepare")
    identity_group = prepare.add_mutually_exclusive_group(required=True)
    identity_group.add_argument("--name")
    identity_group.add_argument("--cas")
    identity_group.add_argument("--cid")
    identity_group.add_argument("--smiles")
    prepare.add_argument("--charge", type=int, required=True)
    prepare.add_argument("--multiplicity", type=int, required=True)
    prepare.add_argument(
        "--provider",
        type=IdentityProvider,
        choices=tuple(IdentityProvider),
        required=True,
    )
    prepare.add_argument(
        "--protocol",
        dest="protocol_id",
        default="ground_state_baseline_r2scan3c_v1",
    )
    prepare.add_argument("--new-conversation", action="store_true")
    prepare.add_argument("--command-id", type=_command_id)
    prepare.add_argument("--save-request")
    prepare.add_argument("--json", action="store_true")

    assess = subparsers.add_parser("assess")
    assess.add_argument("--source-run-id", type=RunId, required=True)
    assess.add_argument(
        "--profile",
        dest="profile_id",
        default="p6.nonlinear.r2scan3c.v1",
    )
    assess.add_argument("--reference-assessment-id", type=AssessmentId)
    assess.add_argument("--expected-source-revision", type=int)
    assess.add_argument("--run-id", type=RunId)
    assess.add_argument("--command-id", type=_command_id)
    assess.add_argument("--save-request")
    assess.add_argument("--json", action="store_true")

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
            required=False if name == "interrupt-id" else required,
        )
    approve.add_argument("--action-hash", required=True)
    approve.add_argument("--envelope-hash", required=True)
    approve.add_argument("--budget-hash", required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    approve.add_argument("--command-id", type=_command_id)
    approve.add_argument("--binding-hash")
    approve.add_argument("--workflow", choices=("p3", "p5"))
    approve.add_argument("--save-request")
    approve.add_argument("--json", action="store_true")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    inspect.add_argument("--workflow", choices=("p3", "p4", "p5", "p6"))
    inspect.add_argument("--json", action="store_true")

    worker = subparsers.add_parser("worker")
    worker.add_argument("--limit", "--max-effects", dest="limit", type=int, default=1)
    worker.add_argument("--run-id", type=RunId)
    worker.add_argument("--drain", action="store_true")
    worker.add_argument("--workflow", choices=("p3", "p4", "p5", "p6"), default="p3")
    worker.add_argument("--allow-network", action="store_true")
    worker.add_argument("--allow-real-orca", action="store_true")
    worker.add_argument("--backend", choices=("fake", "local_orca"), default="fake")
    worker.add_argument("--orca-executable", type=Path)
    worker.add_argument("--orca-version")
    worker.add_argument("--json", action="store_true")

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    cancel.add_argument("--conversation-id", type=ConversationId, required=True)
    cancel.add_argument("--expected-revision", type=int, required=True)
    cancel.add_argument("--reason-code", default="user_cancelled")
    cancel.add_argument("--workflow", choices=("p3", "p4", "p5", "p6"))
    cancel.add_argument("--command-id", type=_command_id)
    cancel.add_argument("--save-request")
    cancel.add_argument("--json", action="store_true")

    confirm = subparsers.add_parser("confirm-identity")
    confirm.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    confirm.add_argument("--conversation-id", type=ConversationId, required=True)
    confirm.add_argument("--expected-revision", type=int, required=True)
    confirm.add_argument("--interrupt-id", type=InterruptId, required=True)
    confirm.add_argument("--query-id", type=WorkflowRecordId)
    confirm.add_argument("--query-hash", required=True)
    confirm.add_argument("--candidate-bundle-id", type=WorkflowRecordId)
    confirm.add_argument("--candidate-bundle-hash")
    confirm.add_argument("--candidate-set-hash", required=True)
    confirm.add_argument("--candidate-id", required=True)
    confirm.add_argument("--candidate-hash", required=True)
    confirm.add_argument(
        "--decision",
        type=IdentityDecision,
        choices=tuple(IdentityDecision),
        required=True,
    )
    confirm.add_argument("--command-id", type=_command_id)
    confirm.add_argument("--save-request")
    confirm.add_argument("--json", action="store_true")

    export_plan = subparsers.add_parser("export-plan")
    export_plan.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    export_plan.add_argument("--format", choices=("md", "json"), default="md")
    export_plan.add_argument("--output", type=Path, required=True)
    export_plan.add_argument("--json", action="store_true")

    report = subparsers.add_parser("report")
    report.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    report.add_argument("--format", choices=("md", "json"), default="md")
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify-report")
    verify.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    verify.add_argument("--report", type=Path)
    verify.add_argument("--json", action="store_true")

    replay = subparsers.add_parser("replay-request")
    replay.add_argument("--file", type=Path, required=True)
    replay.add_argument("--json", action="store_true")

    prepare_execution = subparsers.add_parser("prepare-execution")
    prepare_execution.add_argument("--wall-time-seconds", type=int)
    prepare_execution.add_argument("--source-run-id", type=RunId, required=True)
    prepare_execution.add_argument("--protocol", dest="protocol_id", required=True)
    prepare_execution.add_argument("--run-id", type=RunId)
    prepare_execution.add_argument("--external-opt-result-id", type=WorkflowRecordId)
    prepare_execution.add_argument("--backend", choices=("fake", "local_orca"), default="fake")
    prepare_execution.add_argument("--orca-executable", type=Path)
    prepare_execution.add_argument("--orca-version")
    prepare_execution.add_argument("--command-id", type=_command_id)
    prepare_execution.add_argument("--save-request")
    prepare_execution.add_argument("--json", action="store_true")

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--workflow", choices=("p5",), default="p5")
    reconcile.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    reconcile.add_argument("--expected-revision", type=int)
    reconcile.add_argument("--command-id", type=_command_id)
    reconcile.add_argument("--save-request")
    reconcile.add_argument("--json", action="store_true")

    export_execution = subparsers.add_parser("export-execution")
    export_execution.add_argument("--run-id", "--run", dest="run_id", type=RunId, required=True)
    export_execution.add_argument("--format", choices=("md", "json"), default="json")
    export_execution.add_argument("--output", type=Path, required=True)
    export_execution.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--workflow", choices=("p5",), required=True)
    doctor_parser.add_argument("--probe", action="store_true")
    doctor_parser.add_argument("--orca-executable", type=Path)
    doctor_parser.add_argument("--json", action="store_true")

    # P7 conversation/task surface.  These names intentionally do not reuse
    # the legacy run-oriented commands above.
    agent_chat = subparsers.add_parser("agent-chat")
    chat_target = agent_chat.add_mutually_exclusive_group(required=True)
    chat_target.add_argument("--new-conversation", action="store_true")
    chat_target.add_argument("--conversation")
    agent_chat.add_argument(
        "--planner", choices=("baseline", "fake", "deepseek_chat"), required=True
    )
    agent_chat.add_argument("--allow-llm", action="store_true")
    agent_chat.add_argument("--fallback", choices=("none", "baseline"), default="none")
    agent_chat.add_argument("--text")
    agent_chat.add_argument("--json", action="store_true")

    agent_message = subparsers.add_parser("agent-message")
    agent_message.add_argument("--conversation", required=True)
    agent_message.add_argument("--text", required=True)
    agent_message.add_argument("--save-request", type=Path, required=True)
    agent_message.add_argument(
        "--planner", choices=("baseline", "fake", "deepseek_chat"), default="baseline"
    )
    agent_message.add_argument("--allow-llm", action="store_true")
    agent_message.add_argument("--fallback", choices=("none", "baseline"), default="none")
    agent_message.add_argument("--json", action="store_true")

    agent_work = subparsers.add_parser("agent-work")
    agent_work.add_argument("--conversation", required=True)
    agent_work.add_argument("--max-effects", type=int, default=16)
    agent_work.add_argument("--max-seconds", type=float, default=30.0)
    agent_work.add_argument("--allow-llm", action="store_true")
    agent_work.add_argument("--allow-real-orca", action="store_true")
    agent_work.add_argument("--backend", choices=("fake", "local_orca"), default="fake")
    agent_work.add_argument(
        "--planner", choices=("baseline", "fake", "deepseek_chat"), default="baseline"
    )
    agent_work.add_argument("--fallback", choices=("none", "baseline"), default="none")
    agent_work.add_argument("--watch", action="store_true")
    agent_work.add_argument("--json", action="store_true")

    agent_query = subparsers.add_parser("agent-query")
    agent_query.add_argument("--conversation", required=True)
    agent_query.add_argument("--task")
    agent_query.add_argument("--request-json", type=Path, required=True)
    agent_query.add_argument("--json", action="store_true")

    agent_action = subparsers.add_parser("agent-action")
    agent_action.add_argument("--conversation", required=True)
    agent_action.add_argument("--token", required=True)
    agent_action.add_argument("--decision", choices=("accept", "reject"), required=True)
    agent_action.add_argument("--save-request", type=Path, required=True)
    agent_action.add_argument("--json", action="store_true")

    agent_cancel = subparsers.add_parser("agent-cancel-task")
    agent_cancel.add_argument("--conversation", required=True)
    agent_cancel.add_argument("--task", required=True)
    agent_cancel.add_argument("--expected-revision", type=int, required=True)
    agent_cancel.add_argument("--json", action="store_true")

    agent_interrupt = subparsers.add_parser("agent-interrupt-turn")
    agent_interrupt.add_argument("--conversation", required=True)
    agent_interrupt.add_argument("--turn", required=True)
    agent_interrupt.add_argument("--json", action="store_true")

    agent_link = subparsers.add_parser("agent-link-result")
    agent_link.add_argument("--conversation", required=True)
    agent_link.add_argument("--workflow", choices=("p5", "p6"), required=True)
    agent_link.add_argument("--run", required=True)
    agent_link.add_argument("--json", action="store_true")

    agent_export = subparsers.add_parser("agent-export")
    agent_export.add_argument("--conversation", required=True)
    agent_export.add_argument("--task")
    agent_export.add_argument("--format", choices=("md", "json"), default="md")
    agent_export.add_argument("--output", type=Path, required=True)
    agent_export.add_argument("--json", action="store_true")

    agent_verify = subparsers.add_parser("agent-verify")
    agent_verify.add_argument("--conversation", required=True)
    agent_verify.add_argument("--task")
    agent_verify.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.operation.startswith("agent-"):
            return _handle_p7_cli(args)
        if args.operation == "doctor":
            from orca_agent.execution.orca_config import doctor

            result = doctor(
                args.state_root,
                executable=args.orca_executable,
                probe=args.probe,
            )
            return _emit(result, bool(result.get("ready")), args.json)
        if args.operation == "assess":
            service = _p6_service(args.state_root)
            command = AssessP6Run.create(
                source_p5_run_id=args.source_run_id,
                profile_id=args.profile_id,
                expected_source_revision=args.expected_source_revision,
                reference_assessment_id=args.reference_assessment_id,
                run_id=args.run_id,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.assess(command)
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "prepare-execution":
            service = _p5_service(
                args.state_root,
                backend_kind=args.backend,
                orca_executable=args.orca_executable,
                orca_version=args.orca_version,
            )
            command = PrepareExecution.create(
                source_run_id=args.source_run_id,
                protocol_id=args.protocol_id,
                run_id=args.run_id,
                external_opt_result_id=args.external_opt_result_id,
                wall_time_seconds=args.wall_time_seconds,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.prepare_execution(
                source_run_id=command.source_run_id,
                protocol_id=command.protocol_id,
                run_id=command.run_id,
                command_id=command.command_id,
                external_opt_result_id=command.external_opt_result_id,
                wall_time_seconds=command.wall_time_seconds,
            )
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "approve" and args.workflow == "p5":
            service = _p5_service(args.state_root)
            if args.binding_hash is None:
                raise ValueError("P5 approval requires --binding-hash")
            command = ApproveP5Action.create(
                run_id=args.run_id,
                conversation_id=args.conversation_id,
                action_id=args.action_id,
                action_hash=args.action_hash,
                binding_hash=args.binding_hash,
                envelope_hash=args.envelope_hash,
                budget_hash=args.budget_hash,
                expected_revision=args.expected_revision,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.approve(
                run_id=command.run_id,
                conversation_id=command.conversation_id,
                action_id=command.action_id,
                action_hash=command.action_hash,
                binding_hash=command.binding_hash,
                envelope_hash=command.envelope_hash,
                budget_hash=command.budget_hash,
                expected_revision=command.expected_revision,
                command_id=command.command_id,
            )
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "approve" and args.workflow != "p5":
            if args.interrupt_id is None:
                raise ValueError("P3 approval requires --interrupt-id")
        if args.operation == "inspect" and args.workflow == "p5":
            return _emit(
                _p5_service(args.state_root).inspect(args.run_id).model_dump(mode="json"),
                True,
                args.json,
            )
        if args.operation == "worker" and args.workflow == "p5":
            service = _p5_service(
                args.state_root,
                backend_kind=args.backend,
                orca_executable=args.orca_executable,
                orca_version=args.orca_version,
                allow_real_orca=args.allow_real_orca,
            )
            worker = service.create_worker(allow_real_orca=args.allow_real_orca)
            reports = []
            while True:
                batch = worker.run_once(run_id=args.run_id, limit=max(args.limit, 1))
                reports.extend(asdict(item) for item in batch)
                if not args.drain or not batch:
                    break
            return _emit({"workflow": "p5", "reports": reports}, True, args.json)
        if args.operation == "worker" and args.workflow == "p6":
            if args.allow_network or args.allow_real_orca:
                raise ValueError("P6 worker is offline and rejects capability elevation flags")
            service = _p6_service(args.state_root)
            worker = service.create_worker()
            reports = []
            while True:
                batch = worker.run_once(run_id=args.run_id, limit=max(args.limit, 1))
                reports.extend(asdict(item) for item in batch)
                if not args.drain or not batch:
                    break
            return _emit({"workflow": "p6", "reports": reports}, True, args.json)
        if args.operation == "cancel" and args.workflow == "p5":
            service = _p5_service(args.state_root)
            command = CancelP5Execution.create(
                run_id=args.run_id,
                conversation_id=args.conversation_id,
                expected_revision=args.expected_revision,
                reason_code=args.reason_code,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.cancel(
                run_id=command.run_id,
                conversation_id=command.conversation_id,
                expected_revision=command.expected_revision,
                reason_code=command.reason_code,
                command_id=command.command_id,
            )
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "cancel" and args.workflow == "p6":
            service = _p6_service(args.state_root)
            command = CancelP6Run.create(
                run_id=args.run_id,
                conversation_id=args.conversation_id,
                expected_revision=args.expected_revision,
                reason_code=args.reason_code,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.cancel(command)
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "reconcile":
            service = _p5_service(args.state_root)
            if args.expected_revision is not None:
                current = service.inspect(args.run_id)
                if current.revision != args.expected_revision:
                    raise ValueError("P5 reconcile expected revision is stale")
            command = ReconcileP5Execution.create(
                run_id=args.run_id,
                expected_revision=args.expected_revision or service.inspect(args.run_id).revision,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.reconcile(
                args.run_id,
                command_id=command.command_id,
                expected_revision=command.expected_revision,
            )
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "export-execution":
            service = _p5_service(args.state_root)
            content = service.export_execution(args.run_id, format=args.format)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.format == "json":
                args.output.write_text(
                    json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            else:
                if not isinstance(content, str):
                    raise ValueError("P5 Markdown export is not text")
                args.output.write_text(content, encoding="utf-8")
            return _emit(
                {"valid": True, "path": str(args.output), "format": args.format},
                True,
                args.json,
            )
        if args.operation == "prepare":
            service = _p4_service(args.state_root)
            input_kind, raw_input = _prepare_input(args)
            command = StartPlanningRun.create(
                input_kind=input_kind,
                raw_input=raw_input,
                charge=args.charge,
                multiplicity=args.multiplicity,
                provider=args.provider,
                protocol_id=args.protocol_id,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
                new_conversation=args.new_conversation,
            )
            _save_request(command, args.save_request)
            result = service.start(command)
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "confirm-identity":
            service = _p4_service(args.state_root)
            view = service.inspect(args.run_id)
            if view.candidate_bundle is None:
                raise ValueError("P4 candidate bundle is unavailable")
            command = ConfirmMoleculeIdentity.create(
                run_id=args.run_id,
                conversation_id=args.conversation_id,
                interrupt_id=args.interrupt_id,
                expected_revision=args.expected_revision,
                query_id=args.query_id or view.query.query_id,
                query_hash=args.query_hash,
                candidate_bundle_id=args.candidate_bundle_id or view.candidate_bundle.record_id,
                candidate_bundle_hash=(
                    args.candidate_bundle_hash or view.candidate_bundle.bundle_hash
                ),
                candidate_set_hash=args.candidate_set_hash,
                candidate_id=args.candidate_id,
                candidate_hash=args.candidate_hash,
                decision=args.decision,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.confirm(command)
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "export-plan":
            service = _p4_service(args.state_root)
            content = service.export_plan(args.run_id, format=args.format)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.format == "json":
                args.output.write_text(
                    json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            else:
                if not isinstance(content, str):
                    raise ValueError("P4 Markdown export is not text")
                args.output.write_text(content, encoding="utf-8")
            return _emit(
                {"valid": True, "path": str(args.output), "format": args.format},
                True,
                args.json,
            )
        if args.operation == "worker" and args.workflow == "p4":
            service = _p4_service(args.state_root, allow_network=args.allow_network)
            worker = service.create_worker()
            reports = []
            while True:
                batch = worker.run_once(limit=max(args.limit, 1))
                reports.extend(asdict(item) for item in batch)
                if not args.drain or not batch:
                    break
            return _emit({"workflow": "p4", "reports": reports}, True, args.json)
        if args.operation == "inspect":
            workflow = args.workflow or _detect_workflow(args.state_root, args.run_id)
            if workflow == "p4":
                return _emit(
                    _p4_service(args.state_root).inspect(args.run_id).model_dump(mode="json"),
                    True,
                    args.json,
                )
            if workflow == "p5":
                return _emit(
                    _p5_service(args.state_root).inspect(args.run_id).model_dump(mode="json"),
                    True,
                    args.json,
                )
            if workflow == "p6":
                return _emit(
                    _p6_service(args.state_root).inspect(args.run_id).model_dump(mode="json"),
                    True,
                    args.json,
                )
        if args.operation == "cancel":
            workflow = args.workflow or _detect_workflow(args.state_root, args.run_id)
            if workflow == "p4":
                service = _p4_service(args.state_root)
                command = CancelPlanningRun.create(
                    run_id=args.run_id,
                    conversation_id=args.conversation_id,
                    expected_revision=args.expected_revision,
                    reason_code=args.reason_code,
                    command_id=args.command_id,
                    requested_at_utc=service.clock.now_utc(),
                )
                _save_request(command, args.save_request)
                result = service.cancel(command)
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if workflow == "p5":
                service = _p5_service(args.state_root)
                command = CancelP5Execution.create(
                    run_id=args.run_id,
                    conversation_id=args.conversation_id,
                    expected_revision=args.expected_revision,
                    reason_code=args.reason_code,
                    command_id=args.command_id,
                    requested_at_utc=service.clock.now_utc(),
                )
                _save_request(command, args.save_request)
                result = service.cancel(
                    run_id=command.run_id,
                    conversation_id=command.conversation_id,
                    expected_revision=command.expected_revision,
                    reason_code=command.reason_code,
                    command_id=command.command_id,
                )
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if workflow == "p6":
                service = _p6_service(args.state_root)
                command = CancelP6Run.create(
                    run_id=args.run_id,
                    conversation_id=args.conversation_id,
                    expected_revision=args.expected_revision,
                    reason_code=args.reason_code,
                    command_id=args.command_id,
                    requested_at_utc=service.clock.now_utc(),
                )
                _save_request(command, args.save_request)
                result = service.cancel(command)
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "replay-request":
            command = _load_request(args.file)
            if isinstance(command, (AssessP6Run, CancelP6Run)):
                service = _p6_service(args.state_root)
                result = (
                    service.assess(command)
                    if isinstance(command, AssessP6Run)
                    else service.cancel(command)
                )
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if isinstance(command, (StartPlanningRun, ConfirmMoleculeIdentity, CancelPlanningRun)):
                service = _p4_service(args.state_root)
                if isinstance(command, StartPlanningRun):
                    result = service.start(command)
                elif isinstance(command, ConfirmMoleculeIdentity):
                    result = service.confirm(command)
                else:
                    result = service.cancel(command)
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if isinstance(command, PrepareExecution):
                service = _p5_service(args.state_root)
                result = service.prepare_execution(
                    source_run_id=command.source_run_id,
                    protocol_id=command.protocol_id,
                    run_id=command.run_id,
                    command_id=command.command_id,
                    external_opt_result_id=command.external_opt_result_id,
                    wall_time_seconds=command.wall_time_seconds,
                )
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if isinstance(command, ApproveP5Action):
                service = _p5_service(args.state_root)
                result = service.approve(
                    run_id=command.run_id,
                    conversation_id=command.conversation_id,
                    action_id=command.action_id,
                    action_hash=command.action_hash,
                    binding_hash=command.binding_hash,
                    envelope_hash=command.envelope_hash,
                    budget_hash=command.budget_hash,
                    expected_revision=command.expected_revision,
                    command_id=command.command_id,
                )
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if isinstance(command, CancelP5Execution):
                service = _p5_service(args.state_root)
                result = service.cancel(
                    run_id=command.run_id,
                    conversation_id=command.conversation_id,
                    expected_revision=command.expected_revision,
                    reason_code=command.reason_code,
                    command_id=command.command_id,
                )
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)
            if isinstance(command, ReconcileP5Execution):
                service = _p5_service(args.state_root)
                result = service.reconcile(
                    command.run_id,
                    command_id=command.command_id,
                    expected_revision=command.expected_revision,
                )
                return _emit(result.model_dump(mode="json"), result.accepted, args.json)

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
            command = CancelWaterRun.create(
                run_id=args.run_id,
                conversation_id=args.conversation_id,
                expected_revision=args.expected_revision,
                reason_code=args.reason_code,
                command_id=args.command_id,
                requested_at_utc=service.clock.now_utc(),
            )
            _save_request(command, args.save_request)
            result = service.cancel(command)
            return _emit(result.model_dump(mode="json"), result.accepted, args.json)
        if args.operation == "report":
            workflow = _detect_workflow(args.state_root, args.run_id)
            if workflow == "p6":
                output = _export_p6_report(
                    _p6_service(args.state_root), args.run_id, args.format, args.output
                )
                return _emit(output, True, args.json)
            output = _export_report(service, args.run_id, args.format, args.output)
            return _emit(output, True, args.json)
        if args.operation == "verify-report":
            workflow = _detect_workflow(args.state_root, args.run_id)
            if workflow == "p6":
                service = _p6_service(args.state_root)
                output = P6ReportRenderer(
                    service.database_path, service.state_root, clock=service.clock
                ).verify(args.run_id)
                if args.report is not None:
                    output["exported_report"] = _verify_p6_report(service, args.run_id, args.report)
                    output["valid"] = bool(output.get("valid")) and bool(
                        output["exported_report"]["valid"]
                    )
                if not output.get("valid"):
                    output["code"] = "report_verification_failed"
                return _emit(output, bool(output.get("valid")), args.json)
            try:
                output = P3ReportRenderer(
                    service.database_path,
                    service.state_root,
                    clock=service.clock,
                ).verify(args.run_id)
                if args.report is not None:
                    output["exported_report"] = _verify_report(service, args.run_id, args.report)
                    output["valid"] = output["valid"] and output["exported_report"]["valid"]
            except (StateIntegrityError, ValueError, OSError):
                output = {"valid": False, "code": "report_verification_failed"}
            if not output["valid"]:
                output["code"] = "report_verification_failed"
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


def _handle_p7_cli(args) -> int:
    """Dispatch the explicit P7 CLI without changing legacy run commands."""

    from orca_agent.bootstrap.p7_modules import build_p7_runtime
    from orca_agent.domain.ids import ConversationId, RunId
    from orca_agent.llm.ports import strict_json_loads
    from orca_agent.orchestration.p7_commands import (
        ActionCommand,
        ActionDecision,
        MessageCommand,
    )

    planner_name = getattr(args, "planner", "baseline")
    allow_llm = bool(getattr(args, "allow_llm", False))
    fallback = getattr(args, "fallback", "none")
    backend_kind = getattr(args, "backend", "fake")
    allow_real_orca = bool(getattr(args, "allow_real_orca", False))
    runtime = build_p7_runtime(
        args.state_root,
        planner_name=planner_name,
        allow_llm=allow_llm,
        fallback=fallback,
        backend_kind=backend_kind,
        allow_real_orca=allow_real_orca,
    )

    if args.operation == "agent-chat":
        if args.new_conversation:
            state = runtime.conversation.new_conversation()
            conversation_id = str(state["conversation_id"])
        else:
            conversation_id = str(ConversationId(args.conversation))
            state = runtime.conversation.get_state(conversation_id).model_dump(mode="json")
        if args.text is not None:
            response = runtime.conversation.message(conversation_id, args.text)
            return _emit(
                {"conversation_id": conversation_id, "conversation": state, "response": response},
                bool(response.get("accepted")),
                args.json,
            )
        _emit(state, True, args.json)
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            if text.casefold() in {"exit", "quit", "退出"}:
                break
            response = runtime.conversation.message(conversation_id, text)
            code = _emit(response, bool(response.get("accepted")), args.json)
            if code != 0:
                return code
        return 0

    if args.operation == "agent-message":
        conversation_id = ConversationId(args.conversation)
        command = MessageCommand.create(
            conversation_id=conversation_id,
            text=args.text,
            requested_at_utc=runtime.conversation.clock.now_utc(),
        )
        _save_request(command, args.save_request)
        response = runtime.conversation.message(conversation_id, command.text)
        return _emit(
            {
                "command_id": str(command.command_id),
                "request": command.model_dump(mode="json"),
                "response": response,
            },
            bool(response.get("accepted")),
            args.json,
        )

    if args.operation == "agent-work":
        conversation_id = ConversationId(args.conversation)
        if not args.watch:
            result = runtime.task.progress(
                conversation_id,
                max_effects=args.max_effects,
                max_seconds=args.max_seconds,
                allow_real_orca=args.allow_real_orca,
            )
            return _emit(result, True, args.json)
        started = time.monotonic()
        effects = 0
        batches: list[dict[str, object]] = []
        while time.monotonic() - started < args.max_seconds:
            remaining = args.max_seconds - (time.monotonic() - started)
            result = runtime.task.progress(
                conversation_id,
                max_effects=max(args.max_effects - effects, 1),
                max_seconds=min(1.0, max(remaining, 0.1)),
                allow_real_orca=args.allow_real_orca,
            )
            effects += int(result.get("effects", 0))
            batches.append(result)
            tasks = result.get("tasks", [])
            if not isinstance(tasks, list) or all(
                isinstance(item, dict)
                and item.get("task", {}).get("state")
                in {"result_ready", "ended_without_result", "reconciliation_required"}
                for item in tasks
            ):
                break
            if result.get("effects", 0) == 0:
                break
        return _emit(
            {"conversation_id": str(conversation_id), "effects": effects, "batches": batches},
            True,
            args.json,
        )

    if args.operation == "agent-query":
        request = strict_json_loads(args.request_json.read_bytes())
        if not isinstance(request, dict):
            raise ValueError("query request JSON must be an object")
        result = runtime.query.query(
            ConversationId(args.conversation), task_id=args.task, request=request
        )
        return _emit(result, True, args.json)

    if args.operation == "agent-action":
        conversation_id = ConversationId(args.conversation)
        command = ActionCommand.create(
            conversation_id=conversation_id,
            token=args.token,
            decision=ActionDecision(args.decision),
            requested_at_utc=runtime.conversation.clock.now_utc(),
        )
        _save_request(command, args.save_request)
        result = runtime.task.accept_action(
            conversation_id,
            command.token,
            decision=command.decision.value,
        )
        return _emit(
            {
                "command_id": str(command.command_id),
                "request": command.model_dump(mode="json"),
                "response": result,
            },
            True,
            args.json,
        )

    if args.operation == "agent-cancel-task":
        result = runtime.task.cancel_task(
            ConversationId(args.conversation),
            args.task,
            expected_revision=args.expected_revision,
        )
        return _emit(result, True, args.json)

    if args.operation == "agent-interrupt-turn":
        result = runtime.conversation.interrupt_turn(ConversationId(args.conversation), args.turn)
        return _emit(result, True, args.json)

    if args.operation == "agent-link-result":
        result = runtime.query.link_existing_result(
            ConversationId(args.conversation),
            workflow=args.workflow,
            run_id=RunId(args.run),
        )
        return _emit(result, True, args.json)

    if args.operation == "agent-export":
        value = runtime.query.export(
            ConversationId(args.conversation), task_id=args.task, format=args.format
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "json":
            args.output.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            if not isinstance(value, str):
                raise ValueError("P7 Markdown export is not text")
            args.output.write_text(value, encoding="utf-8")
        return _emit(
            {"valid": True, "path": str(args.output), "format": args.format}, True, args.json
        )

    if args.operation == "agent-verify":
        result = runtime.query.verify(ConversationId(args.conversation), task_id=args.task)
        return _emit(result, bool(result.get("valid")), args.json)

    raise ValueError("unsupported P7 operation")


def _p4_service(state_root: str | Path, *, allow_network: bool = False):
    return P4ApplicationService(
        state_root,
        allow_network=allow_network,
        fake_adapter=FakePubChemAdapter(),
    )


def _p5_service(
    state_root: str | Path,
    *,
    backend_kind: str = "fake",
    orca_executable: Path | None = None,
    orca_version: str | None = None,
    allow_real_orca: bool = False,
) -> P5ApplicationService:
    return P5ApplicationService(
        state_root,
        backend_kind=backend_kind,
        orca_executable=orca_executable,
        orca_version=orca_version,
        allow_real_orca=allow_real_orca,
    )


def _p6_service(state_root: str | Path) -> P6ApplicationService:
    return P6ApplicationService(state_root)


def _prepare_input(args) -> tuple[MoleculeInputKind, str]:
    values = (
        (MoleculeInputKind.NAME, args.name),
        (MoleculeInputKind.CAS, args.cas),
        (MoleculeInputKind.CID, args.cid),
        (MoleculeInputKind.SMILES, args.smiles),
    )
    selected = tuple(item for item in values if item[1] is not None)
    if len(selected) != 1:
        raise ValueError("exactly one P4 molecule input is required")
    return selected[0]


def _detect_workflow(state_root: str | Path, run_id: RunId) -> str:
    database_path = resolve_database_path(state_root)
    if not database_path.exists():
        raise ValueError("state database does not exist")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT schema_version, engine_version FROM runs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
    if row is None:
        raise ValueError("run was not found")
    try:
        schema_version = int(row[0])
    except (TypeError, ValueError):
        raise ValueError("run schema version is invalid") from None
    engine_version = str(row[1])
    if schema_version == P4_SCHEMA_VERSION and engine_version == P4_ENGINE_VERSION:
        return "p4"
    if schema_version == P5_SCHEMA_VERSION and engine_version == P5_ENGINE_VERSION:
        return "p5"
    if schema_version == P6_SCHEMA_VERSION and engine_version == P6_ENGINE_VERSION:
        return "p6"
    if schema_version == P3_SCHEMA_VERSION and engine_version == P3_ENGINE_VERSION:
        return "p3"
    raise ValueError("run workflow version is unsupported")


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
    p4_command_models = {
        "p4.prepare": StartPlanningRun,
        "p4.confirm_identity": ConfirmMoleculeIdentity,
        "p4.cancel": CancelPlanningRun,
    }
    p5_command_models = {
        "p5.create_execution": PrepareExecution,
        "p5.approve_action": ApproveP5Action,
        "p5.request_cancel": CancelP5Execution,
        "p5.reconcile_execution": ReconcileP5Execution,
    }
    p6_command_models = {
        "p6.assess": AssessP6Run,
        "p6.cancel": CancelP6Run,
    }
    if (
        payload.get("schema_version") == P4_SCHEMA_VERSION
        and payload.get("engine_version") == P4_ENGINE_VERSION
    ):
        command_type = payload.get("command_type")
        model_type = p4_command_models.get(command_type)
        if model_type is None:
            raise ValueError("unsupported P4 command type")
        return model_type.model_validate_json(raw, strict=True)
    if (
        payload.get("schema_version") == P5_SCHEMA_VERSION
        and payload.get("engine_version") == P5_ENGINE_VERSION
    ):
        command_type = payload.get("command_type")
        model_type = p5_command_models.get(command_type)
        if model_type is None:
            raise ValueError("unsupported P5 command type")
        return model_type.model_validate_json(raw, strict=True)
    if (
        payload.get("schema_version") == P6_SCHEMA_VERSION
        and payload.get("engine_version") == P6_ENGINE_VERSION
    ):
        command_type = payload.get("command_type")
        model_type = p6_command_models.get(command_type)
        if model_type is None:
            raise ValueError("unsupported P6 command type")
        return model_type.model_validate_json(raw, strict=True)
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


def _verify_report(service, run_id, path: Path):
    if path.suffix.lower() not in (".md", ".json"):
        return {"valid": False, "path": str(path)}
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        entry = P3RecordRepository(uow.connection).latest(
            run_id=run_id,
            record_type="report_manifest",
            model_type=ReportManifestV1,
        )
        if entry is None:
            raise ValueError("report manifest missing")
        manifest = entry[1]
        artifact_id = (
            manifest.markdown_artifact_id
            if path.suffix.lower() == ".md"
            else manifest.json_artifact_id
        )
        artifact = ArtifactRecordRepository(uow.connection).get(artifact_id)
        if artifact is None or artifact.run_id != run_id:
            raise ValueError("report artifact owner differs")
        expected = ArtifactStore(service.state_root).read(artifact)
        valid = path.read_bytes() == expected
        uow.commit()
    return {"valid": valid, "path": str(path)}


def _export_p6_report(service: P6ApplicationService, run_id: RunId, format_name: str, output: Path):
    view = service.inspect(run_id)
    if view.state.phase.value != "completed" or view.report_manifest is None:
        raise ValueError("P6 report is not available until the workflow is completed")
    verification = P6ReportRenderer(
        service.database_path, service.state_root, clock=service.clock
    ).verify(run_id)
    if not verification.get("valid"):
        raise ValueError("P6 report verification failed")
    manifest = view.report_manifest
    artifact_id = (
        manifest.markdown_artifact_id if format_name == "md" else manifest.json_artifact_id
    )
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        artifact = ArtifactRecordRepository(uow.connection).get(artifact_id)
        if artifact is None or artifact.run_id != run_id:
            raise ValueError("P6 report artifact is missing or has the wrong owner")
        content = ArtifactStore(service.state_root).read(artifact)
        uow.commit()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return {"valid": True, "path": str(output), "artifact_id": str(artifact_id)}


def _verify_p6_report(service: P6ApplicationService, run_id: RunId, path: Path):
    if path.suffix.lower() not in (".md", ".json"):
        return {"valid": False, "path": str(path)}
    view = service.inspect(run_id)
    manifest = view.report_manifest
    if manifest is None:
        raise ValueError("P6 report manifest is missing")
    artifact_id = (
        manifest.markdown_artifact_id if path.suffix.lower() == ".md" else manifest.json_artifact_id
    )
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        artifact = ArtifactRecordRepository(uow.connection).get(artifact_id)
        if artifact is None or artifact.run_id != run_id:
            raise ValueError("P6 report artifact owner differs")
        expected = ArtifactStore(service.state_root).read(artifact)
        valid = path.read_bytes() == expected
        uow.commit()
    return {"valid": valid, "path": str(path)}


def _emit(value: object, accepted: bool, json_requested: bool) -> int:
    del json_requested
    try:
        print(json.dumps(value, ensure_ascii=False, default=str))
    except UnicodeEncodeError:
        # Windows consoles may still expose a legacy code page.  Preserve the
        # JSON contract by falling back to escaped Unicode rather than failing
        # an otherwise valid P6 inspection.
        print(json.dumps(value, ensure_ascii=True, default=str))
    return 0 if accepted else 2


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
