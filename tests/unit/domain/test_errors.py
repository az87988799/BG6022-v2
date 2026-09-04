from pydantic import BaseModel, ValidationError

from orca_agent.domain.errors import (
    ContractInvariantError,
    contract_error_from_validation,
)


def test_validation_error_is_converted_to_safe_typed_error() -> None:
    class StrictModel(BaseModel):
        count: int

    try:
        StrictModel(count="not-an-int")
    except ValidationError as error:
        converted = contract_error_from_validation(error)
    else:
        raise AssertionError("expected Pydantic validation error")

    assert isinstance(converted, ContractInvariantError)
    assert converted.code == "contract_invariant_error"
    assert converted.details["errors"][0]["loc"] == "count"
    assert "traceback" not in str(converted).lower()
