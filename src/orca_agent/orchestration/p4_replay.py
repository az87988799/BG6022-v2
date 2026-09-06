"""Strict event replay and snapshot verification for schema-3 P4 runs."""

from __future__ import annotations

import json
from collections.abc import Sequence

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import GENESIS_EVENT_HASH
from orca_agent.domain.ids import EventId, RunId
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p4 import P4WorkflowState

from .p4_kernel import P4KernelEvent, expected_p4_application_result, reduce_p4_event
from .p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from .replay import state_hash


def replay_p4(events: Sequence[P4KernelEvent]) -> P4WorkflowState:
    if not events:
        raise StateIntegrityError("cannot replay an empty P4 event stream")
    state: P4WorkflowState | None = None
    run_id: RunId | None = None
    previous_hash = GENESIS_EVENT_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if not isinstance(event, P4KernelEvent):
            raise StateIntegrityError("P4 event stream contains a non-P4 event")
        if event.schema_version != P4_SCHEMA_VERSION or event.engine_version != P4_ENGINE_VERSION:
            raise StateIntegrityError("P4 event stream contains an unsupported version")
        if (
            event.sequence_no != expected_sequence
            or event.expected_revision != expected_sequence - 1
        ):
            raise StateIntegrityError("P4 event sequence or revision is not contiguous")
        if run_id is None:
            run_id = event.run_id
        elif event.run_id != run_id:
            raise StateIntegrityError("P4 event stream contains multiple runs")
        if event.previous_event_hash != previous_hash:
            raise StateIntegrityError("P4 event hash chain is not contiguous")
        try:
            transition = reduce_p4_event(state, event)
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False), strict=True
            )
            expected = expected_p4_application_result(
                prior_state=state,
                event=event,
                transition=transition,
            )
            if result != expected:
                raise StateIntegrityError("P4 event result does not match its transition")
        except StateIntegrityError:
            raise
        except Exception as error:
            raise StateIntegrityError("P4 event stream cannot be replayed") from error
        state = transition.next_state
        previous_hash = event.event_hash
    if state is None:
        raise StateIntegrityError("P4 event stream produced no state")
    return state


def verify_p4_snapshot(
    *,
    snapshot: P4WorkflowState,
    stored_state_hash: str,
    stored_revision: int,
    stored_last_event_id: EventId,
    events: Sequence[P4KernelEvent],
) -> P4WorkflowState:
    if state_hash(snapshot) != stored_state_hash:
        raise StateIntegrityError("stored P4 snapshot hash does not match state")
    replayed = replay_p4(events)
    if replayed != snapshot:
        raise StateIntegrityError("P4 event replay does not match stored snapshot")
    if stored_revision != len(events):
        raise StateIntegrityError("stored P4 revision does not match event count")
    if events[-1].event_id != stored_last_event_id or events[-1].new_revision != stored_revision:
        raise StateIntegrityError("stored P4 last event does not match event stream")
    return replayed


__all__ = ["replay_p4", "verify_p4_snapshot"]
