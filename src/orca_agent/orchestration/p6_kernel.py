"""Pure event envelope and reducer for the schema-5 derived workflow."""

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
from orca_agent.domain.p6 import P6Phase, P6WorkflowState
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.effects import EffectSpec
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p6_versions import P6_ENGINE_VERSION, P6_SCHEMA_VERSION
from orca_agent.orchestration.state import KernelModel, RunStatus
from orca_agent.orchestration.temporal import ensure_utc

_HASH = re.compile(r"^[0-9a-f]{64}$")


class P6KernelEvent(KernelModel):
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
        if type(value) is not int or value != P6_SCHEMA_VERSION:
            raise ValueError("P6 event schema_version must be 5")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P6_ENGINE_VERSION:
            raise ValueError("P6 event engine_version is unsupported")
        return value

    @field_validator(
        "command_hash", "payload_hash", "result_hash", "previous_event_hash", "event_hash"
    )
    @classmethod
    def _hashes(cls, value: str) -> str:
        if _HASH.fullmatch(value) is None:
            raise ValueError("P6 event hash is invalid")
        return value

    @field_validator("payload", "result", mode="before")
    @classmethod
    def _json_objects(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("occurred_at_utc", "recorded_at_utc")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _invariants(self) -> P6KernelEvent:
        if self.new_revision != self.expected_revision + 1 or self.sequence_no != self.new_revision:
            raise ValueError("P6 event revision and sequence are not contiguous")
        allowed = {
            EventType.RUN_CREATED: {CommandType.P6_ASSESS},
            EventType.RUN_CANCELLED: {CommandType.P6_CANCEL},
            EventType.EFFECT_SUCCEEDED: {CommandType.RECORD_EFFECT_SUCCEEDED},
            EventType.EFFECT_DEAD_LETTERED: {CommandType.RECORD_EFFECT_FAILED},
        }
        if self.command_type not in allowed.get(self.event_type, set()):
            raise ValueError("event type is not valid for its P6 command type")
        try:
            verify_sha256(self.payload, self.payload_hash)
            verify_sha256(self.result, self.result_hash)
        except HashMismatchError as error:
            raise ValueError("P6 event content hash does not match") from error
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
            raise ValueError("P6 event envelope hash does not match")
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
        command_hash: str,
        previous_event_hash: str = GENESIS_EVENT_HASH,
        event_id: EventId | None = None,
        recorded_at_utc: datetime | None = None,
    ) -> P6KernelEvent:
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
            schema_version=P6_SCHEMA_VERSION,
            engine_version=P6_ENGINE_VERSION,
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
            schema_version=P6_SCHEMA_VERSION,
            engine_version=P6_ENGINE_VERSION,
            payload=payload,
            payload_hash=payload_hash,
            result=result_value,
            result_hash=result_hash,
            occurred_at_utc=occurred_at_utc,
            recorded_at_utc=recorded,
            previous_event_hash=previous_event_hash,
            event_hash=event_hash,
        )


class P6Transition(KernelModel):
    next_state: P6WorkflowState
    effects: tuple[EffectSpec, ...] = ()
    interrupt_operations: tuple = ()


def _next_state(event: P6KernelEvent) -> P6WorkflowState:
    raw = event.payload.get("next_state")
    if not isinstance(raw, Mapping):
        raise InvalidTransitionError("P6 event is missing next_state")
    try:
        state = P6WorkflowState.model_validate_json(
            json.dumps(thaw_json(raw), ensure_ascii=False), strict=True
        )
    except Exception as error:
        raise InvalidTransitionError("P6 event next_state is invalid") from error
    if state.run_id != event.run_id:
        raise InvalidTransitionError("P6 next_state run ID does not match event")
    return state


def _effects(event: P6KernelEvent) -> tuple[EffectSpec, ...]:
    raw = event.payload.get("effects", ())
    if not isinstance(raw, (list, tuple)):
        raise InvalidTransitionError("P6 event effects are invalid")
    try:
        values = tuple(
            EffectSpec.model_validate_json(
                json.dumps(thaw_json(item), ensure_ascii=False), strict=True
            )
            for item in raw
        )
    except Exception as error:
        raise InvalidTransitionError("P6 event contains an invalid effect") from error
    if tuple(item.effect_index for item in values) != tuple(range(len(values))):
        raise InvalidTransitionError("P6 effect indexes are not contiguous")
    if any(
        item.effect_type not in {"internal.p6.assess", "internal.p6.render_report"}
        for item in values
    ):
        raise InvalidTransitionError("P6 contains an effect outside its closed registry")
    return values


