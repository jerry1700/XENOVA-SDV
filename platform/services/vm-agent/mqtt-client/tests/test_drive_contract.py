"""The new readiness/topic profile is independent of v2 and legacy v1."""
import os
from types import SimpleNamespace
import pytest
from xenova_mqtt import Identity,topics,policy,EnvelopeError,drive_binding
from xenova_mqtt.envelope import build,validate
from xenova_mqtt.readiness import parse
from xenova_mqtt.drive_binding import BindingCache,BindingFile,atomic_json
from xenova_mqtt.client import PublishTicket

VM='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
HOST='11111111-1111-4111-8111-111111111111'
BOOT='22222222-2222-4222-8222-222222222222'
INSTANCE='33333333-3333-4333-8333-333333333333'

def test_readiness_policy_and_topic_envelope_source_binding():
    topic=topics.vm_app_events(VM,'manual');identity=Identity(vm_id=VM,app_id='manual')
    value=build(identity,'app.readiness',{'applied_generation':1,'progress_seq':'1','readiness':'READY','reason':None},1)
    validate(topic,value)
    for bad in ({**value,'vm_id':OTHER},{**value,'app_id':'autonomous'}):
        with pytest.raises(EnvelopeError):validate(topic,bad)
    assert topics.parse(topic).vm_id==VM and topics.parse(topic).app_id=='manual'
    p=policy.for_type('app.readiness')
    assert p.qos==0 and not p.retain
    assert parse(value['payload']).progress==1

@pytest.mark.parametrize('field,bad',[('progress_seq',1),('progress_seq','01'),('progress_seq','18446744073709551616'),
    ('applied_generation',True),('applied_generation',0),('readiness','unknown'),('reason','unknown')])
def test_readiness_rejects_distorted_values(field,bad):
    payload={'applied_generation':42,'progress_seq':'18446744073709551615','readiness':'READY','reason':None}
    assert parse(payload).progress==2**64-1
    with pytest.raises(ValueError):parse({**payload,field:bad})

def binding():
    return {'schema_version':'1.0-drive-bindings','host_boot_id':HOST,'observed_monotonic_ms':1000,
        'expires_monotonic_ms':4000,'vms':{VM:{'status':'observed','execution_id':OTHER,'boot_id':BOOT,
        'app_instance_id':INSTANCE,'domid':1,'intent_revision':2,'lifecycle_generation':0,
        'xen_unique_id':str(2**64-1),'observed_monotonic_ms':1000}}}

def test_binding_loss_expiry_old_host_and_old_instance_fail_closed(tmp_path):
    path=tmp_path/'binding.json';atomic_json(path,binding())
    c=BindingFile(path,HOST,uid=os.geteuid());c.refresh()
    assert c.accepts(VM,BOOT,INSTANCE,1.1) and not c.accepts(VM,BOOT,OTHER,1.1)
    assert c.current(VM,4.001) is None
    with pytest.raises(ValueError):c.install({**binding(),'host_boot_id':OTHER})
    assert c.current(VM,1.1) is None
    c.install(binding());path.write_text('{broken');c.refresh();assert c.current(VM,1.1) is None

def test_binding_file_parses_once_per_version_without_renewing_lease(tmp_path,monkeypatch):
    path=tmp_path/'binding.json'
    atomic_json(path,binding())
    clock=[1.1]
    cache=BindingFile(path,HOST,clock=lambda:clock[0],uid=os.geteuid())
    load=drive_binding.json.load
    reads=[]

    def counted(stream):
        reads.append(stream.fileno())
        return load(stream)

    monkeypatch.setattr(drive_binding.json,'load',counted)
    for _ in range(10):
        cache.refresh()
        assert cache.current(VM)['execution_id']==OTHER
    clock[0]=4.001
    cache.refresh()
    assert len(reads)==1 and cache.current(VM) is None

    old=path.stat()
    updated={**binding(),'observed_monotonic_ms':4000,'expires_monotonic_ms':7000}
    updated['vms'][VM].update(execution_id=VM,observed_monotonic_ms=4000)
    atomic_json(path,updated)
    os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
    cache.refresh()
    assert len(reads)==2 and cache.current(VM)['execution_id']==VM


@pytest.mark.parametrize('problem',['permission','missing','symlink','malformed'])
def test_binding_file_invalidates_and_recovers_from_path_or_content_changes(tmp_path,problem):
    path=tmp_path/'binding.json'
    atomic_json(path,binding())
    cache=BindingFile(path,HOST,uid=os.geteuid())
    cache.refresh()
    assert cache.current(VM,1.1) is not None

    if problem=='permission':
        path.chmod(0o644)
    elif problem=='missing':
        path.unlink()
    elif problem=='symlink':
        original=path.rename(tmp_path/'original.json')
        path.symlink_to(original)
    else:
        bad=binding()
        del bad['vms'][VM]['execution_id']
        atomic_json(path,bad)
    cache.refresh()
    assert cache.current(VM,1.1) is None

    if problem=='permission':
        path.chmod(0o600)
    else:
        atomic_json(path,binding())
    cache.refresh()
    assert cache.current(VM,1.1)['execution_id']==OTHER


def test_publish_ticket_distinguishes_submission_puback_and_disconnect():
    client=SimpleNamespace(connected=True,connections=1,_generation=0)
    info=SimpleNamespace(rc=0,is_published=lambda:False)
    ticket=PublishTicket({},info,1,0);assert ticket.status(client)=='pending'
    info.is_published=lambda:True;assert ticket.status(client)=='acknowledged'
    client.connections=2;assert ticket.status(client)=='disconnected'
    client.connections=1;info.rc=7;assert ticket.status(client)=='failed'
