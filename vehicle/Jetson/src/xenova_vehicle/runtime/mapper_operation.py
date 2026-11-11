"""명시 운영 요청과 Pi의 일회성 진입 permit을 분리한다.

이 local consumer는 이동·Scene 권한을 발급하지 않는다. 원 Probe/returned lease와
실제로 수락한 Vehicle을 대조하며 시계, 센서 history, parser를 소유하지 않는다.
Pi gate는 STANDBY 전이 때만 검산한다. 수락 설정은 같은 S에서 명시 RUN까지 유지하고
gate100ms E와 request transaction E를 설정의 수명으로 사용하지 않는다.
구 common은 RUN/미지원이며 Reply20도 생략한다. 새 Gateway는 실제 capability1
Reply를 수락하기 전 Probe14를 생략해야 한다. 새 M/common/Gateway의 coherent 배포가
필요하며 구 Gateway의 새 Reply 미지원은 unknown field 제거로 우회하지 않는다.
"""

from dataclasses import dataclass
from collections import OrderedDict
from hashlib import sha256
import json
import threading
from uuid import UUID

from .operation_request import (OperationRequest, OperationRequestError, CONFIG_PROFILE, CONFIG_ACK_PROFILE,
    RESULT_PROFILE, STATUS_PROFILE, CONSUMER_READY_PROFILE, TRANSACTION_US, canonical, configuration, operation_result, result_fields, _uuid)


def mapper_operation_supported():
    """설치된 공용 descriptor/helper의 capability다. READY나 현재 gate가 아니다."""
    try:
        from xenova_drive_world import schema
        from xenova_drive_world import operations
    except ImportError:
        return False
    gate = getattr(schema, 'PiOperationGate', None)
    if gate is None or getattr(operations, 'MAPPER_OPERATION', None) != 1 or not callable(getattr(operations, 'validate_one', None)):
        return False
    # B09 원 9필드만 읽는다. 뒤의 optional route 확장은 이 capability와 무관하다.
    expected = {'gateway_instance': (1, 12), 'control_generation': (2, 13),
        'operation_revision': (3, 4), 'mode': (4, 13), 'wait_confirmed': (5, 8),
        'manual_local_admitted': (6, 8), 'vehicle_sequence': (7, 4),
        'stop_command_sequence': (8, 4), 'valid_until_pi_us': (9, 4)}
    fields = gate.DESCRIPTOR.fields_by_name
    if set(fields) != set(expected) or any(
            (fields[key].number, fields[key].type, fields[key].label) != (*value, 2)
            for key, value in expected.items()):
        return False
    probe = schema.MapperClockProbe.DESCRIPTOR.fields_by_name.get('operation_gate')
    reply = schema.MapperClockReply.DESCRIPTOR.fields_by_name.get('supported_mapper_features')
    return bool(probe is not None and (probe.number, probe.type, probe.label) == (14, 11, 1)
        and probe.message_type is gate.DESCRIPTOR and reply is not None
        and (reply.number, reply.type, reply.label) == (20, 13, 1))


@dataclass(frozen=True, slots=True)
class MapperOperationDecision:
    supported: bool
    standby: bool
    reason: str
    probe_bytes: bytes = b''
    gate_bytes: bytes = b''
    vehicle_admission: object = None
    expires_j_us: int | None = None
    control_generation: int | None = None
    operation_revision: int | None = None


