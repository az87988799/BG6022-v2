"""Schema-2 event envelope and pure reducer for the P3 Water workflow.

P3 deliberately has a separate event/state contract.  The P2 reducer remains
unchanged for schema-1 historical runs; this module is the only owner of the
schema-2 workflow state transition rules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import Field, ValidationError, field_validator, model_validator

from orca_agent.application.errors import InvalidTransitionError, StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.errors import HashMismatchError, InvalidIdentifierError
from orca_agent.domain.hashing import (
    GENESIS_EVENT_HASH,
    event_envelope_hash,
    sha256_hex,
    verify_sha256,
)
from orca_agent.domain.ids import (
    ApprovalGrantId,
    ArtifactId,
    AssessmentId,
    ClaimId,
    CommandId,
    EffectId,
    EventId,
    EvidenceId,
    ExecutionId,
    InterruptId,
    JobId,
    ReportManifestId,
    RunId,
    effect_id_for,
    new_id,
)
from orca_agent.domain.json_types import FrozenJsonObject, JsonObject, freeze_json_object
from orca_agent.domain.p3 import P3RunStatus, P3WorkflowState, WorkflowPhase
from orca_agent.orchestration.codes import HandlerErrorCode, handler_error_message
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p3_versions import P3_ENGINE_VERSION, P3_SCHEMA_VERSION
from orca_agent.orchestration.state import KernelModel, RunStatus
from orca_agent.orchestration.temporal import ensure_utc
from orca_agent.orchestration.transitions import (
    ApplicationOutcome,
    InterruptProjectionOp,
    InterruptProjectionOperation,
    InterruptStatus,
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class P3KernelEvent(KernelModel):
    """Immutable schema-2 event envelope persisted in the shared events table."""

    event_id: EventId
    command_id: CommandId
    command_type: CommandType
    command_hash: str
    run_id: RunId
    sequence_no: int = Field(ge=1)
    expected_revision: int = Field(ge=0)
    new_revision: int = Field(ge=1)
    event_type: EventType
    schema_version: int
    engine_version: str
    payload: FrozenJsonObject
    payload_hash: str
    result: FrozenJsonObject
    result_hash: str
    occurred_at_utc: datetime
    recorded_at_utc: datetime
    previous_event_hash: str
    event_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("P3 event schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("P3 event engine_version is unsupported")
        return value

    @field_validator("payload", "result", mode="before")
    @classmethod
    def _objects(cls, value: object) -> FrozenJsonObject:
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("P3 event payload and result must be JSON objects") from error

    @field_validator(
        "command_hash", "payload_hash", "result_hash", "previous_event_hash", "event_hash"
    )
    @classmethod
    def _hashes(cls, value: str) -> str:
        if _HASH_PATTERN.fullmatch(value) is None:
            raise ValueError("hash must be lowercase SHA-256 hex")
        return value

    @field_validator("occurred_at_utc", "recorded_at_utc")
    @classmethod
    def _timestamps(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _invariants(self) -> P3KernelEvent:
        if self.new_revision != self.expected_revision + 1:
            raise ValueError("new_revision must equal expected_revision + 1")
        if self.sequence_no != self.new_revision:
            raise ValueError("sequence_no must equal new_revision")
        allowed = {
            EventType.RUN_CREATED: {CommandType.CREATE_RUN},
            EventType.INTERRUPT_REQUESTED: {CommandType.REQUEST_INTERRUPT},
            EventType.INTERRUPT_REPLACED: {CommandType.REPLACE_INTERRUPT},
            EventType.INTERRUPT_RESOLVED: {CommandType.RESOLVE_INTERRUPT},
            EventType.INTERRUPT_EXPIRED: {
                CommandType.EXPIRE_INTERRUPT,
                CommandType.RESOLVE_INTERRUPT,
            },
            EventType.RUN_CANCELLED: {CommandType.CANCEL_RUN},
            EventType.EFFECT_SUCCEEDED: {CommandType.RECORD_EFFECT_SUCCEEDED},
            EventType.EFFECT_DEAD_LETTERED: {CommandType.RECORD_EFFECT_FAILED},
        }
        if self.command_type not in allowed[self.event_type]:
            raise ValueError("P3 event type is not valid for its command type")
        try:
            verify_sha256(self.payload, self.payload_hash)
            verify_sha256(self.result, self.result_hash)
        except HashMismatchError as error:
            raise ValueError("P3 event content hash does not match") from error
        expected = event_envelope_hash(
            event_id=str(self.event_id),
            previous_event_hash=self.previous_event_hash,
            command_id=str(self.command_id),
            command_type=self.command_type.value,
            command_hash=self.command_hash,
            run_id=str(self.run_id),
            sequence_no=self.sequence_no,
            expected_revision=self.expected_revision,
            new_revision=self.new_revision,
            event_type=self.event_type.value,
            schema_version=self.schema_version,
            engine_version=self.engine_version,
            payload=self.payload,
            payload_hash=self.payload_hash,
            result=self.result,
            result_hash=self.result_hash,
            occurred_at_utc=_utc_text(self.occurred_at_utc),
            recorded_at_utc=_utc_text(self.recorded_at_utc),
        )
        if self.event_hash != expected:
            raise ValueError("P3 event envelope hash does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        command_id: CommandId,
        command_type: CommandType,
        run_id: RunId,
        sequence_no: int,
        expected_revision: int,
        event_type: EventType,
        payload: JsonObject,
        result: ApplicationResult | JsonObject,
        occurred_at_utc: datetime,
        recorded_at_utc: datetime | None = None,
        event_id: EventId | None = None,
        command_hash: str,
        previous_event_hash: str = GENESIS_EVENT_HASH,
    ) -> P3KernelEvent:
        actual_event_id = event_id or new_id(EventId)
        actual_recorded = recorded_at_utc or occurred_at_utc
        result_value = (
            result.model_dump(mode="json") if isinstance(result, ApplicationResult) else result
        )
        payload_hash = sha256_hex(payload)
        result_hash = sha256_hex(result_value)
        event_hash = event_envelope_hash(
            event_id=str(actual_event_id),
            previous_event_hash=previous_event_hash,
            command_id=str(command_id),
            command_type=command_type.value,
            command_hash=command_hash,
            run_id=str(run_id),
            sequence_no=sequence_no,
            expected_revision=expected_revision,
            new_revision=sequence_no,
            event_type=event_type.value,
            schema_version=P3_SCHEMA_VERSION,
            engine_version=P3_ENGINE_VERSION,
            payload=payload,
            payload_hash=payload_hash,
            result=result_value,
            result_hash=result_hash,
            occurred_at_utc=_utc_text(occurred_at_utc),
            recorded_at_utc=_utc_text(actual_recorded),
        )
        return cls(
            event_id=actual_event_id,
            command_id=command_id,
            command_type=command_type,
            command_hash=command_hash,
            run_id=run_id,
            sequence_no=sequence_no,
            expected_revision=expected_revision,
            new_revision=sequence_no,
            event_type=event_type,
            schema_version=P3_SCHEMA_VERSION,
            engine_version=P3_ENGINE_VERSION,
            payload=payload,
            payload_hash=payload_hash,
            result=result_value,
            result_hash=result_hash,
            occurred_at_utc=occurred_at_utc,
            recorded_at_utc=actual_recorded,
            previous_event_hash=previous_event_hash,
            event_hash=event_hash,
        )


@dataclass(frozen=True)
class P3Transition:
    """Database-neutral result of applying one schema-2 event."""

    next_status: P3RunStatus
    next_state: P3WorkflowState
    effects: tuple[EffectSpec, ...]
    interrupt_operations: tuple[InterruptProjectionOp, ...]
    outcome: ApplicationOutcome

    def __post_init__(self) -> None:
        if self.next_state.status is not self.next_status:
            raise ValueError("P3 transition status does not match next state")
        indexes = tuple(effect.effect_index for effect in self.effects)
        if indexes != tuple(range(len(indexes))):
            raise ValueError("P3 effect indexes must be contiguous and ordered")
        if sum(effect.effect_class is EffectClass.EXTERNAL for effect in self.effects) > 1:
            raise ValueError("P3 transition contains too many external effects")
        pending = tuple(
            operation
            for operation in self.interrupt_operations
            if operation.status is InterruptStatus.PENDING
        )
        if len(pending) > 1:
            raise ValueError("P3 transition creates too many pending interrupts")
        if pending and self.next_state.pending_interrupt_id != pending[0].interrupt_id:
            raise ValueError("P3 pending interrupt does not match state")
        if self.next_state.pending_interrupt_id is None and pending:
            raise ValueError("P3 transition lost its pending interrupt")
        if self.next_status.is_terminal and self.next_state.pending_interrupt_id is not None:
            raise ValueError("P3 terminal state cannot retain a pending interrupt")


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def expected_p3_application_result(
    *,
    prior_state: P3WorkflowState | None,
    event: P3KernelEvent,
    transition: P3Transition,
) -> ApplicationResult:
    if event.event_type is EventType.RUN_CREATED:
        interrupt_id = None
    elif event.event_type in (
        EventType.INTERRUPT_REQUESTED,
        EventType.INTERRUPT_RESOLVED,
        EventType.INTERRUPT_EXPIRED,
    ):
        key = "interrupt_id"
        raw = event.payload.get(key)
        if not isinstance(raw, str):
            raise StateIntegrityError("P3 event result interrupt ID is missing")
        try:
            interrupt_id = InterruptId(raw)
        except (InvalidIdentifierError, TypeError, ValueError) as error:
            raise StateIntegrityError("P3 event result interrupt ID is invalid") from error
    elif event.event_type is EventType.RUN_CANCELLED:
        interrupt_id = None if prior_state is None else prior_state.pending_interrupt_id
    else:
        interrupt_id = None
    return ApplicationResult(
        accepted=transition.outcome.accepted,
        code=transition.outcome.code,
        run_id=event.run_id,
        revision=event.new_revision,
        status=RunStatus(transition.next_state.status.value),
        event_id=event.event_id,
        interrupt_id=interrupt_id,
        details=transition.outcome.details,
    )


def _invalid(reason: str, *, event: P3KernelEvent) -> InvalidTransitionError:
    return InvalidTransitionError(
        reason,
        details={
            "reason": reason,
            "event_type": event.event_type.value,
            "run_id": str(event.run_id),
            "sequence_no": event.sequence_no,
        },
    )


def _value(payload: Mapping[str, object], key: str, *, event: P3KernelEvent) -> object:
    if key not in payload:
        raise _invalid(f"P3 event payload is missing {key}", event=event)
    return payload[key]


def _text(payload: Mapping[str, object], key: str, *, event: P3KernelEvent) -> str:
    value = _value(payload, key, event=event)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"P3 event payload field {key} is invalid", event=event)
    return value.strip()


def _hash_value(payload: Mapping[str, object], key: str, *, event: P3KernelEvent) -> str:
    value = _text(payload, key, event=event)
    if _HASH_PATTERN.fullmatch(value) is None:
        raise _invalid(f"P3 event payload field {key} is not a SHA-256 hash", event=event)
    return value


def _id(payload: Mapping[str, object], key: str, identifier_type, *, event: P3KernelEvent):
    try:
        return identifier_type(str(_value(payload, key, event=event)))
    except (InvalidIdentifierError, TypeError, ValueError) as error:
        raise _invalid(f"P3 event payload field {key} is invalid", event=event) from error


def _timestamp(payload: Mapping[str, object], key: str, *, event: P3KernelEvent) -> datetime:
    value = _value(payload, key, event=event)
    if not isinstance(value, str):
        raise _invalid(f"P3 event payload field {key} is invalid", event=event)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ensure_utc(result)
    except (TypeError, ValueError) as error:
        raise _invalid(f"P3 event payload field {key} is invalid", event=event) from error


def _effects(payload: Mapping[str, object], *, event: P3KernelEvent) -> tuple[EffectSpec, ...]:
    raw = payload.get("effects", [])
    if not isinstance(raw, (list, tuple)):
        raise _invalid("P3 event effects must be a JSON array", event=event)
    try:
        values = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("effect must be an object")
            effect_values = dict(item)
            if isinstance(effect_values.get("effect_class"), str):
                effect_values["effect_class"] = EffectClass(effect_values["effect_class"])
            values.append(EffectSpec.model_validate(effect_values, strict=True))
        effects = tuple(values)
    except (TypeError, ValueError, ValidationError) as error:
        raise _invalid("P3 event contains an invalid effect", event=event) from error
    if tuple(item.effect_index for item in effects) != tuple(range(len(effects))):
        raise _invalid("P3 event effect indexes are not contiguous", event=event)
    if sum(item.effect_class is EffectClass.EXTERNAL for item in effects) > 1:
        raise _invalid("P3 event contains more than one external effect", event=event)
    return effects


def _mapping(
    payload: Mapping[str, object], key: str, *, event: P3KernelEvent
) -> Mapping[str, object]:
    value = _value(payload, key, event=event)
    if not isinstance(value, Mapping):
        raise _invalid(f"P3 event payload field {key} must be an object", event=event)
    return value


def _state_copy(state: P3WorkflowState, **updates: object) -> P3WorkflowState:
    candidate = state.model_copy(update=updates)
    return P3WorkflowState.model_validate_json(
        json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False), strict=True
    )


def _outcome(code: str, *, accepted: bool = True) -> ApplicationOutcome:
    return ApplicationOutcome(accepted=accepted, code=code, details={})


def _pending_insert(
    *,
    event: P3KernelEvent,
    interrupt_id: InterruptId,
    kind: str,
    payload: Mapping[str, object],
    expiry: datetime,
) -> InterruptProjectionOp:
    return InterruptProjectionOp(
        operation=InterruptProjectionOperation.INSERT_PENDING,
        run_id=event.run_id,
        interrupt_id=interrupt_id,
        status=InterruptStatus.PENDING,
        kind=kind,
        payload=dict(payload),
        expires_at_utc=expiry,
        response=None,
        superseded_by=None,
    )


def _finalize(
    *,
    event: P3KernelEvent,
    interrupt_id: InterruptId,
    status: InterruptStatus,
    response: object | None = None,
) -> InterruptProjectionOp:
    return InterruptProjectionOp(
        operation=InterruptProjectionOperation.FINALIZE,
        run_id=event.run_id,
        interrupt_id=interrupt_id,
        status=status,
        kind=None,
        payload=None,
        expires_at_utc=None,
        response=response,
        superseded_by=None,
    )


def _assert_workflow_effect(
    *, effect: EffectSpec, state: P3WorkflowState, expected_type: str, event: P3KernelEvent
) -> None:
    if effect.effect_type != expected_type:
        raise _invalid("P3 effect type does not match workflow stage", event=event)
    expected_class = (
        EffectClass.EXTERNAL
        if expected_type == "external.p3.dispatch_fake"
        else EffectClass.INTERNAL
    )
    if effect.effect_class is not expected_class:
        raise _invalid("P3 effect class does not match workflow stage", event=event)
    payload = effect.payload
    for key, expected in (
        ("run_id", str(state.run_id)),
        ("conversation_id", str(state.conversation_id)),
        ("action_id", str(state.action_id)),
        ("action_hash", state.action_hash),
        ("envelope_hash", state.envelope_hash),
        ("budget_hash", state.budget_hash),
        ("workflow_schema_version", P3_SCHEMA_VERSION),
        ("workflow_engine_version", P3_ENGINE_VERSION),
    ):
        if payload.get(key) != expected:
            raise _invalid(f"P3 effect binding field {key} is invalid", event=event)
    if not isinstance(payload.get("execution_id"), str):
        raise _invalid("P3 effect execution binding is missing", event=event)
    if state.execution_id is not None and payload.get("execution_id") != str(state.execution_id):
        raise _invalid("P3 effect execution binding does not match state", event=event)
    if state.approval_grant_id is not None and payload.get("approval_grant_id") != str(
        state.approval_grant_id
    ):
        raise _invalid("P3 effect approval binding does not match state", event=event)
    try:
        ApprovalGrantId(str(payload.get("approval_grant_id")))
        execution_id = ExecutionId(str(payload.get("execution_id")))
    except (InvalidIdentifierError, TypeError, ValueError) as error:
        raise _invalid("P3 effect identity binding is invalid", event=event) from error
    if expected_type == "external.p3.dispatch_fake":
        idempotency_key = payload.get("idempotency_key")
        expected_key = "p3.fake." + sha256_hex(
            {
                "namespace": "p3.fake.water.v1",
                "run_id": str(state.run_id),
                "action_id": str(state.action_id),
                "action_hash": state.action_hash,
                "envelope_hash": state.envelope_hash,
                "budget_hash": state.budget_hash,
            }
        )
        if idempotency_key != expected_key:
            raise _invalid("P3 dispatch idempotency binding is invalid", event=event)
        if execution_id is None:
            raise _invalid("P3 dispatch execution binding is invalid", event=event)
    elif expected_type == "internal.p3.assess":
        try:
            ArtifactId(str(payload.get("raw_result_artifact_id")))
        except (InvalidIdentifierError, TypeError, ValueError) as error:
            raise _invalid("P3 assessment artifact binding is invalid", event=event) from error
    elif expected_type == "internal.p3.render_report":
        try:
            ArtifactId(str(payload.get("assessment_artifact_id")))
        except (InvalidIdentifierError, TypeError, ValueError) as error:
            raise _invalid("P3 report artifact binding is invalid", event=event) from error
    if expected_type == "external.p3.dispatch_fake" and (
        payload.get("fixture_id") != "water_sp_v1" or payload.get("fixture_version") != "1"
    ):
        raise _invalid("P3 dispatch fixture binding is invalid", event=event)


def _completion_stage(*, state: P3WorkflowState, effect_id: EffectId, event: P3KernelEvent) -> str:
    if state.phase is WorkflowPhase.DISPATCH_PENDING and state.dispatch_effect_id == effect_id:
        return "dispatch"
    if state.phase is WorkflowPhase.ASSESSMENT_PENDING and state.assessment_effect_id == effect_id:
        return "assess"
    if state.phase is WorkflowPhase.REPORT_PENDING and state.report_effect_id == effect_id:
        return "render"
    raise _invalid("P3 completion effect is not the current stage effect", event=event)


def _artifact_ids(raw: object, *, event: P3KernelEvent) -> tuple[ArtifactId, ...]:
    try:
        if not isinstance(raw, (list, tuple)):
            raise ValueError
        values = tuple(ArtifactId(str(item)) for item in raw)
    except (InvalidIdentifierError, TypeError, ValueError) as error:
        raise _invalid("P3 completion artifacts are invalid", event=event) from error
    if len(set(values)) != len(values):
        raise _invalid("P3 completion artifacts are duplicated", event=event)
    return values


def _append_artifacts(
    existing: tuple[ArtifactId, ...], additions: tuple[ArtifactId, ...], *, event: P3KernelEvent
) -> tuple[ArtifactId, ...]:
    if set(existing).intersection(additions):
        raise _invalid("P3 completion repeats an accepted artifact", event=event)
    return existing + additions


def reduce_p3_event(current: P3WorkflowState | None, event: P3KernelEvent) -> P3Transition:
    """Apply one schema-2 event without reading time or touching I/O."""

    if event.schema_version != P3_SCHEMA_VERSION or event.engine_version != P3_ENGINE_VERSION:
        raise _invalid("P3 event version is unsupported", event=event)
    if current is not None and current.run_id != event.run_id:
        raise _invalid("P3 event run_id does not match current state", event=event)
    effects = _effects(event.payload, event=event)

    if event.event_type is EventType.RUN_CREATED:
        if current is not None:
            raise _invalid("P3 RunCreated cannot be applied twice", event=event)
        raw_run_id = _id(event.payload, "run_id", RunId, event=event)
        if raw_run_id != event.run_id:
            raise _invalid("P3 RunCreated payload run_id does not match event", event=event)
        try:
            state = P3WorkflowState.model_validate_json(
                json.dumps(
                    dict(_mapping(event.payload, "workflow_state", event=event)),
                    ensure_ascii=False,
                ),
                strict=True,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise _invalid("P3 RunCreated workflow_state is invalid", event=event) from error
        if (
            state.run_id != event.run_id
            or state.status is not P3RunStatus.CREATED
            or state.phase is not WorkflowPhase.AWAITING_APPROVAL
            or state.pending_interrupt_id is not None
        ):
            raise _invalid("P3 RunCreated workflow_state is not an initial state", event=event)
        if effects:
            raise _invalid("P3 RunCreated cannot dispatch an effect", event=event)
        return P3Transition(P3RunStatus.CREATED, state, effects, (), _outcome("run_created"))

    if current is None:
        raise _invalid("only P3 RunCreated can initialize a run", event=event)
    if current.status.is_terminal or current.phase is WorkflowPhase.COMPLETED:
        raise _invalid("terminal P3 workflows cannot accept further events", event=event)

    if event.event_type is EventType.INTERRUPT_REQUESTED:
        if (
            current.phase is not WorkflowPhase.AWAITING_APPROVAL
            or current.status is not P3RunStatus.CREATED
        ):
            raise _invalid("P3 approval request requires a newly created workflow", event=event)
        interrupt_id = _id(event.payload, "interrupt_id", InterruptId, event=event)
        kind = _text(event.payload, "kind", event=event)
        if kind != "p3.action_approval":
            raise _invalid("P3 interrupt kind is not the approval kind", event=event)
        interrupt_payload = _mapping(event.payload, "payload", event=event)
        if event.payload.get("effects", []) not in ([], ()):
            raise _invalid("P3 approval request cannot dispatch an effect", event=event)
        if interrupt_payload.get("conversation_id") != str(current.conversation_id):
            raise _invalid("P3 approval conversation binding is invalid", event=event)
        if interrupt_payload.get("action_id") != str(current.action_id):
            raise _invalid("P3 approval action binding is invalid", event=event)
        for key, expected in (
            ("action_hash", current.action_hash),
            ("envelope_hash", current.envelope_hash),
            ("budget_hash", current.budget_hash),
            ("fixture_id", "water_sp_v1"),
            ("fixture_version", "1"),
        ):
            if interrupt_payload.get(key) != expected:
                raise _invalid(f"P3 approval {key} binding is invalid", event=event)
        expiry = _timestamp(event.payload, "expires_at_utc", event=event)
        if expiry <= event.occurred_at_utc:
            raise _invalid("P3 approval expiry must be after event time", event=event)
        state = _state_copy(
            current,
            status=P3RunStatus.WAITING_FOR_INPUT,
            pending_interrupt_id=interrupt_id,
            last_outcome_code="interrupt_requested",
        )
        return P3Transition(
            P3RunStatus.WAITING_FOR_INPUT,
            state,
            effects,
            (
                _pending_insert(
                    event=event,
                    interrupt_id=interrupt_id,
                    kind=kind,
                    payload=interrupt_payload,
                    expiry=expiry,
                ),
            ),
            _outcome("interrupt_requested"),
        )

    if event.event_type is EventType.INTERRUPT_RESOLVED:
        if (
            current.phase is not WorkflowPhase.AWAITING_APPROVAL
            or current.status is not P3RunStatus.WAITING_FOR_INPUT
        ):
            raise _invalid("P3 approval resolution requires a pending approval", event=event)
        if current.pending_interrupt_id is None:
            raise _invalid("P3 approval resolution has no pending interrupt", event=event)
        interrupt_id = _id(event.payload, "interrupt_id", InterruptId, event=event)
        if interrupt_id != current.pending_interrupt_id:
            raise _invalid("P3 resolved interrupt does not match state", event=event)
        response = _mapping(event.payload, "response", event=event)
        if response.get("approved") is not True:
            raise _invalid("P3 approval response is not approved", event=event)
        if response.get("action_id") != str(current.action_id):
            raise _invalid("P3 approval response action does not match state", event=event)
        for key, expected in (
            ("action_hash", current.action_hash),
            ("envelope_hash", current.envelope_hash),
            ("budget_hash", current.budget_hash),
        ):
            if response.get(key) != expected:
                raise _invalid(f"P3 approval response {key} is invalid", event=event)
        effects = _effects(event.payload, event=event)
        if len(effects) != 1:
            raise _invalid("P3 approval must emit exactly one dispatch effect", event=event)
        effect = effects[0]
        _assert_workflow_effect(
            effect=effect, state=current, expected_type="external.p3.dispatch_fake", event=event
        )
        approval_id = _id(response, "approval_grant_id", ApprovalGrantId, event=event)
        if effect.payload.get("approval_grant_id") != str(approval_id):
            raise _invalid("P3 approval grant binding is invalid", event=event)
        execution_id = _id(effect.payload, "execution_id", ExecutionId, event=event)
        if effect.payload.get("action_hash") is not None and not isinstance(
            effect.payload.get("action_hash"), str
        ):
            raise _invalid("P3 dispatch action hash is invalid", event=event)
        state = _state_copy(
            current,
            status=P3RunStatus.READY,
            pending_interrupt_id=None,
            last_outcome_code="interrupt_resolved",
            phase=WorkflowPhase.DISPATCH_PENDING,
            approval_grant_id=approval_id,
            dispatch_effect_id=effect_id_for(event.event_id, effect.effect_index),
            assessment_effect_id=None,
            report_effect_id=None,
            execution_id=execution_id,
        )
        return P3Transition(
            P3RunStatus.READY,
            state,
            effects,
            (
                _finalize(
                    event=event,
                    interrupt_id=interrupt_id,
                    status=InterruptStatus.RESOLVED,
                    response=response,
                ),
            ),
            _outcome("interrupt_resolved"),
        )

    if event.event_type is EventType.INTERRUPT_EXPIRED:
        if (
            current.phase is not WorkflowPhase.AWAITING_APPROVAL
            or current.status is not P3RunStatus.WAITING_FOR_INPUT
        ):
            raise _invalid("P3 approval expiry requires a pending approval", event=event)
        if current.pending_interrupt_id is None:
            raise _invalid("P3 approval expiry has no pending interrupt", event=event)
        if effects:
            raise _invalid("P3 approval expiry cannot dispatch an effect", event=event)
        interrupt_id = _id(event.payload, "interrupt_id", InterruptId, event=event)
        if interrupt_id != current.pending_interrupt_id:
            raise _invalid("P3 expired interrupt does not match state", event=event)
        expiry = _timestamp(event.payload, "expires_at_utc", event=event)
        if event.occurred_at_utc < expiry:
            raise _invalid("P3 approval cannot expire before its deadline", event=event)
        state = _state_copy(
            current,
            status=P3RunStatus.FAILED,
            pending_interrupt_id=None,
            phase=WorkflowPhase.FAILED,
            last_outcome_code="approval_expired",
            last_error_code="approval_expired",
        )
        return P3Transition(
            P3RunStatus.FAILED,
            state,
            effects,
            (_finalize(event=event, interrupt_id=interrupt_id, status=InterruptStatus.EXPIRED),),
            _outcome("approval_expired", accepted=False),
        )

    if event.event_type is EventType.RUN_CANCELLED:
        if effects:
            raise _invalid("P3 cancellation cannot dispatch an effect", event=event)
        reason = _text(event.payload, "reason_code", event=event)
        if reason not in {"user_cancelled", "approval_expired", "workflow_failed"}:
            raise _invalid("P3 cancellation reason is not allowed", event=event)
        operations: tuple[InterruptProjectionOp, ...] = ()
        if current.pending_interrupt_id is not None:
            operations = (
                _finalize(
                    event=event,
                    interrupt_id=current.pending_interrupt_id,
                    status=InterruptStatus.CANCELLED,
                ),
            )
        state = _state_copy(
            current,
            status=P3RunStatus.CANCELLED,
            pending_interrupt_id=None,
            phase=WorkflowPhase.CANCELLED,
            last_outcome_code="run_cancelled",
            cancel_reason_code=reason,
        )
        return P3Transition(
            P3RunStatus.CANCELLED, state, effects, operations, _outcome("run_cancelled")
        )

    if event.event_type in (EventType.EFFECT_SUCCEEDED, EventType.EFFECT_DEAD_LETTERED):
        effect_id = _id(event.payload, "effect_id", EffectId, event=event)
        stage = _completion_stage(state=current, effect_id=effect_id, event=event)
        raw_stage = _text(event.payload, "p3_stage", event=event)
        expected_stage = {"dispatch": "dispatch", "assess": "assess", "render": "render"}[stage]
        if raw_stage != expected_stage:
            raise _invalid("P3 completion stage is invalid", event=event)
        if event.event_type is EventType.EFFECT_SUCCEEDED:
            try:
                from orca_agent.orchestration.effect_receipts import parse_effect_success_receipt

                receipt = parse_effect_success_receipt(
                    _value(event.payload, "result_summary", event=event)
                )
            except ValueError as error:
                raise _invalid("P3 completion receipt is invalid", event=event) from error
            additions = tuple(receipt.artifact_ids)
            expected_count = {"dispatch": 1, "assess": 1, "render": 2}[stage]
            if len(additions) != expected_count:
                raise _invalid("P3 completion artifact count is invalid", event=event)
            updates = event.payload.get("p3_updates", {})
            if not isinstance(updates, Mapping):
                raise _invalid("P3 completion updates are invalid", event=event)
            values: dict[str, object] = {
                "status": P3RunStatus.READY,
                "last_outcome_code": "effect_succeeded",
                "accepted_artifact_ids": _append_artifacts(
                    current.accepted_artifact_ids, additions, event=event
                ),
            }
            successor_expected: str | None
            successor_key: str | None
            if stage == "dispatch":
                successor_expected = "internal.p3.assess"
                successor_key = "assessment_effect_id"
                values["phase"] = WorkflowPhase.ASSESSMENT_PENDING
                values["job_id"] = _id(updates, "job_id", JobId, event=event)
                raw_result_artifact_id = _id(
                    updates, "raw_result_artifact_id", ArtifactId, event=event
                )
                for key in ("job_result_hash", "raw_result_hash", "fixture_hash"):
                    _hash_value(updates, key, event=event)
                if len(effects) != 1 or effects[0].effect_type != successor_expected:
                    raise _invalid("P3 dispatch completion must register assessment", event=event)
                if str(raw_result_artifact_id) != str(additions[0]) or effects[0].payload.get(
                    "raw_result_artifact_id"
                ) != str(additions[0]):
                    raise _invalid("P3 assessment raw artifact binding is invalid", event=event)
            elif stage == "assess":
                successor_expected = "internal.p3.render_report"
                successor_key = "report_effect_id"
                values["phase"] = WorkflowPhase.REPORT_PENDING
                values["assessment_id"] = _id(updates, "assessment_id", AssessmentId, event=event)
                values["claim_id"] = _id(updates, "claim_id", ClaimId, event=event)
                _id(updates, "evidence_id", EvidenceId, event=event)
                assessment_artifact_id = _id(
                    updates, "assessment_artifact_id", ArtifactId, event=event
                )
                for key in (
                    "assessment_hash",
                    "claim_hash",
                    "evidence_hash",
                    "assessment_artifact_hash",
                ):
                    _hash_value(updates, key, event=event)
                if len(effects) != 1 or effects[0].effect_type != successor_expected:
                    raise _invalid("P3 assessment completion must register report", event=event)
                if str(assessment_artifact_id) != str(additions[0]) or effects[0].payload.get(
                    "assessment_artifact_id"
                ) != str(additions[0]):
                    raise _invalid("P3 report assessment binding is invalid", event=event)
            else:
                successor_expected = None
                successor_key = None
                values["phase"] = WorkflowPhase.COMPLETED
                values["report_manifest_id"] = _id(
                    updates, "report_manifest_id", ReportManifestId, event=event
                )
                for key in ("manifest_hash", "markdown_hash", "json_hash"):
                    _hash_value(updates, key, event=event)
                if (
                    updates.get("markdown_artifact_id") != str(additions[0])
                    or updates.get("json_artifact_id") != str(additions[1])
                    or updates.get("markdown_hash") is None
                    or updates.get("json_hash") is None
                ):
                    raise _invalid("P3 report artifact binding is invalid", event=event)
                if effects:
                    raise _invalid(
                        "P3 report completion cannot register another effect", event=event
                    )
            if successor_expected is not None:
                _assert_workflow_effect(
                    effect=effects[0], state=current, expected_type=successor_expected, event=event
                )
                if successor_key is not None:
                    values[successor_key] = effect_id_for(event.event_id, 0)
            else:
                values["phase"] = WorkflowPhase.COMPLETED
            return P3Transition(
                P3RunStatus.READY,
                _state_copy(current, **values),
                effects,
                (),
                _outcome("effect_succeeded"),
            )

        error_code = _text(event.payload, "error_code", event=event)
        try:
            allowed = HandlerErrorCode(error_code)
        except ValueError as error:
            raise _invalid("P3 completion error code is not allowed", event=event) from error
        if event.payload.get("error_message") != handler_error_message(allowed):
            raise _invalid("P3 completion error message is not allowed", event=event)
        if effects:
            raise _invalid("P3 failure cannot register successor effects", event=event)
        state = _state_copy(
            current,
            status=P3RunStatus.FAILED,
            phase=WorkflowPhase.FAILED,
            last_outcome_code="effect_dead_lettered",
            last_error_code=allowed.value,
        )
        return P3Transition(P3RunStatus.FAILED, state, (), (), _outcome("effect_dead_lettered"))

    raise _invalid("unsupported P3 event type", event=event)


__all__ = [
    "P3KernelEvent",
    "P3Transition",
    "expected_p3_application_result",
    "reduce_p3_event",
]
