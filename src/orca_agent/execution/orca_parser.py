"""Pure parser for archived ORCA output and Hessian bytes."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from orca_agent.application.p5_errors import (
    GeometryBindingMismatch,
    OptimizationNotConverged,
    OrcaNonzeroExit,
    OutputTruncated,
    RequiredOutputMissing,
    ScfNotConverged,
)
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.p5 import (
    GeometryRecord,
    P5DataOrigin,
    P5ExecutionNode,
    P5NodeKind,
    P5ParseStatus,
    bytes_sha256,
)
from orca_agent.identity.geometry import parse_xyz_bytes
from orca_agent.orchestration.p5_versions import P5_PARSER_VERSION

from .output_contract import BOHR_TO_ANGSTROM

# Frozen default ORCA masses (amu); isotope substitution is outside P5.
ATOMIC_MASSES = {
    "H": 1.008,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "F": 18.998403,
    "P": 30.973762,
    "S": 32.06,
    "Cl": 35.45,
    "Br": 79.904,
    "I": 126.90447,
}


class ParsedOrcaResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    parser_version: str = P5_PARSER_VERSION
    data_origin: P5DataOrigin
    orca_version: str | None
    primitive: P5NodeKind
    input_manifest_hash: str
    output_manifest_hash: str
    normal_termination: bool
    scf_converged: bool
    optimization_converged: bool | None
    exit_code: int
    energy: float
    energy_unit: Literal["Eh"] = "Eh"
    energy_token: str
    frequencies: tuple[float, ...] = ()
    frequency_unit: str | None = None
    hessian_dimension: int | None = None
    optimized_xyz_bytes: bytes | None = None
    hessian_bytes: bytes | None = None
    diagnostics: tuple[str, ...] = ()
    source_locations: dict[str, object]
    parse_status: P5ParseStatus = P5ParseStatus.COMPLETE

    @field_validator("energy")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("energy must be numeric")
        value = float(value)
        if not (-float("inf") < value < float("inf")):
            raise ValueError("energy must be finite")
        return value


def parse_orca_output(
    output_bytes: bytes,
    *,
    primitive: P5ExecutionNode | P5NodeKind | str,
    geometry: GeometryRecord,
    input_manifest_hash: str,
    exit_code: int,
    hessian_bytes: bytes | None = None,
    optimized_xyz_bytes: bytes | None = None,
    data_origin: P5DataOrigin = P5DataOrigin.ORCA_LOCAL,
    stderr_bytes: bytes = b"",
) -> ParsedOrcaResult:
    """Parse one complete output; console summaries are not accepted as input."""

    if not isinstance(output_bytes, bytes) or not output_bytes:
        raise OutputTruncated("ORCA output is empty")
    if type(exit_code) is not int:
        raise ValueError("ORCA exit code must be an integer")
    kind = _kind(primitive)
    try:
        text = output_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise OutputTruncated("ORCA output is not valid UTF-8") from error
    if exit_code != 0:
        raise OrcaNonzeroExit("ORCA exited with a non-zero code")
    normal_matches = list(re.finditer(r"\*+ORCA TERMINATED NORMALLY\*+", text, re.I))
    if len(normal_matches) != 1:
        raise OutputTruncated("ORCA output has no unique normal-termination marker")
    tail = text[normal_matches[0].end() :]
    if any(
        line.strip()
        and not re.fullmatch(
            r"TOTAL RUN TIME:\s*\d+ days \d+ hours \d+ minutes \d+ seconds \d+ msec",
            line.strip(),
            re.I,
        )
        for line in tail.splitlines()
    ):
        raise OutputTruncated("unexpected calculation content after normal termination")
    text = text[: normal_matches[0].start()]
    if len(re.findall(r"Program Version\s+", text, re.I)) > 1 or re.search(
        r"ORCA TERMINATED|ORCA finished by error termination|"
        r"SCF\s+(?:NOT\s+CONVERGED|CONVERGENCE\s+FAILED)",
        text,
        re.I,
    ):
        raise ScfNotConverged("conflicting termination or unconverged calculation")
    energy_matches = list(
        re.finditer(
            r"(?:FINAL SINGLE POINT ENERGY|FINAL ENERGY)\s+(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)", text
        )
    )
    if not energy_matches:
        raise RequiredOutputMissing("ORCA final electronic energy is missing")
    if len(energy_matches) > 1 and kind is P5NodeKind.SP:
        raise OutputTruncated("SP output contains multiple ambiguous final energies")
    energy_match = energy_matches[-1]
    # Every final energy must have its own preceding explicit SCF success.
    # A chapter heading or success in an earlier Opt cycle is not sufficient.
    start = 0
    for match in energy_matches:
        if not re.search(r"\bSCF\s+CONVERGED\b", text[start : match.start()], re.I):
            raise RequiredOutputMissing("final calculation lacks explicit SCF convergence")
        start = match.end()
    if re.search(r"\bSCF\s+CONVERGED\b", text[start:], re.I):
        raise OutputTruncated("SCF calculation after final energy has no bound result")
    energy_token = energy_match.group(1)
    energy = float(energy_token)
    version_match = re.search(
        r"(?:Program Version|ORCA Version|Version)\s*[:=]?\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    orca_version = None if version_match is None else version_match.group(1)
    optimized_xyz: bytes | None = None
    optimization_converged: bool | None = None
    if kind is P5NodeKind.OPT:
        optimization_converged = bool(
            re.search(
                r"(?:THE\s+)?OPTIMIZATION\s+HAS\s+CONVERGED|OPTIMIZATION\s+CONVERGED", text, re.I
            )
        )
        if not optimization_converged:
            raise OptimizationNotConverged("ORCA optimization convergence marker is missing")
        if optimized_xyz_bytes is None:
            raise RequiredOutputMissing("final input.xyz artifact is missing")
        optimized_xyz = optimized_xyz_bytes
        _validate_geometry_binding(optimized_xyz, geometry)
        # Convergence is necessary but not sufficient: bind the actual XYZ file
        # to the single final coordinate section after convergence.
        final_stdout = _extract_optimized_xyz(text, geometry)
        _, actual = parse_xyz_bytes(optimized_xyz)
        _, expected = parse_xyz_bytes(final_stdout)
        if any(
            abs(a - b) > 2e-6
            for p, q in zip(actual, expected, strict=True)
            for a, b in zip(p, q, strict=True)
        ):
            raise GeometryBindingMismatch("final XYZ differs from converged stdout coordinates")
    frequencies: tuple[float, ...] = ()
    hessian_dimension: int | None = None
    frequency_unit: str | None = None
    if kind is P5NodeKind.FREQ:
        frequencies = _extract_frequencies(text, 3 * len(geometry.atom_symbols))
        if not frequencies:
            raise RequiredOutputMissing("ORCA vibrational frequencies are missing")
        frequency_unit = "cm^-1"
        if hessian_bytes is None:
            raise RequiredOutputMissing("ORCA Hessian bytes are missing")
        hessian_dimension = _parse_hessian(hessian_bytes, geometry, frequencies)
    manifest = {
        "output_sha256": bytes_sha256(output_bytes),
        "stderr_sha256": bytes_sha256(stderr_bytes),
        "hessian_sha256": None if hessian_bytes is None else bytes_sha256(hessian_bytes),
        "optimized_xyz_sha256": None if optimized_xyz is None else bytes_sha256(optimized_xyz),
        "normal_termination_offset": normal_matches[0].start(),
        "energy_offset": energy_match.start(),
    }
    return ParsedOrcaResult(
        data_origin=data_origin,
        orca_version=orca_version,
        primitive=kind,
        input_manifest_hash=input_manifest_hash,
        output_manifest_hash=sha256_hex(manifest),
        normal_termination=True,
        scf_converged=True,
        optimization_converged=optimization_converged,
        exit_code=exit_code,
        energy=energy,
        energy_token=energy_token,
        frequencies=frequencies,
        frequency_unit=frequency_unit,
        hessian_dimension=hessian_dimension,
        optimized_xyz_bytes=optimized_xyz,
        hessian_bytes=hessian_bytes,
        source_locations={
            "normal_termination": normal_matches[0].start(),
            "energy": energy_match.start(),
        },
    )


def parse_sp_output(*args, **kwargs) -> ParsedOrcaResult:
    return parse_orca_output(*args, **kwargs)


def parse_opt_output(*args, **kwargs) -> ParsedOrcaResult:
    return parse_orca_output(*args, **kwargs)


def parse_freq_output(*args, **kwargs) -> ParsedOrcaResult:
    return parse_orca_output(*args, **kwargs)


def _kind(value: P5ExecutionNode | P5NodeKind | str) -> P5NodeKind:
    raw = value.kind if isinstance(value, P5ExecutionNode) else value
    try:
        return raw if isinstance(raw, P5NodeKind) else P5NodeKind(str(raw))
    except ValueError as error:
        raise ValueError("P5 primitive kind is invalid") from error


def _extract_optimized_xyz(text: str, geometry: GeometryRecord) -> bytes:
    converged = list(re.finditer(r"(?:THE\s+)?OPTIMIZATION\s+(?:HAS\s+)?CONVERGED", text, re.I))
    if len(converged) != 1:
        raise OptimizationNotConverged("a unique optimization convergence section is required")
    tail = text[converged[0].end() :]
    tail = tail.split("ORCA TERMINATED NORMALLY")[0]
    matches = list(
        re.finditer(
            r"CARTESIAN COORDINATES \(ANGSTROEM\).*?\r?\n[- ]+\r?\n"
            r"(.*?)(?:\r?\n\s*\r?\n|\r?\n\s*-{3,})",
            tail,
            re.I | re.S,
        )
    )
    if len(matches) != 1:
        raise RequiredOutputMissing("unique final converged Cartesian coordinates are missing")
    match = matches[0]
    rows = []
    for line in match.group(1).splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in geometry.atom_symbols:
            rows.append(" ".join(parts[:4]))
    if len(rows) != len(geometry.atom_symbols):
        raise GeometryBindingMismatch("optimized coordinates do not contain the expected atoms")
    return (f"{len(rows)}\nBG6022 P5 optimized geometry\n" + "\n".join(rows) + "\n").encode("utf-8")


def _validate_geometry_binding(value: bytes, geometry: GeometryRecord) -> None:
    symbols, coordinates = parse_xyz_bytes(value)
    if symbols != geometry.atom_symbols or len(coordinates) != len(geometry.coordinates):
        raise GeometryBindingMismatch("optimized geometry atom order does not match input")
    if any(abs(coordinate) > 1.0e5 for point in coordinates for coordinate in point):
        raise GeometryBindingMismatch("optimized geometry contains implausible coordinates")


def _extract_frequencies(text: str, dimension: int) -> tuple[float, ...]:
    marker = re.search(r"VIBRATIONAL FREQUENCIES", text, re.I)
    if marker is None:
        return ()
    tail = text[marker.end() :]
    tail = re.split(r"NORMAL MODES|THERMOCHEMISTRY|ORCA TERMINATED", tail, flags=re.I)[0]
    matches = re.findall(r"^\s*(\d+)\s*:\s*([^\s]+)\s+cm\*\*-1", tail, re.M)
    if [int(i) for i, _ in matches] != list(range(dimension)):
        raise RequiredOutputMissing("complete indexed raw 3N frequency table is required")
    return tuple(_finite_number(value) for _, value in matches)


def _finite_number(token: str) -> float:
    try:
        number = float(token.replace("D", "E").replace("d", "e"))
    except ValueError as error:
        raise RequiredOutputMissing("invalid Hessian/frequency numeric token") from error
    if not math.isfinite(number):
        raise RequiredOutputMissing("nonfinite Hessian/frequency value")
    return number


def _parse_hessian(value: bytes, geometry: GeometryRecord, frequencies: tuple[float, ...]) -> int:
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RequiredOutputMissing("Hessian bytes are not valid UTF-8") from error
    sections = {}
    for match in re.finditer(r"^\s*\$([a-z_]+)[ \t]*\r?\n([^$]*)", text, re.I | re.M):
        name = match.group(1).lower()
        if name in sections:
            raise RequiredOutputMissing("duplicate Hessian section")
        sections[name] = [line.split() for line in match.group(2).splitlines() if line.strip()]
    dimension = 3 * len(geometry.atom_symbols)
    try:
        matrix = sections["hessian"]
        if matrix[0] != [str(dimension)]:
            raise GeometryBindingMismatch("Hessian dimension does not match geometry")
        entries = {}
        columns = []
        for row in matrix[1:]:
            if all(re.fullmatch(r"\d+", token) for token in row):
                columns = [int(token) for token in row]
                if not columns or len(set(columns)) != len(columns):
                    raise ValueError("invalid matrix columns")
                continue
            if len(row) != len(columns) + 1 or not columns:
                raise ValueError("truncated Hessian matrix row")
            index = int(row[0])
            for column, token in zip(columns, row[1:], strict=True):
                key = (index, column)
                if key in entries or not 0 <= index < dimension or not 0 <= column < dimension:
                    raise ValueError("duplicate or invalid Hessian matrix cell")
                entries[key] = _finite_number(token)
        if len(entries) != dimension * dimension:
            raise ValueError("incomplete Hessian matrix")
        if any(abs(v - entries[j, i]) > 1e-7 for (i, j), v in entries.items()):
            raise ValueError("Hessian matrix is not symmetric")
        atoms = sections["atoms"]
        if (
            atoms[0] != [str(len(geometry.atom_symbols))]
            or len(atoms) != len(geometry.atom_symbols) + 1
        ):
            raise ValueError("incomplete Hessian atom section")
        for row, symbol, xyz in zip(
            atoms[1:], geometry.atom_symbols, geometry.coordinates, strict=True
        ):
            if len(row) != 5 or row[0] != symbol:
                raise GeometryBindingMismatch("Hessian atom order differs from input")
            if abs(_finite_number(row[1]) - ATOMIC_MASSES[symbol]) > 0.02:
                raise GeometryBindingMismatch("Hessian atomic mass differs from approved default")
            if any(
                abs(_finite_number(v) * BOHR_TO_ANGSTROM - c) > 2e-6
                for v, c in zip(row[2:], xyz, strict=True)
            ):
                raise GeometryBindingMismatch(
                    "Hessian Bohr coordinates differ from input Angstrom geometry"
                )
        modes = sections["vibrational_frequencies"]
        if modes[0] != [str(dimension)] or len(modes) != dimension + 1:
            raise ValueError("incomplete Hessian frequencies")
        for i, (row, expected) in enumerate(zip(modes[1:], frequencies, strict=True)):
            if len(row) != 2 or int(row[0]) != i or abs(_finite_number(row[1]) - expected) > 0.02:
                raise ValueError("Hessian frequencies differ from raw stdout modes")
    except (KeyError, IndexError, ValueError) as error:
        raise RequiredOutputMissing(f"invalid or incomplete Hessian: {error}") from error
    return dimension


__all__ = [
    "ParsedOrcaResult",
    "parse_freq_output",
    "parse_opt_output",
    "parse_orca_output",
    "parse_sp_output",
]
