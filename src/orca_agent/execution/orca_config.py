"""Explicit local ORCA configuration, fingerprinting, and bounded doctor checks."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from orca_agent.domain.hashing import sha256_hex


def executable_sha256(path: str | Path) -> str:
    target = Path(path).expanduser().resolve()
    if target.suffix.casefold() != ".exe" or not target.is_file() or target.is_symlink():
        raise ValueError("ORCA executable must be a regular .exe file")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_orca_version(path: str | Path, *, timeout_seconds: float = 5.0) -> str:
    """Probe a banner only when explicitly requested; never infer version from a filename."""

    target = Path(path).expanduser().resolve()
    digest = executable_sha256(target)
    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ValueError("ORCA probe timeout must be bounded")
    try:
        completed = subprocess.run(
            [str(target)],
            input=b"",
            capture_output=True,
            cwd=str(target.parent),
            shell=False,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("ORCA version probe failed") from error
    text = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")
    match = re.search(
        r"(?:Program Version|ORCA Version|Version)\s*[:=]?\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if match is None:
        raise RuntimeError(f"ORCA version probe produced no parseable version (sha256={digest})")
    return match.group(1)


def runtime_config(
    *,
    state_root: str | Path,
    executable: str | Path | None,
    orca_version: str | None,
    profile_hash: str,
    probe: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "platform": os.name,
        "state_root": str(Path(state_root).resolve()),
        "profile_hash": profile_hash,
        "nprocs": 1,
        "implicit_threads": 1,
    }
    if executable is not None:
        target = Path(executable).expanduser().resolve()
        digest = executable_sha256(target)
        version = probe_orca_version(target) if probe else orca_version
        result.update(
            {"executable": str(target), "executable_sha256": digest, "orca_version": version}
        )
    else:
        result.update({"executable": None, "executable_sha256": None, "orca_version": None})
    result["runtime_config_hash"] = sha256_hex(result)
    return result


def doctor(
    state_root: str | Path,
    *,
    executable: str | Path | None = None,
    probe: bool = False,
) -> dict[str, object]:
    root = Path(state_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    checks: dict[str, object] = {
        "state_root": str(root),
        "state_root_exists": root.is_dir(),
        "work_root": str(root / "work"),
        "work_root_writable": False,
        "real_execution": False,
        "probe": probe,
    }
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        check_file = work / ".doctor-write-check"
        check_file.write_bytes(b"ok")
        check_file.unlink()
        checks["work_root_writable"] = True
    except OSError:
        checks["work_root_writable"] = False
    if executable is not None:
        try:
            target = Path(executable).expanduser().resolve()
            digest = executable_sha256(target)
            version = probe_orca_version(target) if probe else None
            checks.update(
                {
                    "executable": str(target),
                    "executable_sha256": digest,
                    "orca_version": version,
                    "real_execution": version is not None and version.startswith("6.1"),
                }
            )
        except (OSError, ValueError, RuntimeError) as error:
            checks.update(
                {
                    "executable": str(executable),
                    "executable_error": str(error),
                    "real_execution": False,
                }
            )
    checks["ready"] = bool(checks["state_root_exists"] and checks["work_root_writable"])
    return checks


__all__ = ["doctor", "executable_sha256", "probe_orca_version", "runtime_config"]
