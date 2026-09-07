"""Version identifiers owned by the P5 local execution workflow."""

from __future__ import annotations

P5_SCHEMA_VERSION = 4
P5_ENGINE_VERSION = "p5-local-orca-v1"
P5_POLICY_VERSION = 5
P5_REGISTRY_VERSION = "p5-execution-registry-v1"
P5_COMPILER_VERSION = "orca-compiler-v2"
P5_PARSER_VERSION = "orca-parser-v3"
P5_GEOMETRY_VERSION = "rdkit-etkdgv3-geometry-v1"

__all__ = [
    "P5_COMPILER_VERSION",
    "P5_ENGINE_VERSION",
    "P5_GEOMETRY_VERSION",
    "P5_PARSER_VERSION",
    "P5_POLICY_VERSION",
    "P5_REGISTRY_VERSION",
    "P5_SCHEMA_VERSION",
]
