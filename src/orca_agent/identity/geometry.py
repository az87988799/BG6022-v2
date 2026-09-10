"""Deterministic one-conformer geometry generation for P5."""

from __future__ import annotations

import math
from collections.abc import Mapping

from orca_agent.application.p5_errors import GeometryGenerationFailed, UnsupportedIsotope
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import RunId, WorkflowRecordId, new_id
from orca_agent.domain.p4 import ConfirmedMolecule
from orca_agent.domain.p5 import (
    GeometryDraft,
    GeometryRecord,
    bytes_sha256,
    stable_geometry_hash,
)
from orca_agent.orchestration.p5_versions import P5_GEOMETRY_VERSION

SUPPORTED_ELEMENTS = frozenset({"H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "I"})


def _value(spec: object, name: str, default: object = None) -> object:
    if isinstance(spec, Mapping):
        return spec.get(name, default)
    return getattr(spec, name, default)


def _xyz_bytes(
    atom_symbols: tuple[str, ...],
    coordinates: tuple[tuple[float, float, float], ...],
    precision: int,
) -> bytes:
    lines = [str(len(atom_symbols)), "BG6022 P5 initial geometry"]
    lines.extend(
        f"{symbol} {x:.{precision}f} {y:.{precision}f} {z:.{precision}f}"
        for symbol, (x, y, z) in zip(atom_symbols, coordinates, strict=True)
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def generate_geometry_draft(
    validated_structure_spec: object,
    generation_settings: Mapping[str, object] | None = None,
) -> GeometryDraft:
    """Generate one bounded ETKDGv3 conformer without execution identity."""

    settings = dict(generation_settings or {})
    seed = settings.pop("seed", 6022)
    max_attempts = settings.pop("max_attempts", 20)
    num_threads = settings.pop("num_threads", 1)
    enforce_chirality = settings.pop("enforce_chirality", True)
    xyz_precision = settings.pop("xyz_precision", 8)
    if settings:
        raise ValueError(f"unsupported geometry generation settings: {sorted(settings)}")
    if type(seed) is not int or seed < 0:
        raise ValueError("geometry seed must be a non-negative integer")
    if type(max_attempts) is not int or max_attempts < 1 or max_attempts > 100:
        raise ValueError("geometry max_attempts must be bounded")
    if type(num_threads) is not int or num_threads != 1:
        raise ValueError("geometry generation is fixed to one thread")
    if type(enforce_chirality) is not bool:
        raise ValueError("enforce_chirality must be a boolean")
    if type(xyz_precision) is not int or not 3 <= xyz_precision <= 12:
        raise ValueError("xyz_precision must be between 3 and 12")
    canonical = _value(validated_structure_spec, "canonical_isomeric_smiles")
    formula = _value(validated_structure_spec, "molecular_formula")
    formal_charge = _value(validated_structure_spec, "formal_charge")
    structure_hash = _value(validated_structure_spec, "structure_hash")
    if not all(
        isinstance(item, str) and item.strip() for item in (canonical, formula, structure_hash)
    ):
        raise ValueError("validated structure spec is incomplete")
    if type(formal_charge) is not int:
        raise ValueError("validated structure formal charge is invalid")
    if _value(validated_structure_spec, "isotope_labels", ()):
        raise UnsupportedIsotope("explicit isotope execution is unsupported in P5")
    try:
        from rdkit import Chem, rdBase
        from rdkit.Chem import AllChem, rdDistGeom
    except ModuleNotFoundError as error:
        raise GeometryGenerationFailed("P5 geometry generation requires the p5 extra") from error
    try:
        molecule = Chem.MolFromSmiles(canonical, sanitize=True)
        if molecule is None:
            raise ValueError("validated SMILES could not be parsed")
        if any(atom.GetIsotope() for atom in molecule.GetAtoms()):
            raise UnsupportedIsotope("explicit isotope execution is unsupported in P5")
        molecule = Chem.AddHs(molecule, addCoords=False)
        symbols = tuple(atom.GetSymbol() for atom in molecule.GetAtoms())
        if any(symbol not in SUPPORTED_ELEMENTS for symbol in symbols):
            raise ValueError("validated molecule contains an unsupported element")
        if sum(symbol != "H" for symbol in symbols) > 50:
            raise ValueError("validated molecule exceeds the P5 non-hydrogen atom limit")
        params = rdDistGeom.ETKDGv3()
        params.randomSeed = seed
        params.numThreads = num_threads
        params.enforceChirality = enforce_chirality
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
        return GeometryDraft.create(
            canonical_isomeric_smiles=canonical,
            molecular_formula=formula,
            formal_charge=formal_charge,
            multiplicity=int(_value(validated_structure_spec, "multiplicity", 1)),
            structure_hash=structure_hash,
            atom_symbols=symbols,
            atom_map=atom_map,
            coordinates=coordinates,
            xyz_precision=xyz_precision,
            xyz_bytes=_xyz_bytes(symbols, coordinates, xyz_precision),
            rdkit_version=str(rdBase.rdkitVersion),
            seed=seed,
            max_attempts=max_attempts,
            num_threads=num_threads,
            enforce_chirality=enforce_chirality,
            geometry_hash=geometry_hash,
        )
    except (UnsupportedIsotope, GeometryGenerationFailed):
        raise
    except Exception as error:
        raise GeometryGenerationFailed("ETKDGv3 geometry generation failed") from error


def bind_initial_geometry(
    geometry_draft: GeometryDraft,
    confirmed_molecule: ConfirmedMolecule,
    *,
    run_id: RunId,
    record_id: WorkflowRecordId | None = None,
) -> GeometryRecord:
    """Bind existing coordinates to P4 identity without another Embed call."""

    if not all(
        (
            geometry_draft.canonical_isomeric_smiles
            == confirmed_molecule.canonical_isomeric_smiles,
            geometry_draft.molecular_formula == confirmed_molecule.molecular_formula,
            geometry_draft.formal_charge == confirmed_molecule.formal_charge,
            geometry_draft.multiplicity == confirmed_molecule.multiplicity,
            geometry_draft.structure_hash == confirmed_molecule.structure_hash,
        )
    ):
        raise GeometryGenerationFailed("geometry draft does not match confirmed molecule")
    values = {
        "record_id": str(record_id or new_id(WorkflowRecordId)),
        "run_id": str(run_id),
        "confirmed_molecule_id": str(confirmed_molecule.record_id),
        "identity_hash": confirmed_molecule.identity_record_hash,
        "canonical_isomeric_smiles": geometry_draft.canonical_isomeric_smiles,
        "molecular_formula": geometry_draft.molecular_formula,
        "formal_charge": geometry_draft.formal_charge,
        "multiplicity": geometry_draft.multiplicity,
        "atom_symbols": geometry_draft.atom_symbols,
        "atom_map": geometry_draft.atom_map,
        "coordinates": geometry_draft.coordinates,
        "xyz_precision": geometry_draft.xyz_precision,
        "rdkit_version": geometry_draft.rdkit_version,
        "algorithm_version": P5_GEOMETRY_VERSION,
        "seed": geometry_draft.seed,
        "geometry_hash": geometry_draft.geometry_hash,
        "xyz_bytes_sha256": geometry_draft.xyz_bytes_sha256,
    }
    hash_values = {
        **values,
        "atom_symbols": list(geometry_draft.atom_symbols),
        "atom_map": list(geometry_draft.atom_map),
        "coordinates": [list(point) for point in geometry_draft.coordinates],
    }
    values["record_hash"] = sha256_hex(hash_values)
    bound = GeometryRecord(**values)
    if bound.xyz_bytes() != geometry_draft.xyz_bytes:
        raise GeometryGenerationFailed("bound geometry changed the frozen XYZ bytes")
    return bound


def generate_initial_geometry(
    confirmed: ConfirmedMolecule,
    *,
    run_id: RunId,
    record_id: WorkflowRecordId | None = None,
    seed: int = 6022,
    max_attempts: int = 20,
) -> GeometryRecord:
    """Historical wrapper: generate a draft, then bind it to P4 identity."""

    draft = generate_geometry_draft(
        confirmed,
        {
            "seed": seed,
            "max_attempts": max_attempts,
            "num_threads": 1,
            "enforce_chirality": True,
            "xyz_precision": 8,
        },
    )
    return bind_initial_geometry(draft, confirmed, run_id=run_id, record_id=record_id)


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
    "bind_initial_geometry",
    "generate_geometry_draft",
    "generate_initial_geometry",
    "parse_xyz_bytes",
    "validate_xyz_bytes",
]
