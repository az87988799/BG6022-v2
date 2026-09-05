"""Upgrade databases produced by the published main code, not current writers."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.infrastructure.migrations import apply_migrations
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

BASELINE = "a001a31b6c0123a24e7e5d89774b0a1799024a27"
GENERATOR = """
import sys
from datetime import UTC, datetime, timedelta
from orca_agent.application.service import KernelApplicationService
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.orchestration.commands import (
    CreateRun, CancelRun, RecordEffectSucceeded, RecordEffectFailed,
)
from orca_agent.orchestration.effects import EffectSpec, EffectClass
path, scenario = sys.argv[1:]
clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
service = KernelApplicationService(path, clock=clock)
created = service.execute(CreateRun.create(
    requested_at_utc=clock.now_utc(), effects=() if scenario == 'cancel' else (
        EffectSpec(effect_index=0, effect_type='internal.audit',
                   effect_class=EffectClass.INTERNAL, payload={}),)))
assert created.accepted
if scenario == 'cancel':
    assert service.execute(CancelRun.create(run_id=created.run_id,
        expected_revision=1, reason_code='maintenance',
        requested_at_utc=clock.now_utc())).accepted
else:
    owner = new_id(WorkerId)
    with SQLiteUnitOfWork(path, clock=clock) as u:
        effect = u.outbox.claim_due(worker_id=owner, now=clock.now_utc(),
            lease_duration=timedelta(seconds=30), limit=1)[0]
        if scenario.startswith(('failure', 'retry')):
            u.outbox.mark_failed(effect_id=effect.effect_id, worker_id=owner,
                now=clock.now_utc(), error_code='legacy_timeout',
                error_message='old diagnostic token=internal',
                max_attempts=5 if scenario == 'retry' else 1)
        else:
            assert u.outbox.mark_succeeded(effect_id=effect.effect_id,
                worker_id=owner, now=clock.now_utc())
    if scenario.endswith('audit'):
        common = dict(run_id=created.run_id, expected_revision=1,
            effect_id=effect.effect_id, requested_at_utc=clock.now_utc())
        command = (RecordEffectFailed.create(**common, error_code='legacy_timeout',
            error_message='old diagnostic token=internal') if scenario.startswith('failure') else
            RecordEffectSucceeded.create(**common,
                result_summary={'value': 1} if scenario.startswith('value') else {}))
        assert service.execute(command).accepted
"""


@pytest.fixture(scope="module")
def published_source(tmp_path_factory):
    destination = tmp_path_factory.mktemp("published-main")
    archive = subprocess.check_output(
        ["git", "archive", BASELINE, "src"], cwd=Path(__file__).resolve().parents[2]
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    return destination


def generate(published_source, path, scenario):
    env = {**os.environ, "PYTHONPATH": str(published_source / "src")}
    subprocess.run(
        [sys.executable, "-c", GENERATOR, str(path), scenario],
        cwd=published_source,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "cancel",
        "empty",
        "empty_audit",
        "value_audit",
        "failure",
        "failure_audit",
    ],
)
def test_published_history_upgrade(published_source, tmp_path, scenario):
    path = tmp_path / "old.sqlite3"
    generate(published_source, path, scenario)
    connection = SQLiteConnectionFactory(path).connect()
    query = "SELECT event_id, payload_json, payload_hash, result_json, result_hash FROM events"
    original = connection.execute(query).fetchall()
    apply_migrations(connection)
    assert connection.execute(query).fetchall() == original
    connection.close()
    with SQLiteUnitOfWork(path) as u:
        for run_id in u.runs.list_ids():
            u.runs.get_verified(run_id, u.events, interrupts=u.interrupts, outbox=u.outbox)


def test_historical_retry_diagnostics_are_not_handler_input(published_source, tmp_path):
    from datetime import UTC, datetime, timedelta

    from orca_agent.application.service import KernelApplicationService
    from orca_agent.infrastructure.clock import FrozenClock
    from orca_agent.infrastructure.worker import HandlerResult

    path = tmp_path / "old.sqlite3"
    generate(published_source, path, "retry")
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC) + timedelta(minutes=1))
    observed = []

    def handler(permit):
        observed.append((permit.effect.last_error_code, permit.effect.last_error_message))
        return HandlerResult(success=True)

    service = KernelApplicationService(path, clock=clock)
    assert service.create_worker(handler).run_once()[0].outcome == "succeeded"
    assert observed == [(None, None)]
    with SQLiteUnitOfWork(path) as u:
        for run_id in u.runs.list_ids():
            u.runs.get_verified(run_id, u.events, interrupts=u.interrupts, outbox=u.outbox)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE events SET payload_hash = '" + "0" * 64 + "'",
        "UPDATE outbox SET attempt_count = 0.5",
    ],
)
def test_corrupt_history_rolls_back(published_source, tmp_path, mutation):
    path = tmp_path / "old.sqlite3"
    generate(published_source, path, "empty_audit")
    connection = SQLiteConnectionFactory(path).connect()
    connection.execute(mutation)
    before = tuple(connection.iterdump())
    with pytest.raises(StateIntegrityError):
        apply_migrations(connection)
    assert tuple(connection.iterdump()) == before
    connection.close()
