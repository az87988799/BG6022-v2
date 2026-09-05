"""Pure validation for the fixed P3 Water action."""

from __future__ import annotations

from dataclasses import dataclass

from orca_agent.domain.errors import ContractInvariantError
from orca_agent.domain.models import PlanProposal, ValidatedAction
from orca_agent.planning.water import WaterFixture, validate_water_action


@dataclass(frozen=True)
class P3ValidationResult:
    valid: bool
    code: str
    details: dict[str, object]


def validate_p3_action(
    action: ValidatedAction,
    *,
    proposal: PlanProposal | None = None,
    fixture: WaterFixture | None = None,
) -> P3ValidationResult:
    result = validate_water_action(action, proposal=proposal, fixture=fixture)
    return P3ValidationResult(result.valid, result.code, result.details)


def require_valid_water_action(
    action: ValidatedAction,
    *,
    proposal: PlanProposal | None = None,
    fixture: WaterFixture | None = None,
) -> None:
    result = validate_p3_action(action, proposal=proposal, fixture=fixture)
    if not result.valid:
        raise ContractInvariantError(result.code, details=result.details)


__all__ = ["P3ValidationResult", "require_valid_water_action", "validate_p3_action"]