class MapperOperationGate:
    """M-main 전용 current1 판정기다. 원 source/Gateway 안의 단조 head만 보존한다."""

    def __init__(self):
        self.supported = mapper_operation_supported()
        self._identity = self._probe_client = self._probe_head = self._operation_head = None

    def observe(self, original_probe, returned_lease, *, sensor_state, inputs, origin, now_us):
        """원 ingest의 진입 permit만 소비한다. 부재·만료는 기존 설정을 바꾸지 않는다."""
        if type(now_us) is not int or not 0 <= now_us < 2**63:
            raise ValueError('original actual J time required')
        run = lambda reason: MapperOperationDecision(self.supported, False, reason)
        if not self.supported:
            return run('UNSUPPORTED_OPERATION_SCHEMA')
        if returned_lease is None or inputs is None or sensor_state is None:
            return run('OPERATION_CLOCK_OR_VEHICLE_UNAVAILABLE')
        from xenova_drive_world import decode, encode, schema
        from xenova_drive_world.mapper_clock import ClockLease
        from xenova_drive_world.operations import validate_one
        from .mapper_sensor_state import MapperSensorState, MetricInputs
        from ..storage.contracts import canonical
        if (type(sensor_state) is not MapperSensorState or type(inputs) is not MetricInputs
                or type(returned_lease) is not ClockLease or inputs.clock_lease is not returned_lease
                or type(original_probe) is not bytes or not 0 < len(original_probe) <= 1024
                or returned_lease.raw_probe_bytes != original_probe):
            return run('OPERATION_ORIGINAL_PROBE_REQUIRED')
        try:
            probe = decode(original_probe, schema.MapperClockProbe)
            if (type(origin) is not dict or set(origin) != {'pi_host_boot', 'gateway_instance', 'device_session', 'pi_session'}
                    or inputs.source != returned_lease.source or probe.pi_host_boot != origin['pi_host_boot']
                    or returned_lease.pi_host_boot != origin['pi_host_boot']
                    or probe.vehicle != inputs.source['vehicle'] or probe.device != inputs.source['device']):
                return run('OPERATION_SOURCE_MISMATCH')
            returned_lease.pi_interval_to_j((probe.pi_send_us, probe.pi_send_us), now_us)
            identity = canonical(dict(source=inputs.source, origin=origin))
            if identity != self._identity:
                self._identity, self._probe_head, self._operation_head = identity, None, None
                self._probe_client = None
            if probe.pi_client_instance != self._probe_client:
                self._probe_client, self._probe_head = probe.pi_client_instance, None
            head = (probe.probe_seq, sha256(original_probe).hexdigest())
            if self._probe_head is not None and (head[0] < self._probe_head[0]
                    or head[0] == self._probe_head[0] and head != self._probe_head):
                return run('OPERATION_OLD_OR_CONFLICTING_PROBE')
            self._probe_head = head
            if not probe.HasField('operation_gate'):
                return run('OPERATION_GATE_ABSENT')
            gate = probe.operation_gate
            validate_one(gate)
            if gate.gateway_instance != UUID(origin['gateway_instance']).bytes:
                return run('OPERATION_GATEWAY_MISMATCH')
            operation = gate.control_generation, gate.operation_revision
            if self._operation_head is not None and any(a < b for a, b in zip(operation, self._operation_head)):
                return run('OPERATION_REVISION_REGRESSED')
            self._operation_head = operation
            if not (gate.wait_confirmed or gate.mode == 2 and gate.manual_local_admitted):
                return run('OPERATION_GATE_NOT_PERMITTING')
            # 원 seq를 최신 Vehicle로 대체하지 않는다. 둘 다 원 lease 안에서 살아 있어야 한다.
            if inputs.vehicle is None or not sensor_state.sensors.current_vehicle(inputs.vehicle, now_us):
                return run('OPERATION_CURRENT_VEHICLE_UNAVAILABLE')
            record = next((item for item in sensor_state.vehicle_admissions
                if item.evidence.anchor.sequence == gate.vehicle_sequence), None)
            if record is None:
                return run('OPERATION_ORIGINAL_VEHICLE_UNAVAILABLE')
            evidence = sensor_state.validate_vehicle_admission(record)
            sample = evidence.message()
            if (not record.received_j_us <= record.admitted_j_us <= now_us
                    or evidence.wire_sha256 != sha256(evidence.wire).hexdigest()
                    or evidence.clock_lease.source != inputs.source
                    or evidence.clock_lease.pi_host_boot != origin['pi_host_boot']
                    or sample.sequence != gate.vehicle_sequence
                    or sample.vehicle != inputs.source['vehicle'] or sample.odom_epoch != inputs.source['odom_epoch']
                    or sample.calibration != inputs.source['calibration']
                    or any(getattr(sample, field) != origin[key] for field, key in (
                        ('host_boot', 'pi_host_boot'), ('gateway_instance', 'gateway_instance'),
                        ('device_session', 'device_session'), ('pi_session', 'pi_session')))):
                return run('OPERATION_VEHICLE_SOURCE_MISMATCH')
            if (not evidence.j_interval[1] <= now_us < evidence.valid_until_j_us
                    or evidence.clock_lease.pi_interval_to_j(evidence.pi_interval, now_us) != evidence.j_interval):
                return run('OPERATION_ORIGINAL_VEHICLE_EXPIRED')
            trust = decode(returned_lease.trust_bytes, schema.WorldTrust)
            expires = min(gate.valid_until_pi_us, returned_lease.expires_pi_us) - trust.upper_offset_us
            expires = min(expires, evidence.valid_until_j_us)
            if not probe.pi_send_us < gate.valid_until_pi_us or now_us >= expires:
                return run('OPERATION_GATE_EXPIRED')
            return MapperOperationDecision(True, True,
                'WAIT_CONFIRMED' if gate.wait_confirmed else 'MANUAL_LOCAL_ADMITTED',
                original_probe, encode(gate), record, expires, *operation)
        except (ValueError, TypeError, KeyError, OverflowError):
            return run('OPERATION_INVALID_OR_EXPIRED')


