"""Resume P6 in fresh interpreters and prove its P5 source ledger is unchanged."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from orca_agent.domain.canonical import canonical_json_bytes


def source_fingerprint(root, source):
    with sqlite3.connect(root / "state.sqlite3") as connection:
        tables = ("runs", "events", "workflow_records", "local_jobs", "artifacts", "outbox")
        value = {
            table: [
                list(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE run_id = ? ORDER BY rowid", (source,)
                ).fetchall()
            ]
            for table in tables
        }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root, output = args.state_root.resolve(), args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    calls = []

    def cli(*arguments):
        command = [
            sys.executable,
            "-m",
            "orca_agent",
            "--state-root",
            str(root),
            *map(str, arguments),
            "--json",
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"
        )
        stdout, stderr = process.communicate(timeout=120)
        if process.returncode:
            raise RuntimeError(stdout + stderr)
        result = json.loads(stdout)
        calls.append({"pid": process.pid, "arguments": command[1:], "result": result})
        return result

    before = source_fingerprint(root, args.source_run_id)
    created = cli(
        "assess",
        "--source-run-id",
        args.source_run_id,
        "--save-request",
        output / "assess-request.json",
    )
    run = created["run_id"]
    first = cli("worker", "--workflow", "p6", "--run-id", run)
    second = cli("worker", "--workflow", "p6", "--run-id", run)
    assert [item["reports"][0]["outcome"] for item in (first, second)] == ["succeeded", "succeeded"]
    assert cli("verify-report", "--run-id", run)["valid"]
    assert cli("worker", "--workflow", "p6", "--run-id", run)["reports"] == []
    assert cli("verify-report", "--run-id", run)["valid"]
    after = source_fingerprint(root, args.source_run_id)
    assert before == after
    scripts = Path(__file__).resolve().parent
    packet = output / "packet"
    subprocess.run(
        [
            sys.executable,
            str(scripts / "export_p6_evidence.py"),
            "--state-root",
            str(root),
            "--run-id",
            run,
            "--output",
            str(packet),
        ],
        check=True,
    )
    process = subprocess.run(
        [
            sys.executable,
            str(scripts / "verify_p6_evidence.py"),
            "--mode",
            "archived_packet",
            "--run-id",
            run,
            "--evidence-root",
            str(packet),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = {
        "run_id": run,
        "source_p5_run_id": args.source_run_id,
        "source_ledger_sha256_before": before,
        "source_ledger_sha256_after": after,
        "new_p5_starts": 0,
        "fresh_process_calls": calls,
        "archived_verification": json.loads(process.stdout),
    }
    (output / "restart-evidence.json").write_bytes(canonical_json_bytes(evidence))
    print(
        json.dumps(
            {
                "run_id": run,
                "new_p5_starts": 0,
                "archived_verification": evidence["archived_verification"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
