"""P7 task lifecycle and bounded P4/P5/P6 coordination."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from orca_agent.application.errors import (
    InvalidTransitionError,
    RevisionConflictError,
    StateIntegrityError,
)
from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.application.p6_service import P6ApplicationService
from orca_agent.application.p7_preparation_service import P7PreparationService
from orca_agent.application.p7_runtime_config import P7RuntimeConfig
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    CommandId,
    ConversationId,
    InterruptId,
    RunId,
    TaskId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p4 import IdentityDecision, IdentityProvider, P4Phase
from orca_agent.domain.p5 import GeometryRecord, P5Phase
from orca_agent.domain.p6 import P6Phase
from orca_agent.domain.p7_conversation import MoleculeInputType
from orca_agent.domain.p7_planning import PlanFeedback, PlanningRecord
from orca_agent.domain.p7_preparation import (
    PreparationHandoff,
    PreparationStatus,
    PreparedCalculation,
)
from orca_agent.domain.p7_task import (
    CalculationRequest,
    DeliveryRecord,
    HandoffRecord,
    OutputSpec,
    PendingActionRecord,
    StopReason,
    TaskPhase,
    TaskRecord,
    ValidationStatus,
)
from orca_agent.domain.workflow_authorization import WorkflowExecutionAuthorization
from orca_agent.identity.geometry import bind_initial_geometry
from orca_agent.infrastructure.clock import Clock, SystemClock, format_utc, parse_utc
from orca_agent.infrastructure.p7_records import P7RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.p4_commands import (
    CancelPlanningRun,
    ConfirmMoleculeIdentity,
    StartPlanningRun,
)
from orca_agent.orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from orca_agent.orchestration.p6_commands import AssessP6Run, CancelP6Run
from orca_agent.planning.p5_protocols import get_p5_protocol
from orca_agent.planning.p7_catalog import CapabilityCatalog, build_capability_catalog
from orca_agent.planning.p7_parameter_policy import MoleculePrecheck, default_parameter_policy
from orca_agent.presentation.p7_plans import build_execution_display
from orca_agent.presentation.p7_results import P7ResultPresenter

_P4_PROTOCOL_ID = "ground_state_baseline_r2scan3c_v1"
_P6_PROFILE_ID = "p6.nonlinear.r2scan3c.v1"


def _now(clock: Clock) -> datetime:
    return clock.now_utc()


def _as_json(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return thaw_json(value)


class P7TaskService:
    """Own P7 task state while delegating actual workflow effects to P4/P5/P6."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        catalog: CapabilityCatalog | None = None,
        p4_service: P4ApplicationService | None = None,
        p5_service: P5ApplicationService | None = None,
        p6_service: P6ApplicationService | None = None,
        allow_network: bool = False,
        backend_kind: str = "fake",
        allow_real_orca: bool = False,
        runtime_config: P7RuntimeConfig | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.clock = clock or SystemClock()
        self.runtime_config = runtime_config or P7RuntimeConfig.legacy(
            self.state_root,
            planner_name="baseline",
            allow_llm=False,
            allow_network=allow_network,
            backend_kind=backend_kind,
            allow_real_orca=allow_real_orca,
        )
        allow_network = self.runtime_config.allow_network
        backend_kind = self.runtime_config.backend_kind
        allow_real_orca = self.runtime_config.allow_real_orca
        self.catalog = catalog or build_capability_catalog()
        self.p4 = p4_service or P4ApplicationService(
            self.state_root,
            clock=self.clock,
            fake_adapter=_fake_pubchem(),
            allow_network=allow_network,
        )
        self.preparation = P7PreparationService(
            self.state_root,
            p4_service=self.p4,
            runtime_config=self.runtime_config,
            clock=self.clock,
        )
        self.p5 = p5_service or P5ApplicationService(
            self.state_root,
            clock=self.clock,
            backend_kind=backend_kind,
            orca_executable=self.runtime_config.orca_executable,
            orca_version=self.runtime_config.orca_version,
            allow_real_orca=allow_real_orca,
            required_orca_version=(
                self.runtime_config.expected_orca_version
                if self.runtime_config.profile == "real"
                else None
            ),
        )
        self.p6 = p6_service or P6ApplicationService(self.state_root, clock=self.clock)
        self.presenter = P7ResultPresenter()
        self.allow_real_orca = allow_real_orca

    # Read helpers ----------------------------------------------------
    def get_task(self, conversation_id: ConversationId | str, task_id: str) -> TaskRecord | None:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            task = P7RecordRepository(uow.connection).get_task(str(conversation_id), str(task_id))
            uow.commit()
            return task

    def list_tasks(self, conversation_id: ConversationId | str) -> tuple[TaskRecord, ...]:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            tasks = P7RecordRepository(uow.connection).list_tasks(str(conversation_id))
            uow.commit()
            return tasks

    def trusted_opt_sources(
        self, conversation_id: ConversationId | str
    ) -> dict[str, dict[str, object]]:
        """Return verified, session-local Opt sources for a follow-up plan.

        The mapping is only a planning hint.  P5 repeats the authoritative
        result, geometry, identity, and source-run checks before accepting the
        new execution handoff.
        """

        conversation = str(conversation_id)
        tasks = self.list_tasks(conversation)
        try:
            state = self.get_conversation_state(conversation)
        except AttributeError:
            state = None
        active_task_id = None if state is None else state.active_task_id
        sources: dict[str, dict[str, object]] = {}
        for task in tasks:
            if task.p5_run_id is None or task.p4_run_id is None:
                continue
            try:
                view = self.p5.inspect(RunId(task.p5_run_id))
            except Exception:
                continue
            if view.state.phase is not P5Phase.COMPLETED:
                continue
            result = next(
                (
                    item
                    for item in reversed(view.results)
                    if getattr(getattr(item, "primitive", None), "value", item.primitive) == "opt"
                    and getattr(getattr(item, "parse_status", None), "value", None) == "complete"
                    and getattr(item, "optimized_geometry_artifact_id", None) is not None
                ),
                None,
            )
            if result is None:
                continue
            value = {
                "task_id": task.task_id,
                "alias": task.alias,
                "result_id": str(result.record_id),
                "source_run_id": str(view.plan.source_run_id),
                "source_p5_run_id": str(view.run_id),
                "optimized_geometry_artifact_id": str(result.optimized_geometry_artifact_id),
                "protocol_id": view.plan.protocol_id,
                "protocol_hash": view.plan.protocol_hash,
                "confirmed_molecule_hash": view.plan.confirmed_molecule_hash,
                "completed": True,
            }
            sources[task.alias.casefold()] = value
            sources[task.task_id.casefold()] = value
            if active_task_id == task.task_id or len(tasks) == 1:
                sources["current_task"] = value
        return sources

    def get_conversation_state(self, conversation_id: str):
        """Small read helper kept local to avoid coupling planning to P7 conversation service."""

        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            state = P7RecordRepository(uow.connection).get_conversation(conversation_id)
            uow.commit()
        return state

    def task_view(self, conversation_id: ConversationId | str, task_id: str) -> dict[str, object]:
        task = self.get_task(conversation_id, task_id)
        if task is None:
            raise ValueError("task was not found in this conversation")
        return self._view(task)

    def _view(self, task: TaskRecord) -> dict[str, object]:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            pending = records.list_pending(task.conversation_id)
            delivery = records.get_delivery(task.task_id)
            planning = records.get_planning_record_for_task(task.task_id, task.revision)
            if planning is None and task.request is not None and task.plan is not None:
                planning = records.get_planning_record_for_plan(
                    task.task_id,
                    task.request.request_hash,
                    task.plan.plan_hash,
                )
            preparation = records.get_prepared_for_task(
                task.task_id,
                task.preparation_generation if task.preparation_generation > 0 else None,
            )
            authorization = (
                None
                if task.final_authorization_id is None
                else records.get_execution_authorization(task.final_authorization_id)
            )
            uow.commit()
        if task.final_authorization_id is not None and authorization is None:
            raise StateIntegrityError("task final authorization record is missing")
        result: dict[str, object] = {
            "task": task.model_dump(mode="json"),
            "pending_actions": [
                item.model_dump(mode="json") for item in pending if item.task_id == task.task_id
            ],
            "delivery": None if delivery is None else delivery.model_dump(mode="json"),
            "planning_record": (None if planning is None else planning.model_dump(mode="json")),
            "preparation": (None if preparation is None else preparation.model_dump(mode="json")),
            "execution_authorization": (
                None if authorization is None else authorization.model_dump(mode="json")
            ),
        }
        if task.state is TaskPhase.PLAN_READY and not self.runtime_config.execution_ready:
            result["execution_blocked"] = {
                "code": "execution_not_ready",
                "reasons": list(self.runtime_config.execution_readiness_reasons),
            }
        if task.p4_run_id:
            result["p4"] = self._safe_inspect(self.p4.inspect, RunId(task.p4_run_id))
        if task.p5_run_id:
            result["p5"] = self._safe_inspect(self.p5.inspect, RunId(task.p5_run_id))
        if task.p6_run_id:
            result["p6"] = self._safe_inspect(self.p6.inspect, RunId(task.p6_run_id))
        result["plan_display"] = build_execution_display(
            task,
            planning_record=planning,
            p4=result.get("p4") if isinstance(result.get("p4"), dict) else None,
            p5=result.get("p5") if isinstance(result.get("p5"), dict) else None,
            p6=result.get("p6") if isinstance(result.get("p6"), dict) else None,
        )
        return result

    @staticmethod
    def _safe_inspect(function, run_id: RunId) -> object:
        try:
            value = function(run_id)
            return _as_json(value)
        except Exception as error:
            return {"available": False, "error": type(error).__name__}

    @staticmethod
    def _state_for_normalized(normalized) -> TaskPhase:
        if normalized.validation.status is ValidationStatus.VALID:
            return TaskPhase.PLAN_READY
        if normalized.validation.status is ValidationStatus.NEEDS_CLARIFICATION:
            return TaskPhase.NEEDS_CLARIFICATION
        # v4 unsupported/invalid drafts remain editable so a clarification or
        # replacement parameter can be applied to the same task.  Historical
        # legacy callers retain their terminal unsupported semantics.
        if getattr(normalized, "draft_semantics_version", "legacy") == "p7.draft.v4":
            return TaskPhase.NEEDS_CLARIFICATION
        return TaskPhase.ENDED_WITHOUT_RESULT

    # Task creation and token handling -------------------------------
    def create_task(
        self,
        conversation_id: ConversationId | str,
        *,
        alias: str | None,
        normalized,
        turn_id: str,
        preparation_route: bool = False,
    ) -> dict[str, object]:
        if normalized.request is None or normalized.validation is None:
            raise ValueError("normalized P7 plan is incomplete")
        conversation = str(conversation_id)
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            chosen_alias = self._unique_alias(records, conversation, alias)
            state = self._state_for_normalized(normalized)
            task = TaskRecord(
                task_id=str(new_id(TaskId)),
                conversation_id=conversation,
                alias=chosen_alias,
                revision=1,
                state=state,
                stop_reason=(
                    StopReason.UNSUPPORTED
                    if normalized.validation.status is ValidationStatus.UNSUPPORTED
                    and getattr(normalized, "draft_semantics_version", "legacy") == "legacy"
                    else None
                ),
                request=normalized.request,
                plan=normalized.plan,
                validation=normalized.validation,
                delivery_output_spec=normalized.request.output_spec,
                p4_run_id=self._reused_p4_run_id(records, conversation, normalized.source_task_id),
                created_at_utc=now,
                updated_at_utc=now,
            )
            records.insert_task(task)
            planning_record = self._insert_planning_record(
                records,
                task=task,
                normalized=normalized,
                turn_id=turn_id,
                now=now,
            )
            records.ensure_task_link(
                conversation_id=conversation,
                task_id=task.task_id,
                link_kind="active",
                now=now,
            )
            if (
                state is TaskPhase.PLAN_READY
                and self.runtime_config.execution_ready
                and not preparation_route
            ):
                execution_profile = self._plan_execution_profile(normalized.plan.protocol_id)
                self._insert_pending(
                    records,
                    conversation_id=conversation,
                    task_id=task.task_id,
                    action_type="accept_plan",
                    target_id=task.task_id,
                    expected_revision=task.revision,
                    payload=self._plan_acceptance_payload(
                        task, normalized, execution_profile, planning_record=planning_record
                    ),
                    now=now,
                )
            uow.commit()
        return self._view(task)

    def revise_draft(
        self,
        conversation_id: ConversationId | str,
        task_id: str,
        *,
        normalized,
        turn_id: str,
        expected_revision: int,
        preparation_route: bool = False,
    ) -> dict[str, object]:
        """Apply one validated sparse draft revision atomically."""

        if normalized.request is None or normalized.validation is None:
            raise ValueError("normalized P7 plan is incomplete")
        conversation = str(conversation_id)
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            current = records.get_task(conversation, task_id)
            if current is None:
                raise ValueError("draft task disappeared")
            if current.revision != expected_revision:
                raise RevisionConflictError("draft revision is stale")
            if (
                current.state
                not in {TaskPhase.NEEDS_CLARIFICATION, TaskPhase.DRAFT, TaskPhase.PLAN_READY}
                or current.accepted_plan_hash is not None
                or current.p5_run_id is not None
            ):
                raise InvalidTransitionError("task draft is no longer editable")
            state = self._state_for_normalized(normalized)
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "state": state,
                    "stop_reason": None
                    if getattr(normalized, "draft_semantics_version", "legacy") == "p7.draft.v4"
                    else StopReason.UNSUPPORTED
                    if normalized.validation.status is ValidationStatus.UNSUPPORTED
                    else None,
                    "request": normalized.request,
                    "plan": normalized.plan,
                    "validation": normalized.validation,
                    "accepted_plan_hash": None,
                    "accepted_output_spec_hash": None,
                    "delivery_output_spec": normalized.request.output_spec,
                    "p4_run_id": None,
                    "p5_run_id": None,
                    "p6_run_id": None,
                    "prepared_calculation_id": None,
                    "final_authorization_id": None,
                    "current_delivery_id": None,
                    "updated_at_utc": now,
                }
            )
            records.stale_task_pending(current.task_id, new_revision=updated.revision)
            if not records.update_task(updated, expected_revision=current.revision):
                raise RevisionConflictError("draft revision raced with another update")
            planning_record = self._insert_planning_record(
                records,
                task=updated,
                normalized=normalized,
                turn_id=turn_id,
                now=now,
            )
            if (
                state is TaskPhase.PLAN_READY
                and self.runtime_config.execution_ready
                and not preparation_route
            ):
                execution_profile = self._plan_execution_profile(normalized.plan.protocol_id)
                self._insert_pending(
                    records,
                    conversation_id=conversation,
                    task_id=updated.task_id,
                    action_type="accept_plan",
                    target_id=updated.task_id,
                    expected_revision=updated.revision,
                    payload=self._plan_acceptance_payload(
                        updated, normalized, execution_profile, planning_record=planning_record
                    ),
                    now=now,
                )
            uow.commit()
        return self._view(updated)

    def prepare_v5(self, conversation_id: ConversationId | str, task_id: str) -> dict[str, object]:
        """Run the additive P7 preparation stage for one task generation.

        Preparation is deliberately outside the draft-creation transaction:
        provider lookup and RDKit are bounded external work, while the
        resulting snapshot and the final confirmation token are committed
        together afterward.
        """

        conversation = str(conversation_id)
        task = self.get_task(conversation, task_id)
        if task is None:
            raise ValueError("task was not found in this conversation")
        if task.request is None or task.validation is None or task.plan is None:
            raise InvalidTransitionError("task is not ready for P7 preparation")
        existing_snapshot = self._prepared_snapshot_for_task(task)
        if existing_snapshot is not None:
            if (
                existing_snapshot.task_revision != task.revision
                or existing_snapshot.request_hash != task.request.request_hash
                or existing_snapshot.plan_hash != task.plan.plan_hash
            ):
                raise StateIntegrityError(
                    "existing preparation snapshot does not match the current task revision"
                )
            # Preparation is an immutable, versioned boundary.  A replay
            # after a crash must reuse its persisted P4/RDKit/input facts and
            # only repair missing handoff/token projections.
            self._ensure_preparation_handoffs(existing_snapshot)
            self._ensure_preparation_confirmation(task, existing_snapshot)
            current = self.get_task(conversation, task_id) or task
            return self._view(current)
        generation = task.preparation_generation + 1
        planning = self._planning_record(task)
        source_task = (
            None
            if planning is None or planning.source_task_id is None
            else self.get_task(conversation, planning.source_task_id)
        )
        source_required = planning is not None and (
            planning.source_task_id is not None or planning.external_opt_result_id is not None
        )
        source_reference = (
            None if planning is None else planning.source_task_id or planning.external_opt_result_id
        )
        source_geometry = None
        source_geometry_bytes = None
        source_result_id = None if planning is None else planning.external_opt_result_id
        if source_required:
            if source_task is not None:
                source = self._source_opt_geometry(source_task)
                if source is not None:
                    source_geometry, source_geometry_bytes = source
            snapshot = self.preparation.prepare(
                task,
                generation=generation,
                source_task=source_task,
                source_geometry=source_geometry,
                source_geometry_bytes=source_geometry_bytes,
                source_required=True,
                source_reference=source_reference,
                source_result_id=source_result_id,
            )
        elif source_task is not None:
            source = self._source_opt_geometry(source_task)
            if source is not None:
                source_geometry, source_geometry_bytes = source
            snapshot = self.preparation.prepare(
                task,
                generation=generation,
                source_task=source_task,
                source_geometry=source_geometry,
                source_geometry_bytes=source_geometry_bytes,
                source_result_id=source_result_id,
            )
        else:
            snapshot = self.preparation.prepare(task, generation=generation)

        p4_run_id = thaw_json(snapshot.dependencies).get("p4_run_id")
        if p4_run_id is not None and not isinstance(p4_run_id, str):
            raise StateIntegrityError("preparation snapshot P4 dependency is invalid")
        next_state = (
            TaskPhase.PLAN_READY
            if snapshot.status is PreparationStatus.READY
            else TaskPhase.NEEDS_CLARIFICATION
        )
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            existing = records.get_prepared_for_task(task.task_id, generation)
            if existing is not None:
                uow.commit()
                self._ensure_preparation_handoffs(existing)
                return self._view(self.get_task(conversation, task_id) or task)
            records.insert_prepared_calculation(snapshot)
            current = records.get_task(conversation, task.task_id)
            if current is None:
                raise StateIntegrityError("task disappeared during P7 preparation")
            updated = current.model_copy(
                update={
                    "state": next_state,
                    "preparation_generation": generation,
                    "prepared_calculation_id": snapshot.prepared_id,
                    "p4_run_id": p4_run_id or current.p4_run_id,
                    "updated_at_utc": now,
                }
            )
            if not records.update_task(updated, expected_revision=current.revision):
                raise RevisionConflictError("task changed during P7 preparation")
            self._insert_preparation_handoffs(records, snapshot, now=now)
            if snapshot.status is PreparationStatus.READY and self.runtime_config.execution_ready:
                self._insert_pending(
                    records,
                    conversation_id=conversation,
                    task_id=updated.task_id,
                    action_type="confirm_execution",
                    target_id=snapshot.prepared_id,
                    expected_revision=updated.revision,
                    payload=self._final_confirmation_payload(updated, snapshot),
                    now=now,
                )
            uow.commit()
        return self._view(self.get_task(conversation, task_id) or updated)

    def _ensure_preparation_confirmation(
        self, task: TaskRecord, snapshot: PreparedCalculation
    ) -> None:
        if (
            snapshot.status is not PreparationStatus.READY
            or not self.runtime_config.execution_ready
        ):
            return
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            current = records.get_task(task.conversation_id, task.task_id)
            if current is None:
                raise StateIntegrityError(
                    "task disappeared while restoring preparation confirmation"
                )
            if current.prepared_calculation_id != snapshot.prepared_id:
                raise StateIntegrityError("preparation confirmation is bound to another snapshot")
            if current.final_authorization_id is None:
                self._insert_pending(
                    records,
                    conversation_id=current.conversation_id,
                    task_id=current.task_id,
                    action_type="confirm_execution",
                    target_id=snapshot.prepared_id,
                    expected_revision=current.revision,
                    payload=self._final_confirmation_payload(current, snapshot),
                    now=now,
                )
            uow.commit()

    def _ensure_preparation_handoffs(self, snapshot: PreparedCalculation) -> None:
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            self._insert_preparation_handoffs(P7RecordRepository(uow.connection), snapshot, now=now)
            uow.commit()

    @staticmethod
    def _insert_preparation_handoffs(
        records: P7RecordRepository, snapshot: PreparedCalculation, *, now: datetime
    ) -> None:
        dependencies = thaw_json(snapshot.dependencies)
        if not isinstance(dependencies, dict):
            raise StateIntegrityError("preparation dependencies are invalid")
        p4_run_id = dependencies.get("p4_run_id")
        p4_command_id = dependencies.get("p4_command_id")
        if p4_run_id is not None and not isinstance(p4_run_id, str):
            raise StateIntegrityError("preparation P4 run binding is invalid")
        if p4_command_id is not None and not isinstance(p4_command_id, str):
            raise StateIntegrityError("preparation P4 command binding is invalid")

        def insert(
            target: str,
            *,
            command_id: str,
            child_id: str,
            payload: dict[str, object],
            status: str,
        ) -> None:
            if (
                records.get_preparation_handoff(
                    snapshot.task_id, snapshot.preparation_generation, target
                )
                is not None
            ):
                return
            records.insert_preparation_handoff(
                PreparationHandoff.create(
                    handoff_id=f"p7prep_handoff_{uuid.uuid4().hex}",
                    task_id=snapshot.task_id,
                    preparation_generation=snapshot.preparation_generation,
                    target=target,
                    command_id=command_id,
                    child_id=child_id,
                    expected_revision=snapshot.task_revision,
                    payload=payload,
                    status=status,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )

        identity_status = "linked" if p4_run_id is not None else "failed"
        insert(
            "identity",
            command_id=p4_command_id or f"p7prep_identity_{snapshot.prepared_id}",
            child_id=p4_run_id or snapshot.prepared_id,
            payload={
                "prepared_id": snapshot.prepared_id,
                "snapshot_hash": snapshot.snapshot_hash,
                "p4_run_id": p4_run_id,
                "p4_command_id": p4_command_id,
                "identity_snapshot_hash": sha256_hex(snapshot.identity_snapshot),
            },
            status=identity_status,
        )
        if snapshot.status is not PreparationStatus.READY:
            return
        draft = thaw_json(snapshot.geometry_draft or {})
        draft_hash = (
            draft.get("draft_hash")
            if snapshot.geometry_source == "rdkit_initial" and isinstance(draft, dict)
            else None
        )
        insert(
            "geometry",
            command_id=f"p7prep_geometry_{snapshot.prepared_id}",
            child_id=snapshot.prepared_id,
            payload={
                "prepared_id": snapshot.prepared_id,
                "snapshot_hash": snapshot.snapshot_hash,
                "geometry_source": snapshot.geometry_source,
                "geometry_hash": snapshot.geometry_hash,
                "xyz_bytes_sha256": snapshot.xyz_bytes_sha256,
                "draft_hash": draft_hash,
            },
            status="linked",
        )
        if snapshot.first_input_hash is not None:
            preview = thaw_json(snapshot.first_input_preview or {})
            insert(
                "preview",
                command_id=f"p7prep_preview_{snapshot.prepared_id}",
                child_id=snapshot.prepared_id,
                payload={
                    "prepared_id": snapshot.prepared_id,
                    "snapshot_hash": snapshot.snapshot_hash,
                    "first_input_hash": snapshot.first_input_hash,
                    "input_sha256": preview.get("input_sha256")
                    if isinstance(preview, dict)
                    else None,
                    "manifest_hash": preview.get("manifest_hash")
                    if isinstance(preview, dict)
                    else None,
                },
                status="linked",
            )

    def prepared_calculation(
        self, conversation_id: ConversationId | str, task_id: str
    ) -> PreparedCalculation | None:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            value = P7RecordRepository(uow.connection).get_prepared_for_task(task_id)
            uow.commit()
            return value

    def _source_opt_geometry(self, source_task: TaskRecord) -> tuple[GeometryRecord, bytes] | None:
        if source_task.p5_run_id is None or source_task.p4_run_id is None:
            return None
        view = self.p5.inspect(RunId(source_task.p5_run_id))
        if view.state.phase is not P5Phase.COMPLETED:
            return None
        completed_opt = next(
            (
                item
                for item in reversed(view.results)
                if item.primitive.value == "opt"
                and item.parse_status.value == "complete"
                and item.optimized_geometry_artifact_id is not None
            ),
            None,
        )
        if completed_opt is None:
            return None
        # P5 appends the optimized GeometryRecord in the same source run and
        # keeps the atom/identity binding immutable.  Select the newest
        # non-initial geometry only after verifying the source run is complete.
        if len(view.geometry) < 2:
            return None
        geometry = view.geometry[-1]
        if geometry.run_id != view.run_id:
            raise StateIntegrityError("source Opt geometry run binding is invalid")
        source = self.p4.inspect(RunId(source_task.p4_run_id))
        exact_geometry, exact_bytes, source_result = self.p5.load_optimized_geometry_source(
            WorkflowRecordId(str(completed_opt.record_id)),
            source=source,
            run_id=RunId(source_task.p5_run_id),
        )
        if source_result.record_id != completed_opt.record_id:
            raise StateIntegrityError("source Opt result binding changed during preparation")
        if exact_geometry.geometry_hash != geometry.geometry_hash:
            raise StateIntegrityError("source Opt geometry hash changed during preparation")
        return exact_geometry, exact_bytes

    def _final_confirmation_payload(
        self, task: TaskRecord, snapshot: PreparedCalculation
    ) -> dict[str, object]:
        return {
            "confirmation_type": "p7.final_execution_confirmation.v1",
            "prepared_id": snapshot.prepared_id,
            "task_id": task.task_id,
            "task_revision": snapshot.task_revision,
            "preparation_generation": snapshot.preparation_generation,
            "snapshot_hash": snapshot.snapshot_hash,
            "confirmation_hash": snapshot.confirmation_hash,
            "binding": snapshot.confirmation_binding(),
            "identity": thaw_json(snapshot.identity_snapshot),
            "parameters": thaw_json(snapshot.parameter_snapshot),
            "plan": thaw_json(snapshot.plan_snapshot or {}),
            "first_input_preview": thaw_json(snapshot.first_input_preview or {}),
            "readiness": thaw_json(snapshot.readiness),
            "execution_readiness": {
                "ready": self.runtime_config.execution_ready,
                "profile": self.runtime_config.profile,
                "reasons": list(self.runtime_config.execution_readiness_reasons),
            },
        }

    def _prepared_snapshot_for_task(self, task: TaskRecord) -> PreparedCalculation | None:
        if task.prepared_calculation_id is None:
            return None
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            snapshot = P7RecordRepository(uow.connection).get_prepared_calculation(
                task.prepared_calculation_id
            )
            uow.commit()
        if snapshot is not None and snapshot.task_id != task.task_id:
            raise StateIntegrityError("prepared calculation is bound to another task")
        return snapshot

    def _validated_prepared_snapshot(
        self, task: TaskRecord, payload: dict[str, object]
    ) -> PreparedCalculation:
        if payload.get("confirmation_type") != "p7.final_execution_confirmation.v1":
            raise StateIntegrityError("final confirmation type is invalid")
        prepared_id = payload.get("prepared_id")
        if not isinstance(prepared_id, str) or prepared_id != task.prepared_calculation_id:
            raise InvalidTransitionError(
                "final confirmation does not match the current preparation"
            )
        snapshot = self._prepared_snapshot_for_task(task)
        if snapshot is None or snapshot.prepared_id != prepared_id:
            raise StateIntegrityError("final confirmation preparation snapshot is missing")
        if snapshot.status is not PreparationStatus.READY:
            raise InvalidTransitionError("final confirmation requires a ready preparation snapshot")
        if task.revision < snapshot.task_revision:
            raise RevisionConflictError("final confirmation refers to a future task revision")
        if task.request is None or task.validation is None or task.plan is None:
            raise StateIntegrityError("final confirmation task projection is incomplete")
        if (
            task.request.request_hash != snapshot.request_hash
            or task.validation.validation_hash != snapshot.validation_hash
            or task.plan.plan_hash != snapshot.plan_hash
        ):
            raise InvalidTransitionError("final confirmation is stale for the current draft")
        if payload.get("task_id") != task.task_id:
            raise InvalidTransitionError("final confirmation task binding is invalid")
        if payload.get("task_revision") != snapshot.task_revision:
            raise InvalidTransitionError("final confirmation revision binding is invalid")
        if payload.get("preparation_generation") != snapshot.preparation_generation:
            raise InvalidTransitionError("final confirmation generation binding is invalid")
        if payload.get("snapshot_hash") != snapshot.snapshot_hash:
            raise InvalidTransitionError("final confirmation snapshot hash is invalid")
        if payload.get("confirmation_hash") != snapshot.confirmation_hash:
            raise InvalidTransitionError("final confirmation content hash is invalid")
        binding = thaw_json(payload.get("binding"))
        if binding != snapshot.confirmation_binding():
            raise InvalidTransitionError("final confirmation immutable binding is invalid")
        if (
            snapshot.confirmation_expires_at_utc is None
            or _now(self.clock) >= snapshot.confirmation_expires_at_utc
        ):
            raise InvalidTransitionError("final confirmation has expired")
        return snapshot

    def _persist_final_authorization(
        self, task: TaskRecord, snapshot: PreparedCalculation, now: datetime
    ) -> str:
        authorization_id = f"p7auth_{sha256_hex(snapshot.confirmation_binding())}"
        authorization = self._build_execution_authorization(
            task, snapshot, authorization_id=authorization_id, now=now
        )
        current = self.get_task(task.conversation_id, task.task_id)
        if current is None:
            raise StateIntegrityError("task disappeared while recording final authorization")
        if (
            current.final_authorization_id is not None
            and current.final_authorization_id != authorization_id
        ):
            raise InvalidTransitionError("task already has a different final authorization")
        if current.final_authorization_id is not None:
            with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
                uow.begin()
                existing = P7RecordRepository(uow.connection).get_execution_authorization(
                    authorization_id
                )
                uow.commit()
            if existing is None:
                raise StateIntegrityError("task final authorization record is missing")
            return authorization_id
        # The preparation snapshot binds the original draft revision.  The
        # authorization marker is an orthogonal projection update and must not
        # increment that revision before the derived P4/P5 handoffs are made.
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            latest = records.get_task(task.conversation_id, task.task_id)
            if latest is None:
                raise StateIntegrityError("task disappeared while recording final authorization")
            existing = records.get_execution_authorization(authorization_id)
            if existing is not None:
                if (
                    existing.task_id != latest.task_id
                    or existing.prepared_calculation_id != snapshot.prepared_id
                    or existing.prepared_snapshot_hash != snapshot.snapshot_hash
                    or existing.status == "revoked"
                ):
                    raise StateIntegrityError(
                        "existing final authorization is bound to another snapshot"
                    )
            else:
                records.insert_execution_authorization(authorization)
            if latest.final_authorization_id is None:
                authorized = latest.model_copy(
                    update={"final_authorization_id": authorization_id, "updated_at_utc": now}
                )
                if not records.update_task(authorized, expected_revision=latest.revision):
                    raise RevisionConflictError("task changed while recording final authorization")
            elif latest.final_authorization_id != authorization_id:
                raise InvalidTransitionError("task already has a different final authorization")
            uow.commit()
        return authorization_id

    def _build_execution_authorization(
        self,
        task: TaskRecord,
        snapshot: PreparedCalculation,
        *,
        authorization_id: str,
        now: datetime,
    ) -> WorkflowExecutionAuthorization:
        if task.plan is None:
            raise StateIntegrityError("execution authorization has no task plan")
        nodes = [item.model_dump(mode="json") for item in task.plan.nodes]
        node_credentials = {
            item.node_id: sha256_hex(
                {
                    "authorization_id": authorization_id,
                    "prepared_snapshot_hash": snapshot.snapshot_hash,
                    "node_id": item.node_id,
                    "node": item.model_dump(mode="json"),
                }
            )
            for item in task.plan.nodes
        }
        return WorkflowExecutionAuthorization.create(
            authorization_id=authorization_id,
            task_id=task.task_id,
            prepared_calculation_id=snapshot.prepared_id,
            task_revision=snapshot.task_revision,
            preparation_generation=snapshot.preparation_generation,
            prepared_snapshot_hash=snapshot.snapshot_hash,
            request_hash=snapshot.request_hash,
            validation_hash=snapshot.validation_hash or task.validation.validation_hash,
            plan_hash=snapshot.plan_hash or task.plan.plan_hash,
            geometry_hash=snapshot.geometry_hash or sha256_hex({"geometry": "absent"}),
            xyz_bytes_sha256=snapshot.xyz_bytes_sha256 or sha256_hex({"xyz": "absent"}),
            first_input_hash=snapshot.first_input_hash,
            allowed_nodes={"nodes": nodes, "protocol_id": task.plan.protocol_id},
            budget=thaw_json(task.plan.resources),
            geometry_policy={
                "source": snapshot.geometry_source,
                "geometry_hash": snapshot.geometry_hash,
                "xyz_bytes_sha256": snapshot.xyz_bytes_sha256,
                "draft_hash": thaw_json(snapshot.geometry_draft or {}).get("draft_hash")
                if snapshot.geometry_source == "rdkit_initial"
                and isinstance(thaw_json(snapshot.geometry_draft or {}), dict)
                else None,
            },
            node_credentials=node_credentials,
            issued_at_utc=now,
            expires_at_utc=snapshot.confirmation_expires_at_utc or now,
        )

    def _execution_authorization_for_task(
        self, task: TaskRecord, snapshot: PreparedCalculation | None = None
    ) -> WorkflowExecutionAuthorization | None:
        if task.final_authorization_id is None:
            return None
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            authorization = P7RecordRepository(uow.connection).get_execution_authorization(
                task.final_authorization_id
            )
            uow.commit()
        if authorization is None:
            raise StateIntegrityError("task final authorization record is missing")
        if authorization.task_id != task.task_id:
            raise StateIntegrityError("final authorization task binding is invalid")
        if snapshot is not None and (
            authorization.prepared_calculation_id != snapshot.prepared_id
            or authorization.prepared_snapshot_hash != snapshot.snapshot_hash
        ):
            raise StateIntegrityError("final authorization snapshot binding is invalid")
        return authorization

    @staticmethod
    def _assert_prepared_identity(snapshot: PreparedCalculation, candidate: object) -> None:
        expected = thaw_json(snapshot.identity_snapshot)
        geometry = thaw_json(snapshot.geometry_draft or {})
        if not isinstance(expected, dict) or not isinstance(geometry, dict):
            raise StateIntegrityError("preparation identity/geometry snapshot is invalid")
        expected_candidate = expected.get("candidate")
        if not isinstance(expected_candidate, dict) or not isinstance(candidate, dict):
            raise StateIntegrityError("preparation has no immutable identity candidate")
        for field in (
            "candidate_id",
            "candidate_hash",
            "canonical_isomeric_smiles",
            "molecular_formula",
            "formal_charge",
        ):
            if field in expected_candidate and candidate.get(field) != expected_candidate.get(
                field
            ):
                raise StateIntegrityError(
                    "P4 candidate differs from the prepared identity snapshot"
                )
        if candidate.get("canonical_isomeric_smiles") != geometry.get("canonical_isomeric_smiles"):
            raise StateIntegrityError("P4 candidate differs from the frozen geometry structure")
        if candidate.get("molecular_formula") != geometry.get("molecular_formula"):
            raise StateIntegrityError("P4 candidate differs from the frozen geometry formula")
        if candidate.get("formal_charge") != geometry.get("formal_charge"):
            raise StateIntegrityError("P4 candidate differs from the frozen geometry charge")

    def _authorize_prepared_execution(
        self, task: TaskRecord, payload: dict[str, object], now: datetime
    ) -> dict[str, object]:
        """Consume the sole user confirmation and derive all downstream grants."""

        snapshot = self._validated_prepared_snapshot(task, payload)
        current = self.get_task(task.conversation_id, task.task_id) or task
        p4_effect: object | None = None
        p4_view = self.p4.inspect(RunId(current.p4_run_id or "")) if current.p4_run_id else None
        if p4_view is None:
            raise StateIntegrityError("prepared execution has no P4 identity run")
        if p4_view.state.phase is P4Phase.RESOLVING_IDENTITY:
            reports = self.p4.create_worker().run_once(limit=1)
            p4_effect = {"step": "p4.worker", "reports": [_as_json(item) for item in reports]}
            p4_view = self.p4.inspect(p4_view.run_id)
        if p4_view.state.phase is P4Phase.AWAITING_IDENTITY:
            identity_payload = self._identity_confirmation_payload(
                current, p4_view, snapshot=snapshot
            )
            p4_result = self._confirm_identity(current, identity_payload, now)
            p4_effect = _as_json(p4_result)
            if not getattr(p4_result, "accepted", False):
                return {
                    "status": "identity_confirmation_rejected",
                    "p4": p4_effect,
                }
            p4_view = self.p4.inspect(p4_view.run_id)
        if p4_view.state.phase in {P4Phase.FAILED, P4Phase.CANCELLED}:
            self._finish_without_science(current, reason=StopReason.FAILED)
            return {
                "status": "p4_terminal",
                "p4": _as_json(p4_view),
            }
        if p4_view.state.phase is not P4Phase.PLAN_READY:
            # A final confirmation cannot become a durable parent grant until
            # the P4 identity is actually resolved.  The consumed token is
            # replayable, so a later worker/reconcile pass can continue it.
            return {
                "status": "identity_pending",
                "p4": p4_effect or _as_json(p4_view),
            }
        if p4_view.confirmed_molecule is None:
            raise StateIntegrityError("prepared execution P4 plan has no confirmed molecule")
        self._assert_prepared_identity(snapshot, p4_view.confirmed_molecule.model_dump(mode="json"))
        # Do not mark the task authorized before the exact P4 identity has
        # passed the frozen-snapshot check.  Otherwise a mismatching or
        # rejected identity could leave behind an apparently valid parent
        # authorization that later replay might use.
        authorization_id = self._persist_final_authorization(task, snapshot, now)
        p5_effect: object | None = None
        latest = self.get_task(current.conversation_id, current.task_id) or current
        if p4_view.state.phase is P4Phase.PLAN_READY and latest.p5_run_id is None:
            _count, p5_effect = self._ensure_p5(latest, p4_view)
            latest = self.get_task(current.conversation_id, current.task_id) or latest
        if latest.p5_run_id is not None:
            p5_view = self.p5.inspect(RunId(latest.p5_run_id))
            if p5_view.state.phase is P5Phase.AWAITING_EXECUTION_APPROVAL:
                approval_payload = self._approval_payload(latest, p5_view)
                p5_result = self._approve_execution(latest, approval_payload, now)
                p5_effect = _as_json(p5_result)
        return {
            "authorization_id": authorization_id,
            "status": "authorized",
            "p4": p4_effect,
            "p5": p5_effect,
        }

    @staticmethod
    def _reused_p4_run_id(
        records: P7RecordRepository, conversation_id: str, source_task_id: str | None
    ) -> str | None:
        if source_task_id is None:
            return None
        source = records.get_task(conversation_id, source_task_id)
        if source is None or source.p4_run_id is None:
            return None
        return source.p4_run_id

    def _insert_planning_record(
        self,
        records: P7RecordRepository,
        *,
        task: TaskRecord,
        normalized,
        turn_id: str,
        now: datetime,
    ) -> PlanningRecord | None:
        proposal = getattr(normalized, "proposal", None)
        compilation = getattr(normalized, "compilation", None)
        if proposal is None:
            return None
        # Legacy library callers retain their historical full-chain contract;
        # only a v3 candidate compilation creates planning evidence.
        if self.runtime_config.profile == "legacy" and compilation is None:
            return None
        validation = normalized.validation
        request = normalized.request
        record = PlanningRecord.create(
            proposal_id=f"planproposal_{uuid.uuid4().hex}",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            source_turn_id=turn_id,
            task_revision=task.revision,
            action=proposal.action,
            original_goal=proposal.goal,
            normalized_constraints={
                "hard_constraints": list(request.hard_constraints),
                "prohibited_requests": list(request.prohibited_requests),
                "method": None if request.method is None else _as_json(request.method),
                "environment": None
                if request.environment is None
                else _as_json(request.environment),
                "charge": None if request.charge is None else _as_json(request.charge),
                "multiplicity": None
                if request.multiplicity is None
                else _as_json(request.multiplicity),
                "draft_semantics_version": getattr(normalized, "draft_semantics_version", "legacy"),
                "draft_changes": list(getattr(normalized, "draft_changes", ())),
                "identity_notes": list(getattr(normalized, "identity_notes", ())),
                "raw_molecule_fragment": getattr(normalized, "raw_molecule_fragment", None),
                "precheck": None
                if getattr(normalized, "precheck", None) is None
                else _as_json(normalized.precheck),
                "policy_snapshot": getattr(normalized, "policy_snapshot", None),
                "parameter_sources": {
                    field_name: None
                    if getattr(request, field_name) is None
                    else {
                        "source": getattr(request, field_name).source.value,
                        "source_reference": getattr(request, field_name).source_reference,
                        "source_fragment": getattr(request, field_name).source_fragment,
                        "rule_version": getattr(request, field_name).rule_version,
                    }
                    for field_name in ("method", "environment", "charge", "multiplicity")
                },
            },
            candidate=proposal,
            validation_status=validation.status,
            validation_issues=tuple(
                dict.fromkeys((*validation.issues, *validation.unsupported_requests))
            ),
            feedback=PlanFeedback(
                unsatisfied_outputs=tuple(
                    item.kind.value
                    for item in request.output_spec.quantities
                    if normalized.plan is None or item.kind not in normalized.plan.expected_outputs
                ),
                prohibited_requests=tuple(request.prohibited_requests),
                remaining_budget={"real_concurrency": 1},
            ),
            request_hash=request.request_hash,
            compiled_plan_hash=(None if normalized.plan is None else normalized.plan.plan_hash),
            compiled_protocol_id=(None if normalized.plan is None else normalized.plan.protocol_id),
            source_task_id=getattr(normalized, "source_task_id", None),
            external_opt_result_id=getattr(normalized, "external_opt_result_id", None),
            model_attempt_id=records.latest_model_attempt_id(turn_id),
            created_at_utc=now,
        )
        records.insert_planning_record(record)
        return record

    def planning_record(self, task: TaskRecord) -> PlanningRecord | None:
        """Return the immutable record for the exact current task revision."""

        return self._planning_record(task)

    def _plan_acceptance_payload(
        self,
        task: TaskRecord,
        normalized,
        execution_profile: dict[str, object],
        *,
        planning_record: PlanningRecord | None = None,
    ) -> dict[str, object]:
        if normalized.plan is None:
            raise StateIntegrityError("plan acceptance has no compiled plan")
        record = planning_record
        if record is None and getattr(normalized, "draft_semantics_version", "legacy") != "legacy":
            record = self._planning_record(task)
        return {
            "task_id": task.task_id,
            "task_revision": task.revision,
            "plan_hash": normalized.plan.plan_hash,
            "validation_hash": normalized.validation.validation_hash,
            "output_spec_hash": normalized.request.output_spec.output_spec_hash,
            "capability_id": normalized.plan.capability_id,
            "capability_version": normalized.plan.capability_version,
            "execution_profile": execution_profile,
            "execution_profile_hash": sha256_hex(execution_profile),
            "planning_record_id": None if record is None else record.proposal_id,
            "planning_record_hash": None if record is None else record.record_hash,
            "policy_snapshot_hash": (
                None
                if getattr(normalized, "policy_snapshot", None) is None
                else normalized.policy_snapshot.get("content_hash")
            ),
            "policy_version": (
                None
                if getattr(normalized, "policy_snapshot", None) is None
                else normalized.policy_snapshot.get("policy_version")
            ),
        }

    @staticmethod
    def _unique_alias(
        records: P7RecordRepository, conversation_id: str, requested: str | None
    ) -> str:
        base = (requested or "任务").strip() or "任务"
        if records.get_task_by_alias(conversation_id, base) is None:
            return base
        index = 2
        while records.get_task_by_alias(conversation_id, f"{base}-{index}") is not None:
            index += 1
        return f"{base}-{index}"

    @staticmethod
    def _insert_pending(
        records: P7RecordRepository,
        *,
        conversation_id: str,
        task_id: str | None,
        action_type: str,
        target_id: str,
        expected_revision: int,
        payload: dict[str, object],
        now: datetime,
    ) -> PendingActionRecord:
        existing = records.list_pending(conversation_id)
        for item in existing:
            if (
                item.task_id == task_id
                and item.action_type == action_type
                and item.target_id == target_id
            ):
                return item
        from orca_agent.domain.hashing import sha256_hex

        return_value = PendingActionRecord(
            token=f"p7_{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            task_id=task_id,
            action_type=action_type,
            target_id=target_id,
            expected_revision=expected_revision,
            content_hash=sha256_hex(payload),
            payload=payload,
            created_at_utc=now,
        )
        records.insert_pending(return_value)
        return return_value

    def accept_action(
        self,
        conversation_id: ConversationId | str,
        token: str,
        *,
        decision: str,
    ) -> dict[str, object]:
        if decision not in {"accept", "reject"}:
            raise ValueError("action decision must be accept or reject")
        conversation = str(conversation_id)
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            pending = records.get_pending(conversation, token)
            if pending is None:
                raise ValueError("action token was not found in this conversation")
            if pending.status != "pending":
                if pending.decision == decision:
                    uow.commit()
                    if decision == "accept":
                        return self._replay_accepted_action(conversation, pending)
                    return {"accepted": True, "replayed": True, "token": token}
                raise InvalidTransitionError("action token is already decided")
            from orca_agent.domain.hashing import verify_sha256

            verify_sha256(thaw_json(pending.payload), pending.content_hash)
            task = (
                None if pending.task_id is None else records.get_task(conversation, pending.task_id)
            )
            if task is not None and task.revision != pending.expected_revision:
                records.stale_task_pending(task.task_id, new_revision=task.revision)
                uow.commit()
                raise RevisionConflictError("action token is stale for the current task revision")
            if task is None and pending.task_id is not None:
                raise StateIntegrityError("pending action refers to a missing task")

            # A real-profile final confirmation must not be consumed while
            # the configured ORCA executable/version is still unavailable.
            # Keeping the token pending lets the operator fix the runtime and
            # retry the same immutable confirmation card.
            if (
                decision == "accept"
                and pending.action_type == "confirm_execution"
                and not self.runtime_config.execution_ready
            ):
                uow.commit()
                return self._view(task) | {
                    "accepted": False,
                    "token": token,
                    "code": "execution_not_ready",
                    "reasons": list(self.runtime_config.execution_readiness_reasons),
                }

            if decision == "reject":
                records.consume_pending(
                    conversation_id=conversation,
                    token=token,
                    decision=decision,
                    now=now,
                )
                if task is not None:
                    updated = self._task_update(
                        task,
                        state=TaskPhase.ENDED_WITHOUT_RESULT,
                        stop_reason=StopReason.CANCELLED,
                        now=now,
                    )
                    if not records.update_task(updated, expected_revision=task.revision):
                        raise RevisionConflictError("task changed while rejecting action")
                uow.commit()
                return (
                    self._view(updated if task is not None else task)
                    if task is not None
                    else {"accepted": True, "token": token}
                )

            # Accepting the plan allocates the P4 child and command in the
            # same short transaction as the token decision.  A later process
            # can replay the prepared handoff without inventing new IDs.
            if pending.action_type == "accept_plan":
                if task is None or task.plan is None or task.request is None:
                    raise StateIntegrityError("plan approval is missing its task plan")
                pending_payload = thaw_json(pending.payload)
                if not isinstance(pending_payload, dict):
                    raise StateIntegrityError("plan approval payload is not an object")
                expected_profile = pending_payload.get("execution_profile")
                expected_profile_hash = pending_payload.get("execution_profile_hash")
                if isinstance(expected_profile, dict) and expected_profile_hash != sha256_hex(
                    expected_profile
                ):
                    raise StateIntegrityError("plan approval execution profile is invalid")
                if isinstance(expected_profile, dict) and expected_profile_hash != sha256_hex(
                    self._plan_execution_profile(task.plan.protocol_id)
                ):
                    raise InvalidTransitionError(
                        "the saved execution profile differs from the current P7 profile"
                    )
                if pending_payload.get("plan_hash") != task.plan.plan_hash:
                    raise InvalidTransitionError("plan approval does not match the current plan")
                if pending_payload.get("validation_hash") != task.validation.validation_hash:
                    raise InvalidTransitionError(
                        "plan approval does not match the current validation"
                    )
                if (
                    pending_payload.get("output_spec_hash")
                    != task.request.output_spec.output_spec_hash
                ):
                    raise InvalidTransitionError(
                        "plan approval does not match the current output specification"
                    )
                planning_record_id = pending_payload.get("planning_record_id")
                planning_record_hash = pending_payload.get("planning_record_hash")
                if planning_record_id is not None or planning_record_hash is not None:
                    record = records.get_planning_record_for_task(task.task_id, task.revision)
                    if (
                        record is None
                        or record.proposal_id != planning_record_id
                        or record.record_hash != planning_record_hash
                        or record.request_hash != task.request.request_hash
                        or record.compiled_plan_hash != task.plan.plan_hash
                    ):
                        raise InvalidTransitionError(
                            "plan approval does not match the immutable planning record"
                        )
                    constraints = thaw_json(record.normalized_constraints)
                    if not isinstance(constraints, dict):
                        raise StateIntegrityError("planning record constraints are invalid")
                    policy_snapshot = constraints.get("policy_snapshot")
                    expected_policy_hash = pending_payload.get("policy_snapshot_hash")
                    expected_policy_version = pending_payload.get("policy_version")
                    if isinstance(policy_snapshot, dict):
                        if policy_snapshot.get("content_hash") != expected_policy_hash:
                            raise InvalidTransitionError("plan approval policy snapshot is stale")
                        if policy_snapshot.get("policy_version") != expected_policy_version:
                            raise InvalidTransitionError("plan approval policy version is stale")
                registered_protocol = get_p5_protocol(task.plan.protocol_id)
                if task.plan.protocol_hash != registered_protocol.protocol_hash:
                    raise StateIntegrityError("task plan protocol hash is not registered")
                if (
                    isinstance(expected_profile, dict)
                    and expected_profile.get("protocol_hash") != registered_protocol.protocol_hash
                ):
                    raise InvalidTransitionError("plan approval protocol binding is stale")

                # A Freq-only follow-up reuses the already confirmed P4
                # source.  It must not create another identity run or ask the
                # user to reconfirm the same molecule.
                if task.p4_run_id is not None and records.get_handoff(task.task_id, "p4") is None:
                    records.consume_pending(
                        conversation_id=conversation, token=token, decision=decision, now=now
                    )
                    updated = self._task_update(
                        task,
                        state=TaskPhase.EXECUTION_PENDING,
                        accepted_plan_hash=task.plan.plan_hash,
                        accepted_output_spec_hash=task.request.output_spec.output_spec_hash,
                        now=now,
                    )
                    if not records.update_task(updated, expected_revision=task.revision):
                        raise RevisionConflictError("task changed while accepting reused plan")
                    uow.commit()
                    return self._view(self.get_task(conversation, updated.task_id) or updated)
                p4_run_id = new_id(RunId)
                p4_conversation_id = new_id(ConversationId)
                command_id = new_id(CommandId)
                handoff_payload = self._p4_payload(
                    task, p4_run_id, p4_conversation_id, command_id, now
                )
                handoff = HandoffRecord.create(
                    handoff_id=f"handoff_{uuid.uuid4().hex}",
                    task_id=task.task_id,
                    target="p4",
                    command_id=str(command_id),
                    child_id=str(p4_run_id),
                    expected_revision=task.revision,
                    payload=handoff_payload,
                    created_at_utc=now,
                )
                records.insert_handoff(handoff)
                records.consume_pending(
                    conversation_id=conversation, token=token, decision=decision, now=now
                )
                updated = self._task_update(
                    task,
                    state=TaskPhase.IDENTITY_PENDING,
                    accepted_plan_hash=task.plan.plan_hash,
                    accepted_output_spec_hash=task.request.output_spec.output_spec_hash,
                    p4_run_id=str(p4_run_id),
                    now=now,
                )
                if not records.update_task(updated, expected_revision=task.revision):
                    raise RevisionConflictError("task changed while accepting plan")
                uow.commit()
                self._submit_p4_handoff(updated, handoff)
                return self._view(self.get_task(conversation, updated.task_id) or updated)

            if task is None:
                raise StateIntegrityError("action without a task cannot be executed")
            payload = thaw_json(pending.payload)
            if not isinstance(payload, dict):
                raise StateIntegrityError("action payload is not an object")
            records.consume_pending(
                conversation_id=conversation, token=token, decision=decision, now=now
            )
            uow.commit()

        if pending.action_type == "confirm_identity":
            result = self._confirm_identity(task, payload, now)
            return self._view(self.get_task(conversation, task.task_id) or task) | {
                "downstream": _as_json(result)
            }
        if pending.action_type == "approve_execution":
            result = self._approve_execution(task, payload, now)
            return self._view(self.get_task(conversation, task.task_id) or task) | {
                "downstream": _as_json(result)
            }
        if pending.action_type == "confirm_execution":
            result = self._authorize_prepared_execution(task, payload, now)
            return self._view(self.get_task(conversation, task.task_id) or task) | {
                "accepted": True,
                "downstream": _as_json(result),
            }
        raise InvalidTransitionError(f"unsupported pending action type: {pending.action_type}")

    def _replay_accepted_action(
        self, conversation: str, pending: PendingActionRecord
    ) -> dict[str, object]:
        """Coordinate a previously accepted decision with its original command.

        A consumed token records the user's decision, not proof that the
        downstream command reached its durable receipt.  Replaying the token
        therefore reuses its frozen payload and command ID instead of creating
        a new approval or asking the user to approve the same action again.
        """

        task = None if pending.task_id is None else self.get_task(conversation, pending.task_id)
        if task is None:
            if pending.task_id is not None:
                raise StateIntegrityError("accepted action refers to a missing task")
            return {"accepted": True, "replayed": True, "token": pending.token}
        view = self._view(task)
        if task.state in {
            TaskPhase.ENDED_WITHOUT_RESULT,
            TaskPhase.RESULT_READY,
            TaskPhase.RECONCILIATION_REQUIRED,
        }:
            return view | {"accepted": True, "replayed": True, "token": pending.token}
        payload = thaw_json(pending.payload)
        if not isinstance(payload, dict):
            raise StateIntegrityError("accepted action payload is not an object")
        downstream: object | None = None
        if pending.action_type == "accept_plan":
            handoff = self._handoff(task, "p4")
            if handoff is not None and handoff.status in {"prepared", "submitted"}:
                downstream = self._submit_p4_handoff(task, handoff)
        elif pending.action_type == "confirm_identity":
            downstream = self._confirm_identity(task, payload, _now(self.clock))
        elif pending.action_type == "approve_execution":
            downstream = self._approve_execution(task, payload, _now(self.clock))
        elif pending.action_type == "confirm_execution":
            downstream = self._authorize_prepared_execution(task, payload, _now(self.clock))
        else:
            raise InvalidTransitionError(
                f"cannot replay accepted action type: {pending.action_type}"
            )
        refreshed = self.get_task(conversation, task.task_id) or task
        return self._view(refreshed) | {
            "accepted": True,
            "replayed": True,
            "token": pending.token,
            "downstream": None if downstream is None else _as_json(downstream),
        }

    def _p4_payload(
        self,
        task: TaskRecord,
        run_id: RunId,
        conversation_id: ConversationId,
        command_id: CommandId,
        now: datetime,
    ) -> dict[str, object]:
        if task.request is None:
            raise StateIntegrityError("P4 handoff has no request")
        request = task.request
        if request.molecule_kind is None or request.molecule_value is None:
            raise InvalidTransitionError("a molecule is required before P4 handoff")
        if request.charge is None or request.multiplicity is None:
            raise InvalidTransitionError("electronic state is not resolved")
        return {
            "command_type": "p4.prepare",
            "command_id": str(command_id),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "input_kind": request.molecule_kind.value,
            "raw_input": request.molecule_value,
            "charge": int(request.charge.value),
            "multiplicity": int(request.multiplicity.value),
            "provider": (
                IdentityProvider.LOCAL.value
                if request.molecule_kind is MoleculeInputType.SMILES
                else (
                    IdentityProvider.PUBCHEM.value
                    if self.runtime_config.identity_provider == "pubchem"
                    else IdentityProvider.FAKE.value
                )
            ),
            "protocol_id": _P4_PROTOCOL_ID,
            "new_conversation": True,
            "requested_at_utc": format_utc(now),
        }

    def _plan_execution_profile(self, protocol_id: str) -> dict[str, object]:
        protocol = get_p5_protocol(protocol_id)
        return {
            "profile": self.runtime_config.public_dict(),
            "profile_hash": self.runtime_config.profile_hash,
            "protocol_id": protocol.protocol_id,
            "protocol_hash": protocol.protocol_hash,
            "nprocs": protocol.nprocs,
            "total_memory_mb": protocol.total_memory_mb,
            "maxcore_mb": protocol.maxcore_mb,
            "parallel": protocol.parallel,
            "implicit_threads": protocol.implicit_threads,
            "real_concurrency": 1,
        }

    def _submit_p4_handoff(self, task: TaskRecord, handoff: HandoffRecord) -> object:
        payload = thaw_json(handoff.payload)
        if not isinstance(payload, dict):
            raise StateIntegrityError("P4 handoff payload is not an object")
        command = StartPlanningRun.model_validate_json(
            json.dumps(payload, ensure_ascii=False), strict=True
        )
        self._mark_handoff_submitted(handoff)
        result = self.p4.start(command)
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            current = records.get_task(task.conversation_id, task.task_id)
            stored_handoff = records.get_handoff(task.task_id, "p4")
            if stored_handoff is not None:
                records.update_handoff(
                    stored_handoff.model_copy(
                        update={
                            "status": "linked" if result.accepted else "reconciliation_required",
                            "target_run_id": str(command.run_id) if result.accepted else None,
                            "updated_at_utc": now,
                        }
                    )
                )
            if (
                current is not None
                and not result.accepted
                and current.state is not TaskPhase.ENDED_WITHOUT_RESULT
            ):
                updated = self._task_update(
                    current,
                    state=TaskPhase.ENDED_WITHOUT_RESULT,
                    stop_reason=StopReason.FAILED,
                    now=now,
                )
                records.update_task(updated, expected_revision=current.revision)
            uow.commit()
        return result

    def _confirm_identity(
        self, task: TaskRecord, payload: dict[str, object], now: datetime
    ) -> object:
        p4_run_id = RunId(task.p4_run_id or "")
        command = ConfirmMoleculeIdentity.create(
            run_id=p4_run_id,
            conversation_id=ConversationId(str(payload["p4_conversation_id"])),
            interrupt_id=InterruptId(str(payload["interrupt_id"])),
            expected_revision=int(payload["expected_revision"]),
            query_id=WorkflowRecordId(str(payload["query_id"])),
            query_hash=str(payload["query_hash"]),
            candidate_bundle_id=WorkflowRecordId(str(payload["candidate_bundle_id"])),
            candidate_bundle_hash=str(payload["candidate_bundle_hash"]),
            candidate_set_hash=str(payload["candidate_set_hash"]),
            candidate_id=str(payload["candidate_id"]),
            candidate_hash=str(payload["candidate_hash"]),
            decision=IdentityDecision.ACCEPT,
            command_id=CommandId(str(payload["command_id"])),
            requested_at_utc=parse_utc(str(payload["requested_at_utc"])),
        )
        result = self.p4.confirm(command)
        if result.accepted:
            current = self.get_task(task.conversation_id, task.task_id)
            if current is not None and current.state is TaskPhase.IDENTITY_PENDING:
                self._refresh_task_state(task.task_id, TaskPhase.EXECUTION_PENDING)
        return result

    def _approve_execution(
        self, task: TaskRecord, payload: dict[str, object], now: datetime
    ) -> object:
        profile = payload.get("execution_profile")
        profile_hash = payload.get("execution_profile_hash")
        if profile is not None:
            if not isinstance(profile, dict) or profile_hash != sha256_hex(profile):
                raise StateIntegrityError("execution approval profile is invalid")
            profile_hash_value = profile.get("profile_hash")
            if profile_hash_value is not None:
                # The nested hash is a display/provenance value; the outer
                # hash above binds the whole approval card.  A changed runtime
                # cannot reuse a token created under another profile.
                if profile_hash_value != self.runtime_config.profile_hash:
                    raise InvalidTransitionError(
                        "execution approval was created under a different P7 profile"
                    )
        result = self.p5.approve(
            run_id=RunId(str(payload["run_id"])),
            conversation_id=ConversationId(str(payload["conversation_id"])),
            action_id=ActionId(str(payload["action_id"])),
            action_hash=str(payload["action_hash"]),
            binding_hash=str(payload["binding_hash"]),
            envelope_hash=str(payload["envelope_hash"]),
            budget_hash=str(payload["budget_hash"]),
            expected_revision=int(payload["expected_revision"]),
            command_id=CommandId(str(payload["command_id"])),
        )
        if result.accepted:
            current = self.get_task(task.conversation_id, task.task_id)
            if current is not None and current.state is TaskPhase.EXECUTION_PENDING:
                self._refresh_task_state(task.task_id, TaskPhase.EXECUTING)
        return result

    def _mark_handoff_submitted(self, handoff: HandoffRecord) -> None:
        """Persist the send boundary before invoking a downstream service."""

        if handoff.status == "submitted":
            return
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            current = records.get_handoff(handoff.task_id, handoff.target)
            if current is not None and current.status in {"prepared", "submitted"}:
                records.update_handoff(
                    current.model_copy(update={"status": "submitted", "updated_at_utc": now})
                )
            uow.commit()

    def _refresh_task_state(self, task_id: str, state: TaskPhase) -> TaskRecord:
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            task = self._task_by_id_any(records, task_id)
            if task is None:
                raise StateIntegrityError("task disappeared while updating downstream state")
            updated = self._task_update(task, state=state, now=now)
            if not records.update_task(updated, expected_revision=task.revision):
                raise RevisionConflictError("task revision changed while updating downstream state")
            uow.commit()
            return updated

    @staticmethod
    def _task_by_id_any(records: P7RecordRepository, task_id: str) -> TaskRecord | None:
        row = records.connection.execute(
            "SELECT conversation_id FROM p7_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return None if row is None else records.get_task(str(row[0]), task_id)

    @staticmethod
    def _task_update(task: TaskRecord, *, now: datetime, **updates: object) -> TaskRecord:
        updates.setdefault("revision", task.revision + 1)
        updates.setdefault("updated_at_utc", now)
        return task.model_copy(update=updates)

    # Bounded coordination -------------------------------------------
    def progress(
        self,
        conversation_id: ConversationId | str,
        *,
        max_effects: int = 16,
        max_seconds: float = 30.0,
        allow_real_orca: bool | None = None,
    ) -> dict[str, object]:
        if type(max_effects) is not int or max_effects < 1:
            raise ValueError("max_effects must be positive")
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if allow_real_orca is None:
            allow_real_orca = self.allow_real_orca
        started = time.monotonic()
        effects = 0
        activity: list[dict[str, object]] = []
        while effects < max_effects and time.monotonic() - started < max_seconds:
            tasks = self.list_tasks(conversation_id)
            made_progress = False
            for task in tasks:
                if effects >= max_effects or time.monotonic() - started >= max_seconds:
                    break
                count, item = self._advance_task(task, allow_real_orca=allow_real_orca)
                effects += count
                if item is not None:
                    activity.append(item)
                if count:
                    made_progress = True
            if not made_progress:
                break
        current = self.list_tasks(conversation_id)
        return {
            "conversation_id": str(conversation_id),
            "effects": effects,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "tasks": [self._view(task) for task in current],
            "activity": activity,
        }

    def _advance_task(
        self, task: TaskRecord, *, allow_real_orca: bool | None
    ) -> tuple[int, dict[str, object] | None]:
        if task.state in {
            TaskPhase.RESULT_READY,
            TaskPhase.ENDED_WITHOUT_RESULT,
            TaskPhase.RECONCILIATION_REQUIRED,
        }:
            return 0, None
        if task.p4_run_id is None and task.state in {
            TaskPhase.NEEDS_CLARIFICATION,
            TaskPhase.DRAFT,
        }:
            return 0, {
                "task_id": task.task_id,
                "step": "waiting_for_clarification",
                "state": task.state.value,
            }
        if task.prepared_calculation_id is not None:
            snapshot = self._prepared_snapshot_for_task(task)
            if snapshot is None:
                raise StateIntegrityError("task preparation snapshot is missing")
            if snapshot.status is not PreparationStatus.READY:
                if task.final_authorization_id is not None:
                    raise StateIntegrityError("non-ready preparation has a final authorization")
                return 0, {
                    "task_id": task.task_id,
                    "step": "preparation_blocked",
                    "status": snapshot.status.value,
                    "reason": snapshot.error_message or snapshot.error_code,
                }
            if task.final_authorization_id is None:
                return 0, {
                    "task_id": task.task_id,
                    "step": "waiting_for_final_confirmation",
                    "prepared_calculation_id": task.prepared_calculation_id,
                }
        p4_handoff = self._handoff(task, "p4")
        if p4_handoff is not None and p4_handoff.status in {"prepared", "submitted"}:
            result = self._submit_p4_handoff(task, p4_handoff)
            return 1, {
                "task_id": task.task_id,
                "step": "p4.start.reconciled",
                "accepted": result.accepted,
            }
        if task.p4_run_id is None:
            return 0, {
                "task_id": task.task_id,
                "step": "waiting_for_plan_acceptance",
                "state": task.state.value,
            }

        p4_view = self.p4.inspect(RunId(task.p4_run_id))
        if p4_view.state.phase is P4Phase.RESOLVING_IDENTITY:
            reports = self.p4.create_worker().run_once(limit=1)
            return len(reports), {
                "task_id": task.task_id,
                "step": "p4.worker",
                "reports": [_as_json(item) for item in reports],
            }
        if p4_view.state.phase is P4Phase.AWAITING_IDENTITY:
            if task.prepared_calculation_id is not None and task.final_authorization_id is not None:
                snapshot = self._prepared_snapshot_for_task(task)
                if snapshot is None:
                    raise StateIntegrityError("authorized prepared task has no snapshot")
                result = self._authorize_prepared_execution(
                    task, self._final_confirmation_payload(task, snapshot), _now(self.clock)
                )
                return 1, {
                    "task_id": task.task_id,
                    "step": "p7.final_confirmation.reconciled",
                    **result,
                }
            replay = self._replay_accepted_decision(
                task, action_type="confirm_identity", target_id=str(p4_view.run_id)
            )
            if replay is not None:
                return replay
            if (
                p4_view.candidate_bundle is None
                or not p4_view.candidate_bundle.confirmable
                or len(p4_view.candidate_bundle.candidates) != 1
            ):
                return 0, {
                    "task_id": task.task_id,
                    "step": "identity_clarification",
                    "reason": "identity_candidate_is_not_unique",
                    "candidate_count": (
                        0
                        if p4_view.candidate_bundle is None
                        else len(p4_view.candidate_bundle.candidates)
                    ),
                    "reason_codes": (
                        []
                        if p4_view.candidate_bundle is None
                        else list(p4_view.candidate_bundle.reason_codes)
                    ),
                }
            token = self._ensure_identity_action(task, p4_view)
            if token is None:
                return 1, {
                    "task_id": task.task_id,
                    "step": "identity_rejected",
                    "reason": "recommendation_structure_mismatch",
                }
            return 0, {"task_id": task.task_id, "step": "identity_pending", "token": token}
        if p4_view.state.phase is P4Phase.FAILED or p4_view.state.phase is P4Phase.CANCELLED:
            self._finish_without_science(task, reason=StopReason.FAILED)
            return 1, {
                "task_id": task.task_id,
                "step": "p4_terminal",
                "phase": p4_view.state.phase.value,
            }
        if p4_view.state.phase is P4Phase.PLAN_READY and task.p5_run_id is None:
            count, item = self._ensure_p5(task, p4_view)
            return count, item

        if task.p5_run_id is None:
            return 0, {"task_id": task.task_id, "step": "waiting_for_p5"}
        p5_handoff = self._handoff(task, "p5")
        if p5_handoff is not None and p5_handoff.status in {"prepared", "submitted"}:
            return self._submit_p5_handoff(task, p5_handoff)
        p5_view = self.p5.inspect(RunId(task.p5_run_id))
        if p5_view.state.phase is P5Phase.AWAITING_EXECUTION_APPROVAL:
            if task.prepared_calculation_id is not None and task.final_authorization_id is not None:
                snapshot = self._prepared_snapshot_for_task(task)
                if snapshot is None:
                    raise StateIntegrityError("authorized prepared task has no snapshot")
                result = self._authorize_prepared_execution(
                    task, self._final_confirmation_payload(task, snapshot), _now(self.clock)
                )
                return 1, {
                    "task_id": task.task_id,
                    "step": "p7.final_confirmation.reconciled",
                    **result,
                }
            self._refresh_task_state_if_needed(task, TaskPhase.EXECUTION_PENDING)
            latest = self.get_task(task.conversation_id, task.task_id) or task
            replay = self._replay_accepted_decision(
                latest,
                action_type="approve_execution",
                target_id=str(p5_view.action.action_id) if p5_view.action is not None else None,
            )
            if replay is not None:
                return replay
            if not self.runtime_config.execution_ready:
                return 0, {
                    "task_id": task.task_id,
                    "step": "execution_blocked",
                    "reason": "real_execution_not_ready",
                    "details": list(self.runtime_config.execution_readiness_reasons),
                }
            token = self._ensure_approval_action(latest, p5_view)
            return 0, {"task_id": task.task_id, "step": "execution_pending", "token": token}
        if p5_view.state.phase in {
            P5Phase.DISPATCH_PENDING,
            P5Phase.RUNNING,
            P5Phase.COLLECTING,
            P5Phase.CANCELLING,
            P5Phase.PREPARING,
        }:
            worker = self.p5.create_worker(allow_real_orca=allow_real_orca)
            reports = worker.run_once(run_id=p5_view.run_id, limit=1)
            return len(reports), {
                "task_id": task.task_id,
                "step": "p5.worker",
                "reports": [_as_json(item) for item in reports],
            }
        if p5_view.state.phase is P5Phase.NEEDS_RECONCILIATION:
            self._refresh_task_state_if_needed(task, TaskPhase.RECONCILIATION_REQUIRED)
            return 0, {
                "task_id": task.task_id,
                "step": "reconciliation_required",
                "reason": p5_view.state.last_error_message
                or p5_view.state.last_outcome_code
                or "launch_state_unknown",
            }
        if p5_view.state.phase in {P5Phase.FAILED, P5Phase.CANCELLED}:
            self._create_delivery(task, p5_view=p5_view, p6_view=None)
            return 1, {
                "task_id": task.task_id,
                "step": "p5_terminal",
                "phase": p5_view.state.phase.value,
            }
        if p5_view.state.phase is P5Phase.COMPLETED and task.p6_run_id is None:
            count, item = self._ensure_p6(task, p5_view)
            return count, item

        if task.p6_run_id is None:
            return 0, {"task_id": task.task_id, "step": "waiting_for_p6"}
        p6_handoff = self._handoff(task, "p6")
        if p6_handoff is not None and p6_handoff.status in {"prepared", "submitted"}:
            return self._submit_p6_handoff(task, p6_handoff)
        p6_view = self.p6.inspect(RunId(task.p6_run_id))
        if p6_view.state.phase in {P6Phase.ASSESSMENT_PENDING, P6Phase.REPORT_PENDING}:
            reports = self.p6.create_worker().run_once(run_id=p6_view.run_id, limit=1)
            return len(reports), {
                "task_id": task.task_id,
                "step": "p6.worker",
                "reports": [_as_json(item) for item in reports],
            }
        if p6_view.state.phase in {P6Phase.COMPLETED, P6Phase.FAILED, P6Phase.CANCELLED}:
            self._create_delivery(task, p5_view=p5_view, p6_view=p6_view)
            return 1, {
                "task_id": task.task_id,
                "step": "delivery",
                "phase": p6_view.state.phase.value,
            }
        return 0, {"task_id": task.task_id, "step": "no_action"}

    def _handoff(self, task: TaskRecord, target: str) -> HandoffRecord | None:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            value = P7RecordRepository(uow.connection).get_handoff(task.task_id, target)
            uow.commit()
            return value

    def _replay_accepted_decision(
        self, task: TaskRecord, *, action_type: str, target_id: str | None
    ) -> tuple[int, dict[str, object]] | None:
        """Retry one accepted decision before creating a replacement token."""

        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            actions = tuple(
                item
                for item in records.list_pending(task.conversation_id, status=None)
                if item.task_id == task.task_id
                and item.action_type == action_type
                and item.status == "consumed"
                and item.decision == "accept"
                and (target_id is None or item.target_id == target_id)
            )
            uow.commit()
        if not actions:
            return None
        action = actions[-1]
        payload = thaw_json(action.payload)
        if not isinstance(payload, dict):
            raise StateIntegrityError("accepted decision payload is not an object")
        if action_type == "confirm_identity":
            result = self._confirm_identity(task, payload, _now(self.clock))
            step = "p4.confirm.reconciled"
        elif action_type == "approve_execution":
            result = self._approve_execution(task, payload, _now(self.clock))
            step = "p5.approve.reconciled"
        else:
            raise InvalidTransitionError(f"unsupported accepted decision: {action_type}")
        return 1, {
            "task_id": task.task_id,
            "step": step,
            "token": action.token,
            "accepted": bool(getattr(result, "accepted", False)),
            "code": str(getattr(result, "code", "unknown")),
        }

    def _identity_confirmation_payload(
        self,
        task: TaskRecord,
        view,
        *,
        snapshot: PreparedCalculation | None = None,
    ) -> dict[str, object]:
        if view.candidate_bundle is None or view.interrupt is None:
            raise StateIntegrityError("P4 identity phase has no candidate bundle or interrupt")
        candidate = (
            view.candidate_bundle.candidates[0]
            if len(view.candidate_bundle.candidates) == 1
            else None
        )
        if candidate is None:
            raise InvalidTransitionError("P7 requires an unambiguous P4 identity candidate")
        expected = self._recommended_canonical_smiles(task.request)
        if expected is not None and candidate.canonical_isomeric_smiles != expected:
            # Recommendation cards are bound to an expected structure.  A
            # mismatching provider candidate invalidates the draft and cannot
            # be allowed to reach identity confirmation or P5.
            raise InvalidTransitionError("P4 candidate does not match the requested structure")
        if snapshot is None:
            snapshot = self._prepared_snapshot_for_task(task)
        if snapshot is not None:
            self._assert_prepared_identity(snapshot, candidate.model_dump(mode="json"))
        now = _now(self.clock)
        payload = {
            "p4_conversation_id": str(view.conversation_id),
            "interrupt_id": str(view.interrupt["interrupt_id"]),
            "expected_revision": view.revision,
            "query_id": str(view.query.query_id),
            "query_hash": view.query.query_hash,
            "candidate_bundle_id": str(view.candidate_bundle.record_id),
            "candidate_bundle_hash": view.candidate_bundle.bundle_hash,
            "candidate_set_hash": view.candidate_bundle.candidate_set_hash,
            "candidate_id": candidate.candidate_id,
            "candidate_hash": candidate.candidate_hash,
            "command_id": str(new_id(CommandId)),
            "requested_at_utc": format_utc(now),
        }
        return payload

    def _ensure_identity_action(self, task: TaskRecord, view) -> str | None:
        try:
            payload = self._identity_confirmation_payload(task, view)
        except InvalidTransitionError:
            self._finish_without_science(task, reason=StopReason.UNSUPPORTED)
            return None
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            existing = next(
                (
                    item
                    for item in records.list_pending(task.conversation_id)
                    if item.task_id == task.task_id and item.action_type == "confirm_identity"
                ),
                None,
            )
            if existing is not None:
                uow.commit()
                return existing.token
            action = self._insert_pending(
                records,
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                action_type="confirm_identity",
                target_id=str(view.run_id),
                expected_revision=task.revision,
                payload=payload,
                now=now,
            )
            uow.commit()
            return action.token

    @staticmethod
    def _recommended_canonical_smiles(request: CalculationRequest | None) -> str | None:
        if request is None or request.molecule_kind is None or request.molecule_value is None:
            return None
        if request.molecule_kind is MoleculeInputType.SMILES:
            precheck = MoleculePrecheck(policy=default_parameter_policy()).check(
                input_kind=request.molecule_kind,
                raw_input=request.molecule_value,
                charge=None if request.charge is None else int(request.charge.value),
            )
            return precheck.canonical_isomeric_smiles or request.molecule_value
        molecule = default_parameter_policy().molecule_for(
            request.molecule_kind, request.molecule_value
        )
        return None if molecule is None else molecule.canonical_smiles

    def _ensure_p5(self, task: TaskRecord, p4_view) -> tuple[int, dict[str, object]]:
        now = _now(self.clock)
        existing = self._handoff(task, "p5")
        if existing is None:
            if task.plan is None:
                raise StateIntegrityError("P5 handoff has no approved task plan")
            protocol = get_p5_protocol(task.plan.protocol_id)
            if task.plan.protocol_hash != protocol.protocol_hash:
                raise StateIntegrityError("task plan protocol hash is not registered")
            planning_record = self._planning_record(task)
            external_result_id = (
                None if planning_record is None else planning_record.external_opt_result_id
            )
            p5_run_id = new_id(RunId)
            command_id = new_id(CommandId)
            payload = {
                "command_type": "p5.create_execution",
                "command_id": str(command_id),
                "run_id": str(p5_run_id),
                "source_run_id": str(p4_view.run_id),
                "protocol_id": protocol.protocol_id,
                "protocol_hash": protocol.protocol_hash,
                "external_opt_result_id": external_result_id,
                "wall_time_seconds": None,
                "requested_at_utc": format_utc(now),
                "planning_proposal_id": (
                    None if planning_record is None else planning_record.proposal_id
                ),
            }
            if task.prepared_calculation_id is not None:
                snapshot = self._prepared_snapshot_for_task(task)
                if snapshot is None or snapshot.status is not PreparationStatus.READY:
                    raise StateIntegrityError("prepared task has no ready P5 preparation snapshot")
                payload.update(
                    {
                        "prepared_id": snapshot.prepared_id,
                        "prepared_snapshot_hash": snapshot.snapshot_hash,
                    }
                )
                authorization = self._execution_authorization_for_task(task, snapshot)
                if authorization is None:
                    raise StateIntegrityError(
                        "prepared P5 handoff has no parent execution authorization"
                    )
                if not task.plan.nodes:
                    raise StateIntegrityError("prepared P5 task has no execution nodes")
                payload.update(
                    {
                        "parent_authorization_id": authorization.authorization_id,
                        "parent_authorization_hash": authorization.authorization_hash,
                        "parent_authorization_credential": authorization.credential_for(
                            task.plan.nodes[0].node_id
                        ),
                    }
                )
            existing = HandoffRecord.create(
                handoff_id=f"handoff_{uuid.uuid4().hex}",
                task_id=task.task_id,
                target="p5",
                command_id=str(command_id),
                child_id=str(p5_run_id),
                expected_revision=task.revision,
                payload=payload,
                created_at_utc=now,
            )
            with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
                uow.begin()
                records = P7RecordRepository(uow.connection)
                records.insert_handoff(existing)
                current = records.get_task(task.conversation_id, task.task_id)
                if current is None:
                    raise StateIntegrityError("task disappeared before P5 handoff")
                updated = self._task_update(
                    current, state=TaskPhase.EXECUTION_PENDING, p5_run_id=str(p5_run_id), now=now
                )
                if not records.update_task(updated, expected_revision=current.revision):
                    raise RevisionConflictError("task changed before P5 handoff")
                uow.commit()
            task = updated
        return self._submit_p5_handoff(task, existing)

    def _planning_record(self, task: TaskRecord) -> PlanningRecord | None:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            record = records.get_planning_record_for_task(task.task_id, task.revision)
            if record is None and task.request is not None and task.plan is not None:
                record = records.get_planning_record_for_plan(
                    task.task_id,
                    task.request.request_hash,
                    task.plan.plan_hash,
                )
            uow.commit()
            return record

    def _submit_p5_handoff(
        self, task: TaskRecord, handoff: HandoffRecord
    ) -> tuple[int, dict[str, object]]:
        payload = thaw_json(handoff.payload)
        if not isinstance(payload, dict):
            raise StateIntegrityError("P5 handoff payload is invalid")
        if handoff.status == "linked":
            return 0, {"task_id": task.task_id, "step": "p5.already_linked"}
        protocol_id = payload.get("protocol_id")
        protocol_hash = payload.get("protocol_hash")
        if not isinstance(protocol_id, str) or not isinstance(protocol_hash, str):
            raise StateIntegrityError("P5 handoff has no complete protocol binding")
        protocol = get_p5_protocol(protocol_id)
        if task.plan is None or protocol.protocol_hash != protocol_hash:
            raise StateIntegrityError("P5 handoff protocol binding is inconsistent")
        if task.plan.protocol_id != protocol_id or task.plan.protocol_hash != protocol_hash:
            raise StateIntegrityError("P5 handoff does not match the task plan")
        external_result_id = payload.get("external_opt_result_id")
        preparation_snapshot_id = payload.get("prepared_id")
        preparation_snapshot_hash = payload.get("prepared_snapshot_hash")
        parent_authorization_id = payload.get("parent_authorization_id")
        parent_authorization_hash = payload.get("parent_authorization_hash")
        parent_authorization_credential = payload.get("parent_authorization_credential")
        frozen_geometry = None
        geometry_draft_hash = None
        prepared_geometry_hash = None
        prepared_xyz_bytes_sha256 = None
        if preparation_snapshot_id is not None:
            snapshot = self._prepared_snapshot_for_task(task)
            if snapshot is None or snapshot.prepared_id != preparation_snapshot_id:
                raise StateIntegrityError("P5 handoff preparation snapshot is missing")
            if preparation_snapshot_hash != snapshot.snapshot_hash:
                raise StateIntegrityError("P5 handoff preparation snapshot hash is stale")
            authorization = self._execution_authorization_for_task(task, snapshot)
            if authorization is None:
                raise StateIntegrityError("P5 handoff has no parent execution authorization")
            if (
                parent_authorization_id != authorization.authorization_id
                or parent_authorization_hash != authorization.authorization_hash
            ):
                raise StateIntegrityError("P5 handoff parent authorization is stale")
            if not isinstance(parent_authorization_credential, str):
                raise StateIntegrityError("P5 handoff has no parent node credential")
            if not task.plan.nodes:
                raise StateIntegrityError("prepared P5 task has no execution nodes")
            if parent_authorization_credential != authorization.credential_for(
                task.plan.nodes[0].node_id
            ):
                raise StateIntegrityError("P5 handoff parent node credential is stale")
            prepared_geometry_hash = snapshot.geometry_hash
            prepared_xyz_bytes_sha256 = snapshot.xyz_bytes_sha256
            if snapshot.geometry_source == "rdkit_initial":
                if external_result_id is not None:
                    raise StateIntegrityError(
                        "initial-draft preparation cannot carry an external Opt source"
                    )
                frozen_geometry = self._frozen_geometry_for_handoff(task, payload)
                draft = thaw_json(snapshot.geometry_draft or {})
                if isinstance(draft, dict):
                    geometry_draft_hash = draft.get("draft_hash")
                if not isinstance(geometry_draft_hash, str):
                    raise StateIntegrityError("P5 handoff has no geometry draft hash")
            elif snapshot.geometry_source == "history_opt":
                if external_result_id is None:
                    raise StateIntegrityError(
                        "historical Opt preparation requires an external Opt source"
                    )
            else:
                raise StateIntegrityError("P5 handoff geometry source is invalid")
        self._mark_handoff_submitted(handoff)
        result = self.p5.prepare_execution(
            source_run_id=RunId(str(payload["source_run_id"])),
            protocol_id=protocol_id,
            run_id=RunId(str(payload["run_id"])),
            command_id=CommandId(str(payload["command_id"])),
            external_opt_result_id=(
                None if external_result_id is None else WorkflowRecordId(str(external_result_id))
            ),
            wall_time_seconds=(
                None
                if payload.get("wall_time_seconds") is None
                else int(payload["wall_time_seconds"])
            ),
            frozen_geometry=frozen_geometry,
            geometry_draft_hash=geometry_draft_hash,
            preparation_snapshot_id=(
                None if preparation_snapshot_id is None else str(preparation_snapshot_id)
            ),
            preparation_snapshot_hash=(
                None if preparation_snapshot_hash is None else str(preparation_snapshot_hash)
            ),
            prepared_geometry_hash=prepared_geometry_hash,
            prepared_xyz_bytes_sha256=prepared_xyz_bytes_sha256,
            parent_authorization_id=(
                None if parent_authorization_id is None else str(parent_authorization_id)
            ),
            parent_authorization_hash=(
                None if parent_authorization_hash is None else str(parent_authorization_hash)
            ),
            parent_authorization_credential=(
                None
                if parent_authorization_credential is None
                else str(parent_authorization_credential)
            ),
        )
        now = _now(self.clock)
        updated_handoff = handoff.model_copy(
            update={
                "status": "linked" if result.accepted else "reconciliation_required",
                "target_run_id": str(payload["run_id"]) if result.accepted else None,
                "updated_at_utc": now,
            }
        )
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            records.update_handoff(updated_handoff)
            current = records.get_task(task.conversation_id, task.task_id)
            if current is not None and not result.accepted:
                reconciled = self._task_update(
                    current,
                    state=TaskPhase.RECONCILIATION_REQUIRED,
                    stop_reason=StopReason.FAILED,
                    now=now,
                )
                records.update_task(reconciled, expected_revision=current.revision)
            uow.commit()
        return 1, {
            "task_id": task.task_id,
            "step": "p5.prepare",
            "accepted": result.accepted,
            "run_id": str(payload["run_id"]),
        }

    def _frozen_geometry_for_handoff(
        self, task: TaskRecord, payload: dict[str, object]
    ) -> GeometryRecord | None:
        prepared_id = payload.get("prepared_id")
        if prepared_id is None:
            return None
        if not isinstance(prepared_id, str) or prepared_id != task.prepared_calculation_id:
            raise StateIntegrityError("P5 handoff preparation binding is invalid")
        snapshot = self._prepared_snapshot_for_task(task)
        if snapshot is None or snapshot.status is not PreparationStatus.READY:
            raise StateIntegrityError("P5 handoff has no ready preparation snapshot")
        if payload.get("prepared_snapshot_hash") != snapshot.snapshot_hash:
            raise StateIntegrityError("P5 handoff preparation snapshot hash is stale")
        if snapshot.geometry_source != "rdkit_initial":
            if snapshot.geometry_source == "history_opt":
                return None
            raise StateIntegrityError("P5 handoff geometry source is invalid")
        p4_view = self.p4.inspect(RunId(str(payload["source_run_id"])))
        if p4_view.confirmed_molecule is None:
            raise StateIntegrityError("P5 handoff source has no confirmed molecule")
        draft_snapshot = snapshot.geometry_draft
        if draft_snapshot is None:
            raise StateIntegrityError("ready preparation has no geometry draft")
        draft = self.preparation.draft_from_snapshot(draft_snapshot)
        bound = bind_initial_geometry(
            draft,
            p4_view.confirmed_molecule,
            run_id=RunId(str(payload["run_id"])),
        )
        if (
            bound.geometry_hash != snapshot.geometry_hash
            or bound.xyz_bytes_sha256 != snapshot.xyz_bytes_sha256
        ):
            raise StateIntegrityError("P5 geometry does not match the frozen preparation snapshot")
        return bound

    def _approval_payload(self, task: TaskRecord, view) -> dict[str, object]:
        if view.action is None or view.binding is None:
            raise StateIntegrityError("P5 approval phase has no action/binding")
        execution_profile = self._execution_profile(view)
        confirmation = self._confirmation_card(task, view)
        if confirmation.get("preparation_errors"):
            raise StateIntegrityError(
                "execution confirmation is incomplete: "
                + "; ".join(str(item) for item in confirmation["preparation_errors"])
            )
        return {
            "run_id": str(view.run_id),
            "conversation_id": str(view.conversation_id),
            "action_id": str(view.action.action_id),
            "action_hash": view.action.action_hash,
            "binding_hash": view.binding.binding_hash,
            "envelope_hash": view.action.envelope_hash,
            "budget_hash": view.action.budget_hash,
            "expected_revision": view.revision,
            "command_id": str(new_id(CommandId)),
            "node_id": view.action.node_id,
            "primitive_id": view.action.primitive_id,
            "budget": view.binding.budget.model_dump(mode="json"),
            "execution_profile": execution_profile,
            "execution_profile_hash": sha256_hex(execution_profile),
            "confirmation": confirmation,
            "plan_display": build_execution_display(
                task,
                planning_record=self._planning_record(task),
                p5=_as_json(view),
            ),
        }

    def _ensure_approval_action(self, task: TaskRecord, view) -> str:
        now = _now(self.clock)
        payload = self._approval_payload(task, view)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            existing = next(
                (
                    item
                    for item in records.list_pending(task.conversation_id)
                    if item.task_id == task.task_id
                    and item.action_type == "approve_execution"
                    and item.target_id == str(view.action.action_id)
                ),
                None,
            )
            if existing is not None:
                uow.commit()
                return existing.token
            action = self._insert_pending(
                records,
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                action_type="approve_execution",
                target_id=str(view.action.action_id),
                expected_revision=task.revision,
                payload=payload,
                now=now,
            )
            uow.commit()
            return action.token

    def _execution_profile(self, view) -> dict[str, object]:
        binding = view.binding
        if binding is None:
            raise StateIntegrityError("execution profile has no binding")
        return {
            "profile": self.runtime_config.public_dict(),
            "profile_hash": self.runtime_config.profile_hash,
            "backend_kind": binding.backend_kind,
            "orca_version": binding.orca_version,
            "executable_sha256": binding.executable_sha256,
            "protocol_id": view.plan.protocol_id,
            "protocol_hash": view.plan.protocol_hash,
            "node_id": None if view.action is None else view.action.node_id,
            "budget": binding.budget.model_dump(mode="json"),
        }

    def _confirmation_card(self, task: TaskRecord, view) -> dict[str, object]:
        molecule: dict[str, object] = {
            "input_kind": None,
            "original_input": None,
            "name_cas_cid": None,
            "canonical_smiles": None,
            "formula": None,
            "charge": None,
            "multiplicity": None,
            "source": None,
            "unique_candidate": False,
            "preparation_errors": [],
        }
        request = task.request
        if request is not None:
            molecule["input_kind"] = (
                None if request.molecule_kind is None else request.molecule_kind.value
            )
            molecule["original_input"] = request.molecule_value
            molecule["charge"] = None if request.charge is None else request.charge.value
            molecule["multiplicity"] = (
                None if request.multiplicity is None else request.multiplicity.value
            )
            if request.molecule_kind is not None and request.molecule_kind.value in {
                "name",
                "cas",
                "cid",
            }:
                molecule["name_cas_cid"] = request.molecule_value
        if task.p4_run_id:
            try:
                p4_view = self.p4.inspect(RunId(task.p4_run_id))
            except Exception as error:
                molecule["preparation_errors"] = [
                    f"P4 identity state unavailable: {type(error).__name__}"
                ]
            else:
                confirmed = p4_view.confirmed_molecule
                if confirmed is not None:
                    molecule.update(
                        {
                            "canonical_smiles": confirmed.canonical_isomeric_smiles,
                            "formula": confirmed.molecular_formula,
                            "source": confirmed.provider.value,
                            "unique_candidate": True,
                        }
                    )
                elif getattr(task.request, "molecule_kind", None) is not MoleculeInputType.SMILES:
                    molecule["preparation_errors"] = [
                        "P4 has not produced a confirmed unique molecule identity"
                    ]
        else:
            molecule["preparation_errors"] = ["P4 identity run is missing"]
        return molecule

    def _ensure_p6(self, task: TaskRecord, p5_view) -> tuple[int, dict[str, object]]:
        now = _now(self.clock)
        existing = self._handoff(task, "p6")
        if existing is None:
            p6_run_id = new_id(RunId)
            command_id = new_id(CommandId)
            command = AssessP6Run.create(
                source_p5_run_id=p5_view.run_id,
                requested_at_utc=now,
                run_id=p6_run_id,
                command_id=command_id,
                profile_id=_P6_PROFILE_ID,
                expected_source_revision=p5_view.revision,
                reference_assessment_id=None,
            )
            existing = HandoffRecord.create(
                handoff_id=f"handoff_{uuid.uuid4().hex}",
                task_id=task.task_id,
                target="p6",
                command_id=str(command_id),
                child_id=str(p6_run_id),
                expected_revision=task.revision,
                payload=command.model_dump(mode="json"),
                created_at_utc=now,
            )
            with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
                uow.begin()
                records = P7RecordRepository(uow.connection)
                records.insert_handoff(existing)
                current = records.get_task(task.conversation_id, task.task_id)
                if current is None:
                    raise StateIntegrityError("task disappeared before P6 handoff")
                updated = self._task_update(
                    current, state=TaskPhase.ASSESSING, p6_run_id=str(p6_run_id), now=now
                )
                if not records.update_task(updated, expected_revision=current.revision):
                    raise RevisionConflictError("task changed before P6 handoff")
                uow.commit()
            task = updated
        return self._submit_p6_handoff(task, existing)

    def _submit_p6_handoff(
        self, task: TaskRecord, handoff: HandoffRecord
    ) -> tuple[int, dict[str, object]]:
        payload = thaw_json(handoff.payload)
        if not isinstance(payload, dict):
            raise StateIntegrityError("P6 handoff payload is invalid")
        if handoff.status == "linked":
            return 0, {"task_id": task.task_id, "step": "p6.already_linked"}
        command = AssessP6Run.model_validate_json(
            json.dumps(payload, ensure_ascii=False), strict=True
        )
        self._mark_handoff_submitted(handoff)
        result = self.p6.assess(command)
        now = _now(self.clock)
        updated_handoff = handoff.model_copy(
            update={
                "status": "linked" if result.accepted else "reconciliation_required",
                "target_run_id": str(command.run_id) if result.accepted else None,
                "updated_at_utc": now,
            }
        )
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            records.update_handoff(updated_handoff)
            current = records.get_task(task.conversation_id, task.task_id)
            if current is not None and not result.accepted:
                reconciled = self._task_update(
                    current,
                    state=TaskPhase.RECONCILIATION_REQUIRED,
                    stop_reason=StopReason.FAILED,
                    now=now,
                )
                records.update_task(reconciled, expected_revision=current.revision)
            uow.commit()
        return 1, {
            "task_id": task.task_id,
            "step": "p6.assess",
            "accepted": result.accepted,
            "run_id": str(command.run_id),
        }

    def _refresh_task_state_if_needed(self, task: TaskRecord, state: TaskPhase) -> None:
        current = self.get_task(task.conversation_id, task.task_id)
        if (
            current is not None
            and current.state is not state
            and current.state not in {TaskPhase.RESULT_READY, TaskPhase.ENDED_WITHOUT_RESULT}
        ):
            self._refresh_task_state(task.task_id, state)

    def _finish_without_science(self, task: TaskRecord, *, reason: StopReason) -> None:
        current = self.get_task(task.conversation_id, task.task_id)
        if current is None or current.state in {
            TaskPhase.RESULT_READY,
            TaskPhase.ENDED_WITHOUT_RESULT,
        }:
            return
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            latest = records.get_task(task.conversation_id, task.task_id)
            if latest is not None:
                ended = self._task_update(
                    latest,
                    state=TaskPhase.ENDED_WITHOUT_RESULT,
                    stop_reason=reason,
                    now=_now(self.clock),
                )
                records.stale_task_pending(latest.task_id, new_revision=ended.revision)
                records.update_task(ended, expected_revision=latest.revision)
            uow.commit()

    def _create_delivery(self, task: TaskRecord, *, p5_view, p6_view) -> DeliveryRecord:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            existing = records.get_delivery(task.task_id)
            if existing is not None:
                uow.commit()
                return existing
            current = records.get_task(task.conversation_id, task.task_id) or task
            spec = current.delivery_output_spec or (
                current.request.output_spec if current.request else OutputSpec.default()
            )
            delivery = self.presenter.build_delivery(
                current,
                p5_view=p5_view,
                p6_view=p6_view,
                output_spec=spec,
                now=_now(self.clock),
            )
            records.insert_delivery(delivery)
            next_state = TaskPhase.RESULT_READY
            stop_reason = current.stop_reason
            if p5_view is not None and p5_view.state.phase in {P5Phase.FAILED, P5Phase.CANCELLED}:
                stop_reason = (
                    StopReason.FAILED
                    if p5_view.state.phase is P5Phase.FAILED
                    else StopReason.CANCELLED
                )
            if p6_view is not None and p6_view.state.phase in {P6Phase.FAILED, P6Phase.CANCELLED}:
                stop_reason = (
                    StopReason.FAILED
                    if p6_view.state.phase is P6Phase.FAILED
                    else StopReason.CANCELLED
                )
            updated = self._task_update(
                current,
                state=next_state,
                stop_reason=stop_reason,
                current_delivery_id=delivery.delivery_id,
                now=_now(self.clock),
            )
            if not records.update_task(updated, expected_revision=current.revision):
                raise RevisionConflictError("task changed while publishing delivery")
            uow.commit()
            return delivery

    # Direct task controls -------------------------------------------
    def reconcile_task(
        self, conversation_id: ConversationId | str, task_id: str
    ) -> dict[str, object]:
        """Reconcile an existing P5 launch without creating a new job."""

        task = self.get_task(conversation_id, task_id)
        if task is None:
            raise ValueError("task was not found in this conversation")
        if not task.p5_run_id:
            raise InvalidTransitionError("task has no P5 execution to reconcile")
        p5_view = self.p5.inspect(RunId(task.p5_run_id))
        if p5_view.state.phase is P5Phase.NEEDS_RECONCILIATION:
            reports = self.p5.create_worker(allow_real_orca=self.allow_real_orca).run_once(
                run_id=p5_view.run_id, limit=1
            )
            p5_view = self.p5.inspect(p5_view.run_id)
        else:
            reports = ()
        phase = p5_view.state.phase
        if phase is P5Phase.NEEDS_RECONCILIATION:
            self._refresh_task_state_if_needed(task, TaskPhase.RECONCILIATION_REQUIRED)
        elif phase in {
            P5Phase.DISPATCH_PENDING,
            P5Phase.RUNNING,
            P5Phase.COLLECTING,
            P5Phase.CANCELLING,
        }:
            self._refresh_task_state_if_needed(task, TaskPhase.EXECUTING)
        elif phase is P5Phase.COMPLETED:
            self._refresh_task_state_if_needed(task, TaskPhase.ASSESSING)
        elif phase in {P5Phase.FAILED, P5Phase.CANCELLED}:
            self._create_delivery(task, p5_view=p5_view, p6_view=None)
        refreshed = self.get_task(conversation_id, task_id) or task
        return self._view(refreshed) | {
            "reconciled": True,
            "reports": [_as_json(item) for item in reports],
            "p5_phase": phase.value,
        }

    def cancel_task(
        self,
        conversation_id: ConversationId | str,
        task_id: str,
        *,
        expected_revision: int,
        reason_code: str = "user_cancelled",
    ) -> dict[str, object]:
        task = self.get_task(conversation_id, task_id)
        if task is None:
            raise ValueError("task was not found in this conversation")
        if task.revision != expected_revision:
            raise RevisionConflictError("task cancellation revision is stale")
        downstream: list[object] = []
        if task.p6_run_id:
            try:
                view = self.p6.inspect(RunId(task.p6_run_id))
                if view.state.phase not in {P6Phase.COMPLETED, P6Phase.FAILED, P6Phase.CANCELLED}:
                    downstream.append(
                        self.p6.cancel(
                            CancelP6Run.create(
                                run_id=view.run_id,
                                conversation_id=view.conversation_id,
                                expected_revision=view.revision,
                                requested_at_utc=_now(self.clock),
                                reason_code=reason_code,
                            )
                        )
                    )
            except Exception as error:
                downstream.append({"error": type(error).__name__})
        elif task.p5_run_id:
            try:
                view = self.p5.inspect(RunId(task.p5_run_id))
                if view.state.phase not in {P5Phase.COMPLETED, P5Phase.FAILED, P5Phase.CANCELLED}:
                    downstream.append(
                        self.p5.cancel(
                            run_id=view.run_id,
                            conversation_id=view.conversation_id,
                            expected_revision=view.revision,
                            reason_code=reason_code,
                        )
                    )
            except Exception as error:
                downstream.append({"error": type(error).__name__})
        elif task.p4_run_id:
            try:
                view = self.p4.inspect(RunId(task.p4_run_id))
                if view.state.phase not in {P4Phase.PLAN_READY, P4Phase.FAILED, P4Phase.CANCELLED}:
                    downstream.append(
                        self.p4.cancel(
                            CancelPlanningRun.create(
                                run_id=view.run_id,
                                conversation_id=view.conversation_id,
                                expected_revision=view.revision,
                                reason_code="user_cancelled",
                                requested_at_utc=_now(self.clock),
                            )
                        )
                    )
            except Exception as error:
                downstream.append({"error": type(error).__name__})
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            current = records.get_task(str(conversation_id), task_id)
            if current is None:
                raise ValueError("task was not found in this conversation")
            updated = self._task_update(
                current,
                state=TaskPhase.ENDED_WITHOUT_RESULT,
                stop_reason=StopReason.CANCELLED,
                now=_now(self.clock),
            )
            if not records.update_task(updated, expected_revision=expected_revision):
                raise RevisionConflictError("task cancellation raced with another update")
            uow.commit()
        return self._view(updated) | {"downstream": [_as_json(item) for item in downstream]}

    def link_existing_result(
        self, conversation_id: ConversationId | str, *, workflow: str, run_id: RunId | str
    ) -> dict[str, object]:
        if workflow not in {"p5", "p6"}:
            raise ValueError("workflow must be p5 or p6")
        p5_view = None
        p6_view = None
        if workflow == "p5":
            p5_view = self.p5.inspect(RunId(str(run_id)))
        else:
            p6_view = self.p6.inspect(RunId(str(run_id)))
            p5_view = self.p5.inspect(p6_view.source_p5_run_id)
        conversation = str(conversation_id)
        alias = f"linked-{workflow}-{run_id}"
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            existing = records.get_task_by_alias(conversation, alias)
            if existing is not None:
                uow.commit()
                return self._view(existing)
            task = TaskRecord(
                task_id=str(new_id(TaskId)),
                conversation_id=conversation,
                alias=alias,
                revision=1,
                state=TaskPhase.DRAFT,
                p5_run_id=str(p5_view.run_id) if p5_view is not None else None,
                p6_run_id=str(p6_view.run_id) if p6_view is not None else None,
                delivery_output_spec=OutputSpec.default(),
                created_at_utc=now,
                updated_at_utc=now,
            )
            records.insert_task(task)
            records.ensure_task_link(
                conversation_id=conversation, task_id=task.task_id, link_kind="historical", now=now
            )
            uow.commit()
        delivery = self._create_delivery(task, p5_view=p5_view, p6_view=p6_view)
        return self._view(self.get_task(conversation, task.task_id) or task) | {
            "delivery": delivery.model_dump(mode="json")
        }


def _fake_pubchem():
    from orca_agent.identity.fake_pubchem import FakePubChemAdapter

    return FakePubChemAdapter()


__all__ = ["P7TaskService"]