class SupervisorOperation:
    """S-main의 pending1/history16 owner다. 결과 history는 이 S 안에서 임의 제거하지 않는다."""
    def __init__(self, service_instance, *, clock):
        self.service_instance = _uuid(service_instance)
        if not callable(clock): raise ValueError('actual S monotonic clock required')
        self.clock = clock; self._thread = threading.get_ident()
        self._peer = self._world_dispatch = self._config = None
        self._records = OrderedDict(); self._pending = None; self._uncertain = set()
        self._confirmed = self._desired = 'RUN'; self._revision = self._issued_revision = 0
        self._accepted_id = self._accepted_sha = None
        self._synchronized = False; self._reason = 'MAPPER_NOT_SYNCHRONIZED'
        self._consumer_ready = None

    def _owned(self):
        if threading.get_ident() != self._thread: raise RuntimeError('operation belongs to original S-main')

    def _now(self):
        self._owned(); now = self.clock()
        if type(now) is not int or not 0 <= now < 2**64: raise ValueError('actual S J time required')
        return now

    @property
    def world_enabled(self):
        self._owned(); return self._synchronized and not self._current_uncertain() and self._confirmed == 'RUN'

    def _current_uncertain(self):
        return any(self._records[key]['peer']==self._peer and self._records[key]['revision']>self._revision
            for key in self._uncertain)

    def _world(self):
        if self._world_dispatch is not None: self._world_dispatch(self.world_enabled)

    def bind_peer(self, peer, *, world_dispatch):
        from .data_channel import DataPeer
        self._owned()
        if type(peer) is not DataPeer or peer.service_instance != self.service_instance or not callable(world_dispatch):
            raise ValueError('actual registered M and world-only dispatch port required')
        if self._peer == peer: return
        if self._pending is not None:
            self._pending['result'] = result_fields(self._pending['request'],status='UNKNOWN',reason='MAPPER_REPLACED',
                completed_us=None,revision=self._pending['revision'])
            self._pending = None
        self._peer, self._world_dispatch = peer, world_dispatch
        self._synchronized = False; self._reason = 'CONFIG_SYNC_PENDING'; self._desired = self._confirmed
        self._config = self._consumer_ready = None
        self._world()

    def _new_config(self, now):
        value = dict(profile=CONFIG_PROFILE,service_instance=self.service_instance,config_revision=str(self._revision),
            state=self._confirmed,accepted_request_id=self._accepted_id,accepted_request_sha256=self._accepted_sha)
        from uuid import uuid4
        self._config = dict(body=canonical(value),request_id=str(uuid4()),issued=now,
            expires=min(2**64-1,now+TRANSACTION_US),selected=None,sent=None,result=None)

    def _expire(self, now):
        if not self._synchronized and self._config is not None and now>=self._config['expires']:
            self._reason='CONFIG_EXPIRED'
        if self._pending is not None and now >= self._pending['request'].expires_us:
            record = self._pending; request = record['request']
            record['result'] = result_fields(request,status='UNKNOWN' if record['sent'] is not None else 'EXPIRED',
                reason='ORIGINAL_ACK_NOT_RECEIVED' if record['sent'] is not None else 'REQUEST_EXPIRED',
                completed_us=None,revision=record['revision'])
            self._pending = None; self._desired = self._confirmed
            self._reason = record['result']['reason']
            if record['sent'] is not None:
                self._uncertain.add(request.request_id)
                self._world()

    def detach_peer(self, peer):
        self._owned()
        if peer != self._peer: return False
        self._synchronized = False; self._reason = 'MAPPER_DETACHED'; self._world()
        return True

    def status(self):
        self._expire(self._now())
        return dict(profile=STATUS_PROFILE,service_instance=self.service_instance,desired=self._desired,
            confirmed=self._confirmed,config_revision=str(self._revision),
            mapper_synchronized=bool(self._synchronized and not self._current_uncertain()
                and (self._pending is None or self._pending['sent'] is None)),
            pending_request_id=None if self._pending is None else self._pending['request'].request_id,last_reason=self._reason)

    def submit(self, raw_request_bytes):
        now = self._now(); request = OperationRequest(raw_request_bytes)
        if request.service_instance != self.service_instance: raise OperationRequestError('foreign S operation request')
        self._expire(now); previous = self._records.get(request.request_id)
        if previous is not None:
            if previous['request'].raw_bytes != request.raw_bytes: raise OperationRequestError('same operation key changed original bytes')
            return None if previous['result'] is None else json.loads(canonical(previous['result']))
        if len(self._records) >= 16:
            return result_fields(request,status='REJECTED',reason='HISTORY_FULL',completed_us=None,revision=None)
        reason = ('REQUEST_NOT_CURRENT' if not request.issued_us <= now < request.expires_us else
            'BUSY' if self._pending is not None else 'MAPPER_NOT_SYNCHRONIZED' if not self._synchronized else None)
        record = dict(request=request,revision=None,result=None,selected=None,sent=None,peer=self._peer)
        self._records[request.request_id] = record
        if reason is not None:
            record['result'] = result_fields(request,status='REJECTED',reason=reason,completed_us=None,revision=None)
            return json.loads(canonical(record['result']))
        if self._issued_revision >= 2**64-1: raise OverflowError('operation configuration revision exhausted')
        self._issued_revision += 1; record['revision'] = self._issued_revision
        self._pending = record; self._desired = request.action; self._reason = 'REQUEST_PENDING'
        # permit 거절은 효과0이다. 실제 M APPLIED를 받기 전에는 기존 World를 철회하지 않는다.
        return None

    def query(self, request_id):
        self._expire(self._now()); _uuid(request_id)
        record = self._records.get(request_id)
        if record is None:
            return dict(profile=RESULT_PROFILE,service_instance=self.service_instance,request_id=request_id,
                request_sha256=None,action=None,status='UNKNOWN',reason='UNKNOWN_REQUEST',issued_j_us=None,
                expires_j_us=None,completed_j_us=None,config_revision=None)
        return None if record['result'] is None else json.loads(canonical(record['result']))

    def next_message(self, peer, *, ready=True):
        now = self._now(); self._expire(now)
        if peer != self._peer: raise ValueError('operation selected by foreign M channel')
        if type(ready) is not bool:raise ValueError('actual M bind-ready boolean required')
        if not ready:return None
        if not self._synchronized:
            if self._consumer_ready is None:return None
            if self._config is None:self._new_config(now)
            record = self._config
            if record is None or record['selected'] is not None or now >= record['expires']: return None
            return {},record['body'],record['request_id'],record['issued'],record['expires']
        record = self._pending
        if record is None or record['selected'] is not None: return None
        request = record['request']
        return dict(config_revision=str(record['revision'])),request.raw_bytes,request.request_id,request.issued_us,request.expires_us

    def check_bootstrap_expiry(self):
        """source poll 뒤에만 원 CONFIG 실패를 종결한다. 조회/사용자 요청의 만료와 구분한다."""
        now = self._now()
        if not self._synchronized and self._config is not None and now >= self._config['expires']:
            self._reason = 'CONFIG_EXPIRED'
            raise TimeoutError('original mapper operation CONFIG bootstrap expired')

    def selected(self, packet):
        from .data_channel import DataPacket
        self._owned(); record = self._config if not self._synchronized else self._pending
        if type(packet) is not DataPacket:raise ValueError('original immutable operation packet required')
        if record is None: raise ValueError('operation has no original selected transaction')
        request = record.get('request')
        expected = (record['body'],record['request_id'],record['issued'],record['expires']) if request is None else (
            request.raw_bytes,request.request_id,request.issued_us,request.expires_us)
        header=packet.header
        metadata={} if request is None else dict(config_revision=str(record['revision']))
        if ((packet.body,header['request_id'],packet.issued_us,packet.expires_us) != expected
                or header['metadata']!=metadata or packet.kind!='SOURCE_OPERATION'
                or (header['service_instance'],header['mapper_worker'],header['channel_epoch'],header['sender']) != (
                    self._peer.service_instance,self._peer.mapper_worker,self._peer.channel_epoch,'S')):
            raise ValueError('selected operation changed original transaction')
        if record['selected'] is not None and record['selected'] is not packet:
            raise ValueError('original selected operation handle was replaced')
        record['selected'] = packet

    def sent(self, event):
        from .data_channel import DataSent
        self._owned()
        if type(event) is not DataSent:raise ValueError('actual operation full-send event required')
        records = (self._config,*self._records.values())
        record = next((row for row in records if row is not None and row['selected'] is event.packet),None)
        if record is None or not event.packet.issued_us <= event.started_us <= event.completed_us:
            raise ValueError('operation actual full-send differs from original selection')
        record['sent'] = event

    def receive(self, packet, peer):
        from .data_channel import DataPacket
        self._owned()
        if type(packet) is not DataPacket or packet.kind!='OPERATION_RESULT':raise ValueError('actual M operation result packet required')
        now = self._now()
        header=packet.header
        if (peer != self._peer or header['metadata']!={}
                or (header['service_instance'],header['mapper_worker'],header['channel_epoch'],header['sender']) != (
                    peer.service_instance,peer.mapper_worker,peer.channel_epoch,'M')):
            raise ValueError('operation result differs from registered current M')
        from .operation_request import _object
        marker=_object(packet.body)
        if type(marker) is dict and marker.get('profile')==CONSUMER_READY_PROFILE:
            if set(marker)!={'profile','service_instance'} or marker['service_instance']!=self.service_instance:
                raise ValueError('exact current M consumer-installed marker required')
            now = self._now()
            if now < packet.issued_us:
                raise ValueError('consumer-installed marker has future original issue time')
            if now >= packet.expires_us:
                raise TimeoutError('original mapper operation consumer-installed marker expired')
            original=(packet.body,header['request_id'],packet.issued_us,packet.expires_us)
            if self._consumer_ready is not None and self._consumer_ready!=original:
                raise ValueError('one original consumer-installed marker required per M channel')
            self._consumer_ready=original
            return
        value = operation_result(packet.body)
        if (peer != self._peer or value['service_instance'] != self.service_instance or packet.header['metadata'] != {}
                or packet.issued_us != int(value['completed_j_us']) or not packet.issued_us <= now < packet.expires_us
                or (packet.header['service_instance'],packet.header['mapper_worker'],packet.header['channel_epoch'],packet.header['sender']) != (
                    peer.service_instance,peer.mapper_worker,peer.channel_epoch,'M')):
            raise ValueError('operation result differs from registered current M')
        config_ack = value['profile'] == CONFIG_ACK_PROFILE
        record = self._config if config_ack else self._records.get(value['request_id'])
        if record is None or record['selected'] is None or record['sent'] is None:
            raise ValueError('operation result lacks original full-send')
        original = record['selected']; body_sha = sha256(original.body).hexdigest()
        if (value['request_id'],value['request_sha256'],int(value['issued_j_us']),int(value['expires_j_us'])) != (
                original.header['request_id'],body_sha,original.issued_us,original.expires_us):
            raise ValueError('operation result changed original key/SHA/time')
        if (not record['sent'].started_us <= int(value['completed_j_us']) <= self._now()
                or packet.header['request_id'] != value['request_id']):
            raise ValueError('operation result has future completion or changed header key')
        if config_ack:
            config = configuration(original.body)
            if value['config_revision'] != config['config_revision'] or value['state'] != config['state']:
                raise ValueError('CONFIG ACK changed original confirmed state')
            if record['result'] is not None:
                if record['result']!=value:raise ValueError('original CONFIG ACK changed')
                return
            if value['status'] == 'APPLIED':
                self._synchronized = True; self._reason = 'CONFIG_SYNCHRONIZED'; self._world()
            record['result']=value
            return
        request = record['request']
        if value['action'] != request.action or int(value['config_revision']) != record['revision'] or record['peer'] != peer:
            raise ValueError('operation result changed original action/revision/peer')
        if record['result'] is not None and record['result']['status'] not in ('UNKNOWN','EXPIRED'):
            if record['result'] != value: raise ValueError('original operation terminal changed')
            return
        if value['status'] == 'APPLIED' and record['revision'] > self._revision:
            # 새 World 차단을 먼저 적용하고 외부 조회 결과를 노출한다. 이미 claim된 송신은 회수하지 않는다.
            if request.action == 'STANDBY': self._world_dispatch(False)
            self._confirmed = request.action; self._revision = record['revision']
            self._accepted_id,self._accepted_sha = request.request_id,request.sha256
            self._synchronized = True
            self._uncertain={key for key in self._uncertain if self._records[key]['peer']!=peer
                or self._records[key]['revision']>self._revision}
        self._uncertain.discard(request.request_id)
        self._world()
        record['result'] = value
        if self._pending is record: self._pending = None
        self._desired = self._confirmed if self._pending is None else self._pending['request'].action
        self._reason = ('REQUEST_PENDING' if self._pending is not None else
            'ORIGINAL_ACK_NOT_RECEIVED' if self._current_uncertain() else value['reason'])


