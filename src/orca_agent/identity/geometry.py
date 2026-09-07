"""Deterministic one-conformer geometry generation for P5."""

from __future__ import annotations

import math

from orca_agent.application.p5_errors import GeometryGenerationFailed, UnsupportedIsotope
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import RunId, WorkflowRecordId, new_id
from orca_agent.domain.p4 import ConfirmedMolecule
from orca_agent.domain.p5 import (
    GeometryRecord,
    bytes_sha256,
    stable_geometry_hash,
)
from orca_agent.orchestration.p5_versions import P5_GEOMETRY_VERSION

SUPPORTED_ELEMENTS = frozenset({"H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "I"})


def generate_initial_geometry(
    confirmed: ConfirmedMolecule,
    *,
    run_id: RunId,
    record_id: WorkflowRecordId | None = None,
    seed: int = 6022,
    max_attempts: int = 20,
) -> GeometryRecord:
    """Generate exactly one ETKDGv3 conformer and freeze its canonical XYZ."""

    if type(seed) is not int or seed < 0:
        raise ValueError("geometry seed must be a non-negative integer")
    if type(max_attempts) is not int or max_attempts < 1 or max_attempts > 100:
        raise ValueError("geometry max_attempts must be bounded")
    if getattr(confirmed, "isotope_labels", ()):
        raise UnsupportedIsotope("explicit isotope execution is unsupported in P5")
    try:
        from rdkit import Chem, rdBase
        from rdkit.Chem import AllChem, rdDistGeom
    except ModuleNotFoundError as error:
        raise GeometryGenerationFailed("P5 geometry generation requires the p5 extra") from error
    try:
        molecule = Chem.MolFromSmiles(confirmed.canonical_isomeric_smiles, sanitize=True)
        if molecule is None:
            raise ValueError("confirmed SMILES could not be parsed")
        if any(atom.GetIsotope() for atom in molecule.GetAtoms()):
            raise UnsupportedIsotope("explicit isotope execution is unsupported in P5")
        molecule = Chem.AddHs(molecule, addCoords=False)
        symbols = tuple(atom.GetSymbol() for atom in molecule.GetAtoms())
        if any(symbol not in SUPPORTED_ELEMENTS for symbol in symbols):
            raise ValueError("confirmed molecule contains an unsupported element")
        if sum(symbol != "H" for symbol in symbols) > 50:
            raise ValueError("confirmed molecule exceeds the P5 non-hydrogen atom limit")
        params = rdDistGeom.ETKDGv3()
        params.randomSeed = seed
        params.numThreads = 1
        params.enforceChirality = True
        params.maxIterations = max_attempts
        conformer_id = int(AllChem.EmbedMolecule(molecule, params))
        if conformer_id < 0 or molecule.GetNumConformers() != 1:
            raise ValueError("ETKDGv3 could not produce one conformer")
        conformer = molecule.GetConformer(conformer_id)
        coordinates = tuple(
            (
                float(conformer.GetAtomPosition(index).x),
                float(conformer.GetAtomPosition(index).y),
                float(conformer.GetAtomPosition(index).z),
            )
            for index in range(molecule.GetNumAtoms())
        )
        if any(not math.isfinite(item) for point in coordinates for item in point):
            raise ValueError("ETKDGv3 produced non-finite coordinates")
        for left in range(len(coordinates)):
            for right in range(left):
                distance_sq = sum(
                    (coordinates[left][axis] - coordinates[right][axis]) ** 2 for axis in range(3)
                )
                if distance_sq < 1.0e-8:
                    raise ValueError("ETKDGv3 produced overlapping atoms")
        atom_map = tuple(range(1, len(symbols) + 1))
        geometry_hash = stable_geometry_hash(
            atom_symbols=symbols, atom_map=atom_map, coordinates=coordinates
        )
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "run_id": str(run_id),
            "confirmed_molecule_id": str(confirmed.record_id),
            "identity_hash": confirmed.identity_record_hash,
            "canonical_isomeric_smiles": confirmed.canonical_isomeric_smiles,
            "molecular_formula": confirmed.molecular_formula,
            "formal_charge": confirmed.formal_charge,
            "multiplicity": confirmed.multiplicity,
            "atom_symbols": symbols,
            "atom_map": atom_map,
            "coordinates": coordinates,
            "xyz_precision": 8,
            "rdkit_version": str(rdBase.rdkitVersion),
            "algorithm_version": P5_GEOMETRY_VERSION,
            "seed": seed,
            "geometry_hash": geometry_hash,
        }
        provisional = GeometryRecord(
            **values,
            xyz_bytes_sha256="0" * 64,
            record_hash="0" * 64,
        )
        xyz_hash = bytes_sha256(provisional.xyz_bytes())
        values["xyz_bytes_sha256"] = xyz_hash
        hash_values = {
            **values,
            "atom_symbols": list(symbols),
            "atom_map": list(atom_map),
            "coordinates": [list(point) for point in coordinates],
        }
        values["record_hash"] = sha256_hex(hash_values)
        return GeometryRecord(**values)
    except UnsupportedIsotope:
        raise
    except Exception as error:
        raise GeometryGenerationFailed("ETKDGv3 geometry generation failed") from error


def parse_xyz_bytes(value: bytes) -> tuple[tuple[str, ...], tuple[tuple[float, float, float], ...]]:
    if not isinstance(value, bytes):
        raise TypeError("XYZ content must be bytes")
    try:
        lines = value.decode("utf-8").splitlines()
        if len(lines) < 2:
            raise ValueError("XYZ is truncated")
        count = int(lines[0].strip())
        if count < 1 or len(lines) != count + 2:
            raise ValueError("XYZ atom count does not match lines")
        symbols: list[str] = []
        coordinates: list[tuple[float, float, float]] = []
        for line in lines[2:]:
            parts = line.split()
            if len(parts) != 4:
                raise ValueError("XYZ atom line is malformed")
            point = tuple(float(item) for item in parts[1:])
            if any(not math.isfinite(item) for item in point):
                raise ValueError("XYZ coordinate is not finite")
            symbols.append(parts[0])
            coordinates.append(point)  # type: ignore[arg-type]
        return tuple(symbols), tuple(coordinates)
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise ValueError("XYZ bytes are invalid") from error


def validate_xyz_bytes(value: bytes, geometry: GeometryRecord) -> None:
    symbols, coordinates = parse_xyz_bytes(value)
    if symbols != geometry.atom_symbols:
        raise ValueError("XYZ atom symbols do not match the frozen geometry")
    if len(coordinates) != len(geometry.coordinates):
        raise ValueError("XYZ atom count does not match the frozen geometry")
    if bytes_sha256(value) != geometry.xyz_bytes_sha256:
        raise ValueError("XYZ bytes hash does not match the frozen geometry")


__all__ = [
    "SUPPORTED_ELEMENTS",
    "generate_initial_geometry",
    "parse_xyz_bytes",
    "validate_xyz_bytes",
]
