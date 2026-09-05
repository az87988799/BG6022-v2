from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from threading import Barrier, Event

import pytest
from test_p2_hardening_repair import _authorize, _effect, _seed

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import (
    EffectDispatchBlockedError,
    EffectInFlightError,
    LeaseLostError,
    StorageBusyError,
)
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.infrastructure.outbox import OutboxRepository
from orca_agent.infrastructure.worker import HandlerResult
from orca_agent.orchestration.commands import RequestInterrupt
from orca_agent.orchestration.dispatch_policy import EffectRegistry
from orca_agent.orchestration.effects import EffectClass


def test_fixed_policy_hashes_and_deep_immutability():
    assert [EffectRegistry(policy_version=n).configuration_hash for n in (1, 2)] == [
        "641aac325bc27f97e4b70edc7c205bb09c699ef5810bdc5f3e80b90f116fb230",
        "175f1ec7231bfaf5444d4a28156a3e23c7caff83dcee7eee24e5af62bc55b268",
    ]
    registry = EffectRegistry()
    with pytest.raises(FrozenInstanceError):
        registry.policy_version = 2
    with pytest.raises(TypeError):
        registry._registrations["other"] = registry["internal.test"]
    with pytest.raises(ValueError, match="different rules"):
        EffectRegistry(tuple(EffectRegistry(policy_version=2)._registrations.values()))


@pytest.mark.parametrize("version,accepted", [(1, False), (2, True)])
def test_common_factory_policy_agrees_during_handler(tmp_path, version, accepted):
    effects = (_effect(effect_type="internal.test", effect_class=EffectClass.INTERNAL),)
    clock, path, _, created, _ = _seed(tmp_path, effects=effects)
    service = KernelApplicationService(
        path, clock=clock, registry=EffectRegistry(policy_version=version)
    )

    def handler(permit):
        result = service.execute(
            RequestInterrupt.create(
                run_id=created.run_id,
                expected_revision=1,
                kind="approval",
                payload={},
                expires_at_utc=clock.now_utc() + timedelta(hours=1),
                requested_at_utc=clock.now_utc(),
            )
        )
        assert result.accepted is accepted
        return HandlerResult(success=True)

    assert service.create_worker(handler).run_once()[0].outcome == "succeeded"


def test_policy_change_blocks_active_but_not_committed_retry(tmp_path):
    clock, path, _, _, _ = _seed(tmp_path)
    permit = _authorize(path, clock, new_id(WorkerId))
    changed = EffectCompletionService(path, clock=clock, registry=EffectRegistry(policy_version=2))
    with pytest.raises(EffectDispatchBlockedError):
        changed.complete(permit, HandlerResult(success=True))
    first = EffectCompletionService(path, clock=clock).complete(permit, HandlerResult(success=True))
    clock.advance(timedelta(hours=1))
    assert changed.complete(permit, HandlerResult(success=True)) == first


def test_expired_generation_precedes_policy_rejection(tmp_path):
    clock, path, _, _, _ = _seed(tmp_path)
    permit = _authorize(path, clock, new_id(WorkerId))
    clock.advance(timedelta(minutes=1))
    _authorize(path, clock, new_id(WorkerId))
    with pytest.raises(LeaseLostError):
        EffectCompletionService(
            path, clock=clock, registry=EffectRegistry(policy_version=2)
        ).complete(permit, HandlerResult(success=True))


def test_two_workers_compete_for_one_run_without_uncaught_error(tmp_path, monkeypatch):
    effects = tuple(
        _effect(i, effect_type="internal.test", effect_class=EffectClass.INTERNAL) for i in range(2)
    )
    _, _, service, _, _ = _seed(tmp_path, effects=effects)
    barrier, loser = Barrier(2), Event()
    original = OutboxRepository.authorize_dispatch

    def authorize(self, **kwargs):
        barrier.wait(timeout=10)
        try:
            return original(self, **kwargs)
        except EffectInFlightError:
            loser.set()
            raise

    monkeypatch.setattr(OutboxRepository, "authorize_dispatch", authorize)

    def handler(permit):
        assert loser.wait(timeout=10)
        return HandlerResult(success=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.create_worker(handler).run_once) for _ in range(2)]
        assert sorted(f.result()[0].outcome for f in futures) == ["blocked", "succeeded"]


def test_completion_busy_does_not_reinvoke_handler(tmp_path, monkeypatch):
    _, _, service, _, _ = _seed(tmp_path)
    calls = []

    def handler(permit):
        calls.append(permit)
        return HandlerResult(success=True)

    def busy(*args, **kwargs):
        raise StorageBusyError("database is busy")

    monkeypatch.setattr(EffectCompletionService, "complete", busy)
    assert service.create_worker(handler).run_once(limit=5)[0].outcome == "storage_busy"
    assert len(calls) == 1
