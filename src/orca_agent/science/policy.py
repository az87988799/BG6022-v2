"""Closed scientific-policy registry for P6."""

from __future__ import annotations

from orca_agent.domain.ids import WorkflowRecordId, new_id
from orca_agent.domain.p6 import ScientificPolicy

P6_MINIMUM_PROFILE_ID = "p6.nonlinear.r2scan3c.v1"


def build_policy(*, record_id: WorkflowRecordId | None = None) -> ScientificPolicy:
    return ScientificPolicy.default(record_id=record_id or new_id(WorkflowRecordId))


def get_policy(profile_id: str, *, record_id: WorkflowRecordId | None = None) -> ScientificPolicy:
    if profile_id != P6_MINIMUM_PROFILE_ID:
        raise ValueError("unknown P6 scientific policy profile")
    return build_policy(record_id=record_id)


__all__ = ["P6_MINIMUM_PROFILE_ID", "build_policy", "get_policy"]
