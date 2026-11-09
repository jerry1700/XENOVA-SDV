"""F02 제어의 실제 순수 controller/core 회귀. MQTT/JIO·차량 적용 수락은 아니다."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import sys
import types
import unittest
from uuid import UUID

from xenova_vehicle.transport.world_egress_fault import WorldEgressFault
from xenova_vehicle.transport.world_fault_control import WorldFaultController, MAX_BYTES, MAX_RECORDS, MAX_US, RECOVERY_RECORDS


def uid(number):
    return str(UUID(int=number))


def wire(kind, payload, *, number=100, stamp='2026-11-09T09:00:00.000Z', seq=1):
    return json.dumps(dict(schema_version='1.0',message_id=uid(number),source_id='dom0-server',
        boot_id=uid(200),agent_instance_id=uid(201),type='perception.fault.'+kind,
        seq=seq,sent_at=stamp,payload=payload),separators=(',',':')).encode('ascii')


def payload(publication):
    return json.loads(publication.raw)['payload']


def load_common_mqtt_contract():
    """실제 topics/envelope만 별칭으로 읽는다. package __init__/Paho는 실행하지 않는다.

    Main 시험은 반환 modules를 patch.dict(sys.modules, ...)의 짧은 범위에 사용해
    실제 JIO import도 같은 원 함수로 연결할 수 있다. 함수·클라이언트를 stub하지 않는다.
    """
    base=Path(__file__).resolve().parents[4]/'platform/services/vm-agent/mqtt-client/xenova_mqtt'
    name='_f02_pure_mqtt_contract'
    package=types.ModuleType(name);package.__path__=[str(base)]
    sys.modules[name]=package
    loaded={};pins={}
    for leaf in ('topics','envelope'):
        path=base/(leaf+'.py');pins[leaf]=sha256(path.read_bytes()).hexdigest()
        spec=importlib.util.spec_from_file_location(name+'.'+leaf,path)
        module=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=module;spec.loader.exec_module(module)
        setattr(package,leaf,module);loaded[leaf]=module
    return types.SimpleNamespace(envelope=loaded['envelope'],topics=loaded['topics'],sha256=pins,
        modules={'xenova_mqtt':package,'xenova_mqtt.topics':loaded['topics'],'xenova_mqtt.envelope':loaded['envelope']})


@dataclass(frozen=True)
class Original:
    raw: bytes
    expires_us: int


class WorldFaultControlTests(unittest.TestCase):
    def setUp(self):
        self.core=WorldEgressFault();self.calls=[];self.fail=False
        def apply(mode, *, delay_us, now_us):
            self.calls.append((mode,delay_us,now_us))
            if self.fail:raise RuntimeError('declared callback failure')
            self.core.configure(mode,delay_us=delay_us,now_us=now_us)
        self.wall=datetime(2026,11,9,9,tzinfo=timezone.utc)
        self.control=WorldFaultController('sim-nn',uid(1),apply,boot_id=uid(2),utc_clock=lambda:self.wall)
        self.control.set_source('a'*64,now_us=0)

    def command(self, number=10, *, action='SET', mode='DELAY', delay=100, duration=5,
                window=None, active=None, revision=None):
        snap=self.control.snapshot()
        data=dict(command_id=uid(number),service_instance=uid(1),window_id=window or snap['window_id'],action=action)
        if action=='SET':data.update(mode=mode,delay_ms=delay,duration_s=duration)
        else:data.update(active_command_id=active or snap['active_command_id'],
            expected_revision=str(snap['revision']) if revision is None else revision)
        return wire('command',data,number=number+1000)

    def send(self, raw, now=0, **options):
        return self.control.handle(self.control.command_topic,raw,qos=options.get('qos',1),
            retained=options.get('retained',False),now_us=now)

    def query(self, action='STATUS', *, key=None, now=0):
        data=dict(action=action)
        if key is not None:data['command_id']=key
        return self.control.handle(self.control.query_topic,wire('query',data),qos=1,retained=False,now_us=now)

    def test_status_repoll_keeps_original_window_E_and_never_configures_core(self):
        first=payload(self.query());original=self.control.snapshot()
        later=payload(self.query(now=1_000_000))
        self.assertEqual(first['window_id'],later['window_id'])
        self.assertEqual(self.control.snapshot()['window_expires_us'],original['window_expires_us'])
        self.assertEqual(later['window_remaining_ms'],1000)
        self.assertEqual(self.calls,[])
        self.assertNotEqual(payload(self.query(now=2_000_000))['window_id'],first['window_id'])

    def test_exact_window_boundary_rejects_new_command_without_UTC_permission(self):
        raw=self.command()
        result=self.send(raw,2_000_000)
        self.assertEqual(payload(result)['reason'],'WINDOW_EXPIRED_OR_MISMATCH')
        self.assertEqual(self.calls,[])

    def test_applied_only_after_real_callback_and_duplicate_reconnect_never_renews(self):
        raw=self.command(duration=1)
        applied=self.send(raw,100)
        self.assertEqual(payload(applied)['status'],'APPLIED')
        self.assertEqual(self.calls,[('DELAY',100000,100)])
        deadline=self.control.snapshot()['deadline_us']
        self.assertIs(self.send(raw,200),applied)
        self.assertIs(self.query('RESULT',key=uid(10),now=300),applied)
        self.assertEqual(self.control.snapshot()['deadline_us'],deadline)
        self.assertEqual(len(self.calls),1)
        changed=raw.replace(b'"duration_s":1',b'"duration_s":2')
        self.assertEqual(payload(self.send(changed,400))['reason'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.control.snapshot()['deadline_us'],deadline)

    def test_query_unknown_key_is_unknown_not_a_new_command(self):
        result=self.query('RESULT',key=uid(999))
        self.assertEqual(payload(result)['status'],'UNKNOWN')
        self.assertEqual(payload(result)['reason'],'NOT_FOUND')
        self.assertEqual(self.control.snapshot()['cache_size'],0)
        self.assertEqual(self.calls,[])

    def test_clear_discards_both_slots_and_late_old_clear_cannot_clear_new_SET(self):
        self.send(self.command(),0)
        self.core.offer(Original(b'first',9_000_000),now_us=1)
        self.core.offer(Original(b'second',9_000_000),now_us=2)
        clear=self.command(11,action='CLEAR')
        cleared=self.send(clear,3)
        self.assertEqual(payload(cleared)['status'],'APPLIED')
        self.assertEqual(payload(cleared)['mode'],'PASS')
        self.assertFalse(self.core.snapshot()['held']);self.assertFalse(self.core.snapshot()['pending'])
        self.assertIs(self.send(clear,4),cleared)
        self.send(self.command(12,mode='BLOCK',delay=0),5)
        late=self.command(13,action='CLEAR',active=uid(10),revision='1')
        self.assertEqual(payload(self.send(late,6))['reason'],'ACTIVE_MISMATCH')
        self.assertEqual(self.control.snapshot()['active_command_id'],uid(12))

    def test_busy_new_SET_is_terminal_rejection_and_does_not_replace_active(self):
        self.send(self.command(),0)
        second=self.command(11,mode='BLOCK',delay=0)
        rejected=self.send(second,1)
        self.assertEqual(payload(rejected)['reason'],'BUSY')
        self.send(self.command(12,action='CLEAR'),2)
        self.assertIs(self.send(second,3),rejected)
        self.assertIsNone(self.control.snapshot()['active_command_id'])

    def test_expiry_is_original_monotonic_deadline_and_history_is_not_current_state(self):
        raw=self.command(duration=1)
        original=self.send(raw,100)
        self.core.offer(Original(b'held',9_000_000),now_us=101)
        self.assertIsNone(self.control.tick(now_us=1_000_099))
        ended=self.control.tick(now_us=1_000_100)
        self.assertEqual(payload(ended)['status'],'ENDED')
        self.assertEqual(payload(ended)['reason'],'DURATION_ELAPSED')
        self.assertIsNone(self.control.tick(now_us=1_000_101))
        self.assertEqual(self.calls[-1],('PASS',0,1_000_100))
        self.assertIs(self.send(raw,1_000_102),original)
        current=payload(self.query(now=1_000_103))
        self.assertEqual(current['mode'],'PASS');self.assertIsNone(current['active_command_id'])
        self.assertFalse(self.core.snapshot()['held'])

    def test_source_changes_invalidate_window_but_keep_applied_S_setting_and_original_E(self):
        raw=self.command(duration=120);applied=self.send(raw,100)
        before=self.control.snapshot();calls=list(self.calls)
        self.control.set_source('b'*64,now_us=200)
        after=self.control.snapshot()
        self.assertNotEqual(after['window_id'],before['window_id'])
        self.assertEqual(after['deadline_us'],before['deadline_us'])
        self.assertEqual(after['revision'],before['revision'])
        self.assertEqual(after['active_command_id'],before['active_command_id'])
        self.assertEqual(self.calls,calls)
        self.assertIs(self.send(raw,201),applied)
        self.control.set_source(None,now_us=202)
        self.assertEqual(self.calls,calls)
        self.assertEqual(payload(self.send(self.command(20),203))['reason'],'SOURCE_UNAVAILABLE')
        cleared=self.send(self.command(21,action='CLEAR'),204)
        self.assertEqual(payload(cleared)['status'],'APPLIED')
        self.assertEqual(self.calls[-1],('PASS',0,204))

    def test_same_source_status_and_UTC_regression_do_not_rotate_or_extend_window(self):
        first=self.control.snapshot()
        self.assertIsNone(self.control.set_source('a'*64,now_us=10))
        self.query(now=11)
        self.assertEqual(self.control.snapshot()['window_id'],first['window_id'])
        message=json.loads(self.command())
        message['sent_at']='1900-01-01T00:00:00Z'
        old_utc=json.dumps(message,separators=(',',':')).encode()
        # UTC 송신 진단이 과거여도 실제 접수는 receiver 원2초 window로만 판단한다.
        self.assertEqual(payload(self.send(old_utc,2_000_000))['reason'],'WINDOW_EXPIRED_OR_MISMATCH')

    def test_retained_wrong_qos_topic_and_malformed_inputs_never_apply(self):
        raw=self.command()
        self.assertIsNone(self.send(raw,retained=True))
        for qos in (0,2,True):self.assertIsNone(self.send(raw,qos=qos))
        self.assertIsNone(self.control.handle('xenova/v1/vms/wrong/command',raw,qos=1,retained=False,now_us=0))
        for bad in (b'{',b'{"x":NaN}',raw+b' '*(MAX_BYTES+1),raw.replace(b'"schema_version":"1.0"',b'"schema_version":"1.0","schema_version":"1.0"')):
            self.assertIsNone(self.send(bad))
        self.assertEqual(self.calls,[])

    def test_exact_closed_parameters_and_service_id_are_checked(self):
        for index,changes in enumerate(({'mode':'PASS'},{'delay_ms':True},{'delay_ms':2001},
                {'duration_s':0},{'duration_s':121},{'extra':1},{'service_instance':uid(55)})):
            message=json.loads(self.command(40+index));message['payload'].update(changes)
            result=self.send(json.dumps(message,separators=(',',':')).encode())
            self.assertEqual(payload(result)['status'],'REJECTED')
        self.assertEqual(self.calls,[])

    def test_cache_full_never_evicts_SET_but_current_clear_has_reserved_capacity(self):
        raw=self.command(duration=120);original=self.send(raw)
        for number in range(100,100+MAX_RECORDS-1):
            self.assertEqual(payload(self.send(self.command(number)))['reason'],'BUSY')
        self.assertEqual(self.control.snapshot()['cache_size'],MAX_RECORDS)
        self.assertIs(self.send(raw),original)
        clear=self.command(500,action='CLEAR')
        applied=self.send(clear)
        self.assertEqual(payload(applied)['status'],'APPLIED')
        self.assertEqual(self.control.snapshot()['clear_size'],1)
        self.assertIs(self.send(clear),applied)
        self.assertEqual(payload(self.send(self.command(501)))['reason'],'CACHE_FULL')

    def test_callback_failure_is_unknown_never_applied_or_retried_by_same_key(self):
        self.fail=True;raw=self.command(duration=1)
        result=self.send(raw)
        self.assertEqual(payload(result)['status'],'UNKNOWN')
        self.assertFalse(payload(result)['applied_known'])
        self.assertIs(self.send(raw),result);self.assertEqual(len(self.calls),1)
        self.fail=False
        self.control.tick(now_us=1_000_000)
        self.assertEqual(self.calls[-1],('PASS',0,1_000_000))

    def test_failed_clear_reserved_record_is_bounded_and_not_reapplied_under_new_keys(self):
        self.send(self.command())
        self.fail=True
        raw=self.command(11,action='CLEAR');result=self.send(raw)
        self.assertEqual(payload(result)['status'],'UNKNOWN')
        self.assertIs(self.send(raw),result)
        self.assertEqual(len(self.calls),2)
        self.fail=False
        recovered=self.send(self.command(12,action='CLEAR'))
        self.assertEqual(payload(recovered)['status'],'APPLIED')
        self.assertEqual(self.control.snapshot()['clear_size'],1)
        self.assertEqual(self.control.snapshot()['recovery_size'],1)
        self.assertIs(self.send(raw),result)
        self.assertEqual(len(self.calls),3)

    def test_expiry_failure_is_one_shot_unknown_and_fresh_clear_can_recover_at_full_cache(self):
        self.send(self.command(duration=1))
        for number in range(100,100+MAX_RECORDS-1):self.send(self.command(number))
        self.fail=True
        failed=self.control.tick(now_us=1_000_000)
        original=self.control.snapshot()
        self.assertEqual(payload(failed)['status'],'UNKNOWN')
        self.assertEqual(payload(failed)['mode'],'DELAY')
        for now in (1_000_001,1_002_000,1_100_000):
            self.assertIsNone(self.control.tick(now_us=now))
            self.query(now=now)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.control.snapshot()['deadline_us'],original['deadline_us'])
        self.assertEqual(self.control.snapshot()['revision'],original['revision'])
        self.fail=False
        recovered=self.send(self.command(500,action='CLEAR'),1_100_001)
        self.assertEqual(payload(recovered)['status'],'APPLIED')
        self.assertEqual(payload(recovered)['mode'],'PASS')

    def test_persistent_callback_failure_exhausts_only_fixed_recovery_reserve(self):
        self.send(self.command())
        self.fail=True
        for number in range(20,20+RECOVERY_RECORDS):
            self.assertEqual(payload(self.send(self.command(number,action='CLEAR')))['status'],'UNKNOWN')
        count=len(self.calls)
        result=self.send(self.command(99,action='CLEAR'))
        self.assertEqual(payload(result)['reason'],'RECOVERY_CACHE_FULL')
        self.assertEqual(payload(result)['status'],'UNKNOWN')
        self.assertEqual(len(self.calls),count)
        self.assertEqual(self.control.snapshot()['recovery_size'],RECOVERY_RECORDS)

    def test_monotonic_regression_is_rejected_and_wire_cannot_manufacture_J_time(self):
        self.query(now=100)
        with self.assertRaises(ValueError):self.query(now=99)
        for source in (True,'A'*64,'abc'):
            with self.assertRaises(ValueError):self.control.set_source(source,now_us=100)
        self.assertEqual(self.calls,[])

    def test_common_validator_accepts_request_result_status_and_UTC_is_diagnostic_only(self):
        validate=load_common_mqtt_contract().envelope.validate
        raw=self.command()
        message=json.loads(raw);message['sent_at']='2026-11-09T09:00:00.123456789Z'
        raw=json.dumps(message,separators=(',',':')).encode()
        validate(self.control.command_topic,json.loads(raw))
        result=self.send(raw)
        validate(result.topic,json.loads(result.raw))
        self.wall=datetime(1900,1,1,tzinfo=timezone.utc)
        self.assertIs(self.send(raw,100),result)
        status=self.query(now=100)
        validate(status.topic,json.loads(status.raw))
        self.assertEqual(json.loads(result.raw)['sent_at'],'2026-11-09T09:00:00.000Z')
        self.assertEqual(json.loads(status.raw)['sent_at'],'1900-01-01T00:00:00.000Z')
        invalid=json.loads(self.command(99));invalid['sent_at']=None
        self.assertIsNone(self.send(json.dumps(invalid).encode(),100))
        for bad in ('Upper','dot.name','a'*33):
            with self.assertRaises(ValueError):WorldFaultController(bad,uid(1),lambda *a,**k:None,boot_id=uid(2))

    def test_document_request_examples_match_common_and_closed_payloads_with_current_target_echo(self):
        path=Path(__file__).resolve().parents[4]/'docs/contracts/jetson-perception-fault-control-v1.md'
        validate=load_common_mqtt_contract().envelope.validate
        messages=[json.loads(text) for text in re.findall(r'```json\n(.*?)\n```',path.read_text(encoding='utf-8'),re.S)]
        count=0
        for message in messages:
            if 'schema_version' not in message:continue
            query=message['type']=='perception.fault.query'
            topic=self.control.query_topic if query else self.control.command_topic
            raw=json.dumps(message,separators=(',',':')).encode('ascii')
            self.assertLessEqual(len(raw),MAX_BYTES)
            validate(topic,message);self.control._decode(raw,message['type'])
            if not query:
                message['payload']['service_instance']=self.control.service_instance
                message['payload']['window_id']=self.control.snapshot()['window_id']
                raw=json.dumps(message,separators=(',',':')).encode('ascii')
            result=self.control.handle(topic,raw,qos=1,retained=False,now_us=0)
            self.assertIsNotNone(result)
            validate(result.topic,json.loads(result.raw))
            if not query:self.assertEqual(payload(result)['status'],'APPLIED')
            count+=1
        self.assertEqual(count,4)

    def test_closed_maximum_vectors_fit_both_payload_and_MQTT_remaining_caps(self):
        control=WorldFaultController('d'*32,uid(1),lambda *a,**k:None,boot_id=uid(2))
        control.set_source('f'*64,now_us=0)
        request=wire('command',dict(command_id=uid(10),service_instance=uid(1),
            window_id=control.snapshot()['window_id'],action='SET',mode='DELAY',delay_ms=2000,duration_s=120),
            stamp='9999-12-31T23:59:59.999999999Z',seq=MAX_US)
        self.assertLessEqual(len(request),MAX_BYTES)
        # 1024B 내 공백도 원 bytes 지문에 포함한다. topic+packet ID까지 packet cap으로 센다.
        padded=request+b' '*(MAX_BYTES-len(request))
        self.assertIsNotNone(control.handle(control.command_topic,padded,qos=1,retained=False,now_us=0))
        self.assertLessEqual(2+len(control.command_topic.encode())+2+len(padded),1280)
        control._revision=MAX_US;control._seq=MAX_US-1;control._known=False;control._deadline=MAX_US
        maximum=control._result(uid(10),padded,'REJECTED','WINDOW_EXPIRED_OR_MISMATCH',MAX_US-120_000_000,query_id=uid(11))
        self.assertEqual(len(maximum.raw),818)
        self.assertLessEqual(len(maximum.raw),MAX_BYTES)
        control._seq=MAX_US-1;control._window_end=MAX_US-118_000_000;control._deadline=MAX_US
        control._source=None
        original=next(iter(control._rows.values()))
        control._rows={str(i):original for i in range(MAX_RECORDS)}
        status=control.status(now_us=MAX_US-120_000_000)
        self.assertEqual(len(status.raw),745)
        self.assertLessEqual(len(status.raw),MAX_BYTES)


if __name__=='__main__':unittest.main()
