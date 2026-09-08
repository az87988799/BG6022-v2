"""Prepare the bounded Ethanol acceptance action; never approve or execute it."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from verify_p5_real_orca import cli

from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.execution.orca_config import available_physical_memory_mb, runtime_config
from orca_agent.planning.p5_protocols import P5_OPT_FREQ_SP_4CORE


def _available_memory_mb() -> int | None:
    return available_physical_memory_mb()


def _memory_preflight(required_mb: int) -> dict[str, object]:
    available_mb = _available_memory_mb()
    return {
        "required_mb": required_mb,
        "available_mb": available_mb,
        "ready": available_mb is not None and available_mb >= required_mb,
        "source": "GlobalMemoryStatusEx" if os.name == "nt" else "/proc/meminfo",
    }


def _parallel_preflight(executable: Path) -> dict[str, object]:
    modules = sorted(path.name for path in executable.parent.glob("*_mpi.exe"))
    mpi_path = shutil.which("mpiexec")
    result: dict[str, object] = {
        "platform": os.name,
        "orca_directory": str(executable.parent),
        "mpi_executable": mpi_path,
        "mpi_version": None,
        "mpi_help_ok": False,
        "orca_mpi_module_count": len(modules),
        "orca_mpi_modules": modules,
        "rank_smoke": {
            "requested_ranks": 4,
            "returncode": None,
            "observed_ranks": [],
            "observed_sizes": [],
            "ready": False,
        },
        "ready": False,
    }
    if mpi_path is not None:
        try:
            completed = subprocess.run(
                [mpi_path, "-help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            output = f"{completed.stdout}\n{completed.stderr}"
            match = re.search(r"\[Version\s+([^\]]+)\]", output)
            result.update(
                {
                    "mpi_version": None if match is None else match.group(1),
                    "mpi_help_ok": completed.returncode == 0 and match is not None,
                }
            )
            if result["mpi_help_ok"]:
                try:
                    rank_code = (
                        "import os; print(os.environ.get('PMI_RANK'), os.environ.get('PMI_SIZE'))"
                    )
                    smoke = subprocess.run(
                        [
                            mpi_path,
                            "-n",
                            "4",
                            sys.executable,
                            "-c",
                            rank_code,
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                        check=False,
                    )
                    observed = [line.split() for line in smoke.stdout.splitlines() if line.split()]
                    ranks = sorted(int(pair[0]) for pair in observed if len(pair) == 2)
                    sizes = sorted({int(pair[1]) for pair in observed if len(pair) == 2})
                    result["rank_smoke"] = {
                        "requested_ranks": 4,
                        "returncode": smoke.returncode,
                        "observed_ranks": ranks,
                        "observed_sizes": sizes,
                        "ready": (smoke.returncode == 0 and ranks == [0, 1, 2, 3] and sizes == [4]),
                    }
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        except (OSError, subprocess.TimeoutExpired):
            pass
    result["ready"] = bool(
        os.name == "nt"
        and result["mpi_executable"]
        and result["mpi_help_ok"]
        and result["rank_smoke"]["ready"]
        and modules
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--orca-executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.state_root.resolve()
    if args.output.exists() or (root / "state.sqlite3").exists():
        raise ValueError("preview requires a new isolated state root and output")
    doctor = cli(
        root, "doctor", "--workflow", "p5", "--probe", "--orca-executable", args.orca_executable
    )
    assert doctor["orca_version"] == "6.1.1" and doctor["real_execution"]
    started = cli(
        root,
        "prepare",
        "--smiles",
        "CCO",
        "--charge",
        0,
        "--multiplicity",
        1,
        "--provider",
        "local",
        "--new-conversation",
    )
    cli(root, "worker", "--workflow", "p4")
    source = cli(root, "inspect", "--workflow", "p4", "--run-id", started["run_id"])
    bundle = source["candidate_bundle"]
    candidate = bundle["candidates"][0]
    values = {
        "run-id": source["run_id"],
        "conversation-id": source["conversation_id"],
        "expected-revision": source["revision"],
        "interrupt-id": source["interrupt"]["interrupt_id"],
        "query-id": source["query"]["query_id"],
        "query-hash": source["query"]["query_hash"],
        "candidate-bundle-id": bundle["record_id"],
        "candidate-bundle-hash": bundle["bundle_hash"],
        "candidate-set-hash": bundle["candidate_set_hash"],
        "candidate-id": candidate["candidate_id"],
        "candidate-hash": candidate["candidate_hash"],
        "decision": "accept",
    }
    cli(root, "confirm-identity", *[part for k, v in values.items() for part in ("--" + k, v)])
    created = cli(
        root,
        "prepare-execution",
        "--source-run-id",
        source["run_id"],
        "--protocol",
        P5_OPT_FREQ_SP_4CORE.protocol_id,
        "--backend",
        "local_orca",
        "--orca-executable",
        args.orca_executable,
        "--orca-version",
        "6.1.1",
    )
    view = cli(root, "inspect", "--workflow", "p5", "--run-id", created["run_id"])
    assert view["state"]["phase"] == "awaiting_execution_approval"
    nodes = view["plan"]["nodes"]
    expected_kinds = ["opt", "freq", "sp"]
    if [node["kind"] for node in nodes] != expected_kinds:
        raise ValueError("Ethanol four-core preview has an unexpected node sequence")
    expected_budgets = [
        P5_OPT_FREQ_SP_4CORE.budget_for(kind).model_dump(mode="json")
        for kind in P5_OPT_FREQ_SP_4CORE.nodes
    ]
    actual_budgets = [node["budget"] for node in nodes]
    if actual_budgets != expected_budgets:
        raise ValueError(f"Ethanol four-core node resources differ: {actual_budgets}")
    if view["binding"]["budget"] != expected_budgets[0]:
        raise ValueError("initial binding budget differs from the four-core Opt node")
    runtime = runtime_config(
        state_root=root,
        executable=args.orca_executable,
        orca_version="6.1.1",
        profile_hash=view["binding"]["feature_profile_hash"],
        nprocs=nodes[0]["budget"]["nprocs"],
        implicit_threads=1,
        parallel=P5_OPT_FREQ_SP_4CORE.parallel,
    )
    if runtime["runtime_config_hash"] != view["binding"]["runtime_config_hash"]:
        raise ValueError("four-core preview runtime hash differs from its binding")
    memory = _memory_preflight(nodes[0]["budget"]["total_memory_mb"])
    parallel = _parallel_preflight(args.orca_executable.resolve())
    preview = {
        "status": "AWAITING_EXPLICIT_BOUNDED_APPROVAL",
        "state_root": str(root),
        "run_id": created["run_id"],
        "protocol": view["plan"]["protocol_id"],
        "identity": candidate,
        "method": "r2SCAN-3c",
        "environment": "gas_phase",
        "max_physical_jobs": len(nodes),
        "max_parallel_orca_tasks": 1,
        "total_wall_time_ceiling_seconds": sum(
            node["budget"]["wall_time_seconds"] for node in nodes
        ),
        "per_node_wall_seconds": [node["budget"]["wall_time_seconds"] for node in nodes],
        "cores": nodes[0]["budget"]["nprocs"],
        "memory_mb": nodes[0]["budget"]["total_memory_mb"],
        "maxcore_mb": nodes[0]["budget"]["maxcore_mb"],
        "memory_preflight": memory,
        "parallel_preflight": parallel,
        "no_auto_retry": True,
        "new_orca_calculation_starts": 0,
        "doctor": doctor,
        "runtime_config": runtime,
        "initial_action": view["action"],
        "initial_binding": view["binding"],
        "nodes": nodes,
    }
    preview["preview_hash"] = sha256_hex(preview)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(preview))
    print(
        json.dumps(
            {
                "run_id": created["run_id"],
                "preview_hash": preview["preview_hash"],
                "action_hash": view["action"]["action_hash"],
            }
        )
    )


if __name__ == "__main__":
    main()
