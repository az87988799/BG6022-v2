"""SQLite repositories for P3 workflow, action, job, artifact, and evidence records."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.errors import DomainError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ApprovalGrantId,
    ArtifactId,
    ConversationId,
    EventId,
    EvidenceId,
    ExecutionId,
    JobId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.models import (
    EvidenceRecord,
    PlanProposal,
    ProblemSpec,
    ValidatedAction,
    ValidatedClaim,
)
from orca_agent.domain.p3 import (
    ApprovalGrantV1,
    ExecutionIntent,
    FakeJobResult,
    FixtureScientificAssessment,
    LedgerState,
    P3Model,
    P3WorkflowState,
    ReportManifestV1,
)
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.p3_versions import P3_ENGINE_VERSION, P3_SCHEMA_VERSION

from .clock import format_utc, parse_utc
from .repositories import json_text, json_value, stored_int

ModelT = TypeVar("ModelT", bound=BaseModel)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_RECORD_CONTRACTS: dict[str, tuple[type[BaseModel], int, str]] = {
    "problem_spec": (ProblemSpec, CURRENT_SCHEMA_VERSION, "p1-domain-v1"),
    "plan_proposal": (PlanProposal, CURRENT_SCHEMA_VERSION, "p1-domain-v1"),
    "action_contract": (ValidatedAction, CURRENT_SCHEMA_VERSION, "p1-domain-v1"),
    "claim": (ValidatedClaim, CURRENT_SCHEMA_VERSION, "p1-domain-v1"),
    "workflow_state": (P3WorkflowState, P3_SCHEMA_VERSION, P3_ENGINE_VERSION),
    "approval_grant": (ApprovalGrantV1, P3_SCHEMA_VERSION, P3_ENGINE_VERSION),
    "execution_intent": (ExecutionIntent, P3_SCHEMA_VERSION, P3_ENGINE_VERSION),
    "fake_job_result": (FakeJobResult, P3_SCHEMA_VERSION, P3_ENGINE_VERSION),
    "assessment": (FixtureScientificAssessment, P3_SCHEMA_VERSION, P3_ENGINE_VERSION),
    "report_manifest": (ReportManifestV1, P3_SCHEMA_VERSION, P3_ENGINE_VERSION),
}


def _record_contract(record_type: str) -> tuple[type[BaseModel], int, str]:
    try:
        return _RECORD_CONTRACTS[record_type]
    except KeyError as error:
        raise StateIntegrityError("unsupported P3 workflow record type") from error


def _model_json(value: BaseModel) -> str:
    return json_text(value.model_dump(mode="json"))


def _parse_model(value: str, model_type: type[ModelT], *, what: str) -> ModelT:
    try:
        return model_type.model_validate_json(value, strict=True)
    except (TypeError, ValueError, ValidationError, DomainError, ArithmeticError) as error:
        raise StateIntegrityError(f"stored {what} is invalid") from error


def _record_hash(value: BaseModel) -> str:
    return sha256_hex(value)


class P3RecordRepository:
    """Append-only typed P3 records with strict schema and hash verification."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append(
        self,
        *,
        run_id: RunId,
        record_type: str,
        record: P3Model,
        created_at_utc: datetime,
        source_event_id: object | None = None,
        record_id: WorkflowRecordId | None = None,
    ) -> WorkflowRecordId:
        if getattr(record, "schema_version", None) != P3_SCHEMA_VERSION:
            raise StateIntegrityError("P3 record schema version is unsupported")
        if getattr(record, "engine_version", None) != P3_ENGINE_VERSION:
            raise StateIntegrityError("P3 record engine version is unsupported")
        return self.append_any(
            run_id=run_id,
            record_type=record_type,
            record=record,
            schema_version=P3_SCHEMA_VERSION,
            engine_version=P3_ENGINE_VERSION,
            created_at_utc=created_at_utc,
            source_event_id=source_event_id,
            record_id=record_id,
        )

    def append_any(
        self,
        *,
        run_id: RunId,
        record_type: str,
        record: BaseModel,
        schema_version: int,
        engine_version: str,
        created_at_utc: datetime,
        source_event_id: object | None = None,
        record_id: WorkflowRecordId | None = None,
    ) -> WorkflowRecordId:
        if not record_type.strip():
            raise ValueError("record_type must not be blank")
        expected_model, expected_schema, expected_engine = _record_contract(record_type)
        if not isinstance(record, expected_model):
            raise StateIntegrityError("record type does not match its typed contract")
        if type(schema_version) is not int or schema_version < 1:
            raise ValueError("record schema_version must be positive")
        if not engine_version.strip():
            raise ValueError("record engine_version must not be blank")
        if schema_version != expected_schema or engine_version != expected_engine:
            raise StateIntegrityError("record type has an unsupported storage version")
        record_schema = getattr(record, "schema_version", schema_version)
        record_engine = getattr(record, "engine_version", engine_version)
        if record_schema != schema_version or record_engine != engine_version:
            raise StateIntegrityError("record metadata does not match its storage envelope")
        identifier = record_id or new_id(WorkflowRecordId)
        record_json = _model_json(record)
        record_hash = _record_hash(record)
        try:
            self.connection.execute(
                "INSERT INTO workflow_records(record_id, run_id, record_type, schema_version, "
                "engine_version, record_json, record_hash, source_event_id, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(identifier),
                    str(run_id),
                    record_type,
                    schema_version,
                    engine_version,
                    record_json,
                    record_hash,
                    None if source_event_id is None else str(source_event_id),
                    format_utc(created_at_utc),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("P3 workflow record violates an invariant") from error
        return identifier

    def latest(
        self,
        *,
        run_id: RunId,
        record_type: str,
        model_type: type[ModelT],
    ) -> tuple[WorkflowRecordId, ModelT] | None:
        return self.latest_any(
            run_id=run_id,
            record_type=record_type,
            model_type=model_type,
            schema_version=P3_SCHEMA_VERSION,
            engine_version=P3_ENGINE_VERSION,
        )

    def latest_any(
        self,
        *,
        run_id: RunId,
        record_type: str,
        model_type: type[ModelT],
        schema_version: int,
        engine_version: str,
    ) -> tuple[WorkflowRecordId, ModelT] | None:
        expected_model, expected_schema, expected_engine = _record_contract(record_type)
        if (
            model_type is not expected_model
            or schema_version != expected_schema
            or engine_version != expected_engine
        ):
            raise StateIntegrityError("record lookup does not match its typed contract")
        rows = self.connection.execute(
            "SELECT record_id, schema_version, engine_version, record_json, record_hash, "
            "source_event_id "
            "FROM workflow_records WHERE run_id = ? AND record_type = ? "
            "AND schema_version = ? AND engine_version = ? "
            "ORDER BY created_at_utc DESC, rowid DESC LIMIT 1",
            (str(run_id), record_type, schema_version, engine_version),
        ).fetchone()
        if rows is None:
            return None
        try:
            record_id = WorkflowRecordId(str(rows[0]))
            stored_schema = stored_int(rows[1], what="P3 record schema_version", minimum=1)
            if stored_schema != schema_version or str(rows[2]) != engine_version:
                raise StateIntegrityError("stored workflow record version is unsupported")
            record = _parse_model(str(rows[3]), expected_model, what="P3 record")
            if _model_json(record) != str(rows[3]):
                raise StateIntegrityError("stored P3 record is not canonical JSON")
            stored_hash = str(rows[4])
            if _record_hash(record) != stored_hash:
                raise StateIntegrityError("stored P3 record hash does not match")
            if getattr(record, "run_id", run_id) != run_id:
                raise StateIntegrityError("stored P3 record belongs to another run")
            _verify_source_event(self.connection, rows[5], run_id)
            return record_id, record
        except StateIntegrityError:
            raise
        except (DomainError, TypeError, ValueError, ArithmeticError) as error:
            raise StateIntegrityError("stored P3 workflow record is invalid") from error

    def list_for_run(self, run_id: RunId) -> tuple[tuple[WorkflowRecordId, str, object], ...]:
        rows = self.connection.execute(
            "SELECT record_id, record_type, schema_version, engine_version, record_json, "
            "record_hash, source_event_id, created_at_utc FROM workflow_records "
            "WHERE run_id = ? ORDER BY created_at_utc, rowid",
            (str(run_id),),
        ).fetchall()
        values: list[tuple[WorkflowRecordId, str, object]] = []
        for row in rows:
            try:
                record_id = WorkflowRecordId(str(row[0]))
                record_type = str(row[1])
                model_type, expected_schema, expected_engine = _record_contract(record_type)
                stored_schema = stored_int(row[2], what="P3 record schema_version", minimum=1)
                if stored_schema != expected_schema or str(row[3]) != expected_engine:
                    raise StateIntegrityError("stored P3 record version is unsupported")
                payload = json_value(str(row[4]), what="P3 record")
                record = _parse_model(str(row[4]), model_type, what="P3 record")
                if _model_json(record) != str(row[4]):
                    raise StateIntegrityError("stored workflow record is not canonical JSON")
                if _HASH_PATTERN.fullmatch(str(row[5])) is None:
                    raise StateIntegrityError("stored workflow record hash is invalid")
                if sha256_hex(record) != str(row[5]) or sha256_hex(payload) != str(row[5]):
                    raise StateIntegrityError("stored P3 record hash does not match")
                _verify_source_event(self.connection, row[6], run_id)
                parse_utc(str(row[7]))
                values.append((record_id, record_type, record))
            except StateIntegrityError:
                raise
            except (DomainError, TypeError, ValueError, ArithmeticError) as error:
                raise StateIntegrityError("stored P3 workflow record is invalid") from error
        return tuple(values)


def _verify_source_event(
    connection: sqlite3.Connection, raw_event_id: object | None, run_id: RunId
) -> None:
    if raw_event_id is None:
        return
    try:
        event_id = EventId(str(raw_event_id))
    except DomainError as error:
        raise StateIntegrityError("workflow record source event ID is invalid") from error
    from .repositories import EventRepository

    event = EventRepository(connection).get(event_id)
    if event is None or event.run_id != run_id:
        raise StateIntegrityError("workflow record source event does not belong to the run")


@dataclass(frozen=True)
class StoredAction:
    action: ValidatedAction
    run_id: RunId
    conversation_id: ConversationId
    idempotency_key: str
    approval_grant_id: ApprovalGrantId | None
    execution_id: ExecutionId | None
    ledger_state: LedgerState
    created_at_utc: datetime
    updated_at_utc: datetime


class ActionRepository:
    """Persist immutable action identity and a fenced execution ledger state."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(
        self,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        action: ValidatedAction,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        envelope_hash = sha256_hex(action.execution_envelope)
        budget_hash = sha256_hex(action.budget)
        try:
            self.connection.execute(
                "INSERT INTO actions(action_id, run_id, conversation_id, action_json, action_hash, "
                "envelope_hash, budget_hash, idempotency_key, approval_grant_id, execution_id, "
                "ledger_state, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, ?, ?, ?)",
                (
                    str(action.action_id),
                    str(run_id),
                    str(conversation_id),
                    _model_json(action),
                    action.action_hash,
                    envelope_hash,
                    budget_hash,
                    idempotency_key,
                    LedgerState.PLANNED.value,
                    format_utc(now),
                    format_utc(now),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("action identity violates an invariant") from error

    def _load(self, row: sqlite3.Row) -> StoredAction:
        try:
            action_id = ActionId(str(row[0]))
            action = _parse_model(str(row[3]), ValidatedAction, what="action")
            if action.action_id != action_id or action.action_hash != str(row[4]):
                raise StateIntegrityError("stored action hash or ID does not match")
            if sha256_hex(action.execution_envelope) != str(row[5]):
                raise StateIntegrityError("stored action envelope hash does not match")
            if sha256_hex(action.budget) != str(row[6]):
                raise StateIntegrityError("stored action budget hash does not match")
            run_id = RunId(str(row[1]))
            conversation_id = ConversationId(str(row[2]))
            state = LedgerState(str(row[10]))
            approval = None if row[8] is None else ApprovalGrantId(str(row[8]))
            execution = None if row[9] is None else ExecutionId(str(row[9]))
            return StoredAction(
                action=action,
                run_id=run_id,
                conversation_id=conversation_id,
                idempotency_key=str(row[7]),
                approval_grant_id=approval,
                execution_id=execution,
                ledger_state=state,
                created_at_utc=parse_utc(str(row[11])),
                updated_at_utc=parse_utc(str(row[12])),
            )
        except StateIntegrityError:
            raise
        except (DomainError, TypeError, ValueError, ValidationError, ArithmeticError) as error:
            raise StateIntegrityError("stored action is invalid") from error

    def get(self, action_id: ActionId) -> StoredAction | None:
        row = self.connection.execute(
            "SELECT action_id, run_id, conversation_id, action_json, action_hash, envelope_hash, "
            "budget_hash, idempotency_key, approval_grant_id, execution_id, ledger_state, "
            "created_at_utc, updated_at_utc FROM actions WHERE action_id = ?",
            (str(action_id),),
        ).fetchone()
        return None if row is None else self._load(row)

    def get_by_run(self, run_id: RunId) -> StoredAction | None:
        row = self.connection.execute(
            "SELECT action_id, run_id, conversation_id, action_json, action_hash, envelope_hash, "
            "budget_hash, idempotency_key, approval_grant_id, execution_id, ledger_state, "
            "created_at_utc, updated_at_utc FROM actions WHERE run_id = ? "
            "ORDER BY action_id LIMIT 1",
            (str(run_id),),
        ).fetchone()
        return None if row is None else self._load(row)

    def get_by_idempotency(self, key: str) -> StoredAction | None:
        row = self.connection.execute(
            "SELECT action_id, run_id, conversation_id, action_json, action_hash, envelope_hash, "
            "budget_hash, idempotency_key, approval_grant_id, execution_id, ledger_state, "
            "created_at_utc, updated_at_utc FROM actions WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return None if row is None else self._load(row)

    def update_ledger(
        self,
        *,
        action_id: ActionId,
        state: LedgerState,
        now: datetime,
        approval_grant_id: ApprovalGrantId | None = None,
        execution_id: ExecutionId | None = None,
        expected_state: LedgerState | None = None,
    ) -> StoredAction:
        if not isinstance(state, LedgerState):
            raise ValueError("ledger state must be a LedgerState")
        current = self.get(action_id)
        if current is None:
            raise StateIntegrityError("action ledger row was not found")
        if expected_state is not None and current.ledger_state is not expected_state:
            raise StateIntegrityError("action ledger expected state differs")
        allowed = {
            LedgerState.PLANNED: {LedgerState.APPROVED, LedgerState.CANCELLED, LedgerState.FAILED},
            LedgerState.APPROVED: {
                LedgerState.SUBMITTING,
                LedgerState.SUBMITTED,
                LedgerState.FAILED,
                LedgerState.CANCELLED,
            },
            LedgerState.SUBMITTING: {
                LedgerState.SUBMITTED,
                LedgerState.FAILED,
                LedgerState.CANCELLED,
            },
            LedgerState.SUBMITTED: {LedgerState.SUCCEEDED, LedgerState.FAILED},
            LedgerState.SUCCEEDED: set(),
            LedgerState.FAILED: set(),
            LedgerState.CANCELLED: set(),
        }
        if state is not current.ledger_state and state not in allowed[current.ledger_state]:
            raise StateIntegrityError("action ledger transition is not allowed")
        if (
            approval_grant_id is not None
            and current.approval_grant_id is not None
            and approval_grant_id != current.approval_grant_id
        ):
            raise StateIntegrityError("action approval binding cannot be replaced")
        if (
            execution_id is not None
            and current.execution_id is not None
            and execution_id != current.execution_id
        ):
            raise StateIntegrityError("action execution binding cannot be replaced")
        cursor = self.connection.execute(
            "UPDATE actions SET ledger_state = ?, "
            "approval_grant_id = COALESCE(?, approval_grant_id), "
            "execution_id = COALESCE(?, execution_id), updated_at_utc = ? WHERE action_id = ? "
            "AND ledger_state = ?",
            (
                state.value,
                None if approval_grant_id is None else str(approval_grant_id),
                None if execution_id is None else str(execution_id),
                format_utc(now),
                str(action_id),
                current.ledger_state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise StateIntegrityError("action ledger update was lost")
        stored = self.get(action_id)
        if stored is None:
            raise StateIntegrityError("updated action disappeared")
        return stored


@dataclass(frozen=True)
class StoredJob:
    job_id: JobId
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    input_hash: str
    fixture_id: str
    fixture_version: str
    fixture_hash: str
    status: str
    raw_result_artifact_id: ArtifactId | None
    result_hash: str | None
    created_at_utc: datetime
    updated_at_utc: datetime


class JobRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(self, result: FakeJobResult, *, now: datetime) -> None:
        if not isinstance(result, FakeJobResult):
            raise TypeError("job result must be a FakeJobResult")
        try:
            self.connection.execute(
                "INSERT INTO jobs(job_id, run_id, action_id, execution_id, input_hash, fixture_id, "
                "fixture_version, fixture_hash, status, raw_result_artifact_id, result_hash, "
                "created_at_utc, "
                "updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(result.job_id),
                    str(result.run_id),
                    str(result.action_id),
                    str(result.execution_id),
                    result.input_hash,
                    result.fixture_id,
                    result.fixture_version,
                    result.fixture_hash,
                    result.status,
                    str(result.raw_result_artifact_id),
                    result.result_hash,
                    format_utc(now),
                    format_utc(now),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("job record violates an invariant") from error

    def get_by_execution(self, execution_id: ExecutionId) -> StoredJob | None:
        row = self.connection.execute(
            "SELECT job_id, run_id, action_id, execution_id, input_hash, fixture_id, "
            "fixture_version, fixture_hash, status, raw_result_artifact_id, result_hash, "
            "created_at_utc, "
            "updated_at_utc FROM jobs WHERE execution_id = ?",
            (str(execution_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            stored = StoredJob(
                job_id=JobId(str(row[0])),
                run_id=RunId(str(row[1])),
                action_id=ActionId(str(row[2])),
                execution_id=ExecutionId(str(row[3])),
                input_hash=str(row[4]),
                fixture_id=str(row[5]),
                fixture_version=str(row[6]),
                fixture_hash=str(row[7]),
                status=str(row[8]),
                raw_result_artifact_id=(None if row[9] is None else ArtifactId(str(row[9]))),
                result_hash=None if row[10] is None else str(row[10]),
                created_at_utc=parse_utc(str(row[11])),
                updated_at_utc=parse_utc(str(row[12])),
            )
            if (
                _HASH_PATTERN.fullmatch(stored.input_hash) is None
                or _HASH_PATTERN.fullmatch(stored.fixture_hash) is None
                or (
                    stored.result_hash is not None
                    and _HASH_PATTERN.fullmatch(stored.result_hash) is None
                )
            ):
                raise StateIntegrityError("stored job hash is invalid")
            if stored.status not in {"submitted", "succeeded", "failed"}:
                raise StateIntegrityError("stored job status is invalid")
            if stored.status == "succeeded" and (
                stored.raw_result_artifact_id is None or stored.result_hash is None
            ):
                raise StateIntegrityError("succeeded job is missing its result binding")
            return stored
        except (DomainError, TypeError, ValueError, ArithmeticError) as error:
            raise StateIntegrityError("stored job is invalid") from error


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: ArtifactId
    run_id: RunId
    action_id: ActionId | None
    execution_id: ExecutionId | None
    content_hash: str
    size_bytes: int
    media_type: str
    relative_path: str
    created_at_utc: datetime


class ArtifactRecordRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(self, record: StoredArtifact) -> None:
        if (
            _HASH_PATTERN.fullmatch(record.content_hash) is None
            or record.size_bytes < 0
            or not record.media_type.strip()
            or not record.relative_path.strip()
        ):
            raise StateIntegrityError("artifact metadata is invalid")
        try:
            self.connection.execute(
                "INSERT INTO artifacts(artifact_id, run_id, action_id, execution_id, content_hash, "
                "size_bytes, media_type, relative_path, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.artifact_id),
                    str(record.run_id),
                    None if record.action_id is None else str(record.action_id),
                    None if record.execution_id is None else str(record.execution_id),
                    record.content_hash,
                    record.size_bytes,
                    record.media_type,
                    record.relative_path,
                    format_utc(record.created_at_utc),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("artifact record violates an invariant") from error

    def get(self, artifact_id: ArtifactId) -> StoredArtifact | None:
        row = self.connection.execute(
            "SELECT artifact_id, run_id, action_id, execution_id, content_hash, size_bytes, "
            "media_type, relative_path, created_at_utc FROM artifacts WHERE artifact_id = ?",
            (str(artifact_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            stored = StoredArtifact(
                artifact_id=ArtifactId(str(row[0])),
                run_id=RunId(str(row[1])),
                action_id=None if row[2] is None else ActionId(str(row[2])),
                execution_id=None if row[3] is None else ExecutionId(str(row[3])),
                content_hash=str(row[4]),
                size_bytes=stored_int(row[5], what="artifact size_bytes", minimum=0),
                media_type=str(row[6]),
                relative_path=str(row[7]),
                created_at_utc=parse_utc(str(row[8])),
            )
            if (
                _HASH_PATTERN.fullmatch(stored.content_hash) is None
                or not stored.media_type.strip()
                or not stored.relative_path.strip()
            ):
                raise StateIntegrityError("stored artifact metadata is invalid")
            return stored
        except (DomainError, TypeError, ValueError, ArithmeticError) as error:
            raise StateIntegrityError("stored artifact is invalid") from error


class EvidenceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(
        self,
        *,
        run_id: RunId,
        execution_id: ExecutionId,
        artifact_id: ArtifactId,
        record: object,
        now: datetime,
    ) -> None:
        if not isinstance(record, EvidenceRecord):
            raise ValueError("record must be an EvidenceRecord")
        evidence_id = EvidenceId(str(record.evidence_id))
        if artifact_id not in record.artifact_refs:
            raise StateIntegrityError("evidence artifact is not referenced by the record")
        payload = _model_json(record)
        try:
            self.connection.execute(
                "INSERT INTO evidence(evidence_id, run_id, action_id, execution_id, artifact_id, "
                "evidence_json, evidence_hash, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(evidence_id),
                    str(run_id),
                    str(record.action_id),
                    str(execution_id),
                    str(artifact_id),
                    payload,
                    sha256_hex(record),
                    format_utc(now),
                ),
            )
        except (sqlite3.IntegrityError, DomainError, TypeError, ValueError) as error:
            raise StateIntegrityError("evidence record violates an invariant") from error

    def exists(self, evidence_id: EvidenceId) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM evidence WHERE evidence_id = ?", (str(evidence_id),)
            ).fetchone()
            is not None
        )

    def get(self, evidence_id: EvidenceId):
        stored = self.get_with_binding(evidence_id)
        return None if stored is None else stored.record

    def get_with_binding(self, evidence_id: EvidenceId) -> StoredEvidence | None:
        row = self.connection.execute(
            "SELECT evidence_id, run_id, action_id, execution_id, artifact_id, evidence_json, "
            "evidence_hash FROM evidence WHERE evidence_id = ?",
            (str(evidence_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            record = _parse_model(str(row[5]), EvidenceRecord, what="evidence")
            stored = StoredEvidence(
                evidence_id=EvidenceId(str(row[0])),
                run_id=RunId(str(row[1])),
                action_id=ActionId(str(row[2])),
                execution_id=ExecutionId(str(row[3])),
                artifact_id=ArtifactId(str(row[4])),
                record=record,
            )
            if (
                stored.evidence_id != evidence_id
                or record.evidence_id != evidence_id
                or record.action_id != stored.action_id
                or stored.artifact_id not in record.artifact_refs
                or _HASH_PATTERN.fullmatch(str(row[6])) is None
                or sha256_hex(record) != str(row[6])
            ):
                raise StateIntegrityError("stored evidence hash or ID does not match")
            return stored
        except StateIntegrityError:
            raise
        except (DomainError, TypeError, ValueError, ValidationError, ArithmeticError) as error:
            raise StateIntegrityError("stored evidence is invalid") from error


@dataclass(frozen=True)
class StoredEvidence:
    evidence_id: EvidenceId
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    artifact_id: ArtifactId
    record: EvidenceRecord


__all__ = [
    "ActionRepository",
    "ArtifactRecordRepository",
    "EvidenceRepository",
    "JobRepository",
    "P3RecordRepository",
    "StoredAction",
    "StoredArtifact",
    "StoredEvidence",
    "StoredJob",
]
