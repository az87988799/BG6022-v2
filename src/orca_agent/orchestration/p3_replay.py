"""Strict replay and snapshot verification for schema-2 P3 runs."""

from __future__ import annotations

import json
from collections.abc import Sequence

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import GENESIS_EVENT_HASH
from orca_agent.domain.ids import EventId, RunId
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p3 import P3WorkflowState

from .p3_kernel import P3KernelEvent, expected_p3_application_result, reduce_p3_event
from .p3_versions import P3_ENGINE_VERSION, P3_SCHEMA_VERSION
from .replay import state_hash


def replay_p3(events: Sequence[P3KernelEvent]) -> P3WorkflowState:
    """Rebuild a P3 state while enforcing the complete event/hash chain."""

    if not events:
        raise StateIntegrityError("cannot replay an empty P3 event stream")
    state: P3WorkflowState | None = None
    run_id: RunId | None = None
    previous_hash = GENESIS_EVENT_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if not isinstance(event, P3KernelEvent):
            raise StateIntegrityError("P3 event stream contains a non-P3 event")
        if event.schema_version != P3_SCHEMA_VERSION or event.engine_version != P3_ENGINE_VERSION:
            raise StateIntegrityError("P3 event stream contains an unsupported version")
        if event.sequence_no != expected_sequence:
            raise StateIntegrityError("P3 event sequence is not contiguous")
        if event.expected_revision != expected_sequence - 1:
            raise StateIntegrityError("P3 event revision chain is not contiguous")
        if run_id is None:
            run_id = event.run_id
        elif event.run_id != run_id:
            raise StateIntegrityError("P3 event stream contains multiple runs")
        if event.previous_event_hash != previous_hash:
            raise StateIntegrityError("P3 event hash chain is not contiguous")
        try:
            transition = reduce_p3_event(state, event)
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False), strict=True
            )
            expected = expected_p3_application_result(
                prior_state=state,
                event=event,
                transition=transition,
            )
            if result != expected:
                raise StateIntegrityError("P3 event result does not match its transition")
        except StateIntegrityError:
            raise
        except Exception as error:
            raise StateIntegrityError("P3 event stream cannot be replayed") from error
        state = transition.next_state
        previous_hash = event.event_hash
    if state is None:
        raise StateIntegrityError("P3 event stream produced no state")
    return state


def verify_p3_snapshot(
    *,
    snapshot: P3WorkflowState,
    stored_state_hash: str,
    stored_revision: int,
    stored_last_event_id: EventId,
    events: Sequence[P3KernelEvent],
) -> P3WorkflowState:
    """Ensure the persisted P3 aggregate equals its verified event replay."""

    if state_hash(snapshot) != stored_state_hash:
        raise StateIntegrityError("stored P3 snapshot hash does not match state")
    replayed = replay_p3(events)
    if replayed != snapshot:
        raise StateIntegrityError("P3 event replay does not match stored snapshot")
    if stored_revision != len(events):
        raise StateIntegrityError("stored P3 revision does not match event count")
    if events[-1].event_id != stored_last_event_id:
        raise StateIntegrityError("stored P3 last event does not match event stream")
    if events[-1].new_revision != stored_revision:
        raise StateIntegrityError("P3 last event revision does not match snapshot")
    return replayed


__all__ = ["replay_p3", "verify_p3_snapshot"]
