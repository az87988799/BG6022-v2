"""Typed, prefixed UUID4 identifiers."""

from __future__ import annotations

import re
import uuid
from typing import ClassVar, TypeVar

from pydantic_core import core_schema

from .errors import InvalidIdentifierError


class PrefixedId(str):
    """A string ID whose prefix identifies its domain entity type."""

    prefix: ClassVar[str] = "id"
    _pattern: ClassVar[re.Pattern[str]] = re.compile(r"^id_[0-9a-f]{32}$")

    def __new__(cls, value: str) -> PrefixedId:
        if not isinstance(value, str) or cls._pattern.fullmatch(value) is None:
            raise InvalidIdentifierError(
                f"invalid {cls.__name__}",
                details={"expected_prefix": cls.prefix},
            )
        return str.__new__(cls, value)

    @classmethod
    def _validate_pydantic(cls, value: str) -> PrefixedId:
        return cls(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source: type[object],
        _handler: object,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls._validate_pydantic,
            core_schema.str_schema(strict=True),
        )


class ProblemSpecId(PrefixedId):
    prefix = "problem"
    _pattern = re.compile(r"^problem_[0-9a-f]{32}$")


class PlanProposalId(PrefixedId):
    prefix = "plan"
    _pattern = re.compile(r"^plan_[0-9a-f]{32}$")


class PrimitiveId(PrefixedId):
    prefix = "primitive"
    _pattern = re.compile(r"^primitive_[0-9a-f]{32}$")


class ActionId(PrefixedId):
    prefix = "action"
    _pattern = re.compile(r"^action_[0-9a-f]{32}$")


class EvidenceId(PrefixedId):
    prefix = "evidence"
    _pattern = re.compile(r"^evidence_[0-9a-f]{32}$")


class ClaimId(PrefixedId):
    prefix = "claim"
    _pattern = re.compile(r"^claim_[0-9a-f]{32}$")


class ArtifactId(PrefixedId):
    prefix = "artifact"
    _pattern = re.compile(r"^artifact_[0-9a-f]{32}$")


IdT = TypeVar("IdT", bound=PrefixedId)


def new_id(identifier_type: type[IdT]) -> IdT:
    """Create a new UUID4 ID for the requested typed entity."""

    return identifier_type(f"{identifier_type.prefix}_{uuid.uuid4().hex}")
