"""원격 helper의 실제 profile/API 모양과 비차단·무발행·비밀 비출력 경계.

실제 SSH·broker·Pi에 연결하지 않는다. 모든 자격증명은 이 파일의 합성 fixture다.
"""
from dataclasses import asdict
import importlib
import io
import json
import os
from pathlib import Path
import queue
import threading
from types import SimpleNamespace

import pytest

from live_observer import remote
from xenova_drive_world.mapper_clock import ClockPolicy
from xenova_gateway.operator_contract import request

SECRET='synthetic-credential-never-printed'


def profile():
    policy=ClockPolicy('rc-test-1','oak-1','fixture-config','fixture-calibration','a'*64,
                      5,10,20000,1000000,500000,1000000)
    return dict(version=1,clock=asdict(policy),host='127.0.0.1',port=1883,
                username='fixture-observer',password=SECRET,tof_profiles=[],tof_max_age_us=500000)


@pytest.mark.skipif(os.name!='posix',reason='Pi private regular-file contract')
def test_actual_uplink_profile_storage_and_shape_are_read_without_modification(tmp_path):
    from xenova_gateway.mapper_uplink import UplinkProfile
    path=tmp_path/'uplink.json';raw=json.dumps(profile()).encode();path.write_bytes(raw);path.chmod(0o600)
    before=path.stat()
    actual=UplinkProfile.load(path,uid=os.geteuid())
    assert remote.load_mqtt_settings(path)==(actual.host,actual.port,actual.username,actual.password)
    after=path.stat()
    assert path.read_bytes()==raw and (before.st_ino,before.st_mtime_ns,before.st_mode)==(after.st_ino,after.st_mtime_ns,after.st_mode)


@pytest.mark.parametrize('fault',['public','owner','link','hardlink','fifo','oversize','duplicate'])
@pytest.mark.skipif(os.name!='posix',reason='Pi private regular-file contract')
def test_profile_does_not_follow_links_or_open_public_unbounded_or_wrong_owner_data(tmp_path,fault):
    path=tmp_path/'uplink.json';path.write_text(json.dumps(profile()));path.chmod(0o600)
    selected=path;uid=os.geteuid()
    if fault=='public':path.chmod(0o644)
    elif fault=='owner':uid+=1
    elif fault=='link':selected=tmp_path/'link';selected.symlink_to(path)
    elif fault=='hardlink':os.link(path,tmp_path/'second')
    elif fault=='fifo':selected=tmp_path/'fifo';os.mkfifo(selected,0o600)
    elif fault=='oversize':path.write_bytes(b' '*8193)
    else:path.write_text(json.dumps(profile())[:-1]+',"host":"127.0.0.2"}')
    with pytest.raises((ValueError,OSError)):remote.load_mqtt_settings(selected,uid=uid)


class Client:
    def __init__(self):self.subscriptions=[]
    def subscribe(self,rows):self.subscriptions.append(rows);return 0,1
    def publish(self,*_args,**_kwargs):raise AssertionError('read-only helper attempted publish')


def receive(client,topic='xenova/v1/world/snapshot.pb',payload=b'\0\xff',retain=False):
    client.on_message(client,None,SimpleNamespace(topic=topic,payload=payload,retain=retain))


def take(outbox):
    values=[]
    while True:
        try:values.append(outbox.queue.get_nowait())
        except queue.Empty:return values


def test_fixed_subscription_and_original_binary_payload_are_forwarded_without_publish_or_stdout(monkeypatch):
    outbox=remote.Outbox();client=Client();remote.mqtt_callbacks(client,outbox,threading.Event())
    monkeypatch.setattr(remote,'write_record',lambda *_: (_ for _ in ()).throw(AssertionError('callback wrote stdout')))
    client.on_connect(client,None,None,0)
    assert client.subscriptions==[[(topic,0) for topic in remote.TOPICS]]
    receive(client);row=take(outbox)[0]
    assert row==dict(kind='mqtt',topic='xenova/v1/world/snapshot.pb',payload_base64='AP8=')
    receive(client,retain=True);receive(client,topic='xenova/v1/vehicle/control',payload=SECRET.encode())
    assert take(outbox)==[]


def test_callback_queue_is_bounded_latest_drop_and_retained_never_becomes_live():
    outbox=remote.Outbox();client=Client();remote.mqtt_callbacks(client,outbox,threading.Event())
    for sequence in range(40):receive(client,payload=bytes([sequence]))
    rows=take(outbox)
    assert len(rows)==32 and outbox.dropped==8
    assert remote.base64.b64decode(rows[0]['payload_base64'])==bytes([8])
    assert remote.base64.b64decode(rows[-1]['payload_base64'])==bytes([39])
    receive(client,payload=b'x'*32769)
    assert take(outbox)==[dict(kind='error',source='mqtt',reason='MQTT_OBSERVATION_SIZE')]


def test_failed_connect_or_subscribe_and_disconnect_emit_only_fixed_diagnostics():
    outbox=remote.Outbox();client=Client();stopped=threading.Event();remote.mqtt_callbacks(client,outbox,stopped)
    client.on_connect(client,None,None,5)
    client.on_subscribe(client,None,1,[128])
    client.on_connect_fail(client,None)
    client.on_disconnect(client,None,1)
    reasons=[row['reason'] for row in take(outbox)]
    assert reasons==['MQTT_CONNECT_UNAVAILABLE','MQTT_SUBSCRIBE_UNAVAILABLE','MQTT_CONNECT_UNAVAILABLE','MQTT_DISCONNECTED']
    assert client.subscriptions==[]
    stopped.set();client.on_disconnect(client,None,0);assert take(outbox)==[]


