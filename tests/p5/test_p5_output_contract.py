"""B3–B5 regressions with explicitly synthetic ORCA-format data."""

import pytest

from orca_agent.application.p5_errors import (
    GeometryBindingMismatch,
    P5Error,
    RequiredOutputMissing,
)
from orca_agent.domain.p5 import P5NodeKind
from orca_agent.execution.orca_parser import ATOMIC_MASSES, parse_orca_output
from orca_agent.execution.output_contract import BOHR_TO_ANGSTROM
from tests.p5.test_p5_workflow import _approve_and_run, _prepare


@pytest.fixture(scope="module")
def geometry(tmp_path_factory):
    _, _, view = _prepare(tmp_path_factory.mktemp("parser"), "p5.sp_initial.r2scan3c.v1")
    return view.geometry[0]


def _stdout(kind="freq", *, coords="", frequencies=None):
    lines = ["Program Version 6.1.0", "SCF CONVERGED", "FINAL SINGLE POINT ENERGY -75.0"]
    if kind == "opt":
        section = "CARTESIAN COORDINATES (ANGSTROEM)\n----------------------------\n"
        lines = [section + "O 0 0 0\nH 1.20 0 0\nH 0 1.20 0\n\n"] + lines
        lines.extend(["THE OPTIMIZATION HAS CONVERGED", section + coords + "\n\n"])
    if kind == "freq":
        values = [0.0] * 6 + [-10.2, 3657.2, 3755.1] if frequencies is None else frequencies
        lines.extend(
            ["VIBRATIONAL FREQUENCIES"] + [f"{i}: {v:.6f} cm**-1" for i, v in enumerate(values)]
        )
    lines.append("****ORCA TERMINATED NORMALLY****")
    return ("\n".join(lines) + "\n").encode()


def _hessian(geometry, *, frequencies=None):
    n = len(geometry.atom_symbols) * 3
    values = [0.0] * 6 + [-10.2, 3657.2, 3755.1] if frequencies is None else frequencies
    lines = ["$hessian", str(n)]
    # Real format has column blocks, not one unbounded dense line.
    for start in range(0, n, 5):
        columns = list(range(start, min(n, start + 5)))
        lines.append(" ".join(map(str, columns)))
        for row in range(n):
            lines.append(
                str(row) + " " + " ".join("1.000000" if row == c else "0.000000" for c in columns)
            )
    lines.extend(["$vibrational_frequencies", str(n)] + [f"{i} {v}" for i, v in enumerate(values)])
    lines.extend(["$atoms", str(len(geometry.atom_symbols))])
    for symbol, xyz in zip(geometry.atom_symbols, geometry.coordinates, strict=True):
        lines.append(
            f"{symbol} {ATOMIC_MASSES[symbol]} "
            + " ".join(f"{v / BOHR_TO_ANGSTROM:.12f}" for v in xyz)
        )
    lines.append("$end")
    return ("\n".join(lines) + "\n").encode()


def _parse(geometry, output=None, *, kind="freq", **kwargs):
    return parse_orca_output(
        _stdout(kind) if output is None else output,
        primitive=P5NodeKind(kind),
        geometry=geometry,
        input_manifest_hash="0" * 64,
        exit_code=0,
        **kwargs,
    )


def test_final_opt_artifact_wins_over_initial_coordinates(geometry):
    coords = "O 0 0 0\nH 0.96 0 0\nH 0 0.96 0"
    xyz = ("3\nactual ORCA XYZ byte formatting preserved\n" + coords + "\n").encode()
    parsed = _parse(geometry, _stdout("opt", coords=coords), kind="opt", optimized_xyz_bytes=xyz)
    assert parsed.optimized_xyz_bytes == xyz
    assert b"1.20" not in parsed.optimized_xyz_bytes


