"""Replay and snapshot verification for schema-5 P6 runs."""

from __future__ import annotations

import json
from collections.abc import Sequence

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import GENESIS_EVENT_HASH
from orca_agent.domain.ids import EventId, RunId
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p6 import P6WorkflowState

from .p6_kernel import P6KernelEvent, expected_p6_application_result, reduce_p6_event
from .p6_versions import P6_ENGINE_VERSION, P6_SCHEMA_VERSION
from .replay import state_hash


def replay_p6(events: Sequence[P6KernelEvent]) -> P6WorkflowState:
    if not events:
        raise StateIntegrityError("cannot replay an empty P6 event stream")
    state: P6WorkflowState | None = None
    run_id: RunId | None = None
    previous_hash = GENESIS_EVENT_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if event.schema_version != P6_SCHEMA_VERSION or event.engine_version != P6_ENGINE_VERSION:
            raise StateIntegrityError("P6 event stream contains an unsupported version")
        if (
            event.sequence_no != expected_sequence
            or event.expected_revision != expected_sequence - 1
        ):
            raise StateIntegrityError("P6 event sequence or revision is not contiguous")
        if run_id is None:
            run_id = event.run_id
        elif event.run_id != run_id:
            raise StateIntegrityError("P6 event stream contains multiple runs")
        if event.previous_event_hash != previous_hash:
            raise StateIntegrityError("P6 event hash chain is not contiguous")
        try:
            transition = reduce_p6_event(state, event)
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False), strict=True
            )
            if result != expected_p6_application_result(event=event, transition=transition):
                raise StateIntegrityError("P6 event result does not match its transition")
            state = transition.next_state
            previous_hash = event.event_hash
        except StateIntegrityError:
            raise
        except Exception as error:
            raise StateIntegrityError("P6 event stream cannot be replayed") from error
    if state is None:
        raise StateIntegrityError("P6 event stream produced no state")
    return state


def verify_p6_snapshot(
    *,
    snapshot: P6WorkflowState,
    stored_state_hash: str,
    stored_revision: int,
    stored_last_event_id: EventId,
    events: Sequence[P6KernelEvent],
) -> P6WorkflowState:
    if state_hash(snapshot) != stored_state_hash:
        raise StateIntegrityError("stored P6 snapshot hash does not match state")
    replayed = replay_p6(events)
    if replayed != snapshot:
        raise StateIntegrityError("P6 event replay does not match stored snapshot")
    if stored_revision != len(events) or events[-1].event_id != stored_last_event_id:
        raise StateIntegrityError("stored P6 revision does not match event stream")
    return replayed


__all__ = ["replay_p6", "verify_p6_snapshot"]
