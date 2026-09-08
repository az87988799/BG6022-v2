"""Bounded read-only parsers for P6's additional ORCA observations.

P5 parser v4 remains the authority for the P5 result contract.  This module
only extracts the normal-mode matrix and thermochemistry context from the
already verified bytes, while retaining source character spans and raw number
tokens.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"


@dataclass(frozen=True)
class ThermochemistryObservation:
    temperature_K: float | None
    pressure_atm: float | None
    quasi_rrho: bool | None
    cutoff_frequency_cm1: float | None
    qrrho_reference_frequency_cm1: float | None
    standard_state: str | None
    symmetry_number: int | None
    spans: dict[str, tuple[int, int]]
    missing_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ParsedP6Observations:
    hessian_dimension: int
    frequencies: tuple[float, ...]
    frequency_tokens: tuple[str, ...]
    frequency_spans: tuple[tuple[int, int], ...]
    normal_modes: tuple[tuple[float, ...], ...]
    normal_modes_span: tuple[int, int]
    scale_factor: float | None
    scale_factor_span: tuple[int, int] | None
    stdout_frequency_tokens: tuple[str, ...]
    stdout_frequency_spans: tuple[tuple[int, int], ...]
    stdout_scale_factor: float | None
    thermochemistry: ThermochemistryObservation


def parse_hessian_observations(
    hessian_bytes: bytes, *, allow_missing_modes: bool = False
) -> ParsedP6Observations:
    text = _decode(hessian_bytes, "Hessian")
    sections = _sections(text)
    mode_body, mode_start, mode_end = sections.get("normal_modes", (None, None, None))
    freq_body, freq_start, _ = sections.get("vibrational_frequencies", (None, None, None))
    if (mode_body is None and not allow_missing_modes) or freq_body is None:
        raise ValueError("Hessian is missing normal_modes or vibrational_frequencies")
    dimension, frequencies, tokens, spans = _parse_frequency_section(
        text, freq_body, freq_start or 0
    )
    normal_modes = () if mode_body is None else _parse_normal_modes(mode_body, dimension)
    scale_factor, scale_span = _parse_scalar_section(
        text, sections.get("frequency_scale_factor"), "frequency scale factor"
    )
    return ParsedP6Observations(
        hessian_dimension=dimension,
        frequencies=frequencies,
        frequency_tokens=tokens,
        frequency_spans=spans,
        normal_modes=normal_modes,
        normal_modes_span=(mode_start or 0, mode_end or 0),
        scale_factor=scale_factor,
        scale_factor_span=scale_span,
        stdout_frequency_tokens=(),
        stdout_frequency_spans=(),
        stdout_scale_factor=None,
        thermochemistry=ThermochemistryObservation(
            None, None, None, None, None, None, None, {}, ()
        ),
    )


def parse_stdout_observations(
    stdout_bytes: bytes, base: ParsedP6Observations | None = None
) -> ParsedP6Observations:
    text = _decode(stdout_bytes, "ORCA stdout")
    observations = base or ParsedP6Observations(
        hessian_dimension=0,
        frequencies=(),
        frequency_tokens=(),
        frequency_spans=(),
        normal_modes=(),
        normal_modes_span=(0, 0),
        scale_factor=None,
        scale_factor_span=None,
        stdout_frequency_tokens=(),
        stdout_frequency_spans=(),
        stdout_scale_factor=None,
        thermochemistry=ThermochemistryObservation(
            None, None, None, None, None, None, None, {}, ()
        ),
    )
    stdout_tokens, stdout_spans, stdout_scale = _parse_stdout_frequencies(text)
    thermo = _parse_thermochemistry(text)
    return ParsedP6Observations(
        hessian_dimension=observations.hessian_dimension,
        frequencies=observations.frequencies,
        frequency_tokens=observations.frequency_tokens,
        frequency_spans=observations.frequency_spans,
        normal_modes=observations.normal_modes,
        normal_modes_span=observations.normal_modes_span,
        scale_factor=observations.scale_factor,
        scale_factor_span=observations.scale_factor_span,
        stdout_frequency_tokens=stdout_tokens,
        stdout_frequency_spans=stdout_spans,
        stdout_scale_factor=stdout_scale,
        thermochemistry=thermo,
    )


def _decode(value: bytes, label: str) -> str:
    if not isinstance(value, bytes) or not value:
        raise ValueError(f"{label} bytes are empty")
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} bytes are not UTF-8") from error


def _sections(text: str) -> dict[str, tuple[str, int, int]]:
    matches = list(
        re.finditer(
            r"(?im)^\s*\$([a-z_]+)[ \t]*\r?\n(.*?)(?=^\s*\$[a-z_]+[ \t]*\r?$|\Z)",
            text,
            re.M | re.S,
        )
    )
    values: dict[str, tuple[str, int, int]] = {}
    for match in matches:
        name = match.group(1).casefold()
        if name in values:
            raise ValueError(f"duplicate Hessian section: {name}")
        values[name] = (match.group(2), match.start(2), match.end(2))
    return values


def _parse_frequency_section(
    full_text: str, body: str, body_offset: int
) -> tuple[int, tuple[float, ...], tuple[str, ...], tuple[tuple[int, int], ...]]:
    lines = [(match, match.group(0)) for match in re.finditer(r"[^\r\n]+", body)]
    meaningful = [(match, line.strip()) for match, line in lines if line.strip()]
    if not meaningful:
        raise ValueError("frequency section is empty")
    header = meaningful[0][1].split()
    if len(header) != 1 or not header[0].isdigit():
        raise ValueError("frequency section dimension is invalid")
    dimension = int(header[0])
    if dimension < 1 or dimension > 100_000:
        raise ValueError("frequency section dimension is out of bounds")
    entries: dict[int, tuple[float, str, tuple[int, int]]] = {}
    for match, line in meaningful[1:]:
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError("frequency section row is invalid")
        index = int(parts[0])
        token = parts[1]
        value = _number(token)
        token_start = body_offset + match.start(0) + match.group(0).find(token)
        if index in entries or not 0 <= index < dimension:
            raise ValueError("frequency index is duplicated or out of range")
        entries[index] = (value, token, (token_start, token_start + len(token)))
    if tuple(sorted(entries)) != tuple(range(dimension)):
        raise ValueError("frequency section indices are incomplete")
    values = tuple(entries[index][0] for index in range(dimension))
    tokens = tuple(entries[index][1] for index in range(dimension))
    spans = tuple(entries[index][2] for index in range(dimension))
    return dimension, values, tokens, spans


def _parse_normal_modes(body: str, dimension: int) -> tuple[tuple[float, ...], ...]:
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise ValueError("normal_modes section is empty")
    header = lines[0].split()
    if (
        len(header) != 2
        or any(not item.isdigit() for item in header)
        or tuple(map(int, header)) != (dimension, dimension)
    ):
        raise ValueError("normal_modes dimension is invalid")
    matrix: list[list[float | None]] = [[None] * dimension for _ in range(dimension)]
    columns: list[int] = []
    for line in lines[1:]:
        parts = line.split()
        if parts and all(item.isdigit() for item in parts):
            columns = [int(item) for item in parts]
            if (
                not columns
                or len(set(columns)) != len(columns)
                or any(item >= dimension for item in columns)
            ):
                raise ValueError("normal_modes column header is invalid")
            continue
        if not columns or len(parts) != len(columns) + 1 or not parts[0].isdigit():
            raise ValueError("normal_modes row is invalid")
        row_index = int(parts[0])
        if not 0 <= row_index < dimension:
            raise ValueError("normal_modes row index is out of range")
        for column, token in zip(columns, parts[1:], strict=True):
            if matrix[row_index][column] is not None:
                raise ValueError("normal_modes cell is duplicated")
            matrix[row_index][column] = _number(token)
    if any(item is None for row in matrix for item in row):
        raise ValueError("normal_modes matrix is incomplete")
    return tuple(tuple(float(item) for item in row) for row in matrix)  # type: ignore[arg-type]


def _parse_scalar_section(
    full_text: str, section: tuple[str, int, int] | None, label: str
) -> tuple[float | None, tuple[int, int] | None]:
    if section is None:
        return None, None
    body, offset, _ = section
    match = re.search(_NUMBER, body)
    if match is None:
        raise ValueError(f"{label} is invalid")
    token = match.group(0)
    return _number(token), (offset + match.start(), offset + match.end())


def _parse_stdout_frequencies(
    text: str,
) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...], float | None]:
    marker = re.search(r"VIBRATIONAL FREQUENCIES", text, re.I)
    if marker is None:
        return (), (), None
    end_match = re.search(
        r"NORMAL MODES|THERMOCHEMISTRY|ORCA TERMINATED", text[marker.end() :], re.I
    )
    end = marker.end() + (end_match.start() if end_match else len(text))
    body = text[marker.end() : end]
    matches = list(re.finditer(r"^\s*(\d+)\s*:\s*(" + _NUMBER + r")\s+cm\*\*-1", body, re.M | re.I))
    if not matches:
        return (), (), None
    entries: dict[int, tuple[str, tuple[int, int]]] = {}
    for match in matches:
        index = int(match.group(1))
        token = match.group(2)
        if index in entries:
            raise ValueError("stdout frequency index is duplicated")
        start = marker.end() + match.start(2)
        entries[index] = (token, (start, start + len(token)))
    expected = tuple(range(max(entries) + 1))
    if tuple(sorted(entries)) != expected:
        raise ValueError("stdout frequency indices are incomplete")
    scale_match = re.search(r"Scaling factor for frequencies\s*=\s*(" + _NUMBER + r")", body, re.I)
    scale = None if scale_match is None else _number(scale_match.group(1))
    return (
        tuple(entries[index][0] for index in expected),
        tuple(entries[index][1] for index in expected),
        scale,
    )


def _parse_thermochemistry(text: str) -> ThermochemistryObservation:
    blocks = list(
        re.finditer(
            r"THERMOCHEMISTRY AT\s+"
            + _NUMBER
            + r"K(?P<body>.*?)(?=\bPoint Group:|\bORCA TERMINATED|\Z)",
            text,
            re.I | re.S,
        )
    )
    if len(blocks) != 1:
        return ThermochemistryObservation(
            None, None, None, None, None, None, None, {}, ("thermochemistry_block_not_unique",)
        )
    block = blocks[0]
    body = block.group(0)
    spans: dict[str, tuple[int, int]] = {}

    def value(pattern: str, name: str) -> float | None:
        match = re.search(pattern, body, re.I)
        if match is None:
            return None
        token = match.group(1)
        start = block.start() + match.start(1)
        spans[name] = (start, start + len(token))
        return _number(token)

    temperature = value(r"Temperature\s+\.\.\.\s+(" + _NUMBER + r")\s*K", "temperature_K")
    pressure = value(r"Pressure\s+\.\.\.\s+(" + _NUMBER + r")\s*atm", "pressure_atm")
    cutoff = value(
        r"Cut-Off Frequency\s+\.\.\.\s+(" + _NUMBER + r")\s*cm\^-1", "cutoff_frequency_cm1"
    )
    rrho_match = re.search(r"Quasi RRHO\s+\.\.\.\s+(True|False)", body, re.I)
    rrho = None if rrho_match is None else rrho_match.group(1).casefold() == "true"
    if rrho_match:
        start = block.start() + rrho_match.start(1)
        spans["quasi_rrho"] = (start, start + len(rrho_match.group(1)))
    reference = None
    reference_match = re.search(r"reference frequency of\s+(" + _NUMBER + r")\s*cm-1", text, re.I)
    if reference_match:
        reference = _number(reference_match.group(1))
        spans["qrrho_reference_frequency_cm1"] = (reference_match.start(1), reference_match.end(1))
    symmetry = None
    symmetry_match = re.search(r"Symmetry Number:\s*(\d+)", text, re.I)
    if symmetry_match:
        symmetry = int(symmetry_match.group(1))
        spans["symmetry_number"] = (symmetry_match.start(1), symmetry_match.end(1))
    missing = tuple(
        name
        for name, item in (
            ("temperature_K", temperature),
            ("pressure_atm", pressure),
            ("quasi_rrho", rrho),
            ("cutoff_frequency_cm1", cutoff),
            ("qrrho_reference_frequency_cm1", reference),
            ("symmetry_number", symmetry),
        )
        if item is None
    )
    return ThermochemistryObservation(
        temperature,
        pressure,
        rrho,
        cutoff,
        reference,
        None,
        symmetry,
        spans,
        missing,
    )


def _number(token: str) -> float:
    try:
        value = float(token.replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid numeric token") from error
    if not math.isfinite(value):
        raise ValueError("numeric token is non-finite")
    return value


__all__ = [
    "ParsedP6Observations",
    "ThermochemistryObservation",
    "parse_hessian_observations",
    "parse_stdout_observations",
]
