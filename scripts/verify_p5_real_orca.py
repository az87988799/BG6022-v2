"""Prepare a reviewable Water gate, then execute only with explicit switches.

All scientific input is sent through the installed CLI in separate processes.
The manifest fixes executable hash, state root, run IDs and per-action budgets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from collect_p5_execution_evidence import collect

from orca_agent.domain.ids import RunId
from orca_agent.execution.windows_job import process_start_marker


def cli(root, *args):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "orca_agent.interfaces.cli",
            "--state-root",
            str(root),
            *map(str, args),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(result.stdout.splitlines()[-1])


def approve(root, view):
    return cli(
        root,
        "approve",
        "--workflow",
        "p5",
        "--run-id",
        view["run_id"],
        "--conversation-id",
        view["conversation_id"],
        "--expected-revision",
        view["revision"],
        "--action-id",
        view["action"]["action_id"],
        "--action-hash",
        view["action"]["action_hash"],
        "--binding-hash",
        view["binding"]["binding_hash"],
        "--envelope-hash",
        view["action"]["envelope_hash"],
        "--budget-hash",
        view["action"]["budget_hash"],
        "--save-request",
        root / f"{view['action']['action_id']}-approve.json",
    )


def prepare(root, executable, version, *, gates=None, timeout_seconds=1):
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "water-gate-preview.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["executable"] != str(executable) or manifest["version"] != version:
            raise ValueError("preview belongs to a different executable/version")
        return manifest
    doctor = cli(root, "doctor", "--workflow", "p5", "--probe", "--orca-executable", executable)
    if doctor.get("orca_version") != version or not doctor.get("real_execution"):
        raise ValueError("doctor did not verify the requested full version")
    started = cli(
        root,
        "prepare",
        "--smiles",
        "O",
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
    candidate = source["candidate_bundle"]["candidates"][0]
    cli(
        root,
        "confirm-identity",
        "--run-id",
        source["run_id"],
        "--conversation-id",
        source["conversation_id"],
        "--expected-revision",
        source["revision"],
        "--interrupt-id",
        source["interrupt"]["interrupt_id"],
        "--query-id",
        source["query"]["query_id"],
        "--query-hash",
        source["query"]["query_hash"],
        "--candidate-bundle-id",
        source["candidate_bundle"]["record_id"],
        "--candidate-bundle-hash",
        source["candidate_bundle"]["bundle_hash"],
        "--candidate-set-hash",
        source["candidate_bundle"]["candidate_set_hash"],
        "--candidate-id",
        candidate["candidate_id"],
        "--candidate-hash",
        candidate["candidate_hash"],
        "--decision",
        "accept",
    )
    cases = []
    for gate, protocol, wall in [
        ("R01_R02", "p5.opt_freq_sp.r2scan3c.v1", None),
        ("R03", "p5.opt_only.r2scan3c.v1", 30),
        ("R04", "p5.opt_only.r2scan3c.v1", timeout_seconds),
    ]:
        if gates is not None and gate not in gates:
            continue
        extra = [] if wall is None else ["--wall-time-seconds", wall]
        result = cli(
            root,
            "prepare-execution",
            "--source-run-id",
            source["run_id"],
            "--protocol",
            protocol,
            "--backend",
            "local_orca",
            "--orca-executable",
            executable,
            "--orca-version",
            version,
            "--save-request",
            root / f"{gate}-prepare.json",
            *extra,
        )
        view = cli(root, "inspect", "--workflow", "p5", "--run-id", result["run_id"])
        cases.append(
            {
                "gate": gate,
                "run_id": result["run_id"],
                "protocol": protocol,
                "initial_action": view["action"],
                "initial_binding": view["binding"],
                "nodes": view["plan"]["nodes"],
            }
        )
    manifest = {
        "executable": str(executable),
        "version": version,
        "executable_sha256": doctor["executable_sha256"],
        "state_root": str(root),
        "purpose": "validation",
        "scientific_assessment": "not_evaluated",
        "max_physical_jobs": sum(len(case["nodes"]) for case in cases),
        "total_wall_time_ceiling_seconds": sum(
            node["budget"]["wall_time_seconds"] for case in cases for node in case["nodes"]
        ),
        "cores": 1,
        "memory_mb": 2048,
        "cases": cases,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def execute(manifest, *, gates=None):
    root = Path(manifest["state_root"])
    exe = Path(manifest["executable"])
    if hashlib.sha256(exe.read_bytes()).hexdigest() != manifest["executable_sha256"]:
        raise ValueError("executable changed since preview")
    status_path = root / "gate-status.json"
    statuses = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    for case in manifest["cases"]:
        run_id, gate = case["run_id"], case["gate"]
        if gates is not None and gate not in gates:
            continue
        deadline = time.monotonic() + 3650
        cancellation_sent = False
        while time.monotonic() < deadline:
            view = cli(root, "inspect", "--workflow", "p5", "--run-id", run_id)
            phase = view["state"]["phase"]
            if phase in {"completed", "failed", "cancelled", "needs_reconciliation"}:
                break
            if phase == "awaiting_execution_approval":
                if view["plan"]["nodes"] != case["nodes"]:
                    raise ValueError("node budgets differ from authorized preview")
                if (
                    view["binding"]["executable_sha256"] != manifest["executable_sha256"]
                    or view["binding"]["orca_version"] != manifest["version"]
                ):
                    raise ValueError("action differs from authorized executable")
                approve(root, view)
            elif gate == "R03" and phase == "running" and not cancellation_sent:
                # Verify a physical child exists before requesting cancellation.
                directory = root / "work" / view["job"]["execution_id"]
                if (directory / "orca.pid").exists() and not (
                    directory / "exit_receipt.json"
                ).exists():
                    cli(
                        root,
                        "cancel",
                        "--run-id",
                        run_id,
                        "--conversation-id",
                        view["conversation_id"],
                        "--expected-revision",
                        view["revision"],
                    )
                    cancellation_sent = True
                else:
                    cli(root, "reconcile", "--run-id", run_id)
            else:
                cli(
                    root,
                    "worker",
                    "--workflow",
                    "p5",
                    "--backend",
                    "local_orca",
                    "--allow-real-orca",
                    "--orca-executable",
                    exe,
                    "--orca-version",
                    manifest["version"],
                    "--limit",
                    1,
                    "--run-id",
                    run_id,
                )
            time.sleep(0.05)
        view = cli(root, "inspect", "--workflow", "p5", "--run-id", run_id)
        if gate == "R01_R02" and view["state"]["phase"] == "completed":
            revision = view["revision"]
            cli(root, "replay-request", "--file", root / f"{gate}-prepare.json")
            for request in sorted(root.glob("action_*-approve.json")):
                cli(root, "replay-request", "--file", request)
            replayed = cli(root, "inspect", "--workflow", "p5", "--run-id", run_id)
            if replayed["revision"] != revision or replayed["results"] != view["results"]:
                raise RuntimeError("historical command replay changed completed results")
        verified = collect(root, RunId(run_id))
        jobs = verified["jobs"]
        physical = []
        for job in jobs:
            receipt_path = root / "work" / job["execution_id"] / "exit_receipt.json"
            receipt = (
                json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt_path.exists()
                else {}
            )
            physical.append(receipt.get("physical_start_count") == 1)
            if receipt.get("orca_pid") and process_start_marker(receipt["orca_pid"]) is not None:
                raise RuntimeError("terminal receipt still has a live process")
        status = view["job"]["status"] if view.get("job") else "not_started"
        expected = {"R01_R02": "succeeded", "R03": "cancelled", "R04": "timed_out"}[gate]
        passed = status == expected and bool(physical) and all(physical)
        if gate == "R01_R02":
            passed = passed and view["state"]["phase"] == "completed" and len(jobs) == 3
            passed = (
                passed
                and len(view["results"]) == 3
                and all(
                    result["data_origin"] == "orca_local"
                    and result["parse_status"] == "complete"
                    and result["orca_version"] == manifest["version"]
                    for result in view["results"]
                )
            )
        else:
            passed = passed and len(jobs) == 1
        statuses[gate] = "PASS" if passed else "NOT_EXERCISED" if gate in {"R03", "R04"} else "FAIL"
        evidence = root / f"{gate}-evidence.json"
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("collect_p5_execution_evidence.py")),
                "--state-root",
                str(root),
                "--run-id",
                run_id,
                "--output",
                str(evidence),
            ],
            check=True,
        )
        (root / "gate-status.json").write_text(json.dumps(statuses, indent=2), encoding="utf-8")
        if statuses[gate] != "PASS":
            raise RuntimeError(f"{gate}: {statuses[gate]}; inspect preserved raw evidence")
    return statuses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--orca-executable", type=Path, required=True)
    parser.add_argument("--orca-version", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-water-gate", action="store_true")
    parser.add_argument("--gate", action="append", choices=["R01_R02", "R03", "R04"])
    parser.add_argument("--timeout-seconds", type=int, choices=[1, 3], default=1)
    args = parser.parse_args()
    manifest = prepare(
        args.state_root.resolve(),
        args.orca_executable.resolve(),
        args.orca_version,
        gates=args.gate,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "preview": str(args.state_root.resolve() / "water-gate-preview.json"),
                "max_physical_jobs": manifest["max_physical_jobs"],
                "version": manifest["version"],
            }
        )
    )
    if args.execute and args.confirm_water_gate:
        print(json.dumps(execute(manifest, gates=args.gate)))


if __name__ == "__main__":
    main()
