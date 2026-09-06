"""Closed version routing for P2, P3, and P4 durable values."""

from __future__ import annotations

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.p4 import P4WorkflowState
from orca_agent.orchestration.p4_kernel import P4KernelEvent, reduce_p4_event
from orca_agent.orchestration.p4_replay import replay_p4, verify_p4_snapshot
from orca_agent.orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION


def route_state(schema_version: int, engine_version: str):
    if schema_version == P4_SCHEMA_VERSION and engine_version == P4_ENGINE_VERSION:
        return P4WorkflowState
    raise StateIntegrityError("stored workflow version is unsupported")


def route_event(schema_version: int, engine_version: str):
    if schema_version == P4_SCHEMA_VERSION and engine_version == P4_ENGINE_VERSION:
        return P4KernelEvent
    raise StateIntegrityError("stored workflow version is unsupported")


__all__ = [
    "P4_ENGINE_VERSION",
    "P4_SCHEMA_VERSION",
    "P4KernelEvent",
    "P4WorkflowState",
    "reduce_p4_event",
    "replay_p4",
    "route_event",
    "route_state",
    "verify_p4_snapshot",
]
