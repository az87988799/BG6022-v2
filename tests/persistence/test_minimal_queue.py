from datetime import timedelta

import pytest
from test_p2_hardening_repair import BASE_TIME, _authorize, _claim, _effect, _seed

from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun, RequestInterrupt
from orca_agent.orchestration.effects import EffectClass


@pytest.mark.parametrize("kind", ["unknown", "waiting", "sibling"])
@pytest.mark.parametrize("healthy", [False, True])
def test_cursor_skips_multiple_pages_of_blocked_candidates(tmp_path, kind, healthy):
    effects = tuple(
        _effect(
            i,
            effect_type="unknown" if kind == "unknown" else "internal.test",
            effect_class=EffectClass.INTERNAL,
        )
        for i in range(140)
    )
    clock, path, service, created, _ = _seed(tmp_path, effects=effects)
    if kind == "waiting":
        assert service.execute(
            RequestInterrupt.create(
                run_id=created.run_id,
                expected_revision=1,
                kind="approval",
                payload={},
                expires_at_utc=BASE_TIME + timedelta(hours=1),
                requested_at_utc=BASE_TIME,
            )
        ).accepted
    if kind == "sibling":
        _authorize(path, clock, new_id(WorkerId))
    clock.advance(timedelta(seconds=1))
    if healthy:
        other = service.execute(
            CreateRun.create(
                effects=(_effect(),),
                requested_at_utc=clock.now_utc(),
            )
        )
        assert other.accepted
    claimed = _claim(path, clock, new_id(WorkerId), limit=1)
    assert len(claimed) == int(healthy)
    if healthy:
        assert claimed[0].run_id == other.run_id
    with SQLiteUnitOfWork(path) as u:
        assert u.outbox.count() == 140 + int(healthy)
