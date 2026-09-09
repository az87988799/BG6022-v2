"""Application service for the schema-4 P5 local execution workflow."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import Field

from orca_agent.application.errors import (
    ApplicationError,
    DuplicateCommandConflictError,
    InvalidTransitionError,
    RevisionConflictError,
    StateIntegrityError,
)
from orca_agent.application.p5_dispatch import P5EffectCompletion
from orca_agent.application.p5_errors import (
    ApprovalExpired,
    ApprovalMismatch,
    Cancelled,
    ExecutableVersionMismatch,
    GeometryBindingMismatch,
    Interrupted,
    LaunchStateUnknown,
    ResourceLimitExceeded,
    ResourceUnavailable,
    SourceIntegrityError,
    TimedOut,
)
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import GENESIS_EVENT_HASH, sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ApprovalGrantId,
    CommandId,
    ConversationId,
    EventId,
    ExecutionId,
    JobId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.models import (
    BackendKind,
    Budget,
    ExecutionEnvelope,
    PrimitiveKind,
    PrimitiveSpec,
    ValidatedAction,
)
from orca_agent.domain.p4 import P4Phase
from orca_agent.domain.p5 import (
    GeometryRecord,
    P5ActionRecord,
    P5ActionStatus,
    P5ApprovalGrant,
    P5DataOrigin,
    P5ExecutionBinding,
    P5ExecutionContext,
    P5ExecutionNode,
    P5ExecutionPlan,
    P5GeometrySource,
    P5JobRecord,
    P5JobStatus,
    P5Model,
    P5NodeKind,
    P5ParseStatus,
    P5Phase,
    P5ResultRecord,
    P5WorkflowState,
    bytes_sha256,
)
from orca_agent.execution.local_backend import (
    FakeExecutionBackend,
    LocalOrcaBackend,
    verify_frozen_file_hashes,
)
from orca_agent.execution.orca_compiler import compile_orca_input
from orca_agent.execution.orca_config import runtime_config
from orca_agent.execution.orca_parser import parse_orca_output
from orca_agent.execution.ports import (
    JobObservation,
    LaunchRequest,
)
from orca_agent.execution.windows_job import host_identity
from orca_agent.execution.work_paths import execution_directory
from orca_agent.identity.geometry import (
    generate_initial_geometry,
    parse_xyz_bytes,
    validate_xyz_bytes,
)
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock, format_utc
from orca_agent.infrastructure.execution_resources import (
    RESOURCE_LOCAL_ORCA,
    ExecutionResourceRepository,
)
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.p5_records import LocalJobRepository, P5RecordRepository
from orca_agent.infrastructure.repositories import RunSnapshot
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.codes import HandlerErrorCode
from orca_agent.orchestration.dispatch_policy import P5_EFFECT_REGISTRY
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.p5_commands import P5CommandType, P5EventType
from orca_agent.orchestration.p5_kernel import P5KernelEvent, expected_p5_application_result
from orca_agent.orchestration.p5_versions import (
    P5_ENGINE_VERSION,
    P5_SCHEMA_VERSION,
)
from orca_agent.orchestration.replay import state_hash
from orca_agent.orchestration.state import RunStatus
from orca_agent.planning.p5_protocols import expand_p5_execution_plan, get_p5_protocol


class P5RunView(P5Model):
    run_id: RunId
    conversation_id: ConversationId
    revision: int = Field(ge=1)
    state: P5WorkflowState
    context: P5ExecutionContext
    plan: P5ExecutionPlan
    geometry: tuple[GeometryRecord, ...]
    action: P5ActionRecord | None
    binding: P5ExecutionBinding | None
    grant: P5ApprovalGrant | None
    job: P5JobRecord | None
    results: tuple[P5ResultRecord, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class P5DeliveryReport:
    execution_id: ExecutionId | None
    outcome: str
    status: str
    physical_start_count: int = 0


@dataclass(frozen=True)
class _ExecutionRuntimeSelection:
    backend_kind: str
    executable: Path | None
    orca_version: str | None
    backend: object


class P5ApplicationService:
    """P5 orchestration without a second business queue or scientific claim layer."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        backend: object | None = None,
        backend_kind: str = "fake",
        orca_executable: str | Path | None = None,
        orca_version: str | None = None,
        allow_real_orca: bool = False,
        required_orca_version: str | None = None,
    ) -> None:
        if backend_kind not in {"fake", "local_orca"}:
            raise ValueError("P5 backend_kind must be fake or local_orca")
        if backend is not None and backend_kind not in {"fake", "local_orca"}:
            raise ValueError("P5 backend kind is invalid")
        self.state_root = Path(state_root).resolve()
        self.database_path = resolve_database_path(self.state_root)
        self.clock = clock or SystemClock()
        self.backend_kind = backend_kind
        self.orca_executable = None if orca_executable is None else Path(orca_executable).resolve()
        self.orca_version = orca_version
        self.allow_real_orca = allow_real_orca
        self.required_orca_version = required_orca_version
        self.backend = backend or (
            LocalOrcaBackend(self.state_root)
            if backend_kind == "local_orca"
            else FakeExecutionBackend(self.state_root)
        )

    def prepare_execution(
        self,
        *,
        source_run_id: RunId,
        protocol_id: str,
        run_id: RunId | None = None,
        command_id: CommandId | None = None,
        external_opt_result_id: WorkflowRecordId | None = None,
        wall_time_seconds: int | None = None,
    ) -> ApplicationResult:
        actual_run_id = run_id or new_id(RunId)
        actual_command_id = command_id or new_id(CommandId)
        command_hash = sha256_hex(
            {
                "command_type": P5CommandType.CREATE_EXECUTION.value,
                "run_id": str(actual_run_id),
                "source_run_id": str(source_run_id),
                "protocol_id": protocol_id,
                "external_opt_result_id": None
                if external_opt_result_id is None
                else str(external_opt_result_id),
                **(
                    {"wall_time_seconds": wall_time_seconds}
                    if wall_time_seconds is not None
                    else {}
                ),
            }
        )
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                self._require_p5_kernel(uow)
                uow.begin()
                replayed = self._replayed_command(uow, actual_command_id, command_hash)
                if replayed is not None:
                    uow.commit()
                    return replayed
                uow.commit()
            source = self._read_p4_source(source_run_id)
            if (
                source.state.phase is not P4Phase.PLAN_READY
                or source.confirmed_molecule is None
                or source.prepared_plan is None
            ):
                raise SourceIntegrityError("P5 source must be a verified P4 plan_ready run")
            get_p5_protocol(protocol_id)
            execution_plan = expand_p5_execution_plan(
                wall_time_seconds=wall_time_seconds,
                run_id=actual_run_id,
                source_run_id=source_run_id,
                source_plan_id=source.prepared_plan.record_id,
                source_plan_hash=source.prepared_plan.plan_hash,
                confirmed_molecule_id=source.confirmed_molecule.record_id,
                confirmed_molecule_hash=source.confirmed_molecule.identity_record_hash,
                source_registry_snapshot_id=source.registry.record_id,
                source_registry_snapshot_hash=source.registry.snapshot_hash,
                protocol_id=protocol_id,
                external_opt_result_id=external_opt_result_id,
            )
            external_result = None
            external_geometry_bytes = None
            geometry = generate_initial_geometry(source.confirmed_molecule, run_id=actual_run_id)
            if get_p5_protocol(protocol_id).source_from_opt:
                geometry, external_geometry_bytes, external_result = self._load_external_opt_source(
                    external_opt_result_id,
                    source=source,
                    run_id=actual_run_id,
                )
            return self._persist_prepared_execution(
                command_id=actual_command_id,
                command_hash=command_hash,
                source=source,
                execution_plan=execution_plan,
                geometry=geometry,
                geometry_bytes=external_geometry_bytes,
                upstream_result=external_result,
            )
        except Exception as error:
            return self._rejected(actual_run_id, error)

    def approve(
        self,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        action_id: ActionId,
        action_hash: str,
        binding_hash: str,
        envelope_hash: str,
        budget_hash: str,
        expected_revision: int,
        command_id: CommandId | None = None,
    ) -> ApplicationResult:
        actual_command_id = command_id or new_id(CommandId)
        command_hash = sha256_hex(
            {
                "command_type": P5CommandType.APPROVE_ACTION.value,
                "run_id": str(run_id),
                "conversation_id": str(conversation_id),
                "action_id": str(action_id),
                "action_hash": action_hash,
                "binding_hash": binding_hash,
                "envelope_hash": envelope_hash,
                "budget_hash": budget_hash,
                "expected_revision": expected_revision,
            }
        )
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                replayed = self._replayed_command(uow, actual_command_id, command_hash)
                if replayed is not None:
                    uow.commit()
                    return replayed
                snapshot = self._verified_snapshot(uow, run_id)
                state = self._p5_state(snapshot)
                if snapshot.revision != expected_revision:
                    raise RevisionConflictError("P5 approval revision is stale")
                if (
                    state.phase is not P5Phase.AWAITING_EXECUTION_APPROVAL
                    or state.current_action_id != action_id
                ):
                    raise InvalidTransitionError("P5 has no matching action awaiting approval")
                if state.conversation_id != conversation_id:
                    raise ApprovalMismatch("approval conversation does not own the P5 run")
                records = P5RecordRepository(uow.connection)
                action = records.get_exact_p5(
                    run_id=run_id,
                    record_id=self._record_id_for_action(records, run_id, action_id),
                    record_type="p5.action",
                    model_type=P5ActionRecord,
                )
                binding = (
                    records.get_exact_p5(
                        run_id=run_id,
                        record_id=action.binding_id,
                        record_type="p5.execution_binding",
                        model_type=P5ExecutionBinding,
                    )
                    if action is not None
                    else None
                )
                if action is None or binding is None:
                    raise StateIntegrityError("P5 action or binding is missing")
                if (
                    action.action_id != action_id
                    or action.action_hash != action_hash
                    or action.binding_hash != binding_hash
                    or action.envelope_hash != envelope_hash
                    or action.budget_hash != budget_hash
                    or binding.binding_hash != binding_hash
                ):
                    raise ApprovalMismatch("approval does not match the frozen P5 action")
                now = self.clock.now_utc()
                grant_id = new_id(ApprovalGrantId)
                grant_values = {
                    "grant_id": str(grant_id),
                    "run_id": str(run_id),
                    "conversation_id": str(conversation_id),
                    "interrupt_id": f"p5-approval:{action_id}",
                    "revision": expected_revision,
                    "action_id": str(action_id),
                    "action_hash": action_hash,
                    "binding_hash": binding_hash,
                    "envelope_hash": envelope_hash,
                    "budget_hash": budget_hash,
                    "issued_at_utc": now,
                    "expires_at_utc": now + timedelta(hours=24),
                    "approval_command_id": str(actual_command_id),
                }
                grant_hash_values = {
                    **grant_values,
                    "issued_at_utc": format_utc(now),
                    "expires_at_utc": format_utc(now + timedelta(hours=24)),
                }
                grant = P5ApprovalGrant(
                    **grant_values,
                    grant_hash=sha256_hex(grant_hash_values),
                )
                next_state = state.model_copy(
                    update={
                        "phase": P5Phase.DISPATCH_PENDING,
                        "current_grant_id": grant_id,
                        "last_outcome_code": "action_approved",
                    }
                )
                event, result = self._append_event(
                    uow,
                    snapshot=snapshot,
                    next_state=next_state,
                    command_id=actual_command_id,
                    command_hash=command_hash,
                    command_type=P5CommandType.APPROVE_ACTION,
                    event_type=P5EventType.ACTION_APPROVED,
                    outcome_code="action_approved",
                    now=now,
                    details={
                        "action_id": str(action_id),
                        "grant_id": str(grant_id),
                        "binding_hash": binding_hash,
                        "effects": [
                            EffectSpec(
                                effect_index=0,
                                effect_type="external.p5.launch_orca",
                                effect_class=EffectClass.EXTERNAL,
                                payload={"action_id": str(action_id), "binding_hash": binding_hash},
                            ).model_dump(mode="json")
                        ],
                    },
                )
                records.append_p5(
                    run_id=run_id,
                    record_type="p5.approval_grant",
                    record=grant,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                    record_id=WorkflowRecordId(str(grant_id).replace("approval_", "workflow_", 1)),
                )
                uow.connection.execute(
                    "UPDATE actions SET approval_grant_id = ?, ledger_state = 'approved', "
                    "updated_at_utc = ? WHERE action_id = ?",
                    (str(grant_id), format_utc(now), str(action_id)),
                )
                uow.commit()
                return result
        except Exception as error:
            return self._rejected(run_id, error)

    def create_worker(self, *, allow_real_orca: bool | None = None) -> P5Worker:
        return P5Worker(self, self.allow_real_orca if allow_real_orca is None else allow_real_orca)

    def inspect(self, run_id: RunId) -> P5RunView:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            self._require_p5_kernel(uow)
            uow.begin()
            snapshot = self._verified_snapshot(uow, run_id)
            state = self._p5_state(snapshot)
            records = P5RecordRepository(uow.connection)
            context = records.get_exact_p5(
                run_id=run_id,
                record_id=state.execution_plan_id,
                record_type="p5.execution_plan",
                model_type=P5ExecutionPlan,
            )
            if context is None:
                raise StateIntegrityError("P5 execution plan is missing")
            context_record = records.latest_p5(
                run_id=run_id, record_type="p5.execution_context", model_type=P5ExecutionContext
            )
            if context_record is None:
                raise StateIntegrityError("P5 execution context is missing")
            execution_context = context_record[1]
            entries = records.list_p5_for_run(run_id)
            geometries = tuple(
                item
                for _id, kind, item in entries
                if kind == "p5.geometry" and isinstance(item, GeometryRecord)
            )
            actions = tuple(
                item
                for _id, kind, item in entries
                if kind == "p5.action" and isinstance(item, P5ActionRecord)
            )
            grants = tuple(
                item
                for _id, kind, item in entries
                if kind == "p5.approval_grant" and isinstance(item, P5ApprovalGrant)
            )
            results = tuple(
                item
                for _id, kind, item in entries
                if kind == "p5.result" and isinstance(item, P5ResultRecord)
            )
            action = next(
                (item for item in reversed(actions) if item.action_id == state.current_action_id),
                actions[-1] if actions else None,
            )
            binding = None
            if action is not None:
                binding = next(
                    (
                        item
                        for _id, kind, item in entries
                        if kind == "p5.execution_binding"
                        and getattr(item, "record_id", None) == action.binding_id
                    ),
                    None,
                )
            grant = next(
                (item for item in reversed(grants) if item.grant_id == state.current_grant_id),
                grants[-1] if grants else None,
            )
            job_repository = LocalJobRepository(uow.connection)
            job = (
                job_repository.get_by_execution(state.current_execution_id)
                if state.current_execution_id
                else job_repository.get_by_run(run_id)
            )
            uow.commit()
            return P5RunView(
                run_id=run_id,
                conversation_id=state.conversation_id,
                revision=snapshot.revision,
                state=state,
                context=execution_context,
                plan=context,
                geometry=geometries,
                action=action,
                binding=binding,
                grant=grant,
                job=job,
                results=results,
                diagnostics=("scientific_assessment=not_evaluated", "claim_status=not_generated"),
            )

    def cancel(
        self,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        expected_revision: int,
        reason_code: str = "user_cancelled",
        command_id: CommandId | None = None,
    ) -> ApplicationResult:
        actual_command_id = command_id or new_id(CommandId)
        command_hash = sha256_hex(
            {
                "command_type": P5CommandType.REQUEST_CANCEL.value,
                "run_id": str(run_id),
                "conversation_id": str(conversation_id),
                "expected_revision": expected_revision,
                "reason_code": reason_code,
            }
        )
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                replayed = self._replayed_command(uow, actual_command_id, command_hash)
                if replayed is not None:
                    uow.commit()
                    return replayed
                snapshot = self._verified_snapshot(uow, run_id)
                state = self._p5_state(snapshot)
                if snapshot.revision != expected_revision:
                    raise RevisionConflictError("P5 cancellation revision is stale")
                if state.conversation_id != conversation_id:
                    raise InvalidTransitionError("P5 cancellation conversation does not match")
                if state.phase in {P5Phase.COMPLETED, P5Phase.CANCELLED, P5Phase.FAILED}:
                    raise InvalidTransitionError("P5 run is already terminal")
                now = self.clock.now_utc()
                if state.current_execution_id is None:
                    next_state = state.model_copy(
                        update={
                            "phase": P5Phase.CANCELLED,
                            "status": RunStatus.CANCELLED,
                            "cancel_requested": True,
                            "last_outcome_code": reason_code,
                        }
                    )
                else:
                    next_state = state.model_copy(
                        update={
                            "phase": P5Phase.CANCELLING,
                            "cancel_requested": True,
                            "last_outcome_code": reason_code,
                        }
                    )
                    LocalJobRepository(uow.connection).request_cancel(state.current_execution_id)
                event, result = self._append_event(
                    uow,
                    snapshot=snapshot,
                    next_state=next_state,
                    command_id=actual_command_id,
                    command_hash=command_hash,
                    command_type=P5CommandType.REQUEST_CANCEL,
                    event_type=P5EventType.CANCEL_REQUESTED,
                    outcome_code="cancel_requested",
                    now=now,
                    details={
                        "reason_code": reason_code,
                        "effects": []
                        if state.current_execution_id is None
                        else [self._cancel_effect(state.current_execution_id)],
                    },
                )
                if state.current_execution_id is None and state.current_action_id is not None:
                    uow.connection.execute(
                        "UPDATE actions SET ledger_state = 'cancelled', updated_at_utc = ? "
                        "WHERE action_id = ?",
                        (format_utc(now), str(state.current_action_id)),
                    )
                uow.commit()
            if state.current_execution_id is not None:
                from .p5_control import deliver_control

                deliver_control(self, run_id, enqueue=False)
            return result
        except Exception as error:
            return self._rejected(run_id, error)

    @staticmethod
    def _cancel_effect(execution_id):
        from .p5_control import control_effect

        return control_effect(execution_id, cancel=True)

    def reconcile(
        self,
        run_id: RunId,
        *,
        command_id: CommandId | None = None,
        expected_revision: int | None = None,
    ) -> ApplicationResult:
        command_hash = sha256_hex(
            {
                "command_type": P5CommandType.RECONCILE_EXECUTION.value,
                "run_id": str(run_id),
                "expected_revision": expected_revision,
            }
        )
        try:
            runtime = self._restore_execution_runtime(run_id)
            if command_id is not None:
                with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                    self._require_p5_kernel(uow)
                    uow.begin()
                    replayed = self._replayed_command(uow, command_id, command_hash)
                    if replayed is not None:
                        uow.commit()
                        return replayed
                    uow.commit()
            view = self.inspect(run_id)
            if expected_revision is not None and view.revision != expected_revision:
                raise RevisionConflictError("P5 reconciliation revision is stale")
            if view.state.current_execution_id is None:
                if command_id is None:
                    return ApplicationResult.accepted_result(
                        code="nothing_to_reconcile",
                        run_id=run_id,
                        revision=view.revision,
                        status=view.state.status,
                        event_id=None,
                        details={"phase": view.state.phase.value},
                    )
                return self._append_reconcile_event(
                    run_id=run_id,
                    command_id=command_id,
                    command_hash=command_hash,
                    expected_revision=view.revision,
                    outcome_code="nothing_to_reconcile",
                    details={"phase": view.state.phase.value},
                )
            observation = runtime.backend.poll(str(view.state.current_execution_id))
            if observation.status is P5JobStatus.NEEDS_RECONCILIATION:
                return self._mark_needs_reconciliation(
                    run_id=run_id,
                    execution_id=view.state.current_execution_id,
                    command_id=command_id,
                    command_hash=command_hash,
                    expected_revision=view.revision,
                    reason=observation.message or "execution state is not trustworthy",
                )
            if observation.status in {P5JobStatus.RUNNING, P5JobStatus.STARTING}:
                next_phase = (
                    P5Phase.CANCELLING
                    if view.state.phase is P5Phase.CANCELLING
                    else P5Phase.RUNNING
                )
                if command_id is None and view.state.phase is not P5Phase.NEEDS_RECONCILIATION:
                    return ApplicationResult.accepted_result(
                        code="execution_still_running",
                        run_id=run_id,
                        revision=view.revision,
                        status=view.state.status,
                        event_id=None,
                        details={"execution_id": str(view.state.current_execution_id)},
                    )
                return self._append_reconcile_event(
                    run_id=run_id,
                    command_id=command_id or new_id(CommandId),
                    command_hash=command_hash,
                    expected_revision=view.revision,
                    outcome_code="execution_still_running",
                    details={"execution_id": str(view.state.current_execution_id)},
                    phase=next_phase,
                )
            if view.state.phase is P5Phase.CANCELLING and observation.status in {
                P5JobStatus.CANCELLED,
                P5JobStatus.TIMED_OUT,
                P5JobStatus.INTERRUPTED,
            }:
                collected = self._collect_execution(
                    run_id,
                    observation,
                )
                if command_id is None:
                    return collected
                return self._append_reconcile_event(
                    run_id=run_id,
                    command_id=command_id,
                    command_hash=command_hash,
                    outcome_code="reconciled",
                    details={"execution_id": str(view.state.current_execution_id)},
                )
            collected = self._collect_execution(
                run_id,
                observation,
            )
            if command_id is None:
                return collected
            return self._append_reconcile_event(
                run_id=run_id,
                command_id=command_id,
                command_hash=command_hash,
                outcome_code="reconciled",
                details={
                    "execution_id": str(view.state.current_execution_id),
                    "collection_code": collected.code,
                },
            )
        except Exception as error:
            return self._rejected(run_id, error)

    def _append_reconcile_event(
        self,
        *,
        run_id: RunId,
        command_id: CommandId,
        command_hash: str,
        outcome_code: str,
        details: dict[str, object],
        expected_revision: int | None = None,
        phase: P5Phase | None = None,
    ) -> ApplicationResult:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            snapshot = self._verified_snapshot(uow, run_id)
            if expected_revision is not None and snapshot.revision != expected_revision:
                raise RevisionConflictError("P5 reconciliation revision is stale")
            state = self._p5_state(snapshot)
            if phase is not None and phase is not state.phase:
                state = state.model_copy(update={"phase": phase})
            if phase in {P5Phase.RUNNING, P5Phase.CANCELLING} and state.current_execution_id:
                job = LocalJobRepository(uow.connection).get_by_execution(
                    state.current_execution_id
                )
                if job is not None:
                    LocalJobRepository(uow.connection).update_runtime(
                        execution_id=state.current_execution_id,
                        status=P5JobStatus.RUNNING,
                        last_observation_sequence=job.last_observation_sequence + 1,
                        last_observation_hash=sha256_hex(
                            {"status": P5JobStatus.RUNNING.value, "phase": phase.value}
                        ),
                    )
            _event, result = self._append_event(
                uow,
                snapshot=snapshot,
                next_state=state,
                command_id=command_id,
                command_hash=command_hash,
                command_type=P5CommandType.RECONCILE_EXECUTION,
                event_type=P5EventType.RECONCILED,
                outcome_code=outcome_code,
                now=self.clock.now_utc(),
                details=details,
            )
            uow.commit()
            return result

    def _mark_needs_reconciliation(
        self,
        *,
        run_id: RunId,
        execution_id: ExecutionId,
        command_id: CommandId | None,
        command_hash: str,
        expected_revision: int,
        reason: str,
    ) -> ApplicationResult:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            snapshot = self._verified_snapshot(uow, run_id)
            if snapshot.revision != expected_revision:
                raise RevisionConflictError("P5 reconciliation revision is stale")
            state = self._p5_state(snapshot)
            next_state = state.model_copy(
                update={
                    "phase": P5Phase.NEEDS_RECONCILIATION,
                    "last_outcome_code": "launch_state_unknown",
                    "last_error_code": "launch_state_unknown",
                    "last_error_message": reason[:256],
                }
            )
            job = LocalJobRepository(uow.connection).get_by_execution(execution_id)
            if job is not None:
                LocalJobRepository(uow.connection).update_runtime(
                    execution_id=execution_id,
                    status=P5JobStatus.NEEDS_RECONCILIATION,
                    stop_reason="launch_state_unknown",
                    last_observation_sequence=job.last_observation_sequence + 1,
                    last_observation_hash=sha256_hex(
                        {"status": P5JobStatus.NEEDS_RECONCILIATION.value, "reason": reason}
                    ),
                )
            event, result = self._append_event(
                uow,
                snapshot=snapshot,
                next_state=next_state,
                command_id=command_id or new_id(CommandId),
                command_hash=command_hash,
                command_type=P5CommandType.OBSERVE_JOB,
                event_type=P5EventType.JOB_OBSERVED,
                outcome_code="launch_state_unknown",
                now=self.clock.now_utc(),
                details={"execution_id": str(execution_id), "reason": reason[:256]},
            )
            uow.commit()
            if command_id is None:
                return result
            return result

    def export_execution(self, run_id: RunId, *, format: str = "json") -> dict[str, object] | str:
        view = self.inspect(run_id)
        payload = view.model_dump(mode="json")
        payload["scientific_assessment"] = "not_evaluated"
        payload["claim_status"] = "not_generated"
        if format == "json":
            return payload
        if format != "md":
            raise ValueError("execution export format must be json or md")
        data_origin = view.results[-1].data_origin.value if view.results else "not_started"
        lines = [
            f"# P5 Execution {view.run_id}",
            "",
            f"- Phase: `{view.state.phase.value}`",
            f"- Protocol: `{view.plan.protocol_id}`",
            f"- Data origin: `{data_origin}`",
            "- scientific_assessment: `not_evaluated`",
            "- claim_status: `not_generated`",
            "",
            "## Actions",
            "",
        ]
        for action in [item for item in view.model_dump(mode="python").get("results", [])]:
            lines.append(f"- {action}")
        return "\n".join(lines) + "\n"

    def _read_p4_source(self, source_run_id: RunId):
        from orca_agent.application.p4_service import P4ApplicationService

        return P4ApplicationService(self.state_root).inspect(source_run_id)

    def _restore_execution_runtime(self, run_id: RunId) -> _ExecutionRuntimeSelection:
        """Return trusted per-run routing without mutating shared service defaults."""
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            snapshot = self._verified_snapshot(uow, run_id)
            events = uow.events.list_for_run(snapshot.run_id)
            runtime = events[0].event.payload.get("runtime")
        if runtime is None:
            view = self.inspect(run_id)
            if view.job is None or view.binding.backend_kind != "local_orca":
                return _ExecutionRuntimeSelection(
                    self.backend_kind, self.orca_executable, self.orca_version, self.backend
                )
            spec = json.loads(
                (self.state_root / "work" / str(view.job.execution_id) / "launch.json").read_text()
            )
            candidate = Path(spec["executable"])
            if (
                spec.get("execution_id") != str(view.job.execution_id)
                or spec.get("launch_token") != view.job.launch_token
                or spec.get("binding_hash") != view.binding.binding_hash
                or bytes_sha256(candidate.read_bytes()) != view.binding.executable_sha256
            ):
                raise ExecutableVersionMismatch("historical runtime does not match trusted binding")
            runtime = {
                "backend_kind": "local_orca",
                "executable": str(candidate),
                "orca_version": view.binding.orca_version,
            }
        kind = runtime["backend_kind"]
        if kind == "local_orca":
            executable = Path(str(runtime["executable"])).resolve()
            backend = (
                self.backend
                if self.backend_kind == "local_orca"
                and getattr(self.backend, "backend_kind", None) == "local_orca"
                else LocalOrcaBackend(self.state_root)
            )
            return _ExecutionRuntimeSelection(
                kind,
                executable,
                str(runtime["orca_version"]),
                backend,
            )
        if self.backend_kind == "fake":
            return _ExecutionRuntimeSelection("fake", None, None, self.backend)
        return _ExecutionRuntimeSelection("fake", None, None, FakeExecutionBackend(self.state_root))

    def _load_external_opt_source(
        self,
        external_result_id: WorkflowRecordId | None,
        *,
        source,
        run_id: RunId,
    ) -> tuple[GeometryRecord, bytes, P5ResultRecord]:
        if external_result_id is None:
            raise SourceIntegrityError("freq_from_opt requires an external Opt result")
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            records = P5RecordRepository(uow.connection)
            entry = records.get_by_record_id_p5(
                record_id=external_result_id,
                record_type="p5.result",
                model_type=P5ResultRecord,
            )
            if entry is None:
                raise SourceIntegrityError("external P5 Opt result was not found")
            external_run_id, result = entry
            external_snapshot = self._verified_snapshot(uow, external_run_id)
            external_state = self._p5_state(external_snapshot)
            if external_state.phase is not P5Phase.COMPLETED:
                raise SourceIntegrityError("external P5 Opt run is not completed")
            if external_state.source_run_id != source.run_id:
                raise SourceIntegrityError("external Opt source run differs from P4 source")
            if result.primitive is not P5NodeKind.OPT:
                raise SourceIntegrityError("external P5 result is not an Opt result")
            if result.parse_status is not P5ParseStatus.COMPLETE:
                raise SourceIntegrityError("external Opt result is not parse-complete")
            if result.optimized_geometry_artifact_id is None:
                raise SourceIntegrityError("external Opt result has no optimized geometry")
            artifact = ArtifactRecordRepository(uow.connection).get(
                result.optimized_geometry_artifact_id
            )
            if artifact is None or artifact.run_id != external_run_id:
                raise SourceIntegrityError("external optimized geometry artifact is untrusted")
            geometry_bytes = ArtifactStore(self.state_root).read(artifact)
            geometries = tuple(
                item
                for _record_id, record_type, item in records.list_p5_for_run(external_run_id)
                if record_type == "p5.geometry"
                and isinstance(item, GeometryRecord)
                and item.xyz_bytes_sha256 == artifact.content_hash
            )
            if not geometries:
                raise SourceIntegrityError("external optimized geometry record is missing")
            geometry = geometries[-1]
            if geometry.xyz_bytes_sha256 != artifact.content_hash:
                raise SourceIntegrityError("external geometry bytes do not match artifact")
            if (
                geometry.confirmed_molecule_id != source.confirmed_molecule.record_id
                or geometry.identity_hash != source.confirmed_molecule.identity_record_hash
            ):
                raise SourceIntegrityError("external optimized geometry identity differs")
            validate_xyz_bytes(geometry_bytes, geometry)
            uow.commit()
        return (
            self._geometry_from_xyz(geometry, geometry_bytes, run_id=run_id),
            geometry_bytes,
            result,
        )

    def _persist_prepared_execution(
        self,
        *,
        command_id: CommandId,
        command_hash: str,
        source,
        execution_plan: P5ExecutionPlan,
        geometry: GeometryRecord,
        geometry_bytes: bytes | None = None,
        upstream_result: P5ResultRecord | None = None,
    ) -> ApplicationResult:
        now = self.clock.now_utc()
        run_id = geometry.run_id
        conversation_id = source.conversation_id
        initial_state = P5WorkflowState(
            run_id=run_id,
            status=RunStatus.READY,
            phase=P5Phase.PREPARING,
            conversation_id=conversation_id,
            source_run_id=source.run_id,
            execution_plan_id=execution_plan.record_id,
            execution_plan_hash=execution_plan.plan_hash,
            last_outcome_code="run_created",
        )
        context_values = {
            "record_id": str(new_id(WorkflowRecordId)),
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "source_run_id": str(source.run_id),
            "source_registry_snapshot_id": str(source.registry.record_id),
            "source_registry_snapshot_hash": source.registry.snapshot_hash,
            "confirmed_molecule_id": str(source.confirmed_molecule.record_id),
            "confirmed_molecule_hash": source.confirmed_molecule.identity_record_hash,
            "prepared_plan_id": str(source.prepared_plan.record_id),
            "prepared_plan_hash": source.prepared_plan.plan_hash,
            "execution_plan_id": str(execution_plan.record_id),
            "execution_plan_hash": execution_plan.plan_hash,
        }
        context = P5ExecutionContext(**context_values, context_hash=sha256_hex(context_values))
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            self._require_p5_kernel(uow)
            uow.begin()
            if uow.runs.get(run_id) is not None:
                raise DuplicateCommandConflictError("P5 execution run already exists")
            event, result = self._append_event(
                uow,
                snapshot=None,
                next_state=initial_state,
                command_id=new_id(CommandId),
                command_hash=sha256_hex(
                    {
                        "command_type": P5CommandType.CREATE_EXECUTION.value,
                        "run_id": str(run_id),
                        "source_run_id": str(source.run_id),
                        "execution_plan_id": str(execution_plan.record_id),
                    }
                ),
                command_type=P5CommandType.CREATE_EXECUTION,
                event_type=P5EventType.RUN_CREATED,
                outcome_code="run_created",
                now=now,
                details={
                    "source_run_id": str(source.run_id),
                    "protocol_id": execution_plan.protocol_id,
                    "runtime": {
                        "backend_kind": self.backend_kind,
                        "executable": None
                        if self.orca_executable is None
                        else str(self.orca_executable),
                        "orca_version": self.orca_version,
                    },
                },
            )
            records = P5RecordRepository(uow.connection)
            records.append_p5(
                run_id=run_id,
                record_type="p5.execution_context",
                record=context,
                created_at_utc=now,
                source_event_id=event.event_id,
                record_id=context.record_id,
            )
            records.append_p5(
                run_id=run_id,
                record_type="p5.execution_plan",
                record=execution_plan,
                created_at_utc=now,
                source_event_id=event.event_id,
                record_id=execution_plan.record_id,
            )
            records.append_p5(
                run_id=run_id,
                record_type="p5.geometry",
                record=geometry,
                created_at_utc=now,
                source_event_id=event.event_id,
                record_id=geometry.record_id,
            )
            next_state, action_records = self._build_action(
                uow,
                run_id=run_id,
                conversation_id=conversation_id,
                source=source,
                execution_plan=execution_plan,
                node=execution_plan.nodes[0],
                geometry=geometry,
                geometry_bytes=geometry_bytes,
                upstream_result=upstream_result,
                now=now,
            )
            prepared_event, prepared_result = self._append_event(
                uow,
                snapshot=self._verified_snapshot(uow, run_id),
                next_state=next_state,
                command_id=command_id,
                command_hash=command_hash,
                command_type=P5CommandType.PREPARE_NODE,
                event_type=P5EventType.NODE_PREPARED,
                outcome_code="node_prepared",
                now=now,
                details={
                    "action_id": str(next_state.current_action_id),
                    "binding_hash": next_state.current_binding_hash,
                },
            )
            for record_type, record in action_records:
                records.append_p5(
                    run_id=run_id,
                    record_type=record_type,
                    record=record,
                    created_at_utc=now,
                    source_event_id=prepared_event.event_id,
                    record_id=record.record_id,
                )
            uow.commit()
            return prepared_result

    def _build_action(
        self,
        uow,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        source,
        execution_plan: P5ExecutionPlan,
        node: P5ExecutionNode,
        geometry: GeometryRecord,
        geometry_bytes: bytes | None = None,
        upstream_result: P5ResultRecord | None,
        now: datetime,
    ) -> tuple[P5WorkflowState, tuple[tuple[str, object], ...]]:
        if self.backend_kind == "local_orca":
            if (
                self.orca_executable is None
                or self.orca_executable.suffix.casefold() != ".exe"
                or not self.orca_executable.is_file()
                or self.orca_executable.is_symlink()
            ):
                raise ExecutableVersionMismatch(
                    "local ORCA preparation requires a regular executable .exe"
                )
            if (
                self.required_orca_version is not None
                and self.orca_version != self.required_orca_version
            ):
                raise ExecutableVersionMismatch(
                    "local ORCA preparation requires the exact configured ORCA version"
                )
        artifact_store = ArtifactStore(self.state_root, clock=self.clock)
        action_geometry_bytes = geometry.xyz_bytes() if geometry_bytes is None else geometry_bytes
        protocol = get_p5_protocol(execution_plan.protocol_id)
        if not protocol.budget_is_registered(node.budget, node.kind):
            raise StateIntegrityError("P5 node budget does not match its registered protocol")
        geometry_artifact = artifact_store.put_owned(
            connection=uow.connection,
            run_id=run_id,
            scope_id=str(execution_plan.record_id),
            role="initial_xyz"
            if node.geometry_source.value == "initial_geometry"
            else "optimized_xyz",
            content=action_geometry_bytes,
            media_type="chemical/x-xyz",
        )
        compiled = compile_orca_input(
            node,
            self._method_profile(),
            geometry,
            node.budget,
            protocol.feature_profile(),
            geometry_bytes=action_geometry_bytes,
        )
        action_id = new_id(ActionId)
        input_artifact = artifact_store.put_owned(
            connection=uow.connection,
            run_id=run_id,
            scope_id=str(action_id),
            role="input",
            content=compiled.input_bytes,
            media_type="application/x-orca-input",
        )
        runtime = runtime_config(
            state_root=self.state_root,
            executable=self.orca_executable,
            orca_version=self.orca_version,
            profile_hash=compiled.feature_profile_hash,
            probe=False,
            nprocs=node.budget.nprocs,
            implicit_threads=protocol.implicit_threads,
            parallel=protocol.parallel,
        )
        binding_values = {
            "record_id": str(new_id(WorkflowRecordId)),
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "source_run_id": str(source.run_id),
            "confirmed_molecule_id": str(source.confirmed_molecule.record_id),
            "confirmed_molecule_hash": source.confirmed_molecule.identity_record_hash,
            "prepared_plan_id": str(source.prepared_plan.record_id),
            "prepared_plan_hash": source.prepared_plan.plan_hash,
            "execution_plan_id": str(execution_plan.record_id),
            "execution_plan_hash": execution_plan.plan_hash,
            "node_id": node.node_id,
            "action_id": str(action_id),
            "primitive_id": node.node_id,
            "upstream_result_id": None
            if upstream_result is None
            else str(upstream_result.record_id),
            "upstream_result_hash": None
            if upstream_result is None
            else upstream_result.result_hash,
            "method_profile_id": node.method_profile_id,
            "method_profile_hash": node.method_profile_hash,
            "geometry_artifact_id": str(geometry_artifact.artifact_id),
            "geometry_hash": geometry.geometry_hash,
            "xyz_bytes_sha256": bytes_sha256(action_geometry_bytes),
            "feature_profile_hash": compiled.feature_profile_hash,
            "input_manifest_hash": compiled.manifest_hash,
            "input_sha256": compiled.input_sha256,
            "backend_kind": self.backend_kind,
            "runtime_config_hash": str(runtime["runtime_config_hash"]),
            "orca_version": self.orca_version,
            "executable_sha256": runtime.get("executable_sha256"),
            "budget": node.budget.model_dump(mode="json"),
            "run_budget_seconds": execution_plan.run_budget_seconds,
            "recovery_strategy": "reconcile_no_auto_retry",
        }
        binding = P5ExecutionBinding(**binding_values, binding_hash=sha256_hex(binding_values))
        validated = ValidatedAction.create(
            action_id=action_id,
            proposal_hash=execution_plan.plan_hash,
            primitive=PrimitiveSpec.create(
                kind=PrimitiveKind(node.kind.value),
                molecule_ref=geometry.canonical_isomeric_smiles,
                method_profile_id=node.method_profile_id,
                parameters={"binding_hash": binding.binding_hash, "node_id": node.node_id},
            ),
            execution_envelope=ExecutionEnvelope(
                backend_kind=BackendKind.LOCAL
                if self.backend_kind == "local_orca"
                else BackendKind.FAKE,
                artifact_namespace_id=input_artifact.artifact_id,
            ),
            budget=Budget(
                wall_time_seconds=node.budget.wall_time_seconds,
                memory_mb=node.budget.total_memory_mb,
                cores=node.budget.nprocs,
            ),
        )
        action_values = {
            "record_id": str(new_id(WorkflowRecordId)),
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "action_id": str(action_id),
            "node_id": node.node_id,
            "primitive_id": node.node_id,
            "action_hash": validated.action_hash,
            "envelope_hash": sha256_hex(validated.execution_envelope),
            "budget_hash": sha256_hex(validated.budget),
            "binding_id": str(binding.record_id),
            "binding_hash": binding.binding_hash,
            "input_artifact_id": str(input_artifact.artifact_id),
            "geometry_artifact_id": str(geometry_artifact.artifact_id),
            "status": P5ActionStatus.PLANNED,
            "grant_id": None,
            "execution_id": None,
            "created_at_utc": now,
        }
        action_hash_values = {
            **action_values,
            "action_hash": "0" * 64,
            "status": P5ActionStatus.PLANNED.value,
            "created_at_utc": format_utc(now),
        }
        action_record_hash_values = {
            **action_hash_values,
            "action_hash": action_values["action_hash"],
        }
        action_record = P5ActionRecord(
            **action_values,
            action_record_hash=sha256_hex(action_record_hash_values),
        )
        try:
            uow.connection.execute(
                "INSERT INTO actions("
                "action_id, run_id, conversation_id, action_json, action_hash, envelope_hash, "
                "budget_hash, idempotency_key, approval_grant_id, execution_id, ledger_state, "
                "created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, "
                "'planned', ?, ?)",
                (
                    str(action_id),
                    str(run_id),
                    str(conversation_id),
                    json.dumps(
                        validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    ),
                    action_record.action_hash,
                    action_record.envelope_hash,
                    action_record.budget_hash,
                    f"p5:{run_id}:{action_id}",
                    format_utc(now),
                    format_utc(now),
                ),
            )
        except Exception as error:
            raise StateIntegrityError("P5 action ledger insert failed") from error
        next_state = P5WorkflowState(
            run_id=run_id,
            status=RunStatus.READY,
            phase=P5Phase.AWAITING_EXECUTION_APPROVAL,
            conversation_id=conversation_id,
            source_run_id=source.run_id,
            execution_plan_id=execution_plan.record_id,
            execution_plan_hash=execution_plan.plan_hash,
            current_node_id=node.node_id,
            current_action_id=action_id,
            current_action_hash=action_record.action_hash,
            current_binding_hash=binding.binding_hash,
            last_outcome_code="node_prepared",
        )
        return next_state, (("p5.execution_binding", binding), ("p5.action", action_record))

    def _method_profile(self):
        from orca_agent.planning.registry import METHOD_R2SCAN3C

        return METHOD_R2SCAN3C

    def _verified_snapshot(self, uow, run_id: RunId):
        if uow.runs is None or uow.events is None:
            raise StateIntegrityError("P5 kernel repositories are unavailable")
        snapshot = uow.runs.get_verified(
            run_id, uow.events, interrupts=uow.interrupts, outbox=uow.outbox
        )
        if not isinstance(snapshot.state, P5WorkflowState):
            raise StateIntegrityError("run is not a P5 schema-4 execution run")
        return snapshot

    @staticmethod
    def _p5_state(snapshot) -> P5WorkflowState:
        if not isinstance(snapshot.state, P5WorkflowState):
            raise StateIntegrityError("stored run is not a P5 state")
        return snapshot.state

    @staticmethod
    def _require_p5_kernel(uow) -> None:
        if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
            raise StateIntegrityError("P5 kernel repositories are unavailable")

    def _append_event(
        self,
        uow,
        *,
        snapshot,
        next_state: P5WorkflowState,
        command_id: CommandId,
        command_hash: str,
        command_type: P5CommandType,
        event_type: P5EventType,
        outcome_code: str,
        now: datetime,
        details: dict[str, object],
    ) -> tuple[P5KernelEvent, ApplicationResult]:
        if uow.events is None or uow.runs is None:
            raise StateIntegrityError("P5 event repository is unavailable")
        expected_revision = 0 if snapshot is None else snapshot.revision
        previous = (
            GENESIS_EVENT_HASH
            if snapshot is None
            else self._previous_event_hash(uow, snapshot.last_event_id)
        )
        event_id = new_id(EventId)
        payload = {
            "next_state": next_state.model_dump(mode="json"),
            "outcome_code": outcome_code,
            **details,
        }
        placeholder = ApplicationResult.accepted_result(
            code=outcome_code,
            run_id=next_state.run_id,
            revision=expected_revision + 1,
            status=next_state.status,
            event_id=event_id,
            details={
                "phase": next_state.phase.value,
                "scientific_assessment": "not_evaluated",
                "claim_status": "not_generated",
            },
        )
        event = P5KernelEvent.create(
            command_id=command_id,
            command_type=command_type,
            run_id=next_state.run_id,
            sequence_no=expected_revision + 1,
            expected_revision=expected_revision,
            event_type=event_type,
            payload=payload,
            result=placeholder,
            occurred_at_utc=now,
            command_hash=command_hash,
            previous_event_hash=previous,
            event_id=event_id,
        )
        result = expected_p5_application_result(
            event=event, transition=type("Transition", (), {"next_state": next_state})()
        )
        event = P5KernelEvent.create(
            command_id=command_id,
            command_type=command_type,
            run_id=next_state.run_id,
            sequence_no=expected_revision + 1,
            expected_revision=expected_revision,
            event_type=event_type,
            payload=payload,
            result=result,
            occurred_at_utc=now,
            command_hash=command_hash,
            previous_event_hash=previous,
            event_id=event_id,
        )
        if snapshot is None:
            uow.runs.insert(
                RunSnapshot(
                    run_id=next_state.run_id,
                    schema_version=P5_SCHEMA_VERSION,
                    engine_version=P5_ENGINE_VERSION,
                    revision=1,
                    state=next_state,
                    state_hash=state_hash(next_state),
                    last_event_id=event_id,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        elif not uow.runs.compare_and_swap(
            run_id=next_state.run_id,
            expected_revision=expected_revision,
            state=next_state,
            event_id=event_id,
            updated_at_utc=now,
        ):
            raise RevisionConflictError("P5 state revision changed during transition")
        uow.events.append(event, command_hash=command_hash)
        if next_state.status.is_terminal:
            uow.outbox.cancel_pending_for_run(run_id=next_state.run_id, now=now)
        if details.get("effects"):
            effects = tuple(
                EffectSpec.model_validate_json(json.dumps(item), strict=True)
                for item in details["effects"]
            )
            uow.outbox.register_effects(
                event=event,
                run_id=next_state.run_id,
                effects=effects,
                available_at_utc=now,
                created_at_utc=now,
            )
        return event, result

    @staticmethod
    def _previous_event_hash(uow, event_id: EventId) -> str:
        if uow.events is None:
            raise StateIntegrityError("P5 event repository is unavailable")
        event = uow.events.get(event_id)
        if event is None or not isinstance(event, P5KernelEvent):
            raise StateIntegrityError("P5 previous event is missing")
        return event.event_hash

    @staticmethod
    def _replayed_command(
        uow, command_id: CommandId, command_hash: str
    ) -> ApplicationResult | None:
        if uow.events is None:
            raise StateIntegrityError("P5 event repository is unavailable")
        stored = uow.events.get_by_command_id(command_id)
        if stored is None:
            return None
        if not isinstance(stored.event, P5KernelEvent):
            raise DuplicateCommandConflictError("command ID belongs to another workflow")
        if stored.command_hash != command_hash:
            raise DuplicateCommandConflictError("same command ID has a different payload")
        return ApplicationResult.model_validate_json(
            json.dumps(thaw_json(stored.event.result)), strict=True
        )

    @staticmethod
    def _record_id_for_action(
        records: P5RecordRepository, run_id: RunId, action_id: ActionId
    ) -> WorkflowRecordId:
        matches = [
            item
            for item in records.list_p5_for_run(run_id)
            if item[1] == "p5.action"
            and isinstance(item[2], P5ActionRecord)
            and item[2].action_id == action_id
        ]
        if len(matches) != 1:
            raise StateIntegrityError("P5 action record is missing or duplicated")
        return matches[0][0]

    def _rejected(self, run_id: RunId, error: Exception) -> ApplicationResult:
        try:
            snapshot = self.inspect(run_id)
            status = snapshot.state.status
            revision = snapshot.revision
        except Exception:
            status = RunStatus.CREATED
            revision = 0
        if isinstance(error, ApplicationError):
            code = error.code
            details = dict(error.details)
        else:
            code = getattr(error, "code", "p5_error")
            details = {"message": str(error)[:256]}
        return ApplicationResult.rejected_result(
            code=code, run_id=run_id, revision=revision, status=status, details=details
        )

    def _collect_execution(
        self,
        run_id: RunId,
        observation: JobObservation,
        *,
        command_id: CommandId | None = None,
        command_hash: str | None = None,
    ) -> ApplicationResult:
        try:
            runtime = self._restore_execution_runtime(run_id)
            source_view = self._read_p4_source(self.inspect(run_id).state.source_run_id)
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                snapshot = self._verified_snapshot(uow, run_id)
                state = self._p5_state(snapshot)
                if (
                    state.current_execution_id is None
                    or str(state.current_execution_id) != observation.execution_id
                ):
                    raise StateIntegrityError("observed execution is not the current P5 execution")
                records = P5RecordRepository(uow.connection)
                action = (
                    records.get_exact_p5(
                        run_id=run_id,
                        record_id=self._record_id_for_action(
                            records, run_id, state.current_action_id
                        ),
                        record_type="p5.action",
                        model_type=P5ActionRecord,
                    )
                    if state.current_action_id
                    else None
                )
                if action is None:
                    raise StateIntegrityError("P5 current action is missing")
                binding = records.get_exact_p5(
                    run_id=run_id,
                    record_id=action.binding_id,
                    record_type="p5.execution_binding",
                    model_type=P5ExecutionBinding,
                )
                plan = records.get_exact_p5(
                    run_id=run_id,
                    record_id=state.execution_plan_id,
                    record_type="p5.execution_plan",
                    model_type=P5ExecutionPlan,
                )
                if binding is None or plan is None:
                    raise StateIntegrityError("P5 binding or execution plan is missing")
                node = next(item for item in plan.nodes if item.node_id == action.node_id)
                geometry = next(
                    (
                        item
                        for _id, kind, item in records.list_p5_for_run(run_id)
                        if kind == "p5.geometry"
                        and isinstance(item, GeometryRecord)
                        and item.geometry_hash == binding.geometry_hash
                        and item.xyz_bytes_sha256 == binding.xyz_bytes_sha256
                    ),
                    None,
                )
                if geometry is None:
                    raise GeometryBindingMismatch("P5 input geometry record is missing")
                output = runtime.backend.collect(observation.execution_id)
                output_bytes = output.stdout_path.read_bytes()
                stderr_bytes = output.stderr_path.read_bytes()
                hessian_bytes = (
                    None if output.hessian_path is None else output.hessian_path.read_bytes()
                )
                optimized_xyz_bytes = (
                    None
                    if output.optimized_xyz_path is None
                    else output.optimized_xyz_path.read_bytes()
                )
                artifact_store = ArtifactStore(self.state_root, clock=self.clock)
                execution_ref = ExecutionId(observation.execution_id)
                current_geometry_artifact = ArtifactRecordRepository(uow.connection).get(
                    action.geometry_artifact_id
                )
                if (
                    current_geometry_artifact is None
                    or current_geometry_artifact.run_id != run_id
                    or current_geometry_artifact.artifact_id != binding.geometry_artifact_id
                ):
                    raise StateIntegrityError("P5 current geometry artifact is untrusted")
                current_geometry_bytes = artifact_store.read(current_geometry_artifact)
                if bytes_sha256(current_geometry_bytes) != binding.xyz_bytes_sha256:
                    raise GeometryBindingMismatch(
                        "P5 current geometry artifact does not match the frozen binding"
                    )
                try:
                    work_directory = execution_directory(
                        self.state_root, observation.execution_id, create=False
                    )
                except (OSError, ValueError) as error:
                    raise ResourceLimitExceeded(
                        "P5 frozen execution directory is missing or untrusted"
                    ) from error
                verify_frozen_file_hashes(
                    work_directory,
                    input_sha256=binding.input_sha256,
                    geometry_sha256=binding.xyz_bytes_sha256,
                )

                def archive_output_artifacts(optimized_xyz: bytes | None = None):
                    stdout_ref = artifact_store.put_owned(
                        connection=uow.connection,
                        run_id=run_id,
                        scope_id=observation.execution_id,
                        role="stdout",
                        content=output_bytes,
                        media_type="text/plain",
                        action_id=action.action_id,
                        execution_id=execution_ref,
                    )
                    stderr_ref = artifact_store.put_owned(
                        connection=uow.connection,
                        run_id=run_id,
                        scope_id=observation.execution_id,
                        role="stderr",
                        content=stderr_bytes,
                        media_type="text/plain",
                        action_id=action.action_id,
                        execution_id=execution_ref,
                    )
                    hessian_ref = (
                        None
                        if hessian_bytes is None
                        else artifact_store.put_owned(
                            connection=uow.connection,
                            run_id=run_id,
                            scope_id=observation.execution_id,
                            role="hessian",
                            content=hessian_bytes,
                            media_type="application/octet-stream",
                            action_id=action.action_id,
                            execution_id=execution_ref,
                        )
                    )
                    optimized_ref = (
                        None
                        if optimized_xyz is None
                        else artifact_store.put_owned(
                            connection=uow.connection,
                            run_id=run_id,
                            scope_id=observation.execution_id,
                            role="optimized_xyz",
                            content=optimized_xyz,
                            media_type="chemical/x-xyz",
                            action_id=action.action_id,
                            execution_id=execution_ref,
                        )
                    )
                    return stdout_ref, stderr_ref, hessian_ref, optimized_ref

                stdout_artifact, stderr_artifact, hessian_artifact, _ = archive_output_artifacts(
                    optimized_xyz_bytes
                )
                optimized_artifact = None
                optimized_geometry = None
                parsed = None
                try:
                    expected_origin = (
                        P5DataOrigin.FAKE_FIXTURE
                        if binding.backend_kind == "fake"
                        else P5DataOrigin.ORCA_LOCAL
                    )
                    if output.data_origin is not expected_origin:
                        raise SourceIntegrityError(
                            "execution backend data origin does not match the frozen binding"
                        )
                    if observation.status is P5JobStatus.CANCELLED:
                        raise Cancelled("execution was cancelled before result collection")
                    if observation.status is P5JobStatus.TIMED_OUT:
                        raise TimedOut("execution exceeded its wall-time deadline")
                    if observation.status is P5JobStatus.INTERRUPTED:
                        raise Interrupted("execution ended without a trustworthy completion")
                    parsed = parse_orca_output(
                        output_bytes,
                        primitive=node,
                        geometry=geometry,
                        input_manifest_hash=binding.input_manifest_hash,
                        exit_code=output.exit_code if output.exit_code is not None else 1,
                        hessian_bytes=hessian_bytes,
                        optimized_xyz_bytes=optimized_xyz_bytes,
                        data_origin=output.data_origin,
                        stderr_bytes=stderr_bytes,
                    )
                    if binding.backend_kind == "local_orca" and (
                        parsed.orca_version is None
                        or binding.orca_version is None
                        or parsed.orca_version != binding.orca_version
                    ):
                        raise ExecutableVersionMismatch(
                            "local ORCA output version is not the approved ORCA 6.1 version"
                        )
                    if parsed.optimized_xyz_bytes is not None:
                        optimized_artifact = artifact_store.put_owned(
                            connection=uow.connection,
                            run_id=run_id,
                            scope_id=observation.execution_id,
                            role="optimized_xyz",
                            content=parsed.optimized_xyz_bytes,
                            media_type="chemical/x-xyz",
                            action_id=action.action_id,
                            execution_id=execution_ref,
                        )
                    if parsed.optimized_xyz_bytes is not None:
                        optimized_geometry = self._geometry_from_xyz(
                            geometry, parsed.optimized_xyz_bytes, run_id=run_id
                        )
                except Exception as parse_error:
                    job = LocalJobRepository(uow.connection).get_by_execution(execution_ref)
                    if job is None:
                        raise StateIntegrityError(
                            "P5 job is missing while recording parse failure"
                        ) from parse_error
                    failure_values = {
                        "record_id": str(new_id(WorkflowRecordId)),
                        "run_id": str(run_id),
                        "action_id": str(action.action_id),
                        "execution_id": observation.execution_id,
                        "job_id": str(job.job_id),
                        "data_origin": output.data_origin,
                        "primitive": node.kind,
                        "input_manifest_hash": binding.input_manifest_hash,
                        "output_manifest_hash": sha256_hex(
                            {
                                "stdout_sha256": bytes_sha256(output_bytes),
                                "stderr_sha256": bytes_sha256(stderr_bytes),
                                "hessian_sha256": None
                                if hessian_bytes is None
                                else bytes_sha256(hessian_bytes),
                                "optimized_xyz_sha256": None
                                if parsed is None or parsed.optimized_xyz_bytes is None
                                else bytes_sha256(parsed.optimized_xyz_bytes),
                            }
                        ),
                        "orca_version": binding.orca_version,
                        "executable_sha256": binding.executable_sha256,
                        "exit_code": output.exit_code,
                        "normal_termination": False,
                        "scf_converged": None,
                        "optimization_converged": None,
                        "parse_status": P5ParseStatus.REJECTED,
                        "energy": None,
                        "energy_unit": None,
                        "energy_token": None,
                        "frequencies": (),
                        "frequency_unit": None,
                        "optimized_geometry_artifact_id": None
                        if optimized_artifact is None
                        else str(optimized_artifact.artifact_id),
                        "hessian_artifact_id": None
                        if hessian_artifact is None
                        else str(hessian_artifact.artifact_id),
                        "stdout_artifact_id": str(stdout_artifact.artifact_id),
                        "stderr_artifact_id": str(stderr_artifact.artifact_id),
                        "diagnostics": (
                            getattr(parse_error, "code", "p5_error"),
                            str(parse_error)[:256],
                        ),
                        "source_locations": {},
                        "scientific_assessment": "not_evaluated",
                        "claim_status": "not_generated",
                    }
                    failure_hash_values = {
                        **failure_values,
                        "frequencies": [],
                        "diagnostics": list(failure_values["diagnostics"]),
                    }
                    failure_record = P5ResultRecord(
                        **failure_values,
                        result_hash=sha256_hex(failure_hash_values),
                    )
                    now = self.clock.now_utc()
                    terminal_job_status = (
                        observation.status
                        if observation.status
                        in {
                            P5JobStatus.CANCELLED,
                            P5JobStatus.TIMED_OUT,
                            P5JobStatus.INTERRUPTED,
                        }
                        else P5JobStatus.FAILED
                    )
                    failure_code = getattr(parse_error, "code", "p5_error")
                    failure_phase = (
                        P5Phase.CANCELLED
                        if terminal_job_status is P5JobStatus.CANCELLED
                        else P5Phase.FAILED
                    )
                    failure_status = (
                        RunStatus.CANCELLED
                        if terminal_job_status is P5JobStatus.CANCELLED
                        else RunStatus.FAILED
                    )
                    failure_outcome = (
                        "cancelled"
                        if terminal_job_status is P5JobStatus.CANCELLED
                        else failure_code
                    )
                    next_state = state.model_copy(
                        update={
                            "status": failure_status,
                            "phase": failure_phase,
                            "current_node_id": None,
                            "current_action_id": None,
                            "current_action_hash": None,
                            "current_binding_hash": None,
                            "current_grant_id": None,
                            "current_execution_id": None,
                            "last_result_id": failure_record.record_id,
                            "last_outcome_code": failure_outcome,
                            "last_error_code": failure_code,
                            "last_error_message": str(parse_error)[:256],
                        }
                    )
                    event, failure_result = self._append_event(
                        uow,
                        snapshot=snapshot,
                        next_state=next_state,
                        command_id=command_id or new_id(CommandId),
                        command_hash=command_hash
                        or sha256_hex({"collect": observation.execution_id, "failure": True}),
                        command_type=P5CommandType.COLLECT_RESULT,
                        event_type=P5EventType.RUN_FAILED,
                        outcome_code=failure_outcome,
                        now=now,
                        details={
                            "execution_id": observation.execution_id,
                            "result_id": str(failure_record.record_id),
                            "error_code": failure_code,
                        },
                    )
                    records.append_p5(
                        run_id=run_id,
                        record_type="p5.result",
                        record=failure_record,
                        created_at_utc=now,
                        source_event_id=event.event_id,
                        record_id=failure_record.record_id,
                    )
                    LocalJobRepository(uow.connection).mark_terminal(
                        execution_id=execution_ref,
                        status=terminal_job_status,
                        exit_code=output.exit_code,
                        stop_reason=failure_code,
                        terminal_receipt_id=failure_record.record_id,
                        terminal_receipt_hash=failure_record.result_hash,
                        terminal_at_utc=now,
                    )
                    if binding.backend_kind == "local_orca":
                        ExecutionResourceRepository(uow.connection).release(
                            execution_id=observation.execution_id,
                            generation=job.launch_generation,
                            now_utc=format_utc(now),
                            evidence_ref=f"p5:{run_id}:collect_failed",
                        )
                    uow.connection.execute(
                        "UPDATE actions SET ledger_state = ?, execution_id = ?, "
                        "updated_at_utc = ? WHERE action_id = ?",
                        (
                            "cancelled"
                            if terminal_job_status is P5JobStatus.CANCELLED
                            else "failed",
                            observation.execution_id,
                            format_utc(now),
                            str(action.action_id),
                        ),
                    )
                    uow.commit()
                    return failure_result

                job = LocalJobRepository(uow.connection).get_by_execution(execution_ref)
                if job is None:
                    raise StateIntegrityError("P5 job is missing while recording result")
                result_values = {
                    "record_id": str(new_id(WorkflowRecordId)),
                    "run_id": str(run_id),
                    "action_id": str(action.action_id),
                    "execution_id": observation.execution_id,
                    "job_id": str(job.job_id),
                    "data_origin": parsed.data_origin,
                    "primitive": parsed.primitive,
                    "input_manifest_hash": parsed.input_manifest_hash,
                    "output_manifest_hash": parsed.output_manifest_hash,
                    "orca_version": parsed.orca_version,
                    "executable_sha256": binding.executable_sha256,
                    "exit_code": parsed.exit_code,
                    "normal_termination": parsed.normal_termination,
                    "scf_converged": parsed.scf_converged,
                    "optimization_converged": parsed.optimization_converged,
                    "parse_status": parsed.parse_status,
                    "energy": parsed.energy,
                    "energy_unit": parsed.energy_unit,
                    "energy_token": parsed.energy_token,
                    "frequencies": parsed.frequencies,
                    "frequency_unit": parsed.frequency_unit,
                    "optimized_geometry_artifact_id": None
                    if optimized_artifact is None
                    else str(optimized_artifact.artifact_id),
                    "hessian_artifact_id": None
                    if hessian_artifact is None
                    else str(hessian_artifact.artifact_id),
                    "stdout_artifact_id": str(stdout_artifact.artifact_id),
                    "stderr_artifact_id": str(stderr_artifact.artifact_id),
                    "diagnostics": parsed.diagnostics,
                    "source_locations": parsed.source_locations,
                    "scientific_assessment": "not_evaluated",
                    "claim_status": "not_generated",
                }
                result_hash_values = {
                    **result_values,
                    "frequencies": list(parsed.frequencies),
                    "diagnostics": list(parsed.diagnostics),
                }
                result_record = P5ResultRecord(
                    **result_values,
                    result_hash=sha256_hex(result_hash_values),
                )
                next_node = self._next_node(plan, node.node_id)
                now = self.clock.now_utc()
                if next_node is None:
                    next_state = state.model_copy(
                        update={
                            "phase": P5Phase.COMPLETED,
                            "current_node_id": None,
                            "current_action_id": None,
                            "current_action_hash": None,
                            "current_binding_hash": None,
                            "current_grant_id": None,
                            "current_execution_id": None,
                            "last_result_id": result_record.record_id,
                            "last_outcome_code": "execution_completed",
                        }
                    )
                else:
                    downstream_geometry = optimized_geometry
                    if (
                        downstream_geometry is None
                        and next_node.geometry_source is P5GeometrySource.OPTIMIZED
                    ):
                        downstream_geometry = geometry
                    if downstream_geometry is None:
                        raise GeometryBindingMismatch(
                            "downstream node requires an optimized geometry"
                        )
                    next_state, action_records = self._build_action(
                        uow,
                        run_id=run_id,
                        conversation_id=state.conversation_id,
                        source=source_view,
                        execution_plan=plan,
                        node=next_node,
                        geometry=downstream_geometry,
                        geometry_bytes=parsed.optimized_xyz_bytes
                        if optimized_geometry is not None
                        else current_geometry_bytes,
                        upstream_result=result_record,
                        now=now,
                    )
                event, app_result = self._append_event(
                    uow,
                    snapshot=snapshot,
                    next_state=next_state,
                    command_id=command_id or new_id(CommandId),
                    command_hash=command_hash
                    or sha256_hex(
                        {
                            "collect": observation.execution_id,
                            "result_id": str(result_record.record_id),
                        }
                    ),
                    command_type=P5CommandType.COLLECT_RESULT,
                    event_type=P5EventType.RESULT_COLLECTED,
                    outcome_code="execution_completed" if next_node is None else "node_completed",
                    now=now,
                    details={
                        "result_id": str(result_record.record_id),
                        "execution_id": observation.execution_id,
                        "data_origin": parsed.data_origin.value,
                    },
                )
                if optimized_geometry is not None:
                    records.append_p5(
                        run_id=run_id,
                        record_type="p5.geometry",
                        record=optimized_geometry,
                        created_at_utc=now,
                        source_event_id=event.event_id,
                        record_id=optimized_geometry.record_id,
                    )
                records.append_p5(
                    run_id=run_id,
                    record_type="p5.result",
                    record=result_record,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                    record_id=result_record.record_id,
                )
                if next_node is not None:
                    for record_type, record in action_records:
                        records.append_p5(
                            run_id=run_id,
                            record_type=record_type,
                            record=record,
                            created_at_utc=now,
                            source_event_id=event.event_id,
                            record_id=record.record_id,
                        )
                LocalJobRepository(uow.connection).mark_terminal(
                    execution_id=ExecutionId(observation.execution_id),
                    status=P5JobStatus.SUCCEEDED
                    if parsed.parse_status is P5ParseStatus.COMPLETE
                    else P5JobStatus.FAILED,
                    exit_code=parsed.exit_code,
                    stop_reason="normal_exit",
                    terminal_receipt_id=result_record.record_id,
                    terminal_receipt_hash=result_record.result_hash,
                    terminal_at_utc=now,
                )
                if binding.backend_kind == "local_orca":
                    ExecutionResourceRepository(uow.connection).release(
                        execution_id=observation.execution_id,
                        generation=job.launch_generation,
                        now_utc=format_utc(now),
                        evidence_ref=f"p5:{run_id}:collect_terminal",
                    )
                uow.connection.execute(
                    "UPDATE actions SET ledger_state = ?, execution_id = ?, "
                    "updated_at_utc = ? WHERE action_id = ?",
                    (
                        P5ActionStatus.SUCCEEDED.value
                        if parsed.parse_status is P5ParseStatus.COMPLETE
                        else P5ActionStatus.FAILED.value,
                        observation.execution_id,
                        format_utc(now),
                        str(action.action_id),
                    ),
                )
                uow.commit()
                return app_result
        except Exception as error:
            return self._rejected(run_id, error)

    @staticmethod
    def _next_node(plan: P5ExecutionPlan, current_node_id: str) -> P5ExecutionNode | None:
        for index, item in enumerate(plan.nodes):
            if item.node_id == current_node_id:
                return plan.nodes[index + 1] if index + 1 < len(plan.nodes) else None
        raise StateIntegrityError("P5 current node is not in execution plan")

    @staticmethod
    def _geometry_from_xyz(
        previous: GeometryRecord, value: bytes, *, run_id: RunId
    ) -> GeometryRecord:
        from orca_agent.identity.optimized_compatibility import validate_optimized_identity

        validate_optimized_identity(value, previous)
        symbols, coordinates = parse_xyz_bytes(value)
        if symbols != previous.atom_symbols:
            raise GeometryBindingMismatch("optimized geometry atom mapping changed")
        values = {
            "record_id": str(new_id(WorkflowRecordId)),
            "run_id": str(run_id),
            "confirmed_molecule_id": str(previous.confirmed_molecule_id),
            "identity_hash": previous.identity_hash,
            "canonical_isomeric_smiles": previous.canonical_isomeric_smiles,
            "molecular_formula": previous.molecular_formula,
            "formal_charge": previous.formal_charge,
            "multiplicity": previous.multiplicity,
            "atom_symbols": symbols,
            "atom_map": previous.atom_map,
            "coordinates": coordinates,
            "xyz_precision": previous.xyz_precision,
            "rdkit_version": previous.rdkit_version,
            "algorithm_version": previous.algorithm_version,
            "seed": previous.seed,
        }
        from orca_agent.domain.p5 import stable_geometry_hash

        values["geometry_hash"] = stable_geometry_hash(
            atom_symbols=symbols, atom_map=previous.atom_map, coordinates=coordinates
        )
        values["xyz_bytes_sha256"] = bytes_sha256(value)
        hash_values = {
            **values,
            "atom_symbols": list(symbols),
            "atom_map": list(previous.atom_map),
            "coordinates": [list(point) for point in coordinates],
        }
        values["record_hash"] = sha256_hex(hash_values)
        record = GeometryRecord(**values)
        validate_xyz_bytes(value, record)
        return record

    def _complete_cancel(
        self,
        *,
        run_id: RunId,
        execution_id: ExecutionId,
        command_id: CommandId,
        prior_command: CommandId,
    ) -> ApplicationResult:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            snapshot = self._verified_snapshot(uow, run_id)
            state = self._p5_state(snapshot)
            now = self.clock.now_utc()
            next_state = state.model_copy(
                update={
                    "phase": P5Phase.CANCELLED,
                    "status": RunStatus.CANCELLED,
                    "current_execution_id": None,
                    "current_action_id": None,
                    "current_action_hash": None,
                    "current_binding_hash": None,
                    "current_grant_id": None,
                    "last_outcome_code": "cancelled",
                }
            )
            _event, result = self._append_event(
                uow,
                snapshot=snapshot,
                next_state=next_state,
                command_id=command_id,
                command_hash=sha256_hex(
                    {"cancel_after": str(prior_command), "execution_id": str(execution_id)}
                ),
                command_type=P5CommandType.COMPLETE_CANCEL,
                event_type=P5EventType.RUN_CANCELLED,
                outcome_code="cancelled",
                now=now,
                details={"execution_id": str(execution_id)},
            )
            LocalJobRepository(uow.connection).mark_terminal(
                execution_id=execution_id,
                status=P5JobStatus.CANCELLED,
                exit_code=None,
                stop_reason="cancel_requested",
                terminal_receipt_id=None,
                terminal_receipt_hash=None,
                terminal_at_utc=now,
            )
            job = LocalJobRepository(uow.connection).get_by_execution(execution_id)
            if job is not None and job.backend_kind == "local_orca":
                ExecutionResourceRepository(uow.connection).release(
                    execution_id=str(execution_id),
                    generation=job.launch_generation,
                    now_utc=format_utc(now),
                    evidence_ref=f"p5:{run_id}:cancelled",
                )
            if state.current_action_id is not None:
                uow.connection.execute(
                    "UPDATE actions SET ledger_state = 'cancelled', execution_id = ?, "
                    "updated_at_utc = ? WHERE action_id = ?",
                    (str(execution_id), format_utc(now), str(state.current_action_id)),
                )
            uow.commit()
            return result


class P5Worker:
    def __init__(self, service: P5ApplicationService, allow_real_orca: bool) -> None:
        self.service = service
        self.allow_real_orca = allow_real_orca
        self._resource_connection = None
        self._resource_readiness = _LaunchResourceReadiness(self)

    def run_once(
        self, *, run_id: RunId | None = None, limit: int = 1
    ) -> tuple[P5DeliveryReport, ...]:
        if type(limit) is not int or limit < 1:
            return ()
        reports: list[P5DeliveryReport] = []
        candidates = [run_id] if run_id is not None else self._pending_runs()
        for candidate in candidates[:limit]:
            try:
                runtime = self.service._restore_execution_runtime(candidate)
                view = self.service.inspect(candidate)
                state = view.state
                if state.phase is P5Phase.AWAITING_EXECUTION_APPROVAL:
                    reports.append(
                        P5DeliveryReport(None, "awaiting_execution_approval", state.phase.value)
                    )
                    continue
                with SQLiteUnitOfWork(self.service.database_path) as uow:
                    outstanding_launch = any(
                        effect.effect_type == "external.p5.launch_orca"
                        and effect.status.value in {"pending", "leased", "dispatching"}
                        for effect in uow.outbox.list_for_run(candidate)
                    )
                if state.phase is P5Phase.DISPATCH_PENDING or outstanding_launch:
                    if view.binding.backend_kind == "local_orca" and not self.allow_real_orca:
                        reports.append(
                            P5DeliveryReport(None, "real_execution_disabled", state.phase.value)
                        )
                        continue

                    def handle(permit, candidate=candidate, runtime=runtime):
                        report = self._dispatch(
                            candidate, self.service.inspect(candidate), permit, runtime=runtime
                        )
                        reports.append(report)
                        return HandlerResult(
                            success=report.outcome
                            in {
                                "succeeded",
                                "starting",
                                "running",
                                "needs_reconciliation",
                                "launch_state_unknown",
                                "launch_acknowledged",
                            },
                            error_code=(
                                HandlerErrorCode.RESOURCE_BUSY
                                if report.outcome == "resource_busy"
                                else None
                            ),
                        )

                    OutboxWorker(
                        self.service.database_path,
                        handler=handle,
                        clock=self.service.clock,
                        registry=P5_EFFECT_REGISTRY,
                        completion_service_factory=lambda: P5EffectCompletion(self.service),
                        readiness_check=self._resource_readiness,
                    ).run_once(limit=1)
                    collected = self.service.inspect(candidate)
                    if collected.state.phase is P5Phase.COLLECTING:
                        from .p5_control import deliver_control

                        deliver_control(self.service, candidate)
                    self._release_resource_if_terminal(candidate)
                    continue
                if (
                    state.phase
                    in {
                        P5Phase.RUNNING,
                        P5Phase.COLLECTING,
                        P5Phase.CANCELLING,
                        P5Phase.NEEDS_RECONCILIATION,
                    }
                    and state.current_execution_id is not None
                ):
                    from .p5_control import deliver_control

                    result = deliver_control(self.service, candidate)
                    if result is None:
                        continue
                    refreshed = self.service.inspect(candidate)
                    self._release_resource_if_terminal(candidate)
                    reports.append(
                        P5DeliveryReport(
                            refreshed.state.current_execution_id,
                            result.code,
                            refreshed.state.phase.value,
                            1 if refreshed.job and refreshed.job.launch_consumed_at_utc else 0,
                        )
                    )
            except Exception as error:
                reports.append(
                    P5DeliveryReport(None, getattr(error, "code", "p5_worker_error"), "failed")
                )
        return tuple(reports)

    def _resource_ready(self, effect, snapshot, _now) -> bool:
        if effect.effect_type != "external.p5.launch_orca":
            return True
        if self._resource_connection is None:
            return True
        state = snapshot.state
        records = P5RecordRepository(self._resource_connection)
        binding = None
        for _record_id, record_type, record in records.list_p5_for_run(snapshot.run_id):
            if (
                record_type == "p5.execution_binding"
                and getattr(record, "backend_kind", None) == "local_orca"
            ):
                binding = record
        if binding is None:
            return True
        slot = ExecutionResourceRepository(self._resource_connection).get(RESOURCE_LOCAL_ORCA)
        if slot is None or slot.status == "free":
            return True
        return slot.status == "held" and slot.owner_execution_id == getattr(
            state, "current_execution_id", None
        )

    def _release_resource_if_terminal(self, run_id: RunId) -> None:
        try:
            view = self.service.inspect(run_id)
        except Exception:
            return
        if view.state.phase not in {P5Phase.COMPLETED, P5Phase.FAILED, P5Phase.CANCELLED}:
            return
        if view.binding is None or view.binding.backend_kind != "local_orca" or view.job is None:
            return
        with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
            uow.begin()
            ExecutionResourceRepository(uow.connection).release(
                execution_id=str(view.job.execution_id),
                generation=view.job.launch_generation,
                now_utc=format_utc(self.service.clock.now_utc()),
                evidence_ref=f"p5:{run_id}:{view.state.phase.value}",
            )
            uow.commit()

    def _mark_resource_unknown(self, run_id: RunId, reason: str) -> None:
        try:
            view = self.service.inspect(run_id)
        except Exception:
            return
        if view.binding is None or view.binding.backend_kind != "local_orca" or view.job is None:
            return
        with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
            uow.begin()
            ExecutionResourceRepository(uow.connection).mark_unknown(
                execution_id=str(view.job.execution_id),
                generation=view.job.launch_generation,
                evidence_ref=f"p5:{run_id}:{reason[:120]}",
            )
            uow.commit()

    def _dispatch(
        self,
        run_id: RunId,
        view: P5RunView,
        permit,
        *,
        runtime: _ExecutionRuntimeSelection | None = None,
    ) -> P5DeliveryReport:
        runtime = runtime or self.service._restore_execution_runtime(run_id)
        if view.action is None or view.binding is None or view.state.current_action_id is None:
            return P5DeliveryReport(None, "action_missing", "failed")
        if view.binding.backend_kind == "local_orca" and not self.allow_real_orca:
            return P5DeliveryReport(None, "real_execution_disabled", view.state.phase.value)
        if (
            view.binding.backend_kind == "local_orca"
            and self.service._read_p4_source(
                view.state.source_run_id
            ).confirmed_molecule.provider.value
            == "fake"
        ):
            return P5DeliveryReport(None, "source_integrity_error", "failed")
        try:
            with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
                uow.begin()
                uow.outbox.validate_handler_permit(permit=permit, now=self.service.clock.now_utc())
                snapshot = self.service._verified_snapshot(uow, run_id)
                state = self.service._p5_state(snapshot)
                if permit.effect.payload.get("action_id") != str(state.current_action_id):
                    raise LaunchStateUnknown("dispatch action or phase has changed")
                if state.phase is not P5Phase.DISPATCH_PENDING:
                    # Recover only the short delivery acknowledgement. Never
                    # call start_or_reconcile from this crash-recovery branch.
                    existing = LocalJobRepository(uow.connection).get_by_action(
                        state.current_action_id
                    )
                    if (
                        existing is None
                        or existing.execution_id != state.current_execution_id
                        or existing.launch_consumed_at_utc is None
                        or existing.binding_hash != permit.effect.payload.get("binding_hash")
                    ):
                        raise LaunchStateUnknown("no consumed reservation to acknowledge")
                    uow.commit()
                    return P5DeliveryReport(
                        existing.execution_id, "launch_acknowledged", state.phase.value, 0
                    )
                if state.current_grant_id is None:
                    raise ApprovalMismatch("P5 action has no approval grant")
                records = P5RecordRepository(uow.connection)
                grant_entry = records.latest_p5(
                    run_id=run_id, record_type="p5.approval_grant", model_type=P5ApprovalGrant
                )
                if grant_entry is None or grant_entry[1].grant_id != state.current_grant_id:
                    raise ApprovalMismatch("P5 approval grant is missing")
                grant = grant_entry[1]
                if grant.expires_at_utc <= self.service.clock.now_utc():
                    raise ApprovalExpired("P5 approval grant has expired before launch")
                action = records.get_exact_p5(
                    run_id=run_id,
                    record_id=self.service._record_id_for_action(
                        records, run_id, state.current_action_id
                    ),
                    record_type="p5.action",
                    model_type=P5ActionRecord,
                )
                binding = records.get_exact_p5(
                    run_id=run_id,
                    record_id=action.binding_id,
                    record_type="p5.execution_binding",
                    model_type=P5ExecutionBinding,
                )
                plan = records.get_exact_p5(
                    run_id=run_id,
                    record_id=state.execution_plan_id,
                    record_type="p5.execution_plan",
                    model_type=P5ExecutionPlan,
                )
                artifacts = ArtifactStore(self.service.state_root)
                artifact_records = ArtifactRecordRepository(uow.connection)
                input_record = artifact_records.get(binding.geometry_artifact_id)
                input_bytes_record = artifact_records.get(action.input_artifact_id)
                if input_record is None or input_bytes_record is None:
                    raise StateIntegrityError("P5 action artifacts are missing")
                geometry_bytes = artifacts.read(input_record)
                input_bytes = artifacts.read(input_bytes_record)
                node = next(item for item in plan.nodes if item.node_id == action.node_id)
                protocol = get_p5_protocol(plan.protocol_id)
                if not protocol.budget_is_registered(node.budget, node.kind):
                    raise StateIntegrityError(
                        "P5 node budget does not match its registered protocol"
                    )
                runtime_payload = runtime_config(
                    state_root=self.service.state_root,
                    executable=runtime.executable,
                    orca_version=runtime.orca_version,
                    profile_hash=binding.feature_profile_hash,
                    probe=False,
                    nprocs=node.budget.nprocs,
                    implicit_threads=protocol.implicit_threads,
                    parallel=protocol.parallel,
                )
                if runtime_payload["runtime_config_hash"] != binding.runtime_config_hash:
                    raise StateIntegrityError(
                        "P5 runtime configuration does not match its trusted binding"
                    )
                job = LocalJobRepository(uow.connection).get_by_action(action.action_id)
                if job is None:
                    execution_id = new_id(ExecutionId)
                    job = P5JobRecord(
                        job_id=new_id(JobId),
                        run_id=run_id,
                        action_id=action.action_id,
                        execution_id=execution_id,
                        idempotency_key=f"p5:{run_id}:{action.action_id}",
                        binding_id=binding.record_id,
                        binding_hash=binding.binding_hash,
                        input_manifest_hash=binding.input_manifest_hash,
                        geometry_hash=binding.geometry_hash,
                        backend_kind=binding.backend_kind,
                        launch_token=uuid.uuid4().hex,
                        launch_generation=permit.generation,
                        launch_reserved_at_utc=self.service.clock.now_utc(),
                        status=P5JobStatus.RESERVED,
                        host_identity=host_identity(),
                        executable_sha256=binding.executable_sha256,
                        job_directory_id=str(execution_id),
                        deadline_utc=self.service.clock.now_utc()
                        + timedelta(seconds=binding.budget.wall_time_seconds),
                    )
                    LocalJobRepository(uow.connection).insert(job)
                    next_state = state.model_copy(
                        update={
                            "phase": P5Phase.DISPATCH_PENDING,
                            "current_execution_id": execution_id,
                            "last_outcome_code": "launch_reserved",
                        }
                    )
                    self.service._append_event(
                        uow,
                        snapshot=snapshot,
                        next_state=next_state,
                        command_id=new_id(CommandId),
                        command_hash=sha256_hex({"reserve": str(execution_id)}),
                        command_type=P5CommandType.LAUNCH_ORCA,
                        event_type=P5EventType.JOB_RESERVED,
                        outcome_code="launch_reserved",
                        now=self.service.clock.now_utc(),
                        details={"execution_id": str(execution_id), "job_id": str(job.job_id)},
                    )
                    snapshot = self.service._verified_snapshot(uow, run_id)
                    state = self.service._p5_state(snapshot)
                else:
                    execution_id = job.execution_id
                if binding.backend_kind == "local_orca":
                    resources = ExecutionResourceRepository(uow.connection)
                    slot = resources.get(RESOURCE_LOCAL_ORCA)
                    if slot is None:
                        raise StateIntegrityError("local ORCA resource slot is missing")
                    if slot.status == "unknown" and slot.owner_execution_id == str(execution_id):
                        raise LaunchStateUnknown(
                            "the local ORCA resource is held by an unknown launch state"
                        )
                    if slot.status == "held":
                        if slot.owner_execution_id != str(execution_id) or (
                            slot.owner_generation != permit.generation
                        ):
                            raise ResourceUnavailable(
                                "the project local ORCA resource is held by another execution"
                            )
                    elif slot.status == "free":
                        if not resources.try_acquire(
                            execution_id=str(execution_id),
                            generation=permit.generation,
                            host_identity=job.host_identity,
                            now_utc=format_utc(self.service.clock.now_utc()),
                        ):
                            raise ResourceUnavailable(
                                "the project local ORCA resource became busy before launch"
                            )
                    else:
                        raise StateIntegrityError("local ORCA resource slot has an invalid state")
                uow.commit()
            request = LaunchRequest(
                state_root=self.service.state_root,
                job=job,
                binding=binding,
                node=node,
                input_bytes=input_bytes,
                geometry_bytes=geometry_bytes,
                executable=runtime.executable,
                allow_real=self.allow_real_orca,
                requested_at_utc=self.service.clock.now_utc(),
                permit=permit,
                runtime_config=runtime_payload,
            )
            observation = runtime.backend.start_or_reconcile(request)
            if observation.status is P5JobStatus.NEEDS_RECONCILIATION:
                current = self.service.inspect(run_id)
                result = self.service._mark_needs_reconciliation(
                    run_id=run_id,
                    execution_id=execution_id,
                    command_id=None,
                    command_hash=sha256_hex(
                        {"launch_state_unknown": str(execution_id), "backend": True}
                    ),
                    expected_revision=current.revision,
                    reason=observation.message or "backend returned an unknown launch state",
                )
                self._mark_resource_unknown(
                    run_id,
                    observation.message or "backend returned an unknown launch state",
                )
                refreshed = self.service.inspect(run_id)
                return P5DeliveryReport(
                    execution_id,
                    result.code,
                    refreshed.state.phase.value,
                )
            with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
                uow.begin()
                snapshot = self.service._verified_snapshot(uow, run_id)
                state = self.service._p5_state(snapshot)
                runtime_status = (
                    observation.status
                    if observation.status
                    not in {
                        P5JobStatus.SUCCEEDED,
                        P5JobStatus.FAILED,
                        P5JobStatus.CANCELLED,
                        P5JobStatus.TIMED_OUT,
                        P5JobStatus.INTERRUPTED,
                    }
                    else P5JobStatus.RUNNING
                )
                LocalJobRepository(uow.connection).update_runtime(
                    execution_id=execution_id,
                    status=runtime_status,
                    supervisor_pid=observation.supervisor_pid,
                    orca_pid=observation.orca_pid,
                    last_observation_sequence=1,
                    last_observation_hash=sha256_hex(
                        {"status": observation.status.value, "started": observation.started}
                    ),
                )
                next_phase = (
                    P5Phase.COLLECTING
                    if observation.status
                    in {
                        P5JobStatus.SUCCEEDED,
                        P5JobStatus.FAILED,
                        P5JobStatus.CANCELLED,
                        P5JobStatus.TIMED_OUT,
                        P5JobStatus.INTERRUPTED,
                    }
                    else P5Phase.RUNNING
                )
                next_state = state.model_copy(
                    update={
                        "phase": next_phase,
                        "last_outcome_code": "job_started"
                        if observation.started
                        else "job_reconciled",
                    }
                )
                self.service._append_event(
                    uow,
                    snapshot=snapshot,
                    next_state=next_state,
                    command_id=new_id(CommandId),
                    command_hash=sha256_hex(
                        {"observe": str(execution_id), "status": observation.status.value}
                    ),
                    command_type=P5CommandType.OBSERVE_JOB,
                    event_type=P5EventType.JOB_OBSERVED,
                    outcome_code="job_started",
                    now=self.service.clock.now_utc(),
                    details={"execution_id": str(execution_id), "status": observation.status.value},
                )
                uow.commit()
            refreshed = self.service.inspect(run_id)
            return P5DeliveryReport(
                execution_id,
                observation.status.value,
                refreshed.state.phase.value,
                observation.physical_start_count,
            )
        except ResourceUnavailable as error:
            return P5DeliveryReport(None, error.code, view.state.phase.value)
        except Exception as error:
            if isinstance(error, LaunchStateUnknown):
                try:
                    current = self.service.inspect(run_id)
                    if current.state.current_execution_id is None:
                        raise error
                    result = self.service._mark_needs_reconciliation(
                        run_id=run_id,
                        execution_id=current.state.current_execution_id,
                        command_id=None,
                        command_hash=sha256_hex(
                            {"launch_state_unknown": str(current.state.current_execution_id)}
                        ),
                        expected_revision=current.revision,
                        reason=str(error),
                    )
                    refreshed = self.service.inspect(run_id)
                    self._mark_resource_unknown(run_id, str(error))
                    return P5DeliveryReport(
                        current.state.current_execution_id,
                        result.code,
                        refreshed.state.phase.value,
                    )
                except Exception as reconciliation_error:
                    error = reconciliation_error
            if view.binding is not None and view.binding.backend_kind == "local_orca":
                self._mark_resource_unknown(run_id, str(error))
            return P5DeliveryReport(None, getattr(error, "code", "p5_dispatch_error"), "failed")

    def _pending_runs(self) -> list[RunId]:
        if not self.service.database_path.exists():
            return []
        with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
            uow.begin()
            rows = uow.connection.execute(
                "SELECT run_id FROM runs WHERE schema_version = ? "
                "AND engine_version = ? AND json_extract(state_json, '$.phase') IN "
                "('dispatch_pending', 'running', 'collecting', 'cancelling', "
                "'needs_reconciliation') "
                "ORDER BY updated_at_utc, run_id",
                (P5_SCHEMA_VERSION, P5_ENGINE_VERSION),
            ).fetchall()
            values = [RunId(str(row[0])) for row in rows]
            uow.commit()
            return values


class _LaunchResourceReadiness:
    """Outbox claim hook that shares the claim transaction's SQLite handle."""

    def __init__(self, worker: P5Worker) -> None:
        self.worker = worker

    def bind_connection(self, connection) -> None:
        self.worker._resource_connection = connection

    def __call__(self, effect, snapshot, now) -> bool:
        return self.worker._resource_ready(effect, snapshot, now)


__all__ = ["P5ApplicationService", "P5DeliveryReport", "P5RunView", "P5Worker"]
