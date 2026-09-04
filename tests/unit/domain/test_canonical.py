import math

import pytest

from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.errors import CanonicalizationError


def test_canonical_json_sorts_keys_and_preserves_unicode_and_backslashes() -> None:
    value = {"中": "值", "b": 2, "a": [True, None, "水\\path"]}

    assert canonical_json_bytes(value) == r'{"a":[true,null,"水\\path"],"b":2,"中":"值"}'.encode()


def test_canonical_json_rejects_non_finite_numbers() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(CanonicalizationError):
            canonical_json_bytes({"value": value})


def test_canonical_json_rejects_non_json_values() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"value": (1, 2)})


def test_canonical_json_is_independent_of_mapping_insertion_order() -> None:
    first = {"z": {"b": 2, "a": 1}, "a": [3, 2, 1]}
    second = {"a": [3, 2, 1], "z": {"a": 1, "b": 2}}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
