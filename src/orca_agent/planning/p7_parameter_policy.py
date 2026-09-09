"""Read-only structure checks and the intentionally tiny P7 parameter policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import field_validator

from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object
from orca_agent.domain.p7_conversation import (
    MoleculeInputType,
    P7Model,
    ParameterSource,
    ParameterValue,
)
from orca_agent.identity.rdkit_normalizer import IdentityNormalizationError, RDKitNormalizer

P7_PARAMETER_POLICY_VERSION = "p7-parameter-policy-v1"
WATER_CANONICAL_SMILES = "O"
ETHANOL_CANONICAL_SMILES = "CCO"
OXYGEN_CANONICAL_SMILES = "O=O"


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

    def __init__(self, normalizer: RDKitNormalizer | None = None) -> None:
        self._normalizer = normalizer

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

        if precheck.canonical_isomeric_smiles in {
            WATER_CANONICAL_SMILES,
            ETHANOL_CANONICAL_SMILES,
        }:
            return ElectronicStateResolution(
                charge,
                ParameterValue(
                    name="multiplicity",
                    value=1,
                    value_type="integer",
                    hard_constraint=False,
                    source=ParameterSource.POLICY_RECOMMENDED,
                    source_reference=source_reference,
                    structure_hash=precheck.structure_hash,
                    rule_version=self.policy_version,
                    recommendation_accepted=False,
                ),
                ("multiplicity_recommendation_requires_acceptance",),
                (
                    "exact water/ethanol neutral structure matched the versioned "
                    "singlet recommendation"
                ),
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
    "PrecheckStatus",
    "WATER_CANONICAL_SMILES",
    "accepted_recommendation",
]
