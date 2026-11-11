"""While a console is open, a matching shared observation replaces the periodic platform query."""
import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from dom0_manager.api import console as api
from dom0_manager.drivers.base import Domain
from dom0_manager.errors import PlatformUnavailable
from dom0_manager.models import VmState

VM = UUID('11111111-1111-4111-8111-111111111111')
OTHER = UUID('22222222-2222-4222-8222-222222222222')
DOMAIN = Domain(VM, VmState.running, 1, 512, 5, 'guest', observation_id='execution-one')
# The shared reader prefixes the producer epoch, so it never equals the driver's value.
SHARED = Domain(VM, VmState.running, 1, 512, 5, 'guest', observation_id='epoch:execution-one')


class Peer:
    def __init__(self):
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()

    async def receive(self):
        return await self.input.get()

    async def send_bytes(self, data):
        await self.output.put(data)


class Session:
    def __init__(self):
        self.output = asyncio.Queue()
        self.written = []

    async def read(self):
        return await self.output.get()

    async def write(self, data):
        self.written.append(data)
        await self.output.put(data)


class Hub:
    """Stands in for ObservationHub.list_domains; each step is one snapshot or an error."""
    def __init__(self, *steps):
        self.steps = list(steps)
        self.reads = 0

    def list_domains(self):
        self.reads += 1
        step = self.steps[0] if len(self.steps) == 1 else self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Service:
    def __init__(self, hub):
        self.observations = hub


def direct_checks(monkeypatch, *outcomes):
    calls = []

    async def check(*_):
        calls.append(True)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(api, 'live_domain', check)
    monkeypatch.setattr(api, 'WATCH_INTERVAL_SECONDS', 0.01)
    return calls


async def run_until_disconnect(service, *, seconds=0.3):
    peer, session = Peer(), Session()
    task = asyncio.create_task(api.relay(peer, session, service, VM, 5, observation_id='execution-one'))
    try:
        await asyncio.sleep(seconds)
        await peer.input.put({'type': 'websocket.receive', 'bytes': b'still-live'})
        assert await asyncio.wait_for(peer.output.get(), 1) == b'still-live'
        await peer.input.put({'type': 'websocket.receive', 'text': '{"type":"disconnect"}'})
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_matching_shared_observation_skips_platform_queries_after_one_direct_check(monkeypatch):
    calls = direct_checks(monkeypatch, DOMAIN)
    hub = Hub([SHARED])
    asyncio.run(run_until_disconnect(Service(hub)))
    assert len(calls) == 1  # Only the check that anchors the shared identity.
    assert hub.reads >= 5


@pytest.mark.parametrize('snapshot', [
    PlatformUnavailable('shared VM observation unavailable'),  # stale, foreign boot or unreadable
    [],  # VM absent from the shared view
    [replace(SHARED, vm_id=OTHER)],
    [replace(SHARED, state=VmState.stopping)],
    [replace(SHARED, state=VmState.crashed)],
    [replace(SHARED, domid=6)],
])
def test_unusable_shared_observation_keeps_the_direct_check_every_interval(monkeypatch, snapshot):
    calls = direct_checks(monkeypatch, DOMAIN)
    asyncio.run(run_until_disconnect(Service(Hub(snapshot))))
    assert len(calls) >= 5


def test_no_observation_hub_keeps_the_direct_check(monkeypatch):
    calls = direct_checks(monkeypatch, DOMAIN)
    asyncio.run(run_until_disconnect(None))
    assert len(calls) >= 5


def test_changed_shared_identity_is_confirmed_directly_then_re_anchored(monkeypatch):
    calls = direct_checks(monkeypatch, DOMAIN)
    renamed = replace(SHARED, observation_id='epoch-after-restart:execution-one')
    hub = Hub([SHARED], [SHARED], [SHARED], [renamed])
    asyncio.run(run_until_disconnect(Service(hub)))
    assert len(calls) == 2  # Anchor, then one direct confirmation of the changed shared value.


def test_new_execution_in_shared_view_is_verified_directly_and_closes(monkeypatch):
    restarted = Domain(VM, VmState.running, 1, 512, 6, 'guest', observation_id='execution-two')
    calls = direct_checks(monkeypatch, DOMAIN, restarted)
    hub = Hub([SHARED], [SHARED], [replace(SHARED, domid=6, observation_id='epoch:execution-two')])

    async def scenario():
        peer, session = Peer(), Session()
        with pytest.raises(api.EndRelay) as closed:
            await asyncio.wait_for(api.relay(peer, session, Service(hub), VM, 5, observation_id='execution-one'), 1)
        assert closed.value.code == 4409
        assert not session.written

    asyncio.run(scenario())
    assert len(calls) == 2


def test_shared_domain_reads_only_the_hub_and_hides_its_errors():
    async def scenario():
        assert await api.shared_domain(None, VM) is None
        assert await api.shared_domain(Service(None), VM) is None
        assert await api.shared_domain(Service(Hub(PlatformUnavailable('stale'))), VM) is None
        assert await api.shared_domain(Service(Hub([replace(SHARED, vm_id=OTHER), SHARED])), VM) == SHARED
    asyncio.run(scenario())
