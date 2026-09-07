"""Schema-4 event envelope and pure state transition helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from orca_agent.application.errors import InvalidTransitionError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.errors import HashMismatchError
from orca_agent.domain.hashing import (
    GENESIS_EVENT_HASH,
    event_envelope_hash,
    sha256_hex,
    verify_sha256,
)
from orca_agent.domain.ids import CommandId, EventId, RunId, new_id
from orca_agent.domain.json_types import FrozenJsonObject, JsonObject, freeze_json_object, thaw_json
from orca_agent.domain.p5 import P5WorkflowState
from orca_agent.orchestration.effects import EffectSpec
from orca_agent.orchestration.p5_commands import P5CommandType, P5EventType
from orca_agent.orchestration.p5_versions import P5_ENGINE_VERSION, P5_SCHEMA_VERSION
from orca_agent.orchestration.state import KernelModel
from orca_agent.orchestration.temporal import ensure_utc

_HASH = re.compile(r"^[0-9a-f]{64}$")


class P5KernelEvent(KernelModel):
    event_id: EventId
    command_id: CommandId
    command_type: P5CommandType
    command_hash: str
    run_id: RunId
    sequence_no: int = Field(ge=1)
    expected_revision: int = Field(ge=0)
    new_revision: int = Field(ge=1)
    event_type: P5EventType
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

    _schema = field_validator("schema_version")(
        lambda value: (
            value
            if type(value) is int and value == P5_SCHEMA_VERSION
            else (_ for _ in ()).throw(ValueError("P5 event schema_version must be 4"))
        )
    )
    _engine = field_validator("engine_version")(
        lambda value: (
            value
            if value == P5_ENGINE_VERSION
            else (_ for _ in ()).throw(ValueError("P5 event engine_version is unsupported"))
        )
    )
    _hashes = field_validator(
        "command_hash", "payload_hash", "result_hash", "previous_event_hash", "event_hash"
    )(
        lambda value: (
            value
            if isinstance(value, str) and _HASH.fullmatch(value)
            else (_ for _ in ()).throw(ValueError("P5 event hash is invalid"))
        )
    )

    @field_validator("payload", "result", mode="before")
    @classmethod
    def _objects(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("occurred_at_utc", "recorded_at_utc")
    @classmethod
    def _timestamps(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _invariants(self) -> P5KernelEvent:
        if self.new_revision != self.expected_revision + 1 or self.sequence_no != self.new_revision:
            raise ValueError("P5 event revision and sequence are not contiguous")
        allowed = {
            P5EventType.RUN_CREATED: P5CommandType.CREATE_EXECUTION,
            P5EventType.NODE_PREPARED: P5CommandType.PREPARE_NODE,
            P5EventType.ACTION_APPROVED: P5CommandType.APPROVE_ACTION,
            P5EventType.JOB_RESERVED: P5CommandType.LAUNCH_ORCA,
            P5EventType.JOB_OBSERVED: P5CommandType.OBSERVE_JOB,
            P5EventType.RESULT_COLLECTED: P5CommandType.COLLECT_RESULT,
            P5EventType.CANCEL_REQUESTED: P5CommandType.REQUEST_CANCEL,
            P5EventType.RUN_CANCELLED: P5CommandType.COMPLETE_CANCEL,
            P5EventType.RECONCILED: P5CommandType.RECONCILE_EXECUTION,
            P5EventType.RUN_FAILED: P5CommandType.COLLECT_RESULT,
            P5EventType.EFFECT_SUCCEEDED: P5CommandType.COMPLETE_EFFECT,
            P5EventType.EFFECT_DEAD_LETTERED: P5CommandType.COMPLETE_EFFECT,
        }
        if allowed[self.event_type] is not self.command_type:
            raise ValueError("P5 event type is not valid for its command type")
        try:
            verify_sha256(self.payload, self.payload_hash)
            verify_sha256(self.result, self.result_hash)
        except HashMismatchError as error:
            raise ValueError("P5 event content hash does not match") from error
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
            raise ValueError("P5 event envelope hash does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        command_id: CommandId,
        command_type: P5CommandType,
        run_id: RunId,
        sequence_no: int,
        expected_revision: int,
        event_type: P5EventType,
        payload: JsonObject,
        result: ApplicationResult | JsonObject,
        occurred_at_utc: datetime,
        command_hash: str,
        previous_event_hash: str = GENESIS_EVENT_HASH,
        event_id: EventId | None = None,
        recorded_at_utc: datetime | None = None,
    ) -> P5KernelEvent:
        actual_id = event_id or new_id(EventId)
        recorded = recorded_at_utc or occurred_at_utc
        result_value = (
            result.model_dump(mode="json") if isinstance(result, ApplicationResult) else result
        )
        payload_hash = sha256_hex(payload)
        result_hash = sha256_hex(result_value)
        event_hash = event_envelope_hash(
            event_id=str(actual_id),
            previous_event_hash=previous_event_hash,
            command_id=str(command_id),
            command_type=command_type.value,
            command_hash=command_hash,
            run_id=str(run_id),
            sequence_no=sequence_no,
            expected_revision=expected_revision,
            new_revision=sequence_no,
            event_type=event_type.value,
            schema_version=P5_SCHEMA_VERSION,
            engine_version=P5_ENGINE_VERSION,
            payload=payload,
            payload_hash=payload_hash,
            result=result_value,
            result_hash=result_hash,
            occurred_at_utc=_utc_text(occurred_at_utc),
            recorded_at_utc=_utc_text(recorded),
        )
        return cls(
            event_id=actual_id,
            command_id=command_id,
            command_type=command_type,
            command_hash=command_hash,
            run_id=run_id,
            sequence_no=sequence_no,
            expected_revision=expected_revision,
            new_revision=sequence_no,
            event_type=event_type,
            schema_version=P5_SCHEMA_VERSION,
            engine_version=P5_ENGINE_VERSION,
            payload=payload,
            payload_hash=payload_hash,
            result=result_value,
            result_hash=result_hash,
            occurred_at_utc=occurred_at_utc,
            recorded_at_utc=recorded,
            previous_event_hash=previous_event_hash,
            event_hash=event_hash,
        )


class P5Transition(KernelModel):
    next_state: P5WorkflowState
    effects: tuple[EffectSpec, ...] = ()
    interrupt_operations: tuple = ()


def reduce_p5_event(prior_state: P5WorkflowState | None, event: P5KernelEvent) -> P5Transition:
    """Reduce a P5 fact by validating its complete next-state projection.

    The application service computes the transition from trusted records. The
    event stores that resulting state so replay never reads files or launches a
    process; replay still validates the state contract and the event chain.
    """

    if prior_state is not None and prior_state.run_id != event.run_id:
        raise InvalidTransitionError("P5 event belongs to a different run")
    raw = event.payload.get("next_state")
    if not isinstance(raw, Mapping):
        raise InvalidTransitionError("P5 event is missing next_state")
    try:
        state = P5WorkflowState.model_validate_json(
            json.dumps(thaw_json(raw), ensure_ascii=False), strict=True
        )
    except Exception as error:
        raise InvalidTransitionError("P5 event next_state is invalid") from error
    if state.run_id != event.run_id:
        raise InvalidTransitionError("P5 next_state run ID does not match event")
    effects = tuple(
        EffectSpec.model_validate_json(json.dumps(thaw_json(raw)), strict=True)
        for raw in event.payload.get("effects", ())
    )
    return P5Transition(next_state=state, effects=effects)


def expected_p5_application_result(
    *, event: P5KernelEvent, transition: P5Transition
) -> ApplicationResult:
    return ApplicationResult(
        accepted=True,
        code=str(event.payload.get("outcome_code", event.event_type.value)),
        run_id=event.run_id,
        revision=event.new_revision,
        status=transition.next_state.status,
        event_id=event.event_id,
        interrupt_id=None,
        details={
            "phase": transition.next_state.phase.value,
            "scientific_assessment": "not_evaluated",
            "claim_status": "not_generated",
        },
    )


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "P5KernelEvent",
    "P5Transition",
    "expected_p5_application_result",
    "reduce_p5_event",
]
