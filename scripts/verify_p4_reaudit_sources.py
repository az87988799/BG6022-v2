"""Explicit live CID/CAS source smoke; leaves identities awaiting owner confirmation."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.p4 import IdentityProvider, MoleculeInputKind
from orca_agent.orchestration.p4_commands import StartPlanningRun


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--allow-network", action="store_true", required=True)
    args = parser.parse_args()
    if args.state_root.exists() or args.manifest.exists():
        raise ValueError("state root and manifest must be fresh")
    service = P4ApplicationService(args.state_root, allow_network=args.allow_network)
    cases = []
    for name, kind, value, expected_cid, stereo in (
        ("L-alanine CID", MoleculeInputKind.CID, "5950", 5950, True),
        ("ethanol CAS", MoleculeInputKind.CAS, "64-17-5", 702, False),
    ):
        command = StartPlanningRun.create(
            input_kind=kind,
            raw_input=value,
            charge=0,
            multiplicity=1,
            provider=IdentityProvider.PUBCHEM,
            requested_at_utc=datetime.now(UTC),
        )
        started = service.start(command)
        if not started.accepted:
            raise ValueError("prepare rejected")
        service.create_worker().run_once(limit=1)
        view = service.inspect(started.run_id)
        if view.state.phase.value != "awaiting_identity":
            raise ValueError("live source did not reach awaiting_identity")
        candidates = view.candidate_bundle.candidates
        if len(candidates) != 1 or candidates[0].cid != expected_cid:
            raise ValueError("CID/CAS source mapping differs from expected identity")
        candidate = candidates[0]
        if stereo and (
            "@" not in candidate.source_smiles
            or "@" not in candidate.canonical_isomeric_smiles
            or candidate.stereo_status.value != "defined"
        ):
            raise ValueError("source stereochemistry was not preserved")
        cases.append(
            {
                "name": name,
                "run_id": str(view.run_id),
                "expected_cid": expected_cid,
                "observed_cid": candidate.cid,
                "phase": view.state.phase.value,
                "stereo_required": stereo,
                "stereo_status": candidate.stereo_status.value,
            }
        )
    manifest = {
        "provider": "pubchem",
        "state_root": str(args.state_root.resolve()),
        "cases": cases,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "identity_confirmation": "PENDING_OWNER",
    }
    args.manifest.write_bytes(canonical_json_bytes(manifest))
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
