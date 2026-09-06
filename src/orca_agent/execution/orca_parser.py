"""Pure parser for archived ORCA output and Hessian bytes."""

from __future__ import annotations

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
    if re.search(r"SCF\s+(?:NOT\s+)?CONVERGED|SCF\s+CONVERGENCE\s+FAILED", text, re.I):
        if re.search(r"SCF\s+(?:NOT\s+CONVERGED|CONVERGENCE\s+FAILED)", text, re.I):
            raise ScfNotConverged("ORCA output reports an unconverged SCF")
    scf = bool(re.search(r"SCF\s+CONVERGED|SCF CONVERGENCE", text, re.I))
    if not scf:
        raise RequiredOutputMissing("ORCA SCF convergence marker is missing")
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
        optimized_xyz = _extract_optimized_xyz(text, geometry)
        _validate_geometry_binding(optimized_xyz, geometry)
    frequencies: tuple[float, ...] = ()
    hessian_dimension: int | None = None
    frequency_unit: str | None = None
    if kind is P5NodeKind.FREQ:
        frequencies = _extract_frequencies(text)
        if not frequencies:
            raise RequiredOutputMissing("ORCA vibrational frequencies are missing")
        frequency_unit = "cm^-1"
        if hessian_bytes is None:
            raise RequiredOutputMissing("ORCA Hessian bytes are missing")
        hessian_dimension = _parse_hessian(hessian_bytes, len(geometry.atom_symbols))
    manifest = {
        "output_sha256": bytes_sha256(output_bytes),
        "stderr_sha256": bytes_sha256(stderr_bytes),
        "hessian_sha256": None if hessian_bytes is None else bytes_sha256(hessian_bytes),
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
    match = re.search(
        r"P5\s+OPTIMIZED\s+XYZ\s+BEGIN\s*\n(.*?)\nP5\s+OPTIMIZED\s+XYZ\s+END", text, re.I | re.S
    )
    if match is not None:
        return match.group(1).strip("\n").encode("utf-8") + b"\n"
    match = re.search(
        r"CARTESIAN COORDINATES \(ANGSTROEM\).*?\n[- ]+\n(.*?)(?:\n\s*\n|\n\s*-{3,})",
        text,
        re.I | re.S,
    )
    if match is None:
        raise RequiredOutputMissing("optimized Cartesian coordinates are missing")
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


def _extract_frequencies(text: str) -> tuple[float, ...]:
    marker = re.search(r"VIBRATIONAL FREQUENCIES", text, re.I)
    if marker is None:
        return ()
    tail = text[marker.end() :]
    values: list[float] = []
    for line in tail.splitlines():
        if not line.strip() and values:
            break
        match = re.search(r"(?:^|\s)(?:\d+\s*:\s*|\d+\s+)(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)", line)
        if match:
            values.append(float(match.group(1)))
        elif values and re.search(r"NORMAL|HESSIAN", line, re.I):
            break
    return tuple(values)


def _parse_hessian(value: bytes, atom_count: int) -> int:
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RequiredOutputMissing("Hessian bytes are not valid UTF-8") from error
    if "$hessian" not in text.casefold():
        raise RequiredOutputMissing("Hessian section is missing")
    match = re.search(r"\$hessian\s*\n\s*(\d+)", text, re.I)
    if match is None:
        raise RequiredOutputMissing("Hessian dimension is missing")
    dimension = int(match.group(1))
    if dimension != 3 * atom_count:
        raise GeometryBindingMismatch("Hessian dimension does not match geometry")
    return dimension


__all__ = [
    "ParsedOrcaResult",
    "parse_freq_output",
    "parse_opt_output",
    "parse_orca_output",
    "parse_sp_output",
]
