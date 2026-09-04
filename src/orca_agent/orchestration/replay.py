"""Strict event replay and snapshot integrity helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import GENESIS_EVENT_HASH, sha256_hex
from orca_agent.domain.ids import EventId, RunId
from orca_agent.domain.json_types import thaw_json

from .events import KernelEvent
from .reducer import reduce_event
from .state import KernelState


def replay(events: Sequence[KernelEvent]) -> KernelState:
    """Rebuild state while requiring a contiguous, single-run event stream."""

    if not events:
        raise StateIntegrityError("cannot replay an empty event stream")
    state: KernelState | None = None
    run_id: RunId | None = None
    previous_event_hash = GENESIS_EVENT_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence_no != expected_sequence:
            raise StateIntegrityError("event sequence is not contiguous")
        if event.expected_revision != expected_sequence - 1:
            raise StateIntegrityError("event revision chain is not contiguous")
        if run_id is None:
            run_id = event.run_id
        elif event.run_id != run_id:
            raise StateIntegrityError("event stream contains multiple runs")
        if event.previous_event_hash != previous_event_hash:
            raise StateIntegrityError("event hash chain is not contiguous")
        try:
            transition = reduce_event(state, event)
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False)
            )
            if (
                result.run_id != event.run_id
                or result.event_id != event.event_id
                or result.revision != event.new_revision
                or result.status is not transition.next_status
                or result.code != transition.outcome.code
                or result.accepted is not transition.outcome.accepted
                or result.details != transition.outcome.details
            ):
                raise StateIntegrityError("event result does not match its envelope")
            state = transition.next_state
            previous_event_hash = event.event_hash
        except StateIntegrityError:
            raise
        except Exception as error:
            if isinstance(error, StateIntegrityError):
                raise
            raise StateIntegrityError("event stream cannot be replayed") from error
    if state is None:
        raise StateIntegrityError("event stream produced no state")
    return state


def state_hash(state: KernelState) -> str:
    """Hash the canonical JSON representation of a kernel state."""

    return sha256_hex(state.model_dump(mode="json"))


def verify_snapshot(
    *,
    snapshot: KernelState,
    stored_state_hash: str,
    stored_revision: int,
    stored_last_event_id: EventId,
    events: Sequence[KernelEvent],
) -> KernelState:
    """Ensure stored aggregate metadata and replayed state agree exactly."""

    actual_hash = state_hash(snapshot)
    if actual_hash != stored_state_hash:
        raise StateIntegrityError("stored snapshot hash does not match state")
    replayed = replay(events)
    if replayed != snapshot:
        raise StateIntegrityError("event replay does not match stored snapshot")
    if stored_revision != len(events):
        raise StateIntegrityError("stored revision does not match event count")
    if events[-1].event_id != stored_last_event_id:
        raise StateIntegrityError("stored last event does not match event stream")
    if events[-1].new_revision != stored_revision:
        raise StateIntegrityError("last event revision does not match snapshot revision")
    return replayed


__all__ = ["replay", "state_hash", "verify_snapshot"]