class Operator:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    def call(self,body):
        request(body);self.calls.append(body)
        if self.fail==body['action']:raise RuntimeError(SECRET)
        if body['action']=='status':return dict(version=2,ok=False,error=dict(code='UNAVAILABLE'))
        return dict(version=2,ok=True,data=dict(contract='operation-status-1',supported=False))


def test_operator_uses_two_verified_read_actions_and_preserves_okfalse_original_response():
    client=Operator();outbox=remote.Outbox();remote._operator_once(client,outbox);rows=take(outbox)
    assert client.calls==[dict(version=2,action='status'),dict(version=2,action='operation.status')]
    assert rows[0]==dict(kind='error',source='operation',reason='GATEWAY_STATUS_UNAVAILABLE')
    assert rows[1]['kind']=='operator' and rows[1]['status']==dict(version=2,ok=False,error=dict(code='UNAVAILABLE'))
    assert rows[1]['operation_status']['ok'] is True


def test_secondary_operation_error_does_not_mask_base_status_and_never_serializes_exception():
    client=Operator(fail='operation.status');outbox=remote.Outbox();remote._operator_once(client,outbox)
    rows=take(outbox)
    assert dict(kind='error',source='operation_detail',reason='GATEWAY_OPERATION_STATUS_UNAVAILABLE') in rows
    record=next(row for row in rows if row['kind']=='operator')
    assert record['status']['error']['code']=='UNAVAILABLE' and record['operation_status'] is None
    stream=io.StringIO()
    for row in rows:remote.write_record(stream,row)
    assert SECRET not in stream.getvalue() and 'RuntimeError' not in stream.getvalue()
    assert all(json.loads(line)['kind'] in ('operator','error') for line in stream.getvalue().splitlines())


def test_operator_worker_polls_at_most_once_per_second_with_no_commands(monkeypatch):
    module=importlib.import_module('xenova_gateway.operator_client');client=Operator();created=[]
    def factory(**kwargs):created.append(kwargs);return client
    monkeypatch.setattr(module,'OperatorClient',factory)
    class Provider:
        def context(self):raise RuntimeError('fixture provider unavailable')
    monkeypatch.setattr(importlib.import_module('xenova_gateway.client'),'GatewayClient',lambda *a,**k:Provider())
    ticks=iter((100.,100.2));monkeypatch.setattr(remote.time,'monotonic',lambda:next(ticks))
    class Stop:
        def __init__(self):self.delays=[]
        def is_set(self):return False
        def wait(self,delay):self.delays.append(delay);return True
    stopped=Stop();remote.operator_worker('/run/xenova-gateway/operator.sock',remote.Outbox(),stopped)
    assert len(client.calls)==2 and stopped.delays==[pytest.approx(.8)]
    assert created==[dict(endpoint='/run/xenova-gateway/operator.sock',wait_ms=500,uid=os.geteuid())]


def test_source_namespace_compatibility_with_both_existing_tool_import_styles():
    root=Path(__file__).resolve().parents[5]
    for path in (root/'vehicle/Jetson',root/'vehicle/Jetson/tools'):
        if str(path) not in remote.sys.path:remote.sys.path.insert(0,str(path))
    for name in ('tools.live_observer.sources','live_observer.sources'):
        module=importlib.import_module(name)
        assert module.decode_mqtt('xenova/v1/vehicle/control',b'no command input') is None


def test_basic_gateway_context_remains_visible_when_optional_operator_is_absent(monkeypatch):
    class Provider:
        def __init__(self):self.reads=0
        def context(self):self.reads+=1;return dict(mode='SAFE_STOP',owner='original-owner',generation=1)
    provider=Provider();outbox=remote.Outbox();original=dict(observer_epoch='diagnostic-observer',sequence=0)
    monkeypatch.setattr(remote.os.path,'exists',lambda _:False)
    remote._operator_once(Operator(),outbox,provider=provider,observation=original)
    rows=take(outbox)
    assert provider.reads==1 and len(rows)==1
    assert rows[0]['kind']=='gateway_context' and rows[0]['operator_interface']=='ABSENT'
    assert rows[0]['context']==dict(mode='SAFE_STOP',owner='original-owner',generation=1)
    assert rows[0]['observation']['sequence']==1
    assert 'pi_now_ms' not in rows[0]['context']


def test_unreachable_operator_is_distinct_from_unreachable_gateway(monkeypatch):
    class Broken:
        def context(self):raise RuntimeError(SECRET)
    outbox=remote.Outbox()
    remote._operator_once(Operator(),outbox,provider=Broken(),observation=dict(observer_epoch='observer',sequence=0))
    rows=take(outbox)
    assert any(row.get('reason')=='GATEWAY_CONTEXT_UNAVAILABLE' for row in rows)
    assert not any(row['kind']=='gateway_context' for row in rows)
    assert SECRET not in json.dumps(rows)


def test_available_operator_does_not_query_the_basic_context_fallback():
    class Available:
        def call(self,body):return dict(version=2,ok=True,data=dict(original_action=body['action']))
    class Unused:
        def context(self):raise AssertionError('operator response already available')
    outbox=remote.Outbox()
    remote._operator_once(Available(),outbox,provider=Unused(),observation={})
    rows=take(outbox)
    assert len(rows)==1 and rows[0]['kind']=='operator'