@pytest.mark.parametrize("case", ["missing_xyz", "wrong_xyz", "ambiguous_final", "no_convergence"])
def test_opt_rejects_missing_wrong_or_ambiguous_final_geometry(geometry, case):
    coords = "O 0 0 0\nH 0.96 0 0\nH 0 0.96 0"
    xyz = ("3\nfinal\n" + coords + "\n").encode()
    output = _stdout("opt", coords=coords)
    if case == "missing_xyz":
        xyz = None
    elif case == "wrong_xyz":
        xyz = xyz.replace(b"0.96", b"1.20")
    elif case == "ambiguous_final":
        output = output.replace(
            b"****ORCA",
            b"CARTESIAN COORDINATES (ANGSTROEM)\n---\n" + coords.encode() + b"\n\n****ORCA",
        )
    else:
        output = output.replace(b"HAS CONVERGED", b"NOT CONVERGED")
    with pytest.raises(P5Error):
        _parse(geometry, output, kind="opt", optimized_xyz_bytes=xyz)


def test_complete_block_hessian_retains_negative_raw_modes(geometry):
    parsed = _parse(geometry, hessian_bytes=_hessian(geometry))
    assert len(parsed.frequencies) == 9 and parsed.frequencies[6] == -10.2
    assert parsed.hessian_dimension == 9


@pytest.mark.parametrize(
    "case",
    [
        "empty_matrix",
        "missing_block",
        "nonfinite",
        "wrong_atom",
        "wrong_mass",
        "wrong_geometry",
        "incomplete_modes",
        "duplicate_mode",
        "wrong_dimension",
        "missing_atoms",
        "asymmetric",
        "missing_hessian_modes",
        "nonfinite_mode",
    ],
)
def test_invalid_hessian_and_frequency_tables_are_rejected(geometry, case):
    hess = _hessian(geometry)
    output = _stdout()
    if case == "empty_matrix":
        hess = b"$hessian\n9\n"
    elif case == "missing_block":
        hess = hess.replace(b"0 1 2 3 4", b"0 1 2 3")
    elif case == "nonfinite":
        hess = hess.replace(b"1.000000", b"nan", 1)
    elif case == "wrong_atom":
        hess = hess.replace(b"O 15.999", b"N 15.999")
    elif case == "wrong_mass":
        hess = hess.replace(b"O 15.999", b"O 100.0")
    elif case == "wrong_geometry":
        before = hess.split(b"$atoms")[0]
        hess = before + b"$atoms\n3\nO 15.999 0 0 0\nH 1.008 0 0 0\nH 1.008 0 0 0\n$end\n"
    elif case == "incomplete_modes":
        output = _stdout(frequencies=[1000.0])
    elif case == "duplicate_mode":
        output = output.replace(b"8:", b"7:")
    elif case == "wrong_dimension":
        hess = hess.replace(b"$hessian\n9", b"$hessian\n8")
    elif case == "missing_atoms":
        hess = hess.split(b"$atoms")[0]
    elif case == "asymmetric":
        hess = hess.replace(b"0.000000", b"2.000000", 1)
    elif case == "missing_hessian_modes":
        hess = hess.replace(b"$vibrational_frequencies", b"$unknown")
    elif case == "nonfinite_mode":
        output = output.replace(b"-10.200000", b"nan")
    with pytest.raises((RequiredOutputMissing, GeometryBindingMismatch)):
        _parse(geometry, output, hessian_bytes=hess)


@pytest.mark.parametrize("corruption", [b"SCF NOT CONVERGED", b"truncated", b"nonzero"])
def test_sp_failure_markers_reject_success(geometry, corruption):
    output = _stdout("sp")
    if corruption == b"SCF NOT CONVERGED":
        output = output.replace(b"SCF CONVERGED", corruption)
    elif corruption == b"truncated":
        output = output.replace(b"ORCA TERMINATED NORMALLY", corruption)
    else:
        output = output.replace(b"FINAL SINGLE POINT ENERGY", b"MISSING ENERGY")
    with pytest.raises(P5Error):
        _parse(geometry, output, kind="sp")


def test_fake_cannot_masquerade_as_a_real_receipt(tmp_path):
    from orca_agent.application.p5_errors import LaunchStateUnknown
    from orca_agent.execution.local_backend import LocalOrcaBackend

    service, _, view = _prepare(tmp_path, "p5.opt_only.r2scan3c.v1")
    final = _approve_and_run(service, view)
    backend = LocalOrcaBackend(service.state_root)
    with pytest.raises(LaunchStateUnknown):
        backend.poll(str(final.job.execution_id))
