"""Real Gateway owner/machine ordering with tmp Authority and MQTT transport leaf.

No broker, guest, device, source READY or current command is supplied. Each
machine is the actual begin() product instance; reconnect never replaces it.
"""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from xenova_mqtt.drive_binding import BindingCache
from xenova_gateway.authority import AuthorityStore
from xenova_gateway.sink import MockSink
from xenova_gateway.verified_runner import VerifiedGatewayRunner
from xenova_gateway.warm import WarmDriveMachine

HOST='11111111-1111-4111-8111-111111111111'
A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class Transport:
    """Existing Client/VerifiedRunner port; it creates no inbound messages."""
    connected=True
    connections=1
    def __init__(self):
        self.identity=SimpleNamespace(agent_instance_id=str(uuid4()))
        self.handlers={};self.published=[];self.receive_guard=None
    def set_receive_guard(self,guard):self.receive_guard=guard
    def subscribe(self,topic,handler=None,**_):self.handlers[topic]=handler
    def subscribed(self,_):return True
    def latest(self,_):return None
    def publish(self,*args,**kwargs):self.published.append((args,kwargs))
    def publish_tracked(self,topic,kind,payload):
        self.published.append((topic,kind,payload))
        return SimpleNamespace(envelope={'message_id':str(uuid4())},status=lambda _:'acknowledged')


@pytest.fixture
def runner(tmp_path):
    store=AuthorityStore(tmp_path/'authority',HOST,uid=os.geteuid(),mountpoint=None)
    store.initialize(A)
    bindings=BindingCache(HOST,clock=lambda:100.)
    value=VerifiedGatewayRunner(Transport(),MockSink(),authority=store,bindings=bindings,
        vm_ids=(A,B),clock=lambda:100.,readiness_profile='warm-drive-1')
    try:yield value
    finally:value.shutdown()


def assert_bound(value):
    machine=value.machine;operator=value.operator
    assert callable(value._client.receive_guard)
    assert type(machine) is WarmDriveMachine
    assert machine.manual_registry is operator.sources
    hook=machine.episode_prepare_allowed
    assert hook.__self__ is operator.episodes and hook.__func__ is type(operator.episodes).prepare_allowed
    assert machine.durable_write_available.__self__ is value
    assert not machine.output_permitted and not machine._authorized
    assert not operator.ready and operator.hold
    assert operator.episodes.registry is None and not operator.episodes.peers


def test_cold_operator_configuration_precedes_real_machine_creation(runner):
    assert runner.machine is None
    original=runner.authority.path.read_bytes()
    runner.configure_operator()
    assert runner.machine is None and runner.authority.path.read_bytes()==original
    assert all(getattr(runner.operator,name) is not None for name in
               ('sources','clocks','keyboard','episodes','operations'))
    runner.subscribe();runner.begin()
    assert_bound(runner)


def test_late_operator_configuration_binds_existing_actual_machine(runner):
    runner.subscribe();runner.begin();machine=runner.machine
    assert machine.manual_registry is None
    runner.configure_operator()
    assert runner.machine is machine
    assert_bound(runner)


def test_transport_reconnect_preserves_owner_hook_and_original_episode_ledger(runner):
    runner.configure_operator();runner.subscribe();runner.begin()
    machine=runner.machine;owner=runner.operator.episodes
    identity=owner.epoch,owner.revision,owner.registry
    runner._delivery(100.)
    runner._client.connections+=1
    runner._delivery(100.)
    assert runner.machine is machine and runner.operator.episodes is owner
    assert (owner.epoch,owner.revision,owner.registry)==identity
    assert_bound(runner)


def test_each_real_begin_binds_current_machine_without_recreating_operator(runner):
    runner.configure_operator();runner.subscribe();runner.begin()
    old=runner.machine;operator=runner.operator;epoch=operator.episodes.epoch
    runner.begin()
    assert runner.machine is not old and runner.operator is operator
    assert operator.episodes.epoch==epoch
    assert_bound(runner)
