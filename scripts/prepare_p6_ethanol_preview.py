"""Prepare the bounded Ethanol acceptance action; never approve or execute it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from verify_p5_real_orca import cli

from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex


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
        "p5.opt_freq_sp.r2scan3c.v1",
        "--backend",
        "local_orca",
        "--orca-executable",
        args.orca_executable,
        "--orca-version",
        "6.1.1",
    )
    view = cli(root, "inspect", "--workflow", "p5", "--run-id", created["run_id"])
    assert view["state"]["phase"] == "awaiting_execution_approval"
    preview = {
        "status": "AWAITING_EXPLICIT_BOUNDED_APPROVAL",
        "state_root": str(root),
        "run_id": created["run_id"],
        "identity": candidate,
        "method": "r2SCAN-3c",
        "environment": "gas_phase",
        "max_physical_jobs": 3,
        "total_wall_time_ceiling_seconds": 3000,
        "per_node_wall_seconds": [900, 1800, 300],
        "cores": 1,
        "memory_mb": 2048,
        "no_auto_retry": True,
        "new_orca_calculation_starts": 0,
        "doctor": doctor,
        "initial_action": view["action"],
        "initial_binding": view["binding"],
        "nodes": view["plan"]["nodes"],
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
