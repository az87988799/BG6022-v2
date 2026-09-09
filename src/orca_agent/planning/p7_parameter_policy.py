"""Read-only structure checks and the intentionally tiny P7 parameter policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from orca_agent.domain.hashing import verify_sha256
from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object
from orca_agent.domain.p7_conversation import (
    MoleculeInputType,
    P7Model,
    ParameterSource,
    ParameterValue,
)
from orca_agent.identity.rdkit_normalizer import IdentityNormalizationError, RDKitNormalizer

P7_PARAMETER_POLICY_VERSION = "p7-parameter-policy-v1"
P7_PARAMETER_POLICY_V2 = "p7-parameter-policy-v2"
WATER_CANONICAL_SMILES = "O"
ETHANOL_CANONICAL_SMILES = "CCO"
OXYGEN_CANONICAL_SMILES = "O=O"


class PolicyRecommendation(P7Model):
    value: str
    rule_id: str
    applies_to: tuple[str, ...]

    @field_validator("applies_to", mode="before")
    @classmethod
    def _applies_to_tuple(cls, value: object) -> tuple[object, ...]:
        if isinstance(value, list):
            return tuple(value)
        return value  # type: ignore[return-value]

    @field_validator("value", "rule_id", "applies_to")
    @classmethod
    def _recommendation_text(cls, value, info):
        if isinstance(value, tuple):
            return tuple(str(item).strip() for item in value)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{getattr(info, 'field_name', 'recommendation')} is blank")
        return value.strip()


class PolicyMolecule(P7Model):
    canonical_name: str
    aliases: tuple[str, ...]
    cas: str
    cid: str
    canonical_smiles: str
    molecular_formula: str
    charge: int
    multiplicity: int
    rule_id: str

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases_tuple(cls, value: object) -> tuple[object, ...]:
        if isinstance(value, list):
            return tuple(value)
        return value  # type: ignore[return-value]

    @field_validator(
        "canonical_name",
        "aliases",
        "cas",
        "cid",
        "canonical_smiles",
        "molecular_formula",
        "rule_id",
    )
    @classmethod
    def _molecule_text(cls, value, info):
        if isinstance(value, tuple):
            values = tuple(str(item).strip() for item in value)
            if not values or any(not item for item in values):
                raise ValueError(f"{getattr(info, 'field_name', 'aliases')} is invalid")
            if len(values) != len(set(item.casefold() for item in values)):
                raise ValueError("molecule aliases must be unique")
            return values
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{getattr(info, 'field_name', 'molecule')} is blank")
        return value.strip()

    @field_validator("multiplicity")
    @classmethod
    def _multiplicity(cls, value: int) -> int:
        if type(value) is not int or value < 1:
            raise ValueError("policy multiplicity must be a positive integer")
        return value


class ParameterPolicyV2(P7Model):
    """Frozen, hash-bound defaults shared by planning and identity checks."""

    policy_version: Literal[P7_PARAMETER_POLICY_V2] = P7_PARAMETER_POLICY_V2
    policy_id: str
    content_hash: str
    scope: str
    recommendations: dict[str, PolicyRecommendation]
    molecules: tuple[PolicyMolecule, ...]
    special_rules: FrozenJsonObject = {}

    @field_validator("policy_id", "scope")
    @classmethod
    def _policy_text(cls, value: str, info: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{getattr(info, 'field_name', 'policy')} is blank")
        return value.strip()

    @field_validator("content_hash")
    @classmethod
    def _policy_hash(cls, value: str) -> str:
        if (
            len(value) != 64
            or value != value.casefold()
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("policy content_hash must be lowercase SHA-256 hex")
        return value

    @field_validator("recommendations")
    @classmethod
    def _recommendations(
        cls, value: dict[str, PolicyRecommendation]
    ) -> dict[str, PolicyRecommendation]:
        if set(value) != {"method", "environment"}:
            raise ValueError("policy recommendations must define method and environment")
        return dict(value)

    @field_validator("molecules", mode="before")
    @classmethod
    def _molecules_tuple(cls, value: object) -> tuple[object, ...]:
        if isinstance(value, list):
            return tuple(value)
        return value  # type: ignore[return-value]

    @field_validator("molecules")
    @classmethod
    def _molecules(cls, value: tuple[PolicyMolecule, ...]) -> tuple[PolicyMolecule, ...]:
        names = tuple(item.canonical_name for item in value)
        if len(names) != len(set(names)):
            raise ValueError("policy molecule names must be unique")
        return value

    @field_validator("special_rules", mode="before")
    @classmethod
    def _rules(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @model_validator(mode="after")
    def _content_matches(self) -> ParameterPolicyV2:
        verify_sha256(self.model_dump(mode="json", exclude={"content_hash"}), self.content_hash)
        return self

    @property
    def snapshot_hash(self) -> str:
        return self.content_hash

    def molecule_for(self, input_kind: MoleculeInputType, value: str) -> PolicyMolecule | None:
        normalized = value.strip().casefold()
        if input_kind is MoleculeInputType.NAME:
            return next(
                (
                    item
                    for item in self.molecules
                    if normalized == item.canonical_name.casefold()
                    or normalized in {alias.casefold() for alias in item.aliases}
                ),
                None,
            )
        if input_kind is MoleculeInputType.CAS:
            return next(
                (item for item in self.molecules if normalized == item.cas.casefold()),
                None,
            )
        if input_kind is MoleculeInputType.CID:
            canonical = normalized.lstrip("0") or "0"
            return next(
                (item for item in self.molecules if canonical == item.cid.casefold().lstrip("0")),
                None,
            )
        return None

    def molecule_for_smiles(self, canonical_smiles: str) -> PolicyMolecule | None:
        return next(
            (item for item in self.molecules if item.canonical_smiles == canonical_smiles), None
        )

    def recommendation(self, field_name: str) -> PolicyRecommendation:
        try:
            return self.recommendations[field_name]
        except KeyError as error:
            raise ValueError(f"policy recommendation is not registered: {field_name}") from error

    def public_snapshot(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _policy_path() -> Path:
    return Path(__file__).resolve().parents[1] / "resources" / "p7" / "parameter-policy.v2.json"


@lru_cache(maxsize=1)
def load_parameter_policy(path: str | Path | None = None) -> ParameterPolicyV2:
    policy_path = _policy_path() if path is None else Path(path).resolve()
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"unable to load P7 parameter policy: {policy_path}") from error
    return ParameterPolicyV2.model_validate_json(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":")), strict=True
    )


def default_parameter_policy() -> ParameterPolicyV2:
    return load_parameter_policy()


class PrecheckStatus(str):
    VALID = "valid"
    INVALID = "invalid"
    DEPENDENCY_MISSING = "dependency_missing"


class MoleculePrecheckResult(P7Model):
    status: Literal["valid", "invalid", "dependency_missing"]
    input_kind: MoleculeInputType
    raw_input: str
    canonical_isomeric_smiles: str | None = None
    molecular_formula: str | None = None
    formal_charge: int | None = None
    structure_hash: str | None = None
    stereo_status: str | None = None
    checks: FrozenJsonObject = {}
    reason_code: str | None = None
    message: str | None = None

    @field_validator("raw_input")
    @classmethod
    def _raw(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("raw_input is invalid")
        return value.strip()

    @field_validator("checks", mode="before")
    @classmethod
    def _checks(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)


@dataclass(frozen=True)
class ElectronicStateResolution:
    charge: ParameterValue | None
    multiplicity: ParameterValue | None
    issues: tuple[str, ...]
    recommendation: str | None = None


class MoleculePrecheck:
    """Run only local graph validation; never creates a P4/P5 task."""

    policy_version = P7_PARAMETER_POLICY_VERSION

    def __init__(
        self,
        normalizer: RDKitNormalizer | None = None,
        policy: ParameterPolicyV2 | None = None,
    ) -> None:
        self._normalizer = normalizer
        self.policy = policy

    def check(
        self,
        *,
        input_kind: MoleculeInputType,
        raw_input: str,
        charge: int | None = None,
    ) -> MoleculePrecheckResult:
        if input_kind is not MoleculeInputType.SMILES:
            return MoleculePrecheckResult(
                status="valid",
                input_kind=input_kind,
                raw_input=raw_input,
                checks={"local_graph_check": "not_applicable"},
            )
        try:
            normalizer = self._normalizer or RDKitNormalizer()
            actual_charge = normalizer.formal_charge(raw_input)
            if charge is not None and actual_charge != charge:
                return MoleculePrecheckResult(
                    status="invalid",
                    input_kind=input_kind,
                    raw_input=raw_input,
                    formal_charge=actual_charge,
                    reason_code="charge_structure_conflict",
                    message="the requested charge conflicts with the formal charge in the SMILES",
                    checks={"formal_charge": actual_charge, "charge_matches": False},
                )
            normalized = normalizer.normalize(
                raw_input,
                charge=actual_charge,
                multiplicity=1,
            )
            return MoleculePrecheckResult(
                status="valid",
                input_kind=input_kind,
                raw_input=raw_input,
                canonical_isomeric_smiles=normalized.canonical_isomeric_smiles,
                molecular_formula=normalized.molecular_formula,
                formal_charge=normalized.formal_charge,
                structure_hash=normalized.structure_hash,
                stereo_status=normalized.stereo_status,
                checks={**normalized.checks, "precheck_only": True},
            )
        except (ModuleNotFoundError, RuntimeError):
            return MoleculePrecheckResult(
                status="dependency_missing",
                input_kind=input_kind,
                raw_input=raw_input,
                reason_code="rdkit_unavailable",
                message="the p4/p5 optional dependency RDKit is not installed",
            )
        except (IdentityNormalizationError, TypeError, ValueError) as error:
            return MoleculePrecheckResult(
                status="invalid",
                input_kind=input_kind,
                raw_input=raw_input,
                reason_code=getattr(error, "code", "invalid_structure"),
                message=str(error)[:512],
            )

    def resolve_electronic_state(
        self,
        *,
        precheck: MoleculePrecheckResult,
        requested_charge: int | None,
        requested_multiplicity: int | None,
        source_reference: str,
        charge_source_fragment: str | None = None,
        multiplicity_source_fragment: str | None = None,
        require_recommendation_acceptance: bool = True,
    ) -> ElectronicStateResolution:
        if precheck.status != "valid":
            return ElectronicStateResolution(
                None, None, (precheck.reason_code or "invalid_structure",)
            )
        if precheck.formal_charge is None or precheck.structure_hash is None:
            return ElectronicStateResolution(
                None,
                None,
                ("formal_charge_unresolved",),
            )
        if requested_charge is not None and requested_charge != precheck.formal_charge:
            return ElectronicStateResolution(None, None, ("charge_structure_conflict",))
        charge = ParameterValue(
            name="charge",
            value=precheck.formal_charge,
            value_type="integer",
            hard_constraint=requested_charge is not None,
            source=(
                ParameterSource.USER_EXPLICIT
                if requested_charge is not None
                else ParameterSource.STRUCTURE_DERIVED
            ),
            source_reference=source_reference,
            source_fragment=charge_source_fragment,
            structure_hash=precheck.structure_hash,
        )
        if requested_multiplicity is not None:
            if requested_multiplicity < 1:
                return ElectronicStateResolution(None, None, ("invalid_multiplicity",))
            if precheck.canonical_isomeric_smiles == OXYGEN_CANONICAL_SMILES:
                return ElectronicStateResolution(
                    charge,
                    None,
                    ("electronic_state_conflict",),
                    "molecular oxygen is outside the singlet baseline policy",
                )
            multiplicity = ParameterValue(
                name="multiplicity",
                value=requested_multiplicity,
                value_type="integer",
                hard_constraint=True,
                source=ParameterSource.USER_EXPLICIT,
                source_reference=source_reference,
                source_fragment=multiplicity_source_fragment,
                structure_hash=precheck.structure_hash,
            )
            if requested_multiplicity != 1:
                return ElectronicStateResolution(
                    charge,
                    multiplicity,
                    ("unsupported_electronic_state",),
                )
            return ElectronicStateResolution(charge, multiplicity, ())

        if precheck.canonical_isomeric_smiles == OXYGEN_CANONICAL_SMILES:
            return ElectronicStateResolution(
                charge,
                None,
                ("multiplicity_required", "oxygen_triplet_requires_separate_policy"),
            )

        recommendation = (
            None
            if self.policy is None
            else self.policy.molecule_for_smiles(precheck.canonical_isomeric_smiles)
        )
        if recommendation is not None or precheck.canonical_isomeric_smiles in {
            WATER_CANONICAL_SMILES,
            ETHANOL_CANONICAL_SMILES,
        }:
            rule_version = (
                recommendation.rule_id
                if recommendation is not None
                else self.policy_version
            )
            recommended_multiplicity = (
                recommendation.multiplicity if recommendation is not None else 1
            )
            recommendation_reason = (
                f"exact {recommendation.canonical_name} structure matched the versioned "
                "singlet recommendation"
                if recommendation is not None
                else (
                    "exact water/ethanol neutral structure matched the versioned "
                    "singlet recommendation"
                )
            )
            return ElectronicStateResolution(
                charge,
                ParameterValue(
                    name="multiplicity",
                    value=recommended_multiplicity,
                    value_type="integer",
                    hard_constraint=False,
                    source=ParameterSource.POLICY_RECOMMENDED,
                    source_reference=source_reference,
                    structure_hash=precheck.structure_hash,
                    rule_version=rule_version,
                    recommendation_accepted=False,
                ),
                ("multiplicity_recommendation_requires_acceptance",)
                if require_recommendation_acceptance
                else (),
                recommendation_reason,
            )
        return ElectronicStateResolution(charge, None, ("multiplicity_required",))


def accepted_recommendation(value: ParameterValue) -> ParameterValue:
    """Convert a displayed recommendation into an explicit accepted value."""

    if value.source is not ParameterSource.POLICY_RECOMMENDED or value.rule_version is None:
        raise ValueError("only a policy recommendation can be accepted")
    return value.model_copy(
        update={
            "source": ParameterSource.USER_ACCEPTED_RECOMMENDATION,
            "hard_constraint": True,
            "recommendation_accepted": True,
        }
    )


__all__ = [
    "ETHANOL_CANONICAL_SMILES",
    "ElectronicStateResolution",
    "MoleculePrecheck",
    "MoleculePrecheckResult",
    "OXYGEN_CANONICAL_SMILES",
    "P7_PARAMETER_POLICY_VERSION",
    "P7_PARAMETER_POLICY_V2",
    "ParameterPolicyV2",
    "PolicyMolecule",
    "PolicyRecommendation",
    "PrecheckStatus",
    "WATER_CANONICAL_SMILES",
    "accepted_recommendation",
    "default_parameter_policy",
    "load_parameter_policy",
]
