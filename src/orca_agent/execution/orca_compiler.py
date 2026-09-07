"""Pure, closed ORCA 6.1 input compiler for the P5 registry."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

from orca_agent.application.p5_errors import UnsupportedExecutionProfile
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.p5 import GeometryRecord, P5Budget, P5ExecutionNode, P5NodeKind, bytes_sha256
from orca_agent.orchestration.p5_versions import P5_COMPILER_VERSION
from orca_agent.planning.registry import METHOD_R2SCAN3C

from .output_contract import GEOMETRY_NAME, HESSIAN_NAME, INPUT_NAME, OPTIMIZED_XYZ_NAME


class CompiledInputBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_bytes: bytes
    geometry_bytes: bytes
    input_filename: str = INPUT_NAME
    geometry_filename: str = GEOMETRY_NAME
    input_sha256: str
    geometry_sha256: str
    feature_profile: dict[str, object]
    feature_profile_hash: str
    compiler_version: str = P5_COMPILER_VERSION
    manifest: dict[str, object]
    manifest_hash: str

    @field_validator("input_bytes", "geometry_bytes")
    @classmethod
    def _bytes(cls, value: bytes) -> bytes:
        if not isinstance(value, bytes) or not value:
            raise ValueError("compiled file bytes must be non-empty")
        return value

    def as_files(self) -> dict[str, bytes]:
        return {self.input_filename: self.input_bytes, self.geometry_filename: self.geometry_bytes}


_FORBIDDEN = re.compile(r"(?:\r|\$new_job|\.nodes|include|basis|d4|mpirun|shell|;|&&|\\)", re.I)


def compile_orca_input(
    registered_primitive: P5ExecutionNode | dict[str, object],
    method_profile: object,
    geometry_record: GeometryRecord,
    resource_settings: P5Budget | dict[str, object] | None,
    feature_profile: dict[str, object] | None,
    *,
    geometry_bytes: bytes | None = None,
) -> CompiledInputBundle:
    """Compile only the closed P5 mapping; never reads paths or starts a process."""

    node = _node(registered_primitive)
    if node.method_profile_id != METHOD_R2SCAN3C.registry_id:
        raise UnsupportedExecutionProfile("P5 method profile is not registered")
    if node.method_profile_hash != METHOD_R2SCAN3C.entry_hash:
        raise UnsupportedExecutionProfile("P5 method profile hash is not registered")
    if not _is_r2scan3c(method_profile):
        raise UnsupportedExecutionProfile(
            "method profile content is not the frozen r2SCAN-3c profile"
        )
    budget = _budget(resource_settings, node.kind)
    profile = _feature_profile(feature_profile, budget)
    keyword = {P5NodeKind.SP: "SP", P5NodeKind.OPT: "Opt", P5NodeKind.FREQ: "Freq"}[node.kind]
    lines = [f"! r2SCAN-3c TightSCF {keyword}", f"%maxcore {budget.maxcore_mb}"]
    if bool(profile["parallel"]):
        lines.append(f"%pal nprocs {budget.nprocs} end")
    lines.extend(
        [
            f"* xyzfile {geometry_record.formal_charge} "
            f"{geometry_record.multiplicity} geometry.xyz",
            "",
        ]
    )
    input_bytes = "\n".join(lines).encode("utf-8")
    frozen_geometry_bytes = (
        geometry_record.xyz_bytes() if geometry_bytes is None else geometry_bytes
    )
    if not isinstance(frozen_geometry_bytes, bytes) or not frozen_geometry_bytes:
        raise ValueError("geometry_bytes must be non-empty bytes")
    if bytes_sha256(frozen_geometry_bytes) != geometry_record.xyz_bytes_sha256:
        raise UnsupportedExecutionProfile("geometry bytes do not match the frozen geometry")
    geometry_bytes = frozen_geometry_bytes
    if _FORBIDDEN.search(input_bytes.decode("utf-8")):
        raise UnsupportedExecutionProfile("compiled ORCA input contains a forbidden construct")
    manifest = {
        "compiler_version": P5_COMPILER_VERSION,
        "method_profile_id": METHOD_R2SCAN3C.registry_id,
        "method_profile_hash": METHOD_R2SCAN3C.entry_hash,
        "primitive_id": node.node_id,
        "primitive_kind": node.kind.value,
        "input_filename": INPUT_NAME,
        "geometry_filename": GEOMETRY_NAME,
        "expected_outputs": {"opt": OPTIMIZED_XYZ_NAME, "freq": HESSIAN_NAME},
        "files": {
            "input.inp": {"sha256": bytes_sha256(input_bytes), "size_bytes": len(input_bytes)},
            "geometry.xyz": {
                "sha256": bytes_sha256(geometry_bytes),
                "size_bytes": len(geometry_bytes),
            },
        },
        "budget": budget.model_dump(mode="json"),
        "feature_profile": profile,
    }
    return CompiledInputBundle(
        input_bytes=input_bytes,
        geometry_bytes=geometry_bytes,
        input_sha256=bytes_sha256(input_bytes),
        geometry_sha256=bytes_sha256(geometry_bytes),
        feature_profile=profile,
        feature_profile_hash=sha256_hex(profile),
        manifest=manifest,
        manifest_hash=sha256_hex(manifest),
    )


def _node(value: P5ExecutionNode | dict[str, object]) -> P5ExecutionNode:
    if isinstance(value, P5ExecutionNode):
        return value
    if isinstance(value, dict):
        try:
            return P5ExecutionNode.model_validate(value, strict=True)
        except Exception as error:
            raise UnsupportedExecutionProfile("registered primitive is invalid") from error
    raise TypeError("registered_primitive must be a P5ExecutionNode")


def _is_r2scan3c(value: object) -> bool:
    if value is METHOD_R2SCAN3C:
        return True
    return (
        getattr(value, "registry_id", None) == METHOD_R2SCAN3C.registry_id
        and getattr(value, "entry_hash", None) == METHOD_R2SCAN3C.entry_hash
        and getattr(value, "method_name", None) == "r2SCAN-3c"
    )


def _budget(value: P5Budget | dict[str, object] | None, kind: P5NodeKind) -> P5Budget:
    if value is None:
        return P5Budget.defaults_for(kind)
    if isinstance(value, P5Budget):
        return value
    if not isinstance(value, dict):
        raise TypeError("resource_settings must be a P5Budget or object")
    data = dict(value)
    total = int(data.get("total_memory_mb", 2048))
    nprocs = int(data.get("nprocs", 1))
    data.setdefault("maxcore_mb", (total * 75) // (100 * nprocs))
    data.setdefault("wall_time_seconds", P5Budget.defaults_for(kind).wall_time_seconds)
    data.setdefault("run_wall_time_seconds", 3600)
    return P5Budget.model_validate(data, strict=True)


def _feature_profile(value: dict[str, object] | None, budget: P5Budget) -> dict[str, object]:
    profile = {"parallel": False, "nprocs": budget.nprocs, "implicit_threads": 1}
    if value is not None:
        if not isinstance(value, dict) or set(value) - {"parallel", "implicit_threads"}:
            raise UnsupportedExecutionProfile("feature profile contains unsupported fields")
        profile.update(value)
    if type(profile["parallel"]) is not bool or type(profile["implicit_threads"]) is not int:
        raise UnsupportedExecutionProfile("feature profile types are invalid")
    if profile["parallel"] is False and budget.nprocs != 1:
        raise UnsupportedExecutionProfile(
            "nprocs above one requires the validated parallel profile"
        )
    if profile["parallel"] and budget.nprocs < 1:
        raise UnsupportedExecutionProfile("parallel profile has invalid nprocs")
    if profile["implicit_threads"] != 1:
        raise UnsupportedExecutionProfile("P5 fixes implicit computational threads to one")
    return profile


__all__ = ["CompiledInputBundle", "compile_orca_input"]
