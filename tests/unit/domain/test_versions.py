import pytest

from orca_agent.domain.errors import UnsupportedSchemaVersionError
from orca_agent.domain.versions import validate_schema_version


def test_schema_version_one_is_supported() -> None:
    assert validate_schema_version(1) == 1


@pytest.mark.parametrize("value", [0, 2, "1", None, True])
def test_unknown_or_non_integer_schema_versions_are_rejected(value: object) -> None:
    with pytest.raises(UnsupportedSchemaVersionError) as error:
        validate_schema_version(value)
    assert error.value.code == "unsupported_schema_version"
