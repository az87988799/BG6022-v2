import re

import pytest

from orca_agent.domain.errors import InvalidIdentifierError
from orca_agent.domain.ids import (
    ActionId,
    ArtifactId,
    ClaimId,
    EvidenceId,
    PlanProposalId,
    PrimitiveId,
    ProblemSpecId,
    new_id,
)


@pytest.mark.parametrize(
    "identifier_type,prefix",
    [
        (ProblemSpecId, "problem"),
        (PlanProposalId, "plan"),
        (PrimitiveId, "primitive"),
        (ActionId, "action"),
        (EvidenceId, "evidence"),
        (ClaimId, "claim"),
        (ArtifactId, "artifact"),
    ],
)
def test_new_id_uses_uuid4_hex_and_expected_prefix(identifier_type: type[str], prefix: str) -> None:
    identifier = new_id(identifier_type)

    assert re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", identifier)
    assert identifier_type(identifier) == identifier


@pytest.mark.parametrize(
    "identifier_type,value",
    [
        (ProblemSpecId, "plan_" + "0" * 32),
        (PlanProposalId, "plan_" + "0" * 31),
        (PrimitiveId, "primitive_" + "G" * 32),
        (ActionId, "action_/" + "0" * 31),
    ],
)
def test_invalid_identifiers_fail_closed(identifier_type: type[str], value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        identifier_type(value)


def test_identifier_rejects_whitespace_and_non_string_values() -> None:
    with pytest.raises(InvalidIdentifierError):
        ProblemSpecId("problem_" + "0" * 31 + " ")
    with pytest.raises(InvalidIdentifierError):
        ProblemSpecId(1)  # type: ignore[arg-type]
