"""Read-only replay of one authorized real ORCA run; never invokes ORCA."""

import hashlib
from pathlib import Path

import pytest

from orca_agent.execution.orca_parser import parse_orca_output
from orca_agent.identity.geometry import parse_xyz_bytes
from tests.p5.test_p5_workflow import _prepare


def test_real_multicycle_crlf_opt_preserves_actual_final_xyz(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "orca_6_1_1_water_opt"
    _, _, view = _prepare(tmp_path, "p5.opt_only.r2scan3c.v1")
    raw = (fixture / "stdout.out").read_bytes()
    actual_xyz = (fixture / "input.xyz").read_bytes()
    assert b"\r\n" in raw
    assert hashlib.sha256(raw).hexdigest() == (
        "d05245e18d406d3d59e7a80ef607eaef889291b085cd2ba6691b17231bbd77af"
    )
    parsed = parse_orca_output(
        raw,
        primitive="opt",
        geometry=view.geometry[0],
        input_manifest_hash=view.binding.input_manifest_hash,
        exit_code=0,
        optimized_xyz_bytes=actual_xyz,
    )
    assert parsed.orca_version == "6.1.1"
    assert parsed.energy == pytest.approx(-76.418938721015)
    assert parsed.optimized_xyz_bytes == actual_xyz
    assert (
        parse_xyz_bytes(actual_xyz)[1]
        != parse_xyz_bytes((fixture / "geometry.xyz").read_bytes())[1]
    )
    assert raw.count(b"GEOMETRY OPTIMIZATION CYCLE") == 4
