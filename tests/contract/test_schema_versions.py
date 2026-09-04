import pytest

from orca_agent.domain.errors import UnsupportedSchemaVersionError
from orca_agent.domain.ids import ProblemSpecId, new_id
from orca_agent.domain.models import Environment, ProblemSpec


def test_unknown_contract_schema_version_is_rejected() -> None:
    with pytest.raises(UnsupportedSchemaVersionError):
        ProblemSpec(
            record_id=new_id(ProblemSpecId),
            schema_version=2,
            goal="Compute energy",
            molecule_ref="water",
            charge=0,
            multiplicity=1,
            environment=Environment.GAS,
            target_properties=("energy",),
            constraints={},
        )
