"""Offline replay of the immutable failed Freq packet; never executes ORCA."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from orca_agent.domain.p5 import GeometryRecord
from orca_agent.execution.control_evidence import control_gate_status
from orca_agent.execution.orca_parser import parse_orca_output


def replay():
    repository = Path(__file__).resolve().parents[1]
    packet = repository / "docs/evidence/p5-freq-blocker"
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        data = (packet / entry["path"]).read_bytes()
        if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"original evidence changed: {entry['path']}")
    evidence = json.loads((packet / "evidence.json").read_text(encoding="utf-8"))
    records = evidence["records"]
    binding = [r["record"] for r in records if r["record_type"] == "p5.execution_binding"][-1]
    geometry = GeometryRecord.model_validate_json(
        json.dumps([r["record"] for r in records if r["record_type"] == "p5.geometry"][-1])
    )
    if geometry.geometry_hash != binding["geometry_hash"]:
        raise ValueError("geometry does not match original binding")
    for name, key in (("geometry.xyz", "xyz_bytes_sha256"), ("input.inp", "input_sha256")):
        if hashlib.sha256((packet / name).read_bytes()).hexdigest() != binding[key]:
            raise ValueError("original input binding mismatch")
    receipt = json.loads((packet / "exit_receipt.json").read_text(encoding="utf-8"))
    parsed = parse_orca_output(
        (packet / "stdout.out").read_bytes(),
        primitive="freq",
        geometry=geometry,
        input_manifest_hash=binding["input_manifest_hash"],
        exit_code=receipt["exit_code"],
        hessian_bytes=(packet / "hessian.txt").read_bytes(),
    )
    controls = {}
    control_packet = repository / "docs/evidence/p5-c-controls"
    control_manifest = json.loads((control_packet / "manifest.json").read_text(encoding="utf-8"))
    for entry in control_manifest["files"]:
        data = (control_packet / entry["path"]).read_bytes()
        if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"original control evidence changed: {entry['path']}")
    for gate, status in (("R03", "cancelled"), ("R04", "timed_out")):
        control_receipt = json.loads((control_packet / gate / "exit_receipt.json").read_text())
        controls[gate] = control_gate_status(control_receipt, status)
        if controls[gate] != "PASS":
            raise ValueError("original successful control evidence no longer passes")
    return {
        "schema": "p5-frozen-freq-replay/v1",
        "replayed_at_utc": datetime.now(UTC).isoformat(),
        "implementation_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip(),
        "parser_version": parsed.parser_version,
        "original_execution_id": receipt["execution_id"],
        "original_files_verified": manifest["files"],
        "control_files_verified": len(control_manifest["files"]),
        "control_gates_replayed": controls,
        "parse_status": parsed.parse_status.value,
        "energy_eh": parsed.energy,
        "hessian_dimension": parsed.hessian_dimension,
        "raw_frequencies_cm_inverse": parsed.frequencies,
        "new_orca_starts": 0,
        "historical_run_changed": False,
        "R01_R02": "NOT_COMPLETED_BY_OFFLINE_REPLAY",
        "scientific_assessment": "not_evaluated",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # New evidence only: never overwrite an existing replay or original packet.
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "parse_status": result["parse_status"]}))


if __name__ == "__main__":
    main()