class MapperOperationController:
    """M-main의 CONFIG/명시 요청 consumer다. 적용 callback만 기존 compute 채택 lock을 사용한다."""
    def __init__(self, peer, *, apply, clock):
        from .data_channel import DataPeer
        if type(peer) is not DataPeer or not callable(apply) or not callable(clock):
            raise ValueError('registered M operation consumer required')
        self.peer,self.apply,self.clock = peer,apply,clock
        self.gate = MapperOperationGate(); self.synchronized = False; self.state = 'RUN'; self.revision = 0
        self._history = OrderedDict(); self._config_original = None; self._thread = threading.get_ident()

    def process(self, packet, *, inputs=None, sensor_state=None, origin=None):
        from .operation_request import _object, _decimal
        from .data_channel import DataPacket
        if threading.get_ident() != self._thread: raise RuntimeError('operation consumer belongs to original M-main')
        if type(packet) is not DataPacket or packet.kind!='SOURCE_OPERATION':raise ValueError('actual S operation packet required')
        value = _object(packet.body); now = self.clock(); header = packet.header
        if type(now) is not int or not 0 <= now < 2**64: raise ValueError('actual M J time required')
        if ((header['service_instance'],header['mapper_worker'],header['channel_epoch'],header['sender']) != (
                self.peer.service_instance,self.peer.mapper_worker,self.peer.channel_epoch,'S') or now < packet.issued_us):
            raise ValueError('operation transaction is not original/current')
        config = value.get('profile') == CONFIG_PROFILE if type(value) is dict else False
        if config:
            if header['metadata'] != {}: raise ValueError('exact CONFIG metadata required')
            original = (packet.body,header['request_id'],packet.issued_us,packet.expires_us)
            if self._config_original is not None:
                if self._config_original[0] != original: raise ValueError('initial CONFIG changed on the same M channel')
                return self._config_original[1]
            value = configuration(packet.body)
            if header['metadata'] != {} or value['service_instance'] != self.peer.service_instance:
                raise ValueError('foreign CONFIG transaction')
            revision = int(value['config_revision']); action = value['state']
            if self.synchronized and revision < self.revision: raise ValueError('CONFIG revision regressed')
            reason = 'CONFIG_EXPIRED' if now >= packet.expires_us else None
            if reason is None:
                try: checked = self.apply(action,packet.expires_us,None)
                except OperationRequestError as error: reason = str(error)
            if reason is not None: checked = self.clock()
            result = dict(profile=CONFIG_ACK_PROFILE,service_instance=self.peer.service_instance,request_id=header['request_id'],
                request_sha256=sha256(packet.body).hexdigest(),state=action,status='APPLIED' if reason is None else 'EXPIRED',
                reason='CONFIG_SYNCHRONIZED' if reason is None else reason,
                issued_j_us=str(packet.issued_us),expires_j_us=str(packet.expires_us),completed_j_us=str(checked),config_revision=str(revision))
            raw = canonical(result); operation_result(raw)
            if reason is None: self.synchronized,self.state,self.revision = True,action,revision
            self._config_original = original,raw
            return raw
        request = OperationRequest(packet.body)
        if (request.service_instance != self.peer.service_instance or header['request_id'] != request.request_id
                or (packet.issued_us,packet.expires_us) != (request.issued_us,request.expires_us)
                or set(header['metadata']) != {'config_revision'}):
            raise ValueError('operator request differs from original S delivery')
        revision = _decimal(header['metadata']['config_revision'])
        previous = self._history.get(request.request_id)
        if previous is not None:
            if previous[0] != (request.raw_bytes,revision): raise ValueError('held operator request changed original bytes/revision')
            return previous[1]
        reason = ('REQUEST_EXPIRED' if now >= request.expires_us else 'MAPPER_NOT_SYNCHRONIZED' if not self.synchronized else 'STALE_CONFIG_REVISION' if revision <= self.revision
            else 'HISTORY_FULL' if len(self._history) >= 16 else None)
        decision = None
        if reason is None and request.action == 'STANDBY' and self.state != 'STANDBY':
            lease = None if inputs is None else inputs.clock_lease
            decision = self.gate.observe(None if lease is None else lease.raw_probe_bytes,lease,
                sensor_state=sensor_state,inputs=inputs,origin=origin,now_us=now)
            if not decision.standby: reason = decision.reason
        if reason is None:
            try: checked = self.apply(request.action,request.expires_us,None if decision is None else decision.expires_j_us)
            except OperationRequestError as error: reason = str(error)
            if reason is None:
                self.state,self.revision = request.action,revision
                result = result_fields(request,status='APPLIED',reason='CONFIGURATION_APPLIED',completed_us=checked,revision=revision)
        if reason is not None: result = result_fields(request,status='REJECTED',reason=reason,completed_us=self.clock(),revision=revision)
        raw = canonical(result); operation_result(raw)
        if len(self._history) < 16: self._history[request.request_id] = (request.raw_bytes,revision),raw
        return raw