def reduce_p6_event(prior_state: P6WorkflowState | None, event: P6KernelEvent) -> P6Transition:
    if prior_state is not None and prior_state.run_id != event.run_id:
        raise InvalidTransitionError("P6 event belongs to a different run")
    state = _next_state(event)
    effects = _effects(event)
    if event.event_type is EventType.RUN_CREATED:
        if prior_state is not None or state.phase is not P6Phase.ASSESSMENT_PENDING:
            raise InvalidTransitionError("P6 run creation is duplicated or has the wrong phase")
        if len(effects) != 1 or effects[0].effect_type != "internal.p6.assess":
            raise InvalidTransitionError("P6 creation must emit one assessment effect")
        if state.assessment_effect_id != effects[0].effect_id(event.event_id):
            raise InvalidTransitionError("P6 assessment effect ID is not bound to the event")
        return P6Transition(next_state=state, effects=effects)
    if prior_state is None:
        raise InvalidTransitionError("only P6 run creation can initialize a run")
    if prior_state.status.is_terminal:
        raise InvalidTransitionError("terminal P6 runs cannot accept further events")
    if event.event_type is EventType.RUN_CANCELLED:
        if (
            state.phase is not P6Phase.CANCELLED
            or state.status is not RunStatus.CANCELLED
            or effects
        ):
            raise InvalidTransitionError("P6 cancellation transition is invalid")
        return P6Transition(next_state=state)
    raw_effect_id = event.payload.get("effect_id")
    if not isinstance(raw_effect_id, str):
        raise InvalidTransitionError("P6 effect completion is missing effect_id")
    if event.event_type is EventType.EFFECT_DEAD_LETTERED:
        if raw_effect_id not in {
            str(prior_state.assessment_effect_id),
            str(prior_state.report_effect_id),
        }:
            raise InvalidTransitionError(
                "P6 dead-letter effect does not belong to the active phase"
            )
        if state.phase is not P6Phase.FAILED or state.status is not RunStatus.FAILED or effects:
            raise InvalidTransitionError("P6 dead-letter transition is invalid")
        return P6Transition(next_state=state)
    if event.event_type is not EventType.EFFECT_SUCCEEDED:
        raise InvalidTransitionError("P6 event type is not a workflow transition")
    if prior_state.phase is P6Phase.ASSESSMENT_PENDING:
        if raw_effect_id != str(prior_state.assessment_effect_id):
            raise InvalidTransitionError("P6 assessment completion does not match state")
        if state.phase is not P6Phase.REPORT_PENDING or state.assessment_id is None:
            raise InvalidTransitionError("P6 assessment completion has no report phase")
        if len(effects) != 1 or effects[0].effect_type != "internal.p6.render_report":
            raise InvalidTransitionError("P6 assessment completion must emit report effect")
        if state.report_effect_id != effects[0].effect_id(event.event_id):
            raise InvalidTransitionError("P6 report effect ID is not bound to the event")
    elif prior_state.phase is P6Phase.REPORT_PENDING:
        if raw_effect_id != str(prior_state.report_effect_id):
            raise InvalidTransitionError("P6 report completion does not match state")
        if (
            state.phase is not P6Phase.COMPLETED
            or state.report_manifest_id is None
            or not state.report_artifact_ids
            or effects
        ):
            raise InvalidTransitionError("P6 report completion is invalid")
    else:
        raise InvalidTransitionError("P6 success is not allowed in this phase")
    return P6Transition(next_state=state, effects=effects)


def expected_p6_application_result(
    *, event: P6KernelEvent, transition: P6Transition
) -> ApplicationResult:
    phase = transition.next_state.phase.value
    assessment = "not_evaluated" if phase == P6Phase.ASSESSMENT_PENDING.value else "available"
    claims = "not_generated" if not transition.next_state.claim_ids else "generated"
    return ApplicationResult(
        accepted=True,
        code=str(event.payload.get("outcome_code", event.event_type.value)),
        run_id=event.run_id,
        revision=event.new_revision,
        status=transition.next_state.status,
        event_id=event.event_id,
        interrupt_id=None,
        details={"phase": phase, "scientific_assessment": assessment, "claim_status": claims},
    )


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "P6KernelEvent",
    "P6Transition",
    "expected_p6_application_result",
    "reduce_p6_event",
]
