"""P3 fake execution gateway with a short transaction / external call / short transaction split."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ApprovalGrantId,
    ConversationId,
    EffectId,
    EventId,
    ExecutionId,
    JobId,
    RunId,
)
from orca_agent.domain.models import ValidatedAction
from orca_agent.domain.p3 import (
    ApprovalGrantV1,
    ExecutionIntent,
    FakeJobResult,
    LedgerState,
    P3WorkflowState,
    WorkflowPhase,
    hash_model_fields,
)
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.p3_records import (
    ActionRepository,
    ArtifactRecordRepository,
    JobRepository,
    P3RecordRepository,
)
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.effect_receipts import EffectSuccessReceiptV1
from orca_agent.orchestration.effects import EffectClass
from orca_agent.planning.water import WaterFixture, load_water_fixture

from .fake_backend import FakeBackend

_P3_JOB_NAMESPACE = uuid.UUID("f7711d1e-c3c8-4ac4-9f19-2a8af0ee12be")


def build_idempotency_key(
    *,
    run_id: RunId,
    action_id: ActionId,
    action_hash: str,
    envelope_hash: str,
    budget_hash: str,
) -> str:
    """Return the stable key from the complete logical execution binding."""

    return "p3.fake." + sha256_hex(
        {
            "namespace": "p3.fake.water.v1",
            "run_id": str(run_id),
            "action_id": str(action_id),
            "action_hash": action_hash,
            "envelope_hash": envelope_hash,
            "budget_hash": budget_hash,
        }
    )


class FakeExecutionGateway:
    """Route only the registered fake dispatch effect to the offline backend."""

    def __init__(
        self,
        database_path: str | Path,
        state_root: str | Path,
        *,
        backend: FakeBackend | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.database_path = resolve_database_path(database_path)
        self.state_root = Path(state_root)
        self.clock = clock or SystemClock()
        self.backend = backend or FakeBackend(self.state_root, clock=self.clock)
        self.artifacts = ArtifactStore(self.state_root, clock=self.clock)

    def execute(self, permit: object):
        """Submit or recover one fake execution and return a typed worker result."""

        from orca_agent.infrastructure.worker import HandlerResult

        effect = getattr(permit, "effect", None)
        if effect is None or effect.effect_type != "external.p3.dispatch_fake":
            return HandlerResult(success=False)
        if effect.effect_class is not EffectClass.EXTERNAL:
            return HandlerResult(success=False)
        try:
            payload = dict(effect.payload)
            run_id = RunId(str(effect.run_id))
            payload_run_id = RunId(_payload_text(payload, "run_id"))
            if payload_run_id != run_id:
                raise StateIntegrityError("fake effect run binding is invalid")
            conversation_id = ConversationId(_payload_text(payload, "conversation_id"))
            action_id = ActionId(str(payload["action_id"]))
            action_hash = _payload_text(payload, "action_hash")
            envelope_hash = _payload_text(payload, "envelope_hash")
            budget_hash = _payload_text(payload, "budget_hash")
            approval_id = ApprovalGrantId(str(payload["approval_grant_id"]))
            execution_id = ExecutionId(str(payload["execution_id"]))
            idempotency_key = str(payload["idempotency_key"])
            if (
                payload.get("workflow_schema_version") != 2
                or payload.get("workflow_engine_version") != "p3-water-v1"
                or payload.get("fixture_id") != "water_sp_v1"
                or payload.get("fixture_version") != "1"
            ):
                raise StateIntegrityError("fake effect fixture or workflow version is invalid")
            action, approval, intent, fixture = self._load_binding(
                permit=permit,
                run_id=run_id,
                effect_id=EffectId(str(effect.effect_id)),
                source_event_id=effect.source_event_id,
                conversation_id=conversation_id,
                action_id=action_id,
                action_hash=action_hash,
                envelope_hash=envelope_hash,
                budget_hash=budget_hash,
                approval_id=approval_id,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
            )
            result = self.backend.submit_or_get(intent=intent, action=action, fixture=fixture)
            job = self._persist_result(
                permit=permit,
                run_id=run_id,
                action=action,
                approval=approval,
                intent=intent,
                fixture=fixture,
                result=result,
                source_event_id=effect.source_event_id,
            )
            return HandlerResult(
                success=True,
                result_summary=EffectSuccessReceiptV1(artifact_ids=(job.raw_result_artifact_id,)),
            )
        except Exception:
            return HandlerResult(success=False)

    def _load_binding(
        self,
        *,
        permit,
        run_id: RunId,
        effect_id: EffectId,
        source_event_id: EventId,
        conversation_id: ConversationId,
        action_id: ActionId,
        action_hash: str,
        envelope_hash: str,
        budget_hash: str,
        approval_id: ApprovalGrantId,
        execution_id: ExecutionId,
        idempotency_key: str,
    ) -> tuple[ValidatedAction, ApprovalGrantV1, ExecutionIntent, WaterFixture]:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StateIntegrityError("fake gateway kernel repositories are unavailable")
            uow.outbox.validate_handler_permit(permit=permit, now=self.clock.now_utc())
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if not isinstance(snapshot.state, P3WorkflowState):
                raise StateIntegrityError("fake gateway requires a P3 workflow")
            if (
                snapshot.state.phase is not WorkflowPhase.DISPATCH_PENDING
                or snapshot.state.dispatch_effect_id != effect_id
                or snapshot.state.conversation_id != conversation_id
            ):
                raise StateIntegrityError("fake gateway effect is not the current dispatch")
            if (
                snapshot.state.action_id != action_id
                or snapshot.state.action_hash != action_hash
                or snapshot.state.envelope_hash != envelope_hash
                or snapshot.state.budget_hash != budget_hash
                or snapshot.state.approval_grant_id != approval_id
                or snapshot.state.execution_id != execution_id
            ):
                raise StateIntegrityError("fake gateway workflow binding is invalid")
            actions = ActionRepository(uow.connection)
            stored = actions.get(action_id)
            if (
                stored is None
                or stored.run_id != run_id
                or stored.conversation_id != conversation_id
            ):
                raise StateIntegrityError("fake effect action binding is invalid")
            actual_envelope_hash = sha256_hex(stored.action.execution_envelope)
            actual_budget_hash = sha256_hex(stored.action.budget)
            if (
                stored.action.action_hash != action_hash
                or actual_envelope_hash != envelope_hash
                or actual_budget_hash != budget_hash
            ):
                raise StateIntegrityError("fake effect action hashes are invalid")
            expected_key = build_idempotency_key(
                run_id=run_id,
                action_id=stored.action.action_id,
                action_hash=stored.action.action_hash,
                envelope_hash=sha256_hex(stored.action.execution_envelope),
                budget_hash=sha256_hex(stored.action.budget),
            )
            if stored.idempotency_key != expected_key or stored.idempotency_key != idempotency_key:
                raise StateIntegrityError("fake effect idempotency key is invalid")
            if stored.approval_grant_id != approval_id or stored.execution_id != execution_id:
                raise StateIntegrityError("fake effect execution binding is invalid")
            approval_entry = P3RecordRepository(uow.connection).latest(
                run_id=run_id,
                record_type="approval_grant",
                model_type=ApprovalGrantV1,
            )
            if approval_entry is None or approval_entry[1].approval_grant_id != approval_id:
                raise StateIntegrityError("approval grant is missing")
            approval = approval_entry[1]
            if (
                stored.ledger_state is LedgerState.APPROVED
                and self.clock.now_utc() >= approval.expires_at_utc
            ):
                raise StateIntegrityError("approval grant has expired")
            if (
                approval.run_id != run_id
                or approval.conversation_id != conversation_id
                or approval.interrupt_id != snapshot.state.approval_interrupt_id
                or approval.action_id != stored.action.action_id
                or approval.action_hash != stored.action.action_hash
                or approval.source_revision != snapshot.revision - 1
            ):
                raise StateIntegrityError("approval action binding is invalid")
            if approval.envelope_hash != sha256_hex(stored.action.execution_envelope):
                raise StateIntegrityError("approval envelope binding is invalid")
            if approval.budget_hash != sha256_hex(stored.action.budget):
                raise StateIntegrityError("approval budget binding is invalid")
            if stored.ledger_state not in (
                LedgerState.APPROVED,
                LedgerState.SUBMITTING,
                LedgerState.SUBMITTED,
            ):
                raise StateIntegrityError("action is not executable")
            if stored.ledger_state is LedgerState.APPROVED:
                actions.update_ledger(
                    action_id=action_id,
                    state=LedgerState.SUBMITTING,
                    expected_state=LedgerState.APPROVED,
                    now=self.clock.now_utc(),
                )
            intent = ExecutionIntent.create(
                run_id=run_id,
                action_id=action_id,
                approval_grant_id=approval_id,
                idempotency_key=idempotency_key,
                execution_id=execution_id,
            )
            existing_intent = P3RecordRepository(uow.connection).latest(
                run_id=run_id,
                record_type="execution_intent",
                model_type=ExecutionIntent,
            )
            if existing_intent is not None:
                existing_value = existing_intent[1]
                if (
                    existing_value.run_id != run_id
                    or existing_value.action_id != action_id
                    or existing_value.approval_grant_id != approval_id
                    or existing_value.execution_id != execution_id
                    or existing_value.idempotency_key != idempotency_key
                ):
                    raise StateIntegrityError("execution intent binding is invalid")
                intent = existing_intent[1]
            else:
                raise StateIntegrityError("approved execution intent is missing")
            uow.commit()
        return stored.action, approval, intent, load_water_fixture()

    def _persist_result(
        self,
        *,
        permit,
        run_id: RunId,
        action: ValidatedAction,
        approval: ApprovalGrantV1,
        intent: ExecutionIntent,
        fixture: WaterFixture,
        result,
        source_event_id: EventId,
    ) -> FakeJobResult:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            uow.outbox.validate_handler_permit(permit=permit, now=self.clock.now_utc())
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if (
                not isinstance(snapshot.state, P3WorkflowState)
                or snapshot.state.phase is not WorkflowPhase.DISPATCH_PENDING
                or snapshot.state.dispatch_effect_id != permit.effect.effect_id
                or snapshot.state.action_id != action.action_id
                or snapshot.state.action_hash != action.action_hash
                or snapshot.state.approval_grant_id != approval.approval_grant_id
                or snapshot.state.execution_id != intent.execution_id
                or source_event_id != permit.effect.source_event_id
            ):
                raise StateIntegrityError("result is no longer the current dispatch")
            actions = ActionRepository(uow.connection)
            stored = actions.get(action.action_id)
            if (
                stored is None
                or stored.run_id != run_id
                or stored.conversation_id != approval.conversation_id
                or stored.approval_grant_id != approval.approval_grant_id
                or stored.execution_id != intent.execution_id
                or stored.action != action
                or stored.idempotency_key != intent.idempotency_key
                or stored.ledger_state not in (LedgerState.SUBMITTING, LedgerState.SUBMITTED)
            ):
                raise StateIntegrityError("action disappeared before result persistence")
            expected_input_hash = sha256_hex(
                {
                    "action_hash": action.action_hash,
                    "execution_id": str(intent.execution_id),
                    "fixture_hash": fixture.fixture_hash,
                }
            )
            if (
                result.execution_id != intent.execution_id
                or result.input_hash != expected_input_hash
            ):
                raise StateIntegrityError("fake result execution binding is invalid")
            if result.fixture_hash != fixture.fixture_hash:
                raise StateIntegrityError("fake result fixture binding is invalid")
            _verify_raw_result(
                result.raw_result,
                action=action,
                execution_id=intent.execution_id,
                input_hash=expected_input_hash,
                fixture=fixture,
            )
            existing = JobRepository(uow.connection).get_by_execution(result.execution_id)
            if existing is not None:
                if (
                    existing.run_id != run_id
                    or existing.action_id != action.action_id
                    or existing.execution_id != intent.execution_id
                    or existing.input_hash != expected_input_hash
                    or existing.fixture_id != fixture.fixture_id
                    or existing.fixture_version != fixture.fixture_version
                    or existing.status != "succeeded"
                    or existing.raw_result_artifact_id is None
                    or existing.result_hash is None
                ):
                    raise StateIntegrityError("existing fake job binding is invalid")
                job_entry = P3RecordRepository(uow.connection).latest(
                    run_id=run_id,
                    record_type="fake_job_result",
                    model_type=FakeJobResult,
                )
                if job_entry is None or job_entry[1].job_id != existing.job_id:
                    raise StateIntegrityError("existing fake job record is missing")
                job_record = job_entry[1]
                if (
                    job_record.run_id != run_id
                    or job_record.action_id != action.action_id
                    or job_record.execution_id != intent.execution_id
                    or job_record.input_hash != expected_input_hash
                    or job_record.fixture_hash != fixture.fixture_hash
                    or job_record.raw_result_artifact_id != existing.raw_result_artifact_id
                    or job_record.result_hash != existing.result_hash
                ):
                    raise StateIntegrityError("existing fake job record binding is invalid")
                artifact = ArtifactRecordRepository(uow.connection).get(
                    existing.raw_result_artifact_id
                )
                if (
                    artifact is None
                    or artifact.run_id != run_id
                    or artifact.action_id != action.action_id
                    or artifact.execution_id != intent.execution_id
                ):
                    raise StateIntegrityError("existing fake result artifact binding is invalid")
                _verify_raw_result(
                    self.artifacts.read(artifact),
                    action=action,
                    execution_id=intent.execution_id,
                    input_hash=expected_input_hash,
                    fixture=fixture,
                )
                uow.commit()
                return FakeJobResult(
                    job_id=existing.job_id,
                    schema_version=2,
                    engine_version="p3-water-v1",
                    run_id=existing.run_id,
                    action_id=existing.action_id,
                    execution_id=existing.execution_id,
                    input_hash=existing.input_hash,
                    fixture_id="water_sp_v1",
                    fixture_version="1",
                    fixture_hash=fixture.fixture_hash,
                    status="succeeded",
                    raw_result_artifact_id=existing.raw_result_artifact_id,
                    result_hash=existing.result_hash,
                )
            artifact = self.artifacts.put(
                connection=uow.connection,
                run_id=run_id,
                content=result.raw_result,
                media_type="application/json",
                action_id=action.action_id,
                execution_id=result.execution_id,
            )
            values = {
                "job_id": _deterministic_job_id(result.execution_id),
                "schema_version": 2,
                "engine_version": "p3-water-v1",
                "run_id": run_id,
                "action_id": action.action_id,
                "execution_id": result.execution_id,
                "input_hash": result.input_hash,
                "fixture_id": fixture.fixture_id,
                "fixture_version": fixture.fixture_version,
                "fixture_hash": fixture.fixture_hash,
                "status": "succeeded",
                "raw_result_artifact_id": artifact.artifact_id,
            }
            job = FakeJobResult(
                **values,
                result_hash=hash_model_fields(FakeJobResult, values, exclude="result_hash"),
            )
            JobRepository(uow.connection).insert(job, now=self.clock.now_utc())
            actions.update_ledger(
                action_id=action.action_id,
                state=LedgerState.SUBMITTED,
                expected_state=LedgerState.SUBMITTING,
                now=self.clock.now_utc(),
            )
            P3RecordRepository(uow.connection).append(
                run_id=run_id,
                record_type="fake_job_result",
                record=job,
                created_at_utc=self.clock.now_utc(),
                source_event_id=source_event_id,
            )
            uow.commit()
            return job


def _payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StateIntegrityError(f"fake effect payload is missing {key}")
    return value


def _deterministic_job_id(execution_id: ExecutionId) -> JobId:
    return JobId(f"job_{uuid.uuid5(_P3_JOB_NAMESPACE, f'job:{execution_id}').hex}")


def _verify_raw_result(
    content: bytes,
    *,
    action: ValidatedAction,
    execution_id: ExecutionId,
    input_hash: str,
    fixture: WaterFixture,
) -> None:
    if not isinstance(content, bytes):
        raise StateIntegrityError("fake result content is not bytes")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateIntegrityError("fake result content is not valid JSON") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != content:
        raise StateIntegrityError("fake result content is not canonical JSON")
    expected = {
        "format": "p3-fake-water-result-v1",
        "schema_version": 2,
        "engine_version": "p3-water-v1",
        "execution_id": str(execution_id),
        "action_id": str(action.action_id),
        "input_hash": input_hash,
        "fixture_id": fixture.fixture_id,
        "fixture_version": fixture.fixture_version,
        "fixture_hash": fixture.fixture_hash,
        "energy": fixture.energy,
        "unit": fixture.unit,
        "source": fixture.source,
    }
    if value != expected:
        raise StateIntegrityError("fake result content binding is invalid")


__all__ = ["FakeExecutionGateway", "build_idempotency_key"]
