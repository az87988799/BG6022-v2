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
from orca_agent.domain.p5 import P5Phase
from orca_agent.domain.p6 import P6Phase
from orca_agent.domain.p7_conversation import MoleculeInputType
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
from orca_agent.planning.p5_protocols import P5_DEFAULT_PROTOCOL
from orca_agent.planning.p7_catalog import CapabilityCatalog, build_capability_catalog
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
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.clock = clock or SystemClock()
        self.catalog = catalog or build_capability_catalog()
        self.p4 = p4_service or P4ApplicationService(
            self.state_root,
            clock=self.clock,
            fake_adapter=_fake_pubchem(),
            allow_network=allow_network,
        )
        self.p5 = p5_service or P5ApplicationService(
            self.state_root,
            clock=self.clock,
            backend_kind=backend_kind,
            allow_real_orca=allow_real_orca,
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
            uow.commit()
        result: dict[str, object] = {
            "task": task.model_dump(mode="json"),
            "pending_actions": [
                item.model_dump(mode="json") for item in pending if item.task_id == task.task_id
            ],
            "delivery": None if delivery is None else delivery.model_dump(mode="json"),
        }
        if task.p4_run_id:
            result["p4"] = self._safe_inspect(self.p4.inspect, RunId(task.p4_run_id))
        if task.p5_run_id:
            result["p5"] = self._safe_inspect(self.p5.inspect, RunId(task.p5_run_id))
        if task.p6_run_id:
            result["p6"] = self._safe_inspect(self.p6.inspect, RunId(task.p6_run_id))
        return result

    @staticmethod
    def _safe_inspect(function, run_id: RunId) -> object:
        try:
            value = function(run_id)
            return _as_json(value)
        except Exception as error:
            return {"available": False, "error": type(error).__name__}

    # Task creation and token handling -------------------------------
    def create_task(
        self,
        conversation_id: ConversationId | str,
        *,
        alias: str | None,
        normalized,
        turn_id: str,
    ) -> dict[str, object]:
        if normalized.request is None or normalized.validation is None:
            raise ValueError("normalized P7 plan is incomplete")
        conversation = str(conversation_id)
        now = _now(self.clock)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            chosen_alias = self._unique_alias(records, conversation, alias)
            state = (
                TaskPhase.PLAN_READY
                if normalized.validation.status is ValidationStatus.VALID
                else TaskPhase.NEEDS_CLARIFICATION
                if normalized.validation.status is ValidationStatus.NEEDS_CLARIFICATION
                else TaskPhase.ENDED_WITHOUT_RESULT
            )
            task = TaskRecord(
                task_id=str(new_id(TaskId)),
                conversation_id=conversation,
                alias=chosen_alias,
                revision=1,
                state=state,
                stop_reason=(
                    StopReason.UNSUPPORTED
                    if normalized.validation.status is ValidationStatus.UNSUPPORTED
                    else None
                ),
                request=normalized.request,
                plan=normalized.plan,
                validation=normalized.validation,
                delivery_output_spec=normalized.request.output_spec,
                created_at_utc=now,
                updated_at_utc=now,
            )
            records.insert_task(task)
            records.ensure_task_link(
                conversation_id=conversation,
                task_id=task.task_id,
                link_kind="active",
                now=now,
            )
            if state is TaskPhase.PLAN_READY:
                self._insert_pending(
                    records,
                    conversation_id=conversation,
                    task_id=task.task_id,
                    action_type="accept_plan",
                    target_id=task.task_id,
                    expected_revision=task.revision,
                    payload={
                        "task_id": task.task_id,
                        "task_revision": task.revision,
                        "plan_hash": normalized.plan.plan_hash,
                        "validation_hash": normalized.validation.validation_hash,
                        "output_spec_hash": normalized.request.output_spec.output_spec_hash,
                        "capability_id": normalized.plan.capability_id,
                        "capability_version": normalized.plan.capability_version,
                    },
                    now=now,
                )
            uow.commit()
        return self._view(task)

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
                else IdentityProvider.FAKE.value
            ),
            "protocol_id": _P4_PROTOCOL_ID,
            "new_conversation": True,
            "requested_at_utc": format_utc(now),
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
        p4_handoff = self._handoff(task, "p4")
        if p4_handoff is not None and p4_handoff.status in {"prepared", "submitted"}:
            result = self._submit_p4_handoff(task, p4_handoff)
            return 1, {
                "task_id": task.task_id,
                "step": "p4.start.reconciled",
                "accepted": result.accepted,
            }
        if task.p4_run_id is None:
            return 0, {"task_id": task.task_id, "step": "waiting_for_plan_acceptance"}

        p4_view = self.p4.inspect(RunId(task.p4_run_id))
        if p4_view.state.phase is P4Phase.RESOLVING_IDENTITY:
            reports = self.p4.create_worker().run_once(limit=1)
            return len(reports), {
                "task_id": task.task_id,
                "step": "p4.worker",
                "reports": [_as_json(item) for item in reports],
            }
        if p4_view.state.phase is P4Phase.AWAITING_IDENTITY:
            replay = self._replay_accepted_decision(
                task, action_type="confirm_identity", target_id=str(p4_view.run_id)
            )
            if replay is not None:
                return replay
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
            self._refresh_task_state_if_needed(task, TaskPhase.EXECUTION_PENDING)
            latest = self.get_task(task.conversation_id, task.task_id) or task
            replay = self._replay_accepted_decision(
                latest,
                action_type="approve_execution",
                target_id=str(p5_view.action.action_id) if p5_view.action is not None else None,
            )
            if replay is not None:
                return replay
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

    def _ensure_identity_action(self, task: TaskRecord, view) -> str | None:
        now = _now(self.clock)
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
            self._finish_without_science(task, reason=StopReason.UNSUPPORTED)
            return None
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
        key = request.molecule_value.casefold().strip()
        known = {
            (MoleculeInputType.NAME, "water"): "O",
            (MoleculeInputType.NAME, "ethanol"): "CCO",
            (MoleculeInputType.CAS, "7732-18-5"): "O",
            (MoleculeInputType.CAS, "64-17-5"): "CCO",
            (MoleculeInputType.CID, "962"): "O",
            (MoleculeInputType.CID, "702"): "CCO",
        }
        if request.molecule_kind is MoleculeInputType.NAME and key == "酒精":
            key = "ethanol"
        return known.get((request.molecule_kind, key))

    def _ensure_p5(self, task: TaskRecord, p4_view) -> tuple[int, dict[str, object]]:
        now = _now(self.clock)
        existing = self._handoff(task, "p5")
        if existing is None:
            p5_run_id = new_id(RunId)
            command_id = new_id(CommandId)
            payload = {
                "command_type": "p5.create_execution",
                "command_id": str(command_id),
                "run_id": str(p5_run_id),
                "source_run_id": str(p4_view.run_id),
                "protocol_id": P5_DEFAULT_PROTOCOL.protocol_id,
                "external_opt_result_id": None,
                "wall_time_seconds": None,
                "requested_at_utc": format_utc(now),
            }
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

    def _submit_p5_handoff(
        self, task: TaskRecord, handoff: HandoffRecord
    ) -> tuple[int, dict[str, object]]:
        payload = thaw_json(handoff.payload)
        if not isinstance(payload, dict):
            raise StateIntegrityError("P5 handoff payload is invalid")
        if handoff.status == "linked":
            return 0, {"task_id": task.task_id, "step": "p5.already_linked"}
        self._mark_handoff_submitted(handoff)
        result = self.p5.prepare_execution(
            source_run_id=RunId(str(payload["source_run_id"])),
            protocol_id=str(payload["protocol_id"]),
            run_id=RunId(str(payload["run_id"])),
            command_id=CommandId(str(payload["command_id"])),
            external_opt_result_id=None,
            wall_time_seconds=None,
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

    def _ensure_approval_action(self, task: TaskRecord, view) -> str:
        if view.action is None or view.binding is None:
            raise StateIntegrityError("P5 approval phase has no action/binding")
        now = _now(self.clock)
        payload = {
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
        }
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
