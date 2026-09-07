"""Deterministic classification of ORCA's archived normal-mode layout."""

from __future__ import annotations

import math
from dataclasses import dataclass

from orca_agent.domain.p5 import GeometryRecord
from orca_agent.domain.p6 import ModeClassification, P6ModeKind, ScientificPolicy

_ATOMIC_MASSES = {
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


@dataclass(frozen=True)
class ModeAnalysis:
    classifications: tuple[ModeClassification, ...]
    real_frequencies: tuple[float, ...]
    layout_supported: bool
    reason: str | None
    principal_inertias: tuple[float, ...] = ()


def classify_modes(
    geometry: GeometryRecord,
    frequencies: tuple[float, ...],
    frequency_tokens: tuple[str, ...],
    normal_modes: tuple[tuple[float, ...], ...],
    policy: ScientificPolicy,
) -> ModeAnalysis:
    """Classify all 3N modes without dropping or reordering any observation."""

    dimension = 3 * len(geometry.atom_symbols)
    basic_reason: str | None = None
    if len(frequencies) != dimension or len(frequency_tokens) != dimension:
        basic_reason = "frequency_layout_incomplete"
    if len(normal_modes) != dimension or any(len(row) != dimension for row in normal_modes):
        basic_reason = "normal_mode_matrix_incomplete"
    if any(not math.isfinite(value) for row in normal_modes for value in row):
        basic_reason = "normal_mode_matrix_nonfinite"
    inertias: tuple[float, ...] = ()
    if basic_reason is None:
        try:
            inertias = _principal_inertias(geometry)
        except ValueError as error:
            basic_reason = str(error)
    if basic_reason is None:
        if len(geometry.atom_symbols) < policy.nonlinear_min_atoms:
            basic_reason = "molecule_has_fewer_than_three_atoms"
        elif inertias[-1] <= policy.inertia_floor_amu_angstrom2:
            basic_reason = "principal_inertia_is_too_small"
        elif inertias[0] / inertias[-1] <= policy.nonlinear_inertia_ratio_floor:
            basic_reason = "molecule_is_linear_or_near_linear"

    if basic_reason is not None:
        return ModeAnalysis(
            classifications=tuple(
                _classification(
                    index,
                    frequencies,
                    frequency_tokens,
                    normal_modes,
                    P6ModeKind.UNCLASSIFIED,
                    basic_reason,
                )
                for index in range(min(dimension, len(frequencies), len(frequency_tokens)))
            ),
            real_frequencies=(),
            layout_supported=False,
            reason=basic_reason,
            principal_inertias=inertias,
        )

    projected_ok = True
    for index in range(min(6, dimension)):
        column_max = _column_max(normal_modes, index)
        if (
            abs(frequencies[index]) > policy.projected_frequency_abs_max_cm1
            or column_max > policy.projected_mode_abs_max
        ):
            projected_ok = False
    if dimension < 6:
        projected_ok = False
        basic_reason = "normal_nonlinear_projection_requires_six_modes"

    classifications: list[ModeClassification] = []
    if not projected_ok:
        reason = "unsupported_mode_layout"
        classifications.extend(
            _classification(
                index,
                frequencies,
                frequency_tokens,
                normal_modes,
                P6ModeKind.UNCLASSIFIED,
                reason,
            )
            for index in range(dimension)
        )
        return ModeAnalysis(tuple(classifications), (), False, reason, inertias)

    for index in range(dimension):
        column_max = _column_max(normal_modes, index)
        if index < 6:
            kind = P6ModeKind.PROJECTED_RIGID
            reason = None
        elif column_max == 0.0:
            kind = P6ModeKind.UNCLASSIFIED
            reason = "vibrational_mode_column_is_zero"
        else:
            kind = P6ModeKind.VIBRATIONAL_CANDIDATE
            reason = None
        classifications.append(
            _classification(
                index,
                frequencies,
                frequency_tokens,
                normal_modes,
                kind,
                reason,
            )
        )
    candidates = tuple(
        item.frequency for item in classifications if item.kind is P6ModeKind.VIBRATIONAL_CANDIDATE
    )
    if any(item.kind is P6ModeKind.UNCLASSIFIED for item in classifications[6:]):
        return ModeAnalysis(
            tuple(classifications), candidates, False, "unsupported_mode_layout", inertias
        )
    return ModeAnalysis(tuple(classifications), candidates, True, None, inertias)


def _classification(
    index: int,
    frequencies: tuple[float, ...],
    frequency_tokens: tuple[str, ...],
    normal_modes: tuple[tuple[float, ...], ...],
    kind: P6ModeKind,
    reason: str | None,
) -> ModeClassification:
    frequency = frequencies[index] if index < len(frequencies) else 0.0
    token = frequency_tokens[index] if index < len(frequency_tokens) else "unavailable"
    column_max = _column_max(normal_modes, index) if index < len(normal_modes) else None
    return ModeClassification(
        mode_index=index,
        frequency=frequency,
        frequency_token=token,
        kind=kind,
        mode_column_max_abs=column_max,
        reason=reason,
    )


def _column_max(matrix: tuple[tuple[float, ...], ...], index: int) -> float:
    if not matrix or any(index >= len(row) for row in matrix):
        return float("inf")
    return max(abs(row[index]) for row in matrix)


def _principal_inertias(geometry: GeometryRecord) -> tuple[float, ...]:
    try:
        masses = tuple(_ATOMIC_MASSES[symbol] for symbol in geometry.atom_symbols)
    except KeyError as error:
        raise ValueError("unknown_atomic_mass") from error
    total = math.fsum(masses)
    if total <= 0 or len(masses) != len(geometry.coordinates):
        raise ValueError("geometry_mass_binding_invalid")
    center = tuple(
        math.fsum(
            mass * point[axis] for mass, point in zip(masses, geometry.coordinates, strict=True)
        )
        / total
        for axis in range(3)
    )
    try:
        import numpy as np
    except ImportError as error:
        raise ValueError("numpy_required_for_inertia") from error
    tensor = np.zeros((3, 3), dtype=float)
    for mass, point in zip(masses, geometry.coordinates, strict=True):
        x, y, z = (point[axis] - center[axis] for axis in range(3))
        tensor += mass * np.array(
            [
                [y * y + z * z, -x * y, -x * z],
                [-x * y, x * x + z * z, -y * z],
                [-x * z, -y * z, x * x + y * y],
            ],
            dtype=float,
        )
    values = tuple(float(item) for item in np.linalg.eigvalsh(tensor))
    if any(not math.isfinite(item) or item < 0 for item in values):
        raise ValueError("principal_inertia_invalid")
    return values


__all__ = ["ModeAnalysis", "classify_modes"]
