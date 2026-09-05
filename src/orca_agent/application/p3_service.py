"""Application service for the schema-2 P3 Water fake vertical slice."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

from pydantic import field_validator

from orca_agent.application.errors import (
    ApplicationError,
    DuplicateCommandConflictError,
    EffectInFlightError,
    InterruptExpiredError,
    InvalidTransitionError,
    RevisionConflictError,
    StateIntegrityError,
    StorageError,
)
from orca_agent.domain.errors import DomainError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ApprovalGrantId,
    ArtifactId,
    CommandId,
    ConversationId,
    EffectId,
    EventId,
    ExecutionId,
    InterruptId,
    PlanProposalId,
    PrimitiveId,
    ProblemSpecId,
    RunId,
    is_new_external_command_id,
    new_id,
)
from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object
from orca_agent.domain.models import ValidatedAction, ValidatedClaim
from orca_agent.domain.p3 import (
    ApprovalGrantV1,
    ExecutionIntent,
    FixtureScientificAssessment,
    LedgerState,
    P3Model,
    P3RunStatus,
    P3WorkflowState,
    ReportManifestV1,
    WorkflowPhase,
)
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.p3_records import (
    ActionRepository,
    ArtifactRecordRepository,
    EvidenceRepository,
    JobRepository,
    P3RecordRepository,
)
from orca_agent.infrastructure.repositories import RunSnapshot
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CommandType, RequestInterrupt
from orca_agent.orchestration.dispatch_policy import P3_EFFECT_REGISTRY
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p3_kernel import (
    P3KernelEvent,
    P3Transition,
    expected_p3_application_result,
    reduce_p3_event,
)
from orca_agent.orchestration.p3_versions import P3_ENGINE_VERSION, P3_SCHEMA_VERSION
from orca_agent.orchestration.replay import state_hash
from orca_agent.orchestration.state import RunStatus
from orca_agent.planning.water import build_water_plan

from ..evidence.pipeline import P3EvidencePipeline
from ..execution.commands import ApproveAction, CancelWaterRun, StartWaterRun
from ..execution.fake_backend import FakeBackend
from ..execution.gateway import FakeExecutionGateway, build_idempotency_key
from ..reporting.renderer import P3ReportRenderer
from .effect_completion import EffectCompletionService

_P3_OBJECT_NAMESPACE = uuid.UUID("f7711d1e-c3c8-4ac4-9f19-2a8af0ee12be")


class P3Result(P3Model):
    accepted: bool
    code: str
    run_id: RunId
    revision: int
    phase: WorkflowPhase
    conversation_id: ConversationId
    action_id: ActionId | None
    approval_interrupt_id: InterruptId | None
    dispatch_effect_id: EffectId | None
    details: FrozenJsonObject

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)


class P3RunView(P3Model):
    run_id: RunId
    conversation_id: ConversationId
    revision: int
    kernel_status: RunStatus
    state: P3WorkflowState
    action: ValidatedAction
    ledger_state: LedgerState
    outbox: tuple[dict[str, object], ...]
    diagnostics: tuple[str, ...] = ()


class P3ApplicationService:
    """Public P3 commands backed by the shared transactional kernel."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        backend: FakeBackend | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.state_root = Path(state_root)
        self.database_path = resolve_database_path(self.state_root)
        self.clock = clock or SystemClock()
        self.backend = backend or FakeBackend(self.state_root, clock=self.clock)
        self.max_attempts = max_attempts

    def start(self, command: StartWaterRun | None = None) -> P3Result:
        command = command or StartWaterRun.create(requested_at_utc=self.clock.now_utc())
        try:
            if not is_new_external_command_id(command.command_id):
                raise InvalidTransitionError("new P3 command ID must be UUID4")
            if not command.new_conversation:
                raise InvalidTransitionError("P3 start requires a new conversation")
            plan = build_water_plan(
                artifact_namespace_id=_deterministic_workflow_id(
                    ArtifactId, command.run_id, "artifact_namespace"
                ),
                problem_spec_id=_deterministic_workflow_id(
                    ProblemSpecId, command.run_id, "problem_spec"
                ),
                proposal_id=_deterministic_workflow_id(PlanProposalId, command.run_id, "proposal"),
                primitive_id=_deterministic_workflow_id(PrimitiveId, command.run_id, "primitive"),
                action_id=_deterministic_workflow_id(ActionId, command.run_id, "action"),
            )
            approval_interrupt = _deterministic_workflow_id(
                InterruptId, command.run_id, "approval_interrupt"
            )
            now = self.clock.now_utc()
            initial = P3WorkflowState(
                run_id=command.run_id,
                schema_version=P3_SCHEMA_VERSION,
                engine_version=P3_ENGINE_VERSION,
                status=P3RunStatus.CREATED,
                pending_interrupt_id=None,
                last_outcome_code="run_created",
                cancel_reason_code=None,
                phase=WorkflowPhase.AWAITING_APPROVAL,
                conversation_id=command.conversation_id,
                problem_spec_id=plan.problem.record_id,
                proposal_id=plan.proposal.proposal_id,
                action_id=plan.action.action_id,
                action_hash=plan.action.action_hash,
                envelope_hash=sha256_hex(plan.action.execution_envelope),
                budget_hash=sha256_hex(plan.action.budget),
                approval_interrupt_id=approval_interrupt,
                approval_grant_id=None,
                dispatch_effect_id=None,
                assessment_effect_id=None,
                report_effect_id=None,
                execution_id=None,
                job_id=None,
                assessment_id=None,
                claim_id=None,
                report_manifest_id=None,
                accepted_artifact_ids=(),
                last_error_code=None,
            )
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                if any(
                    item is None
                    for item in (
                        uow.runs,
                        uow.events,
                        uow.interrupts,
                        uow.outbox,
                        uow.command_receipts,
                    )
                ):
                    raise StorageError("kernel repositories are unavailable")
                existing = uow.runs.get(command.run_id)
                if existing is not None:
                    receipt = uow.command_receipts.get(command.command_id)
                    if (
                        receipt is None
                        or receipt.command_hash != command.command_hash()
                        or not isinstance(existing.state, P3WorkflowState)
                    ):
                        raise DuplicateCommandConflictError("P3 start command is already bound")
                    verified = uow.runs.get_verified(
                        command.run_id,
                        uow.events,
                        interrupts=uow.interrupts,
                        outbox=uow.outbox,
                    )
                    if not isinstance(verified.state, P3WorkflowState):
                        raise StateIntegrityError("P3 command is bound to a non-P3 run")
                    uow.commit()
                    return self._result_from_state(
                        command.run_id,
                        command.conversation_id,
                        verified.revision,
                        verified.state,
                        True,
                    )

                created_event, created_transition, created_result = self._create_p3_run(
                    uow=uow,
                    command=command,
                    state=initial,
                    occurred_at_utc=now,
                )
                request = RequestInterrupt.create(
                    run_id=command.run_id,
                    expected_revision=created_result.revision,
                    interrupt_id=approval_interrupt,
                    kind="p3.action_approval",
                    payload={
                        "conversation_id": str(command.conversation_id),
                        "action_id": str(plan.action.action_id),
                        "action_hash": plan.action.action_hash,
                        "envelope_hash": sha256_hex(plan.action.execution_envelope),
                        "budget_hash": sha256_hex(plan.action.budget),
                        "fixture_id": plan.fixture.fixture_id,
                        "fixture_version": plan.fixture.fixture_version,
                    },
                    expires_at_utc=now + timedelta(hours=24),
                    command_id=new_id(CommandId),
                    requested_at_utc=now,
                )
                request_result, request_transition, request_event = self._persist_p3_event(
                    uow=uow,
                    current=RunSnapshot(
                        run_id=command.run_id,
                        schema_version=P3_SCHEMA_VERSION,
                        engine_version=P3_ENGINE_VERSION,
                        revision=created_result.revision,
                        state=created_transition.next_state,
                        state_hash=state_hash(created_transition.next_state),
                        last_event_id=created_event.event_id,
                        created_at_utc=now,
                        updated_at_utc=now,
                    ),
                    command_id=request.command_id,
                    command_type=CommandType.REQUEST_INTERRUPT,
                    command_hash=request.command_hash(),
                    event_type=EventType.INTERRUPT_REQUESTED,
                    payload=request.event_payload(),
                    occurred_at_utc=now,
                )
                actions = ActionRepository(uow.connection)
                idempotency_key = build_idempotency_key(
                    run_id=command.run_id,
                    action_id=plan.action.action_id,
                    action_hash=plan.action.action_hash,
                    envelope_hash=sha256_hex(plan.action.execution_envelope),
                    budget_hash=sha256_hex(plan.action.budget),
                )
                actions.insert(
                    run_id=command.run_id,
                    conversation_id=command.conversation_id,
                    action=plan.action,
                    idempotency_key=idempotency_key,
                    now=now,
                )
                records = P3RecordRepository(uow.connection)
                records.append_any(
                    run_id=command.run_id,
                    record_type="problem_spec",
                    record=plan.problem,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                    created_at_utc=now,
                    source_event_id=request_event.event_id,
                )
                records.append_any(
                    run_id=command.run_id,
                    record_type="plan_proposal",
                    record=plan.proposal,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                    created_at_utc=now,
                    source_event_id=request_event.event_id,
                )
                records.append_any(
                    run_id=command.run_id,
                    record_type="action_contract",
                    record=plan.action,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                    created_at_utc=now,
                    source_event_id=request_event.event_id,
                )
                records.append(
                    run_id=command.run_id,
                    record_type="workflow_state",
                    record=request_transition.next_state,
                    created_at_utc=now,
                    source_event_id=request_event.event_id,
                )
                uow.commit()
                return self._result_from_state(
                    command.run_id,
                    command.conversation_id,
                    request_result.revision,
                    request_transition.next_state,
                    True,
                )
        except Exception as error:
            return self._safe_reject(command.run_id, command.conversation_id, error)

    def approve(self, command: ApproveAction) -> P3Result:
        try:
            if not is_new_external_command_id(command.command_id):
                raise InvalidTransitionError("new P3 command ID must be UUID4")
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                if any(
                    item is None
                    for item in (
                        uow.runs,
                        uow.events,
                        uow.interrupts,
                        uow.outbox,
                        uow.command_receipts,
                    )
                ):
                    raise StorageError("kernel repositories are unavailable")
                existing_receipt = uow.command_receipts.get(command.command_id)
                if existing_receipt is not None:
                    if existing_receipt.command_hash != command.command_hash():
                        raise DuplicateCommandConflictError("approval command hash conflicts")
                    snapshot = uow.runs.get_verified(
                        command.run_id,
                        uow.events,
                        interrupts=uow.interrupts,
                        outbox=uow.outbox,
                    )
                    if not isinstance(snapshot.state, P3WorkflowState):
                        raise StateIntegrityError("approval is bound to a non-P3 run")
                    uow.commit()
                    return self._result_from_state(
                        command.run_id,
                        command.conversation_id,
                        snapshot.revision,
                        snapshot.state,
                        True,
                    )
                snapshot = uow.runs.get_verified(
                    command.run_id,
                    uow.events,
                    interrupts=uow.interrupts,
                    outbox=uow.outbox,
                )
                if not isinstance(snapshot.state, P3WorkflowState):
                    raise InvalidTransitionError("approval requires a schema-2 P3 run")
                state = snapshot.state
                if state.phase is not WorkflowPhase.AWAITING_APPROVAL:
                    raise InvalidTransitionError("P3 action is not awaiting approval")
                if state.conversation_id != command.conversation_id:
                    raise InvalidTransitionError("conversation does not own the workflow")
                if snapshot.revision != command.expected_revision:
                    raise RevisionConflictError("approval expected revision is stale")
                if state.pending_interrupt_id != command.interrupt_id:
                    raise InvalidTransitionError("approval interrupt does not match workflow")
                interrupt = uow.interrupts.get(command.interrupt_id)
                if interrupt is None or interrupt.status.value != "pending":
                    raise InvalidTransitionError("approval interrupt is not pending")
                now = self.clock.now_utc()
                if interrupt.expires_at_utc <= now:
                    raise InterruptExpiredError("approval interrupt has expired")
                action_record = ActionRepository(uow.connection).get(command.action_id)
                if action_record is None or action_record.run_id != command.run_id:
                    raise InvalidTransitionError("approval action is not owned by the run")
                if (
                    action_record.action.action_hash != command.action_hash
                    or sha256_hex(action_record.action.execution_envelope) != command.envelope_hash
                    or sha256_hex(action_record.action.budget) != command.budget_hash
                    or action_record.action.action_id != state.action_id
                ):
                    raise InvalidTransitionError("approval action binding is invalid")
                if any(
                    interrupt.payload.get(key) != expected
                    for key, expected in (
                        ("conversation_id", str(command.conversation_id)),
                        ("action_id", str(command.action_id)),
                        ("action_hash", command.action_hash),
                        ("envelope_hash", command.envelope_hash),
                        ("budget_hash", command.budget_hash),
                    )
                ):
                    raise StateIntegrityError("approval interrupt projection is inconsistent")
                approval = ApprovalGrantV1.create(
                    approval_grant_id=command.approval_grant_id
                    or _deterministic_approval_id(command.run_id, command.action_id),
                    run_id=command.run_id,
                    conversation_id=command.conversation_id,
                    interrupt_id=command.interrupt_id,
                    action=action_record.action,
                    source_revision=snapshot.revision,
                    approved_at_utc=now,
                    expires_at_utc=interrupt.expires_at_utc,
                )
                intent = ExecutionIntent.create(
                    run_id=command.run_id,
                    action_id=command.action_id,
                    approval_grant_id=approval.approval_grant_id,
                    idempotency_key=action_record.idempotency_key,
                    execution_id=_deterministic_workflow_id(
                        ExecutionId, command.run_id, "execution"
                    ),
                )
                effect = EffectSpec(
                    effect_index=0,
                    effect_type="external.p3.dispatch_fake",
                    effect_class=EffectClass.EXTERNAL,
                    payload={
                        "run_id": str(command.run_id),
                        "conversation_id": str(command.conversation_id),
                        "action_id": str(command.action_id),
                        "action_hash": command.action_hash,
                        "envelope_hash": command.envelope_hash,
                        "budget_hash": command.budget_hash,
                        "approval_grant_id": str(approval.approval_grant_id),
                        "execution_id": str(intent.execution_id),
                        "idempotency_key": intent.idempotency_key,
                        "fixture_id": "water_sp_v1",
                        "fixture_version": "1",
                        "workflow_schema_version": P3_SCHEMA_VERSION,
                        "workflow_engine_version": P3_ENGINE_VERSION,
                    },
                )
                payload = {
                    "interrupt_id": str(command.interrupt_id),
                    "response": {
                        "approved": True,
                        "action_id": str(command.action_id),
                        "action_hash": command.action_hash,
                        "envelope_hash": command.envelope_hash,
                        "budget_hash": command.budget_hash,
                        "approval_grant_id": str(approval.approval_grant_id),
                    },
                    "effects": [effect.model_dump(mode="json")],
                }
                result, transition, event = self._persist_p3_event(
                    uow=uow,
                    current=snapshot,
                    command_id=command.command_id,
                    command_type=CommandType.RESOLVE_INTERRUPT,
                    command_hash=command.command_hash(),
                    event_type=EventType.INTERRUPT_RESOLVED,
                    payload=payload,
                    occurred_at_utc=now,
                )
                records = P3RecordRepository(uow.connection)
                records.append(
                    run_id=command.run_id,
                    record_type="approval_grant",
                    record=approval,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                )
                records.append(
                    run_id=command.run_id,
                    record_type="execution_intent",
                    record=intent,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                )
                ActionRepository(uow.connection).update_ledger(
                    action_id=command.action_id,
                    state=LedgerState.APPROVED,
                    now=now,
                    approval_grant_id=approval.approval_grant_id,
                    execution_id=intent.execution_id,
                )
                records.append(
                    run_id=command.run_id,
                    record_type="workflow_state",
                    record=transition.next_state,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                )
                uow.commit()
                return self._result_from_state(
                    command.run_id,
                    command.conversation_id,
                    result.revision,
                    transition.next_state,
                    True,
                )
        except Exception as error:
            return self._safe_reject(command.run_id, command.conversation_id, error)

    def cancel(self, command: CancelWaterRun) -> P3Result:
        try:
            if not is_new_external_command_id(command.command_id):
                raise InvalidTransitionError("new P3 command ID must be UUID4")
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                if any(
                    item is None
                    for item in (
                        uow.runs,
                        uow.events,
                        uow.interrupts,
                        uow.outbox,
                        uow.command_receipts,
                    )
                ):
                    raise StorageError("kernel repositories are unavailable")
                existing = uow.command_receipts.get(command.command_id)
                if existing is not None:
                    if existing.command_hash != command.command_hash():
                        raise DuplicateCommandConflictError("cancel command hash conflicts")
                    snapshot = uow.runs.get_verified(
                        command.run_id,
                        uow.events,
                        interrupts=uow.interrupts,
                        outbox=uow.outbox,
                    )
                    if not isinstance(snapshot.state, P3WorkflowState):
                        raise StateIntegrityError("cancel is bound to a non-P3 run")
                    uow.commit()
                    return self._result_from_state(
                        command.run_id,
                        command.conversation_id,
                        snapshot.revision,
                        snapshot.state,
                        True,
                    )
                snapshot = uow.runs.get_verified(
                    command.run_id,
                    uow.events,
                    interrupts=uow.interrupts,
                    outbox=uow.outbox,
                )
                if not isinstance(snapshot.state, P3WorkflowState):
                    raise InvalidTransitionError("cancellation requires a schema-2 P3 run")
                if snapshot.revision != command.expected_revision:
                    raise RevisionConflictError("cancel expected revision is stale")
                if snapshot.state.conversation_id != command.conversation_id:
                    raise InvalidTransitionError("conversation does not own the workflow")
                if uow.outbox.has_dispatching_effect(command.run_id):
                    raise EffectInFlightError("cancellation is blocked during dispatch")
                action = ActionRepository(uow.connection).get_by_run(command.run_id)
                if action is None:
                    raise StateIntegrityError("cancellation action is missing")
                if action.ledger_state is LedgerState.SUBMITTING or (
                    action.ledger_state is LedgerState.SUBMITTED
                    and snapshot.state.phase is WorkflowPhase.DISPATCH_PENDING
                ):
                    raise EffectInFlightError("execution submission requires reconciliation")
                result, transition, event = self._persist_p3_event(
                    uow=uow,
                    current=snapshot,
                    command_id=command.command_id,
                    command_type=CommandType.CANCEL_RUN,
                    command_hash=command.command_hash(),
                    event_type=EventType.RUN_CANCELLED,
                    payload={"reason_code": command.reason_code},
                    occurred_at_utc=self.clock.now_utc(),
                )
                ActionRepository(uow.connection).update_ledger(
                    action_id=snapshot.state.action_id,
                    state=LedgerState.CANCELLED,
                    now=self.clock.now_utc(),
                )
                P3RecordRepository(uow.connection).append(
                    run_id=command.run_id,
                    record_type="workflow_state",
                    record=transition.next_state,
                    created_at_utc=self.clock.now_utc(),
                    source_event_id=event.event_id,
                )
                uow.commit()
                return self._result_from_state(
                    command.run_id,
                    command.conversation_id,
                    result.revision,
                    transition.next_state,
                    True,
                )
        except Exception as error:
            return self._safe_reject(command.run_id, command.conversation_id, error)

    def inspect(self, run_id: RunId) -> P3RunView:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StorageError("kernel repositories are unavailable")
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if not isinstance(snapshot.state, P3WorkflowState):
                raise InvalidTransitionError("run is not a schema-2 P3 workflow")
            action = ActionRepository(uow.connection).get_by_run(run_id)
            if action is None:
                raise StateIntegrityError("P3 action is missing")
            # Inspect is a persistence boundary too.  Validate every typed
            # P3 record before exposing a view so an unrelated corrupted
            # append-only row cannot remain invisible until a later stage.
            P3RecordRepository(uow.connection).list_for_run(run_id)
            view = P3RunView(
                run_id=run_id,
                conversation_id=snapshot.state.conversation_id,
                revision=snapshot.revision,
                kernel_status=RunStatus(snapshot.state.status.value),
                state=snapshot.state,
                action=action.action,
                ledger_state=action.ledger_state,
                diagnostics=(
                    ("execution_reconciliation_required",)
                    if action.ledger_state is LedgerState.SUBMITTING
                    or (
                        action.ledger_state is LedgerState.SUBMITTED
                        and snapshot.state.phase
                        in (WorkflowPhase.DISPATCH_PENDING, WorkflowPhase.FAILED)
                    )
                    else ()
                ),
                outbox=tuple(
                    {
                        "effect_id": str(item.effect_id),
                        "effect_type": item.effect_type,
                        "status": item.status.value,
                        "attempt_count": item.attempt_count,
                    }
                    for item in uow.outbox.list_for_run(run_id)
                ),
            )
            uow.commit()
            return view

    def create_worker(self, *, worker_id=None, lease_duration=timedelta(seconds=30)):
        gateway = FakeExecutionGateway(
            self.database_path,
            self.state_root,
            backend=self.backend,
            clock=self.clock,
        )
        evidence = P3EvidencePipeline(self.database_path, self.state_root, clock=self.clock)
        reporter = P3ReportRenderer(self.database_path, self.state_root, clock=self.clock)

        def handler(permit):
            effect_type = permit.effect.effect_type
            if effect_type == "external.p3.dispatch_fake":
                return gateway.execute(permit)
            if effect_type == "internal.p3.assess":
                return evidence.assess(permit)
            if effect_type == "internal.p3.render_report":
                return reporter.render(permit)
            from orca_agent.infrastructure.worker import HandlerResult

            return HandlerResult(success=False)

        def successor_factory(*, permit, completion, snapshot):
            if not completion.success or completion.receipt is None:
                return ()
            if not isinstance(snapshot.state, P3WorkflowState):
                raise StateIntegrityError("P3 completion received a non-P3 snapshot")
            effect = permit.effect
            ids = completion.receipt.artifact_ids
            common = {
                "run_id": str(snapshot.state.run_id),
                "conversation_id": _payload_text(effect.payload, "conversation_id"),
                "action_id": _payload_text(effect.payload, "action_id"),
                "action_hash": snapshot.state.action_hash,
                "envelope_hash": snapshot.state.envelope_hash,
                "budget_hash": snapshot.state.budget_hash,
                "approval_grant_id": str(snapshot.state.approval_grant_id),
                "execution_id": _payload_text(effect.payload, "execution_id"),
                "workflow_schema_version": P3_SCHEMA_VERSION,
                "workflow_engine_version": P3_ENGINE_VERSION,
            }
            if effect.effect_type == "external.p3.dispatch_fake" and len(ids) == 1:
                return (
                    EffectSpec(
                        effect_index=0,
                        effect_type="internal.p3.assess",
                        effect_class=EffectClass.INTERNAL,
                        payload={**common, "raw_result_artifact_id": str(ids[0])},
                    ),
                )
            if effect.effect_type == "internal.p3.assess" and len(ids) == 1:
                return (
                    EffectSpec(
                        effect_index=0,
                        effect_type="internal.p3.render_report",
                        effect_class=EffectClass.INTERNAL,
                        payload={**common, "assessment_artifact_id": str(ids[0])},
                    ),
                )
            if effect.effect_type == "internal.p3.render_report" and len(ids) == 2:
                return ()
            raise StateIntegrityError("P3 completion receipt does not match effect stage")

        def metadata_factory(*, uow, permit, completion, snapshot):
            if not completion.success or completion.receipt is None:
                return {}
            effect_type = permit.effect.effect_type
            records = P3RecordRepository(uow.connection)
            if effect_type == "external.p3.dispatch_fake":
                execution_id = ExecutionId(_payload_text(permit.effect.payload, "execution_id"))
                job = JobRepository(uow.connection).get_by_execution(execution_id)
                if job is None:
                    raise StateIntegrityError("P3 job result is missing during completion")
                if job.raw_result_artifact_id is None or job.result_hash is None:
                    raise StateIntegrityError("P3 job result is missing its hashes")
                raw_artifact = ArtifactRecordRepository(uow.connection).get(
                    job.raw_result_artifact_id
                )
                if raw_artifact is None:
                    raise StateIntegrityError("P3 job artifact hash is not bound")
                return {
                    "p3_stage": "dispatch",
                    "p3_updates": {
                        "job_id": str(job.job_id),
                        "job_result_hash": job.result_hash,
                        "raw_result_artifact_id": str(job.raw_result_artifact_id),
                        "raw_result_hash": raw_artifact.content_hash,
                        "fixture_hash": job.fixture_hash,
                    },
                }
            if effect_type == "internal.p3.assess":
                assessment_entry = records.latest(
                    run_id=permit.effect.run_id,
                    record_type="assessment",
                    model_type=FixtureScientificAssessment,
                )
                if assessment_entry is None:
                    raise StateIntegrityError("P3 assessment record is missing during completion")
                assessment = assessment_entry[1]
                claim_entry = records.latest_any(
                    run_id=permit.effect.run_id,
                    record_type="claim",
                    model_type=ValidatedClaim,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                )
                evidence = EvidenceRepository(uow.connection).get_with_binding(
                    assessment.evidence_ids[0]
                )
                if (
                    claim_entry is None
                    or evidence is None
                    or len(completion.receipt.artifact_ids) != 1
                ):
                    raise StateIntegrityError("P3 assessment completion records are incomplete")
                assessment_artifact_id = str(completion.receipt.artifact_ids[0])
                assessment_artifact = ArtifactRecordRepository(uow.connection).get(
                    ArtifactId(assessment_artifact_id)
                )
                if assessment_artifact is None:
                    raise StateIntegrityError("P3 assessment artifact is missing during completion")
                return {
                    "p3_stage": "assess",
                    "p3_updates": {
                        "assessment_id": str(assessment.assessment_id),
                        "claim_id": str(assessment.claim_id),
                        "assessment_hash": assessment.assessment_hash,
                        "claim_hash": sha256_hex(claim_entry[1]),
                        "evidence_id": str(evidence.evidence_id),
                        "evidence_hash": sha256_hex(evidence.record),
                        "assessment_artifact_id": assessment_artifact_id,
                        "assessment_artifact_hash": assessment_artifact.content_hash,
                    },
                }
            reporter.verify_in_transaction(uow, snapshot)
            manifest_entry = records.latest(
                run_id=permit.effect.run_id,
                record_type="report_manifest",
                model_type=ReportManifestV1,
            )
            if manifest_entry is None:
                raise StateIntegrityError("P3 report manifest is missing during completion")
            manifest = manifest_entry[1]
            markdown_artifact = ArtifactRecordRepository(uow.connection).get(
                manifest.markdown_artifact_id
            )
            json_artifact = ArtifactRecordRepository(uow.connection).get(manifest.json_artifact_id)
            if (
                markdown_artifact is None
                or json_artifact is None
                or markdown_artifact.content_hash != manifest.markdown_hash
                or json_artifact.content_hash != manifest.json_hash
            ):
                raise StateIntegrityError("P3 report artifacts are not bound to the manifest")
            return {
                "p3_stage": "render",
                "p3_updates": {
                    "report_manifest_id": str(manifest.report_manifest_id),
                    "manifest_hash": manifest.manifest_hash,
                    "markdown_artifact_id": str(manifest.markdown_artifact_id),
                    "markdown_hash": manifest.markdown_hash,
                    "json_artifact_id": str(manifest.json_artifact_id),
                    "json_hash": manifest.json_hash,
                },
            }

        def completion_hook(*, uow, permit, completion, event, successors):
            actions = ActionRepository(uow.connection)
            action = actions.get_by_run(permit.effect.run_id)
            if action is None:
                raise StateIntegrityError("P3 action is missing during completion")
            if not completion.success:
                if action.ledger_state in (LedgerState.SUBMITTING, LedgerState.SUBMITTED):
                    return
                next_ledger = LedgerState.FAILED
            elif permit.effect.effect_type == "internal.p3.render_report":
                next_ledger = LedgerState.SUCCEEDED
            elif permit.effect.effect_type == "external.p3.dispatch_fake":
                next_ledger = LedgerState.SUBMITTED
            else:
                return
            actions.update_ledger(
                action_id=action.action.action_id,
                state=next_ledger,
                now=self.clock.now_utc(),
            )

        from orca_agent.infrastructure.worker import OutboxWorker

        def completion_factory():
            return EffectCompletionService(
                self.database_path,
                clock=self.clock,
                registry=P3_EFFECT_REGISTRY,
                max_attempts=self.max_attempts,
                successor_effect_factory=successor_factory,
                completion_metadata_factory=metadata_factory,
                completion_hook=completion_hook,
            )

        return OutboxWorker(
            self.database_path,
            handler,
            clock=self.clock,
            worker_id=worker_id,
            lease_duration=lease_duration,
            max_attempts=self.max_attempts,
            registry=P3_EFFECT_REGISTRY,
            completion_service_factory=completion_factory,
        )

    def _create_p3_run(
        self,
        *,
        uow: SQLiteUnitOfWork,
        command: StartWaterRun,
        state: P3WorkflowState,
        occurred_at_utc,
    ) -> tuple[P3KernelEvent, P3Transition, object]:
        if any(item is None for item in (uow.runs, uow.events, uow.command_receipts)):
            raise StorageError("kernel repositories are unavailable")
        event_id = new_id(EventId)
        from orca_agent.application.results import ApplicationResult

        placeholder = ApplicationResult.accepted_result(
            code="run_created",
            run_id=command.run_id,
            revision=1,
            status=RunStatus.CREATED,
            event_id=event_id,
        )
        payload = {
            "run_id": str(command.run_id),
            "workflow_state": state.model_dump(mode="json"),
            "effects": [],
        }
        candidate = P3KernelEvent.create(
            event_id=event_id,
            command_id=command.command_id,
            command_type=CommandType.CREATE_RUN,
            run_id=command.run_id,
            sequence_no=1,
            expected_revision=0,
            event_type=EventType.RUN_CREATED,
            payload=payload,
            result=placeholder,
            occurred_at_utc=occurred_at_utc,
            command_hash=command.command_hash(),
        )
        transition = reduce_p3_event(None, candidate)
        result = expected_p3_application_result(
            prior_state=None, event=candidate, transition=transition
        )
        event = P3KernelEvent.create(
            event_id=candidate.event_id,
            command_id=candidate.command_id,
            command_type=candidate.command_type,
            run_id=candidate.run_id,
            sequence_no=candidate.sequence_no,
            expected_revision=candidate.expected_revision,
            event_type=candidate.event_type,
            payload=payload,
            result=result,
            occurred_at_utc=candidate.occurred_at_utc,
            recorded_at_utc=candidate.recorded_at_utc,
            command_hash=candidate.command_hash,
            previous_event_hash=candidate.previous_event_hash,
        )
        uow.runs.insert(
            RunSnapshot(
                run_id=command.run_id,
                schema_version=P3_SCHEMA_VERSION,
                engine_version=P3_ENGINE_VERSION,
                revision=1,
                state=transition.next_state,
                state_hash=state_hash(transition.next_state),
                last_event_id=event.event_id,
                created_at_utc=occurred_at_utc,
                updated_at_utc=occurred_at_utc,
            )
        )
        uow.events.append(event, command_hash=command.command_hash())
        uow.command_receipts.append_event(event=event, recorded_at_utc=occurred_at_utc)
        return event, transition, result

    def _persist_p3_event(
        self,
        *,
        uow: SQLiteUnitOfWork,
        current: RunSnapshot,
        command_id: CommandId,
        command_type: CommandType,
        command_hash: str,
        event_type: EventType,
        payload: dict[str, object],
        occurred_at_utc,
    ) -> tuple[object, P3Transition, P3KernelEvent]:
        if any(
            item is None
            for item in (uow.runs, uow.events, uow.interrupts, uow.outbox, uow.command_receipts)
        ):
            raise StorageError("kernel repositories are unavailable")
        if not isinstance(current.state, P3WorkflowState):
            raise StateIntegrityError("P3 event requires a schema-2 state")
        previous = uow.events.get(current.last_event_id)
        if not isinstance(previous, P3KernelEvent):
            raise StateIntegrityError("P3 previous event is missing or has the wrong version")
        event_id = new_id(EventId)
        from orca_agent.application.results import ApplicationResult

        placeholder = ApplicationResult.accepted_result(
            code=event_type.value,
            run_id=current.run_id,
            revision=current.revision + 1,
            status=RunStatus(current.state.status.value),
            event_id=event_id,
        )
        candidate = P3KernelEvent.create(
            event_id=event_id,
            command_id=command_id,
            command_type=command_type,
            run_id=current.run_id,
            sequence_no=current.revision + 1,
            expected_revision=current.revision,
            event_type=event_type,
            payload=payload,
            result=placeholder,
            occurred_at_utc=occurred_at_utc,
            command_hash=command_hash,
            previous_event_hash=previous.event_hash,
        )
        transition = reduce_p3_event(current.state, candidate)
        result = expected_p3_application_result(
            prior_state=current.state,
            event=candidate,
            transition=transition,
        )
        event = P3KernelEvent.create(
            event_id=candidate.event_id,
            command_id=candidate.command_id,
            command_type=candidate.command_type,
            run_id=candidate.run_id,
            sequence_no=candidate.sequence_no,
            expected_revision=candidate.expected_revision,
            event_type=candidate.event_type,
            payload=payload,
            result=result,
            occurred_at_utc=candidate.occurred_at_utc,
            recorded_at_utc=candidate.recorded_at_utc,
            command_hash=candidate.command_hash,
            previous_event_hash=candidate.previous_event_hash,
        )
        uow.events.append(event, command_hash=command_hash)
        uow.outbox.register_effects(
            event=event,
            run_id=current.run_id,
            effects=transition.effects,
            available_at_utc=occurred_at_utc,
            created_at_utc=occurred_at_utc,
        )
        uow.interrupts.apply_operations(event=event, operations=transition.interrupt_operations)
        if transition.next_state.status.is_terminal:
            uow.outbox.cancel_pending_for_run(run_id=current.run_id, now=occurred_at_utc)
        if not uow.runs.compare_and_swap(
            run_id=current.run_id,
            expected_revision=current.revision,
            state=transition.next_state,
            event_id=event.event_id,
            updated_at_utc=occurred_at_utc,
        ):
            raise RevisionConflictError("P3 expected revision was not current")
        uow.command_receipts.append_event(event=event, recorded_at_utc=occurred_at_utc)
        return result, transition, event

    def _result_from_state(
        self,
        run_id: RunId,
        conversation_id: object,
        revision: int,
        state: P3WorkflowState,
        accepted: bool,
    ) -> P3Result:
        return P3Result(
            accepted=accepted,
            code="p3_state",
            run_id=run_id,
            revision=revision,
            phase=state.phase,
            conversation_id=conversation_id,
            action_id=state.action_id,
            approval_interrupt_id=state.approval_interrupt_id,
            dispatch_effect_id=state.dispatch_effect_id,
            details={"phase": state.phase.value},
        )

    def _safe_reject(self, run_id: RunId, conversation_id: object, error: Exception) -> P3Result:
        phase = WorkflowPhase.FAILED
        revision = 0
        action_id = None
        interrupt_id = None
        effect_id = None
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.runs is not None:
                    snapshot = uow.runs.get(run_id)
                    if snapshot is not None:
                        revision = snapshot.revision
                        if isinstance(snapshot.state, P3WorkflowState):
                            phase = snapshot.state.phase
                            action_id = snapshot.state.action_id
                            interrupt_id = snapshot.state.approval_interrupt_id
                            effect_id = snapshot.state.dispatch_effect_id
                if uow.connection is not None and uow.connection.in_transaction:
                    uow.rollback()
        except Exception:
            pass
        if isinstance(error, ApplicationError):
            code = error.code
            details = dict(error.details)
        elif isinstance(error, DomainError):
            code = "state_integrity_error"
            details = {}
        elif isinstance(error, sqlite3.OperationalError):
            code = "storage_busy" if "locked" in str(error).casefold() else "storage_error"
            details = {}
        else:
            code = "p3_request_rejected"
            details = {"error": type(error).__name__}
        return P3Result(
            accepted=False,
            code=code,
            run_id=run_id,
            revision=revision,
            phase=phase,
            conversation_id=conversation_id,
            action_id=action_id,
            approval_interrupt_id=interrupt_id,
            dispatch_effect_id=effect_id,
            details=details,
        )


def _deterministic_approval_id(run_id: RunId, action_id: ActionId) -> ApprovalGrantId:
    return ApprovalGrantId(
        f"approval_{uuid.uuid5(_P3_OBJECT_NAMESPACE, f'{run_id}:{action_id}:approval').hex}"
    )


def _deterministic_workflow_id(identifier_type, run_id: RunId, role: str):
    return identifier_type(
        f"{identifier_type.prefix}_{uuid.uuid5(_P3_OBJECT_NAMESPACE, f'{run_id}:{role}').hex}"
    )


def _payload_text(payload: object, key: str) -> str:
    value = payload.get(key) if hasattr(payload, "get") else None
    if not isinstance(value, str) or not value.strip():
        raise StateIntegrityError(f"P3 effect payload is missing {key}")
    return value


__all__ = ["P3ApplicationService", "P3Result", "P3RunView"]
