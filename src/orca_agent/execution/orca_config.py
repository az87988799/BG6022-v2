"""Explicit local ORCA configuration, fingerprinting, and bounded doctor checks."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from orca_agent.domain.hashing import sha256_hex

_THREAD_ENVIRONMENT = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}


def available_physical_memory_mb() -> int | None:
    """Return current physical memory available to the host, when measurable."""

    if os.name == "nt":

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys // (1024 * 1024))
        return None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


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
        # ORCA 6.1 prints no banner without an argument, and treats --version
        # as an input filename. An absent file in a fresh directory yields the
        # banner and an input-open error without starting any calculation.
        with tempfile.TemporaryDirectory(prefix="orca-version-probe-") as directory:
            completed = subprocess.run(
                [str(target), "missing-version-probe.inp"],
                input=b"",
                capture_output=True,
                cwd=directory,
                shell=False,
                timeout=timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
    nprocs: int = 1,
    implicit_threads: int = 1,
    parallel: bool = False,
    total_memory_mb: int | None = None,
    maxcore_mb: int | None = None,
) -> dict[str, object]:
    if type(nprocs) is not int or nprocs < 1:
        raise ValueError("runtime nprocs must be a positive integer")
    if type(implicit_threads) is not int or implicit_threads != 1:
        raise ValueError("runtime implicit threads must be exactly one")
    if type(parallel) is not bool or (nprocs > 1 and not parallel):
        raise ValueError("multi-process runtime requires parallel mode")
    if total_memory_mb is not None and (type(total_memory_mb) is not int or total_memory_mb < 256):
        raise ValueError("runtime total memory must be at least 256 MB")
    if maxcore_mb is not None and (type(maxcore_mb) is not int or maxcore_mb < 1):
        raise ValueError("runtime maxcore must be positive")
    if total_memory_mb is not None and maxcore_mb is not None:
        expected_maxcore = (total_memory_mb * 75) // (100 * nprocs)
        if maxcore_mb != expected_maxcore:
            raise ValueError("runtime maxcore must follow the 75% per-process rule")
    result: dict[str, object] = {
        "platform": os.name,
        "state_root": str(Path(state_root).resolve()),
        "profile_hash": profile_hash,
        "nprocs": nprocs,
        "implicit_threads": implicit_threads,
    }
    if total_memory_mb is not None:
        result["total_memory_mb"] = total_memory_mb
    if maxcore_mb is not None:
        result["maxcore_mb"] = maxcore_mb
    if nprocs > 1 or parallel:
        result.update(
            {
                "parallel": parallel,
                "thread_environment": dict(_THREAD_ENVIRONMENT),
            }
        )
    if executable is not None:
        target = Path(executable).expanduser().resolve()
        digest = executable_sha256(target)
        version = probe_orca_version(target) if probe else orca_version
        if probe and (version != orca_version or re.fullmatch(r"6\.1\.\d+", version) is None):
            raise ValueError("probed full ORCA version differs from the requested version")
        if probe and executable_sha256(target) != digest:
            raise ValueError("ORCA executable changed during version probing")
        result.update(
            {"executable": str(target), "executable_sha256": digest, "orca_version": version}
        )
    else:
        result.update({"executable": None, "executable_sha256": None, "orca_version": None})
    result["runtime_config_hash"] = sha256_hex(result)
    return result


def validate_runtime_config(
    value: Mapping[str, object], *, expected_nprocs: int, expected_parallel: bool
) -> dict[str, object]:
    """Validate the immutable runtime envelope used by a trusted launch ticket."""

    if not isinstance(value, Mapping):
        raise ValueError("runtime config must be an object")
    config = dict(value)
    if config.get("nprocs") != expected_nprocs:
        raise ValueError("runtime nprocs does not match the trusted budget")
    if config.get("implicit_threads") != 1:
        raise ValueError("runtime implicit threads must be exactly one")
    if "total_memory_mb" in config:
        if type(config["total_memory_mb"]) is not int or int(config["total_memory_mb"]) < 256:
            raise ValueError("runtime total memory is invalid")
    if "maxcore_mb" in config:
        if type(config["maxcore_mb"]) is not int or int(config["maxcore_mb"]) < 1:
            raise ValueError("runtime maxcore is invalid")
    if "total_memory_mb" in config and "maxcore_mb" in config:
        expected_maxcore = (int(config["total_memory_mb"]) * 75) // (100 * expected_nprocs)
        if int(config["maxcore_mb"]) != expected_maxcore:
            raise ValueError("runtime maxcore is not bound to total memory and nprocs")
    has_parallel_fields = "parallel" in config or "thread_environment" in config
    if expected_nprocs > 1 or expected_parallel:
        if config.get("parallel") is not expected_parallel:
            raise ValueError("runtime parallel mode does not match the trusted profile")
        if config.get("thread_environment") != _THREAD_ENVIRONMENT:
            raise ValueError("runtime thread environment is not the fixed one-thread profile")
    elif has_parallel_fields:
        raise ValueError("single-process runtime contains unexpected parallel fields")
    supplied_hash = config.get("runtime_config_hash")
    body = {key: item for key, item in config.items() if key != "runtime_config_hash"}
    if supplied_hash != sha256_hex(body):
        raise ValueError("runtime config hash does not match its content")
    return config


def execution_environment(value: Mapping[str, object]) -> dict[str, str]:
    """Return the child environment, applying only the registered thread controls."""

    config = validate_runtime_config(
        value,
        expected_nprocs=int(value.get("nprocs", 0)),
        expected_parallel=bool(value.get("parallel", False)),
    )
    environment = os.environ.copy()
    thread_environment = config.get("thread_environment")
    if thread_environment is not None:
        if not isinstance(thread_environment, Mapping):
            raise ValueError("runtime thread environment must be an object")
        environment.update({str(key): str(item) for key, item in thread_environment.items()})
    return environment


def doctor(
    state_root: str | Path,
    *,
    executable: str | Path | None = None,
    probe: bool = False,
    expected_version: str = "6.1.1",
    orca_version: str | None = None,
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
        "expected_orca_version": expected_version,
        "thread_environment": dict(_THREAD_ENVIRONMENT),
        "available_memory_mb": available_physical_memory_mb(),
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
    dependency_checks: dict[str, bool] = {}
    for module_name in ("numpy", "rdkit", "httpx"):
        try:
            dependency_checks[module_name] = importlib.util.find_spec(module_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            dependency_checks[module_name] = False
    checks["dependencies"] = dependency_checks
    mpiexec = shutil.which("mpiexec")
    checks["mpi_available"] = mpiexec is not None
    checks["mpi_smoke"] = {
        "attempted": False,
        "ready": False,
        "executable": mpiexec,
    }
    if mpiexec is not None:
        smoke = checks["mpi_smoke"]
        assert isinstance(smoke, dict)
        smoke["attempted"] = True
        try:
            completed = subprocess.run(
                [mpiexec, "-n", "4", sys.executable, "-c", "pass"],
                capture_output=True,
                cwd=root,
                timeout=20,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            smoke["ready"] = completed.returncode == 0
            if completed.returncode != 0:
                smoke["error"] = (completed.stderr or completed.stdout).decode(
                    "utf-8", errors="replace"
                )[:512]
        except (OSError, subprocess.TimeoutExpired) as error:
            smoke["error"] = type(error).__name__
    checks["mpi_smoke_ready"] = bool(checks["mpi_smoke"].get("ready"))
    checks["memory_sufficient"] = (
        checks["available_memory_mb"] is None or int(checks["available_memory_mb"]) >= 2048
    )
    if executable is not None:
        try:
            target = Path(executable).expanduser().resolve()
            digest = executable_sha256(target)
            digest_after = executable_sha256(target)
            if digest != digest_after:
                raise RuntimeError("ORCA executable changed while being fingerprinted")
            version = probe_orca_version(target) if probe else orca_version
            checks.update(
                {
                    "executable": str(target),
                    "executable_sha256": digest,
                    "orca_version": version,
                    "real_execution": version == expected_version,
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
    checks["ready"] = bool(
        checks["state_root_exists"]
        and checks["work_root_writable"]
        and checks["real_execution"]
        and checks["memory_sufficient"]
        and all(dependency_checks.values())
        and (checks["mpi_smoke_ready"] or os.name != "nt")
    )
    return checks


__all__ = [
    "available_physical_memory_mb",
    "doctor",
    "executable_sha256",
    "execution_environment",
    "probe_orca_version",
    "runtime_config",
    "validate_runtime_config",
]
