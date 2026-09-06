"""Schema-3 event envelope and pure reducer for the P4 workflow."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime

from pydantic import Field, ValidationError, field_validator, model_validator

from orca_agent.application.errors import InvalidTransitionError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.errors import HashMismatchError, InvalidIdentifierError
from orca_agent.domain.hashing import (
    GENESIS_EVENT_HASH,
    event_envelope_hash,
    sha256_hex,
    verify_sha256,
)
from orca_agent.domain.ids import (
    CommandId,
    ConversationId,
    EffectId,
    EventId,
    InterruptId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.json_types import FrozenJsonObject, JsonObject, freeze_json_object
from orca_agent.domain.p4 import P4Phase, P4WorkflowState
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from orca_agent.orchestration.state import KernelModel, RunStatus
from orca_agent.orchestration.temporal import ensure_utc
from orca_agent.orchestration.transitions import (
    ApplicationOutcome,
    InterruptProjectionOp,
    InterruptProjectionOperation,
    InterruptStatus,
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class P4KernelEvent(KernelModel):
    """Immutable schema-3 event envelope stored in the shared events table."""

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
        if type(value) is not int or value != P4_SCHEMA_VERSION:
            raise ValueError("P4 event schema_version must be 3")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P4_ENGINE_VERSION:
            raise ValueError("P4 event engine_version is unsupported")
        return value

    @field_validator("payload", "result", mode="before")
    @classmethod
    def _objects(cls, value: object) -> FrozenJsonObject:
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("P4 event payload and result must be JSON objects") from error

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
    def _invariants(self) -> P4KernelEvent:
        if self.new_revision != self.expected_revision + 1 or self.sequence_no != self.new_revision:
            raise ValueError("P4 event revision and sequence are not contiguous")
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
            raise ValueError("P4 event type is not valid for its command type")
        try:
            verify_sha256(self.payload, self.payload_hash)
            verify_sha256(self.result, self.result_hash)
        except HashMismatchError as error:
            raise ValueError("P4 event content hash does not match") from error
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
            raise ValueError("P4 event envelope hash does not match")
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
    ) -> P4KernelEvent:
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
            schema_version=P4_SCHEMA_VERSION,
            engine_version=P4_ENGINE_VERSION,
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
            schema_version=P4_SCHEMA_VERSION,
            engine_version=P4_ENGINE_VERSION,
            payload=payload,
            payload_hash=payload_hash,
            result=result_value,
            result_hash=result_hash,
            occurred_at_utc=occurred_at_utc,
            recorded_at_utc=actual_recorded,
            previous_event_hash=previous_event_hash,
            event_hash=event_hash,
        )


class P4Transition(KernelModel):
    """Database-neutral output of a P4 event reduction."""

    next_status: RunStatus
    next_state: P4WorkflowState
    effects: tuple[EffectSpec, ...]
    interrupt_operations: tuple[InterruptProjectionOp, ...]
    outcome: ApplicationOutcome

    @model_validator(mode="after")
    def _invariants(self) -> P4Transition:
        if self.next_state.status is not self.next_status:
            raise ValueError("P4 transition status does not match state")
        if tuple(item.effect_index for item in self.effects) != tuple(range(len(self.effects))):
            raise ValueError("P4 effect indexes are not contiguous")
        if sum(item.effect_class is EffectClass.EXTERNAL for item in self.effects) > 1:
            raise ValueError("P4 transition contains more than one external effect")
        pending = tuple(
            item for item in self.interrupt_operations if item.status is InterruptStatus.PENDING
        )
        if len(pending) > 1 or (
            pending and pending[0].interrupt_id != self.next_state.pending_interrupt_id
        ):
            raise ValueError("P4 pending interrupt does not match state")
        if self.next_state.pending_interrupt_id is None and pending:
            raise ValueError("P4 pending operation was dropped")
        return self


def expected_p4_application_result(
    *, prior_state: P4WorkflowState | None, event: P4KernelEvent, transition: P4Transition
) -> ApplicationResult:
    interrupt_id: InterruptId | None = None
    if event.event_type is EventType.EFFECT_SUCCEEDED:
        request = event.payload.get("confirmation_request")
        if isinstance(request, Mapping) and isinstance(request.get("interrupt_id"), str):
            interrupt_id = InterruptId(request["interrupt_id"])
    elif event.event_type in (EventType.INTERRUPT_RESOLVED, EventType.INTERRUPT_EXPIRED):
        raw = event.payload.get("interrupt_id")
        if isinstance(raw, str):
            interrupt_id = InterruptId(raw)
    elif event.event_type is EventType.RUN_CANCELLED and prior_state is not None:
        interrupt_id = prior_state.pending_interrupt_id
    return ApplicationResult(
        accepted=transition.outcome.accepted,
        code=transition.outcome.code,
        run_id=event.run_id,
        revision=event.new_revision,
        status=transition.next_state.status,
        event_id=event.event_id,
        interrupt_id=interrupt_id,
        details=transition.outcome.details,
    )


def _invalid(reason: str, *, event: P4KernelEvent) -> InvalidTransitionError:
    return InvalidTransitionError(
        reason,
        details={
            "event_type": event.event_type.value,
            "run_id": str(event.run_id),
            "sequence_no": event.sequence_no,
        },
    )


def _value(payload: Mapping[str, object], key: str, *, event: P4KernelEvent) -> object:
    if key not in payload:
        raise _invalid(f"P4 event payload is missing {key}", event=event)
    return payload[key]


def _text(payload: Mapping[str, object], key: str, *, event: P4KernelEvent) -> str:
    value = _value(payload, key, event=event)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"P4 event payload field {key} is invalid", event=event)
    return value.strip()


def _id(payload: Mapping[str, object], key: str, identifier_type, *, event: P4KernelEvent):
    try:
        return identifier_type(str(_value(payload, key, event=event)))
    except (InvalidIdentifierError, TypeError, ValueError) as error:
        raise _invalid(f"P4 event payload field {key} is invalid", event=event) from error


def _hash_value(payload: Mapping[str, object], key: str, *, event: P4KernelEvent) -> str:
    value = _text(payload, key, event=event)
    if _HASH_PATTERN.fullmatch(value) is None:
        raise _invalid(f"P4 event payload field {key} is not a SHA-256 hash", event=event)
    return value


def _timestamp(payload: Mapping[str, object], key: str, *, event: P4KernelEvent) -> datetime:
    value = _value(payload, key, event=event)
    if not isinstance(value, str):
        raise _invalid(f"P4 event payload field {key} is invalid", event=event)
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError) as error:
        raise _invalid(f"P4 event payload field {key} is invalid", event=event) from error


def _mapping(
    payload: Mapping[str, object], key: str, *, event: P4KernelEvent
) -> Mapping[str, object]:
    value = _value(payload, key, event=event)
    if not isinstance(value, Mapping):
        raise _invalid(f"P4 event payload field {key} must be an object", event=event)
    return value


def _effects(payload: Mapping[str, object], *, event: P4KernelEvent) -> tuple[EffectSpec, ...]:
    raw = payload.get("effects", ())
    if not isinstance(raw, (list, tuple)):
        raise _invalid("P4 event effects must be an array", event=event)
    try:
        values: list[EffectSpec] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("effect must be an object")
            effect_values = dict(item)
            if isinstance(effect_values.get("effect_class"), str):
                effect_values["effect_class"] = EffectClass(effect_values["effect_class"])
            values.append(EffectSpec.model_validate(effect_values, strict=True))
        effects = tuple(values)
    except (TypeError, ValueError, ValidationError) as error:
        raise _invalid("P4 event contains an invalid effect", event=event) from error
    if tuple(item.effect_index for item in effects) != tuple(range(len(effects))):
        raise _invalid("P4 event effect indexes are not contiguous", event=event)
    return effects


def _state(
    *,
    run_id: RunId,
    conversation_id,
    query_id: WorkflowRecordId,
    query_hash: str,
    registry_snapshot_id: WorkflowRecordId,
    registry_snapshot_hash: str,
    status: RunStatus,
    phase: P4Phase,
    identity_effect_id: EffectId | None,
    pending_interrupt_id: InterruptId | None,
    candidate_bundle_id: WorkflowRecordId | None,
    candidate_bundle_hash: str | None,
    candidate_set_hash: str | None,
    confirmed_molecule_id: WorkflowRecordId | None,
    confirmed_molecule_hash: str | None,
    prepared_plan_id: WorkflowRecordId | None,
    prepared_plan_hash: str | None,
    last_outcome_code: str | None,
    last_error_code: str | None,
) -> P4WorkflowState:
    return P4WorkflowState(
        run_id=run_id,
        schema_version=P4_SCHEMA_VERSION,
        engine_version=P4_ENGINE_VERSION,
        status=status,
        phase=phase,
        conversation_id=conversation_id,
        query_id=query_id,
        query_hash=query_hash,
        registry_snapshot_id=registry_snapshot_id,
        registry_snapshot_hash=registry_snapshot_hash,
        identity_effect_id=identity_effect_id,
        pending_interrupt_id=pending_interrupt_id,
        candidate_bundle_id=candidate_bundle_id,
        candidate_bundle_hash=candidate_bundle_hash,
        candidate_set_hash=candidate_set_hash,
        confirmed_molecule_id=confirmed_molecule_id,
        confirmed_molecule_hash=confirmed_molecule_hash,
        prepared_plan_id=prepared_plan_id,
        prepared_plan_hash=prepared_plan_hash,
        last_outcome_code=last_outcome_code,
        last_error_code=last_error_code,
    )


def _outcome(
    code: str, *, accepted: bool = True, details: JsonObject | None = None
) -> ApplicationOutcome:
    values = {} if details is None else details
    values.setdefault("phase", code if code in {item.value for item in P4Phase} else "")
    return ApplicationOutcome(accepted=accepted, code=code, details=values)


def _pending_insert(
    *, event: P4KernelEvent, request: Mapping[str, object]
) -> InterruptProjectionOp:
    return InterruptProjectionOp(
        operation=InterruptProjectionOperation.INSERT_PENDING,
        run_id=event.run_id,
        interrupt_id=InterruptId(str(request["interrupt_id"])),
        status=InterruptStatus.PENDING,
        kind=str(request["kind"]),
        payload=dict(request),
        expires_at_utc=_timestamp(request, "expires_at_utc", event=event),
        response=None,
        superseded_by=None,
    )


def reduce_p4_event(current: P4WorkflowState | None, event: P4KernelEvent) -> P4Transition:
    """Apply one P4 event without network, file, RDKit, or registry I/O."""

    if current is not None and current.run_id != event.run_id:
        raise _invalid("P4 event run_id does not match state", event=event)
    payload = event.payload
    effects = _effects(payload, event=event)

    if event.event_type is EventType.RUN_CREATED:
        if current is not None:
            raise _invalid("P4 RunCreated cannot be applied twice", event=event)
        run_id = _id(payload, "run_id", RunId, event=event)
        conversation_id = _id(payload, "conversation_id", ConversationId, event=event)
        query_id = _id(payload, "query_id", WorkflowRecordId, event=event)
        registry_id = _id(payload, "registry_snapshot_id", WorkflowRecordId, event=event)
        query_hash = _hash_value(payload, "query_hash", event=event)
        registry_hash = _hash_value(payload, "registry_snapshot_hash", event=event)
        if run_id != event.run_id or len(effects) != 1:
            raise _invalid("P4 RunCreated bindings are invalid", event=event)
        effect = effects[0]
        if effect.effect_type not in {"external.p4.resolve_pubchem", "internal.p4.resolve_smiles"}:
            raise _invalid("P4 RunCreated effect is not an identity effect", event=event)
        for key, expected in (
            ("run_id", str(run_id)),
            ("query_id", str(query_id)),
            ("query_hash", query_hash),
            ("registry_snapshot_id", str(registry_id)),
            ("registry_snapshot_hash", registry_hash),
        ):
            if effect.payload.get(key) != expected:
                raise _invalid(f"P4 effect binding field {key} is invalid", event=event)
        state = _state(
            run_id=run_id,
            conversation_id=conversation_id,
            query_id=query_id,
            query_hash=query_hash,
            registry_snapshot_id=registry_id,
            registry_snapshot_hash=registry_hash,
            status=RunStatus.CREATED,
            phase=P4Phase.RESOLVING_IDENTITY,
            identity_effect_id=effect.effect_id(event.event_id),
            pending_interrupt_id=None,
            candidate_bundle_id=None,
            candidate_bundle_hash=None,
            candidate_set_hash=None,
            confirmed_molecule_id=None,
            confirmed_molecule_hash=None,
            prepared_plan_id=None,
            prepared_plan_hash=None,
            last_outcome_code="run_created",
            last_error_code=None,
        )
        return P4Transition(
            next_status=RunStatus.CREATED,
            next_state=state,
            effects=effects,
            interrupt_operations=(),
            outcome=ApplicationOutcome(
                accepted=True,
                code="run_created",
                details={
                    "phase": P4Phase.RESOLVING_IDENTITY.value,
                    "query_id": str(query_id),
                    "registry_snapshot_id": str(registry_id),
                    "identity_effect_id": str(state.identity_effect_id),
                },
            ),
        )

    if current is None:
        raise _invalid("only P4 RunCreated can initialize a run", event=event)
    if current.phase in (P4Phase.PLAN_READY, P4Phase.CANCELLED, P4Phase.FAILED):
        raise _invalid("P4 terminal phase cannot accept further events", event=event)

    if event.event_type is EventType.EFFECT_SUCCEEDED:
        if current.phase is not P4Phase.RESOLVING_IDENTITY or len(effects) != 0:
            raise _invalid("P4 identity completion is out of order", event=event)
        if _id(payload, "effect_id", EffectId, event=event) != current.identity_effect_id:
            raise _invalid("P4 identity completion effect does not match state", event=event)
        try:
            from orca_agent.orchestration.effect_receipts import parse_effect_success_receipt

            parse_effect_success_receipt(_value(payload, "result_summary", event=event))
        except ValueError as error:
            raise _invalid("P4 identity completion receipt is invalid", event=event) from error
        resolution = _text(payload, "resolution", event=event)
        if resolution == "success":
            request = _mapping(payload, "confirmation_request", event=event)
            request_id = _id(request, "interrupt_id", InterruptId, event=event)
            if request.get("kind") != "confirm_molecule_identity":
                raise _invalid("P4 confirmation interrupt kind is invalid", event=event)
            if request.get("run_id") != str(current.run_id):
                raise _invalid("P4 confirmation interrupt run binding is invalid", event=event)
            if (
                request.get("query_id") != str(current.query_id)
                or request.get("query_hash") != current.query_hash
            ):
                raise _invalid("P4 confirmation query binding is invalid", event=event)
            bundle_id = _id(request, "candidate_bundle_id", WorkflowRecordId, event=event)
            bundle_hash = _hash_value(request, "candidate_bundle_hash", event=event)
            candidate_set_hash = _hash_value(request, "candidate_set_hash", event=event)
            if request.get("effect_id") != str(current.identity_effect_id):
                raise _invalid("P4 confirmation effect binding is invalid", event=event)
            expiry = _timestamp(request, "expires_at_utc", event=event)
            if expiry <= event.occurred_at_utc:
                raise _invalid("P4 confirmation expiry is invalid", event=event)
            state = current.model_copy(
                update={
                    "status": RunStatus.WAITING_FOR_INPUT,
                    "phase": P4Phase.AWAITING_IDENTITY,
                    "pending_interrupt_id": request_id,
                    "candidate_bundle_id": bundle_id,
                    "candidate_bundle_hash": bundle_hash,
                    "candidate_set_hash": candidate_set_hash,
                    "last_outcome_code": "identity_candidates_ready",
                    "last_error_code": None,
                }
            )
            state = P4WorkflowState.model_validate_json(
                json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
            )
            return P4Transition(
                next_status=RunStatus.WAITING_FOR_INPUT,
                next_state=state,
                effects=(),
                interrupt_operations=(_pending_insert(event=event, request=request),),
                outcome=ApplicationOutcome(
                    accepted=True,
                    code="identity_candidates_ready",
                    details={
                        "phase": P4Phase.AWAITING_IDENTITY.value,
                        "candidate_bundle_id": str(bundle_id),
                        "candidate_bundle_hash": bundle_hash,
                        "candidate_set_hash": candidate_set_hash,
                        "interrupt_id": str(request_id),
                    },
                ),
            )
        if resolution == "failed":
            error_code = _text(payload, "identity_error_code", event=event)
            state = current.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "phase": P4Phase.FAILED,
                    "last_outcome_code": "identity_resolution_failed",
                    "last_error_code": error_code,
                }
            )
            state = P4WorkflowState.model_validate_json(
                json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
            )
            return P4Transition(
                next_status=RunStatus.FAILED,
                next_state=state,
                effects=(),
                interrupt_operations=(),
                outcome=ApplicationOutcome(
                    accepted=False,
                    code=error_code,
                    details={"phase": P4Phase.FAILED.value, "error_code": error_code},
                ),
            )
        raise _invalid("P4 identity resolution status is invalid", event=event)

    if event.event_type is EventType.EFFECT_DEAD_LETTERED:
        if current.phase is not P4Phase.RESOLVING_IDENTITY or len(effects) != 0:
            raise _invalid("P4 identity failure is out of order", event=event)
        if _id(payload, "effect_id", EffectId, event=event) != current.identity_effect_id:
            raise _invalid("P4 identity failure effect does not match state", event=event)
        raw = _text(payload, "error_code", event=event)
        state = current.model_copy(
            update={
                "status": RunStatus.FAILED,
                "phase": P4Phase.FAILED,
                "last_outcome_code": "identity_resolution_failed",
                "last_error_code": raw,
            }
        )
        state = P4WorkflowState.model_validate_json(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
        )
        return P4Transition(
            next_status=RunStatus.FAILED,
            next_state=state,
            effects=(),
            interrupt_operations=(),
            outcome=ApplicationOutcome(
                accepted=False,
                code=raw,
                details={"phase": P4Phase.FAILED.value, "error_code": raw},
            ),
        )

    if event.event_type is EventType.INTERRUPT_RESOLVED:
        if current.phase is not P4Phase.AWAITING_IDENTITY or current.pending_interrupt_id is None:
            raise _invalid("P4 identity confirmation is out of order", event=event)
        interrupt_id = _id(payload, "interrupt_id", InterruptId, event=event)
        if interrupt_id != current.pending_interrupt_id:
            raise _invalid("P4 confirmation interrupt does not match state", event=event)
        response = _mapping(payload, "response", event=event)
        if (
            response.get("query_hash") != current.query_hash
            or response.get("candidate_bundle_id") != str(current.candidate_bundle_id)
            or response.get("candidate_bundle_hash") != current.candidate_bundle_hash
            or response.get("candidate_set_hash") != current.candidate_set_hash
        ):
            raise _invalid("P4 confirmation set binding is invalid", event=event)
        decision = response.get("decision")
        if decision == "reject":
            state = current.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "phase": P4Phase.FAILED,
                    "pending_interrupt_id": None,
                    "last_outcome_code": "identity_rejected",
                    "last_error_code": "identity_rejected",
                }
            )
            state = P4WorkflowState.model_validate_json(
                json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
            )
            outcome = ApplicationOutcome(
                accepted=False,
                code="identity_rejected",
                details={"phase": P4Phase.FAILED.value},
            )
            next_status = RunStatus.FAILED
        elif decision == "accept":
            confirmed_id = _id(payload, "confirmed_molecule_id", WorkflowRecordId, event=event)
            confirmed_hash = _hash_value(payload, "confirmed_molecule_hash", event=event)
            plan_id = _id(payload, "prepared_plan_id", WorkflowRecordId, event=event)
            plan_hash = _hash_value(payload, "prepared_plan_hash", event=event)
            state = current.model_copy(
                update={
                    "status": RunStatus.READY,
                    "phase": P4Phase.PLAN_READY,
                    "pending_interrupt_id": None,
                    "confirmed_molecule_id": confirmed_id,
                    "confirmed_molecule_hash": confirmed_hash,
                    "prepared_plan_id": plan_id,
                    "prepared_plan_hash": plan_hash,
                    "last_outcome_code": "plan_ready",
                    "last_error_code": None,
                }
            )
            state = P4WorkflowState.model_validate_json(
                json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
            )
            outcome = ApplicationOutcome(
                accepted=True,
                code="plan_ready",
                details={
                    "phase": P4Phase.PLAN_READY.value,
                    "identity_confirmed": True,
                    "planning_valid": True,
                    "execution_ready": False,
                    "execution_approved": False,
                    "real_scientific_result": False,
                    "confirmed_molecule_id": str(confirmed_id),
                    "prepared_plan_id": str(plan_id),
                    "prepared_plan_hash": plan_hash,
                },
            )
            next_status = RunStatus.READY
        else:
            raise _invalid("P4 confirmation decision is invalid", event=event)
        return P4Transition(
            next_status=next_status,
            next_state=state,
            effects=(),
            interrupt_operations=(
                InterruptProjectionOp(
                    operation=InterruptProjectionOperation.FINALIZE,
                    run_id=event.run_id,
                    interrupt_id=interrupt_id,
                    status=InterruptStatus.RESOLVED,
                    kind=None,
                    payload=None,
                    expires_at_utc=None,
                    response=response,
                    superseded_by=None,
                ),
            ),
            outcome=outcome,
        )

    if event.event_type is EventType.INTERRUPT_EXPIRED:
        if current.phase is not P4Phase.AWAITING_IDENTITY or current.pending_interrupt_id is None:
            raise _invalid("P4 interrupt expiry is out of order", event=event)
        interrupt_id = _id(payload, "interrupt_id", InterruptId, event=event)
        if interrupt_id != current.pending_interrupt_id:
            raise _invalid("P4 expired interrupt does not match state", event=event)
        expiry = _timestamp(payload, "expires_at_utc", event=event)
        if event.occurred_at_utc < expiry:
            raise _invalid("P4 interrupt expired before its deadline", event=event)
        state = current.model_copy(
            update={
                "status": RunStatus.FAILED,
                "phase": P4Phase.FAILED,
                "pending_interrupt_id": None,
                "last_outcome_code": "identity_confirmation_expired",
                "last_error_code": "identity_confirmation_expired",
            }
        )
        state = P4WorkflowState.model_validate_json(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
        )
        return P4Transition(
            next_status=RunStatus.FAILED,
            next_state=state,
            effects=(),
            interrupt_operations=(
                InterruptProjectionOp(
                    operation=InterruptProjectionOperation.FINALIZE,
                    run_id=event.run_id,
                    interrupt_id=interrupt_id,
                    status=InterruptStatus.EXPIRED,
                    kind=None,
                    payload=None,
                    expires_at_utc=None,
                    response=None,
                    superseded_by=None,
                ),
            ),
            outcome=ApplicationOutcome(
                accepted=False,
                code="identity_confirmation_expired",
                details={"phase": P4Phase.FAILED.value},
            ),
        )

    if event.event_type is EventType.RUN_CANCELLED:
        if current.phase is P4Phase.PLAN_READY:
            raise _invalid("P4 plan-ready run cannot be cancelled", event=event)
        operations: tuple[InterruptProjectionOp, ...] = ()
        if current.pending_interrupt_id is not None:
            operations = (
                InterruptProjectionOp(
                    operation=InterruptProjectionOperation.FINALIZE,
                    run_id=event.run_id,
                    interrupt_id=current.pending_interrupt_id,
                    status=InterruptStatus.CANCELLED,
                    kind=None,
                    payload=None,
                    expires_at_utc=None,
                    response=None,
                    superseded_by=None,
                ),
            )
        reason = _text(payload, "reason_code", event=event)
        state = current.model_copy(
            update={
                "status": RunStatus.CANCELLED,
                "phase": P4Phase.CANCELLED,
                "pending_interrupt_id": None,
                "last_outcome_code": "run_cancelled",
                "last_error_code": None,
            }
        )
        state = P4WorkflowState.model_validate_json(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False), strict=True
        )
        return P4Transition(
            next_status=RunStatus.CANCELLED,
            next_state=state,
            effects=(),
            interrupt_operations=operations,
            outcome=ApplicationOutcome(
                accepted=True,
                code="run_cancelled",
                details={"phase": P4Phase.CANCELLED.value, "reason_code": reason},
            ),
        )

    raise _invalid("unsupported P4 event type", event=event)


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["P4KernelEvent", "P4Transition", "expected_p4_application_result", "reduce_p4_event"]
