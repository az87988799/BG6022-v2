"""The sole RDKit adapter for P4 identity normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from orca_agent.domain.hashing import sha256_hex
from orca_agent.orchestration.p4_versions import P4_NORMALIZER_VERSION


class IdentityNormalizationError(ValueError):
    """A typed, safe error raised before a candidate can enter a bundle."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NormalizedStructure:
    source_smiles: str
    canonical_isomeric_smiles: str
    molecular_formula: str
    formal_charge: int
    fragment_count: int
    radical_electron_count: int
    stereo_status: str
    isotope_labels: tuple[int, ...]
    rdkit_version: str
    normalization_strategy: str
    structure_hash: str
    checks: dict[str, object]


class RDKitNormalizer:
    """Normalize without selecting fragments, stereoisomers, or tautomers."""

    strategy_version: ClassVar[str] = P4_NORMALIZER_VERSION
    max_input_chars: ClassVar[int] = 4096
    supported_elements: ClassVar[frozenset[str]] = frozenset(
        {"H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "I"}
    )
    max_non_h_atoms: ClassVar[int] = 50

    def __init__(self) -> None:
        try:
            from rdkit import Chem, rdBase
            from rdkit.Chem import rdMolDescriptors

            self._chem = Chem
            self._rd_base = rdBase
            self._descriptors = rdMolDescriptors
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "P4 identity normalization requires the p4 optional dependencies"
            ) from error

    def normalize(self, smiles: str, *, charge: int, multiplicity: int) -> NormalizedStructure:
        if not isinstance(smiles, str) or not smiles.strip() or "\x00" in smiles:
            raise IdentityNormalizationError("invalid_identity", "SMILES is empty or invalid")
        source = smiles.strip()
        if "|" in source or any(character.isspace() for character in source):
            raise IdentityNormalizationError(
                "invalid_identity", "CXSMILES and annotated SMILES are unsupported in P4"
            )
        if len(source) > self.max_input_chars:
            raise IdentityNormalizationError("identity_result_limit_exceeded", "SMILES is too long")
        if type(charge) is not int or type(multiplicity) is not int or multiplicity < 1:
            raise IdentityNormalizationError(
                "invalid_identity", "charge or multiplicity is invalid"
            )
        try:
            molecule = self._chem.MolFromSmiles(source, sanitize=True)
        except Exception as error:  # RDKit may expose implementation exceptions.
            raise IdentityNormalizationError(
                "invalid_identity", "SMILES could not be parsed"
            ) from error
        if molecule is None:
            raise IdentityNormalizationError("invalid_identity", "SMILES could not be parsed")
        try:
            self._chem.SanitizeMol(molecule)
        except Exception as error:
            raise IdentityNormalizationError(
                "invalid_identity", "SMILES sanitization failed"
            ) from error

        if any(atom.GetAtomicNum() == 0 or atom.HasQuery() for atom in molecule.GetAtoms()):
            raise IdentityNormalizationError(
                "invalid_identity", "query or wildcard atoms are not allowed"
            )

        actual_charge = int(self._chem.GetFormalCharge(molecule))
        if actual_charge != charge:
            raise IdentityNormalizationError(
                "invalid_identity", "molecular charge does not match input"
            )

        fragments = self._chem.GetMolFrags(molecule, asMols=False, sanitizeFrags=False)
        fragment_count = len(fragments)
        radical_electrons = sum(atom.GetNumRadicalElectrons() for atom in molecule.GetAtoms())
        isotope_labels = tuple(
            sorted(atom.GetIsotope() for atom in molecule.GetAtoms() if atom.GetIsotope())
        )
        potential_stereo = tuple(self._chem.FindPotentialStereo(molecule))
        unspecified_stereo = any(item.specified.name == "Unspecified" for item in potential_stereo)
        specified_stereo = bool(potential_stereo) and not unspecified_stereo
        stereo_status = (
            "unspecified"
            if unspecified_stereo
            else ("defined" if specified_stereo else "not_applicable")
        )
        non_h_atoms = sum(atom.GetAtomicNum() != 1 for atom in molecule.GetAtoms())
        elements = tuple(sorted({atom.GetSymbol() for atom in molecule.GetAtoms()}))
        unsupported_elements = tuple(
            element for element in elements if element not in self.supported_elements
        )
        atomic_electron_sum = sum(
            atom.GetAtomicNum() for atom in self._chem.AddHs(molecule).GetAtoms()
        )
        electron_count = atomic_electron_sum - charge
        spin_parity_valid = (
            electron_count >= 0
            and (electron_count - (multiplicity - 1)) >= 0
            and ((electron_count - (multiplicity - 1)) % 2 == 0)
        )
        if not spin_parity_valid:
            raise IdentityNormalizationError(
                "invalid_identity", "charge and multiplicity parity is invalid"
            )

        canonical = self._chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
        formula = self._descriptors.CalcMolFormula(molecule)
        rdkit_version = str(self._rd_base.rdkitVersion)
        checks: dict[str, object] = {
            "sanitized": True,
            "query_or_wildcard": False,
            "fragment_count": fragment_count,
            "radical_electron_count": radical_electrons,
            "stereo_status": stereo_status,
            "isotope_labels": list(isotope_labels),
            "elements": list(elements),
            "unsupported_elements": list(unsupported_elements),
            "non_h_atom_count": non_h_atoms,
            "max_non_h_atoms": self.max_non_h_atoms,
            "charge_matches": True,
            "electron_count": electron_count,
            "multiplicity": multiplicity,
            "spin_parity_valid": spin_parity_valid,
            "protocol_range_supported": not unsupported_elements
            and fragment_count == 1
            and radical_electrons == 0
            and not unspecified_stereo
            and non_h_atoms <= self.max_non_h_atoms,
        }
        structure_hash = sha256_hex(
            {
                "canonical_isomeric_smiles": canonical,
                "formal_charge": actual_charge,
                "isotope_labels": list(isotope_labels),
                "normalization_strategy": self.strategy_version,
                "rdkit_version": rdkit_version,
            }
        )
        return NormalizedStructure(
            source_smiles=source,
            canonical_isomeric_smiles=canonical,
            molecular_formula=formula,
            formal_charge=actual_charge,
            fragment_count=fragment_count,
            radical_electron_count=radical_electrons,
            stereo_status=stereo_status,
            isotope_labels=isotope_labels,
            rdkit_version=rdkit_version,
            normalization_strategy=self.strategy_version,
            structure_hash=structure_hash,
            checks=checks,
        )


__all__ = ["IdentityNormalizationError", "NormalizedStructure", "RDKitNormalizer"]
