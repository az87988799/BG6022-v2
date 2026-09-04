import pytest

from orca_agent.domain.errors import HashMismatchError
from orca_agent.domain.hashing import sha256_hex, verify_sha256


def test_sha256_golden_vector() -> None:
    expected = "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"

    assert sha256_hex({"a": 1}) == expected


def test_hash_changes_when_value_changes() -> None:
    assert sha256_hex({"a": 1}) != sha256_hex({"a": 2})


def test_hash_verification_accepts_and_rejects() -> None:
    expected = sha256_hex({"a": 1})
    verify_sha256({"a": 1}, expected)

    with pytest.raises(HashMismatchError) as error:
        verify_sha256({"a": 2}, expected)
    assert error.value.code == "hash_mismatch"


def test_hash_verification_rejects_noncanonical_text() -> None:
    with pytest.raises(HashMismatchError):
        verify_sha256({"a": 1}, "A" * 64)
