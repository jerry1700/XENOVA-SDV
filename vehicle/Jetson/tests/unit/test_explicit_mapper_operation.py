"""명시 요청→실제 source seqpacket→compute fence→S 설정의 software 회귀다.

Pi Gate/clock/Vehicle은 기존 typed software fixture이며 물리 STOP·실차 보정이 아니다.
Processor는 계산 경계 대역이다. GT·가짜 READY·native token·DB 연결은 넣지 않는다.
JIO world-only generation은 별도 실제 JIO 시험이 맡고 여기서는 S callback 순서를 기록한다.
"""

from copy import deepcopy
from hashlib import sha256
import json
import socket
import threading
import time
import unittest
from uuid import UUID

import test_mapper_compute as compute_fixtures
import test_mapper_operation as gate_fixtures
import test_mapper_phase as phase_fixtures
import test_source_runtime as source_fixtures
from xenova_drive_world import encode,schema
from xenova_drive_world.mapper_clock import ClockPolicy,restore_returned_probe
from xenova_vehicle.runtime.data_channel import DataLink,DataPeer,data_pair
from xenova_vehicle.runtime.lifecycle_policy import AbsoluteDeadline
from xenova_vehicle.runtime.mapper_compute import MapperCompute
from xenova_vehicle.runtime.mapper_control import MapperControl
from xenova_vehicle.runtime.mapper_operation import SupervisorOperation,MapperOperationController,mapper_operation_supported
from xenova_vehicle.runtime.mapper_progress import MapperJobWatch
from xenova_vehicle.runtime.mapper_sensor_state import MapperSensorState
from xenova_vehicle.runtime.operation_request import OperationRequestError,canonical
from xenova_vehicle.runtime.resident_workers import apply_compute_operation
from xenova_vehicle.runtime.source_runtime import MapperSource,SupervisorSource
from xenova_vehicle.transport.jetson_jio import JioInput


SUPPORTED=mapper_operation_supported()


class OperationRig:
    def __init__(self,previous=None,*,installed=True):
        if previous is None:
            self.profile,self.raw,self.policy,self.sample,self.bootstrap,self.peer,self.incarnation=source_fixtures.fixture()
            self.clock=compute_fixtures.Clock(1000)
            self.jio=source_fixtures.JioPort(self.policy,'oak-front')
            self.owner=SupervisorOperation(self.peer.service_instance,clock=self.clock)
            self.material=({'service_instance':self.peer.service_instance,'storage_worker':source_fixtures.uid(),
                'storage_profile_sha256':'c'*64,'database_uuid':self.bootstrap['database_uuid'],'db_schema_version':1},self.bootstrap)
        else:
            self.profile,self.raw,self.policy,self.sample,self.bootstrap,self.incarnation=(
                previous.profile,previous.raw,previous.policy,previous.sample,previous.bootstrap,previous.incarnation)
            self.clock,self.jio,self.owner,self.material=previous.clock,previous.jio,previous.owner,previous.material
            self.peer=DataPeer(previous.peer.service_instance,source_fixtures.uid(),source_fixtures.uid())
        self.processor=gate_fixtures.OperationProcessor();self.factories=[]
        def factory(generation):self.factories.append(generation);return self.processor
        self.compute=MapperCompute(factory,source_generation='A',clock=self.clock)
        left,right=data_pair()
        self.mapper=MapperSource(DataLink(left,self.peer,'M',clock=self.clock),self.raw,self.policy,
            mapper_instance=self.incarnation,device='oak-front',clock=self.clock)
        self.controller=MapperOperationController(self.peer,apply=lambda action,E,gateE:apply_compute_operation(
            self.compute,self.mapper,action=action,expires_us=E,gate_expires_us=gateE,clock=self.clock),clock=self.clock)
        if installed:self.mapper.install_operation(self.controller)
        self.supervisor=SupervisorSource(DataLink(right,self.peer,'S',clock=self.clock),self.raw,self.jio,
            mapper_instance=self.incarnation,mapper_boot=source_fixtures.uid(),clock_epoch=self.raw.fields['clock_epoch'],
            operation_owner=self.owner,clock=self.clock)
        self.sensor=None;self.inputs=None;self.probe=None
        self.compute.start()
        if previous is None and installed:self.jio.inputs.append(JioInput('VEHICLE',encode(self.sample),self.clock()))
        self.cycle()

    def ingest(self):
        if self.sensor is not None:self.inputs=self.sensor.ingest(self.mapper.context_bytes(),now_us=self.clock())
        self.mapper.operation_step(inputs=self.inputs,sensor_state=self.sensor,origin=self.mapper.origin)

    def cycle(self,count=8):
        for _ in range(count):
            self.supervisor.pump(self.material,bind_ready=True,on_publication=lambda *_:None)
            self.mapper.pump(before_outputs=self.ingest)

    def close(self):
        self.compute.request_stop();self.compute.join(2)
        self.mapper.close();self.supervisor.close()

    def request(self,number,action,*,lifetime=50000):
        return canonical(dict(profile='jetson-operation-request-1',service_instance=self.peer.service_instance,
            request_id=compute_fixtures.identifier(100+number),action=action,
            issued_j_us=str(self.clock()),expires_j_us=str(self.clock()+lifetime)))

    def add_gate(self,*,present=True,manual=False):
        self.clock.set(1110)
        source=self.mapper.source;origin=self.mapper.origin
        policy=ClockPolicy(source['vehicle'],source['device'],source['configuration'],source['calibration'],
            source['primitive'],0,0,100,1_000_000,200_000,1_000_000)
        trust=schema.WorldTrust(**{key:value for key,value in source.items() if key!='device'},
            lower_offset_us=0,upper_offset_us=0,expires_pi_us=1_001_000,max_error_us=100)
        self.probe=schema.MapperClockProbe(major=1,vehicle=source['vehicle'],device=source['device'],
            pi_host_boot=origin['pi_host_boot'],pi_client_instance=source_fixtures.uid(),probe_seq=1,
            nonce=b'p'*16,pi_send_us=self.clock(),admission_revision=1,mapper_trust=trust,
            expected_mapper_boot=source['mapper_boot'],expected_mapper_instance=source['mapper_instance'],
            expected_clock_epoch=source['clock_epoch'])
        if present:
            self.probe.operation_gate.CopyFrom(schema.PiOperationGate(gateway_instance=UUID(origin['gateway_instance']).bytes,
                control_generation=1,operation_revision=1,mode=2 if manual else 0,wait_confirmed=not manual,
                manual_local_admitted=manual,vehicle_sequence=2,stop_command_sequence=0 if manual else 17,
                valid_until_pi_us=self.clock()+100000))
        raw=encode(self.probe);lease=restore_returned_probe(raw,policy,source,origin['pi_host_boot'],self.clock())
        self.jio.current_lease=lease;self.jio.inputs.append(JioInput('PROBE',raw,self.clock(),lease=lease))
        vehicle=schema.VehicleSample();vehicle.CopyFrom(self.sample)
        vehicle.sequence=2;vehicle.source_sample+=1;vehicle.source_us+=10;vehicle.pi_sample_us=1100
        self.jio.inputs.append(JioInput('VEHICLE',encode(vehicle),self.clock(),lease=lease))
        self.sensor=MapperSensorState(self.profile,self.raw,policy,tof_leaves={},vehicle_max_age_us=200000,
            tof_max_age_us=200000,max_speed_mps=.5,max_yaw_rate_rps=2.,max_gap_us=100000,capacity=16,
            mapper_worker=self.peer.mapper_worker,mapper_instance=self.incarnation)
        self.cycle()


@unittest.skipUnless(hasattr(socket,'SOCK_SEQPACKET'),'actual Linux local seqpacket required')
class ExplicitMapperOperationTests(unittest.TestCase):
    def rig(self,previous=None,*,installed=True):
        rig=OperationRig(previous,installed=installed);self.addCleanup(rig.close);return rig

    @unittest.skipUnless(SUPPORTED,'NOT_SUPPORTED: coherent B09 Gate required')
    def test_default_run_keeps_geometry_with_actual_wait_or_manual_permit_and_no_request(self):
        for manual in (False,True):
            with self.subTest(manual=manual):
                f=self.rig();f.add_gate(manual=manual)
                original_admissions=f.sensor.vehicle_admissions;source=f.mapper.source
                self.assertTrue(f.mapper.operation_ready);self.assertEqual(f.controller.state,'RUN')
                self.assertIsNone(f.owner.status()['pending_request_id'])
                f.compute.submit(compute_fixtures.item(1));compute_fixtures.event_of(f.compute,'GEOMETRY_COMPLETED')
                self.assertEqual(f.compute.snapshot()['operation_serial'],0)
                self.assertEqual(f.processor.accepted_jobs[-1],compute_fixtures.identifier(1))
                self.assertTrue(f.jio.world_enabled);self.assertIs(f.mapper.source,source)
                self.assertEqual(f.sensor.vehicle_admissions,original_admissions)

    def test_rejected_standby_has_no_effect_on_running_geometry_or_world_source(self):
        f=self.rig();source=f.mapper.source;head=f.mapper._output_heads.copy()
        original=f.compute.snapshot()['operation_serial']
        raw=f.request(1,'STANDBY');self.assertIsNone(f.owner.submit(raw));f.cycle()
        result=f.owner.query(compute_fixtures.identifier(101))
        self.assertEqual(result['status'],'REJECTED');self.assertNotEqual(result['reason'],'CONFIGURATION_APPLIED')
        self.assertEqual(f.compute.snapshot()['operation_serial'],original)
        self.assertEqual((f.controller.state,f.owner.status()['confirmed']),('RUN','RUN'))
        self.assertTrue(f.jio.world_enabled);self.assertIs(f.mapper.source,source);self.assertEqual(f.mapper._output_heads,head)
        f.compute.submit(compute_fixtures.item(1));compute_fixtures.event_of(f.compute,'GEOMETRY_COMPLETED')

    @unittest.skipUnless(SUPPORTED,'NOT_SUPPORTED: coherent B09 Gate required')
    def test_standby_latches_after_gate_and_transaction_expire_then_explicit_run_and_m_restart(self):
        f=self.rig();f.add_gate();original_source=deepcopy(f.mapper.source)
        raw=f.request(1,'STANDBY');f.owner.submit(raw);f.cycle()
        applied=f.owner.query(compute_fixtures.identifier(101));self.assertEqual(applied['status'],'APPLIED')
        self.assertFalse(f.jio.world_enabled);self.assertTrue(f.compute.snapshot()['operation_standby'])
        f.clock.set(111111);f.cycle()
        self.assertEqual(f.owner.query(compute_fixtures.identifier(101)),applied)
        self.assertEqual(f.controller.state,'STANDBY');self.assertEqual(f.mapper.source,original_source)
        self.assertFalse(f.compute.submit(compute_fixtures.item(2)))
        fresh=self.rig(f)
        self.assertEqual((fresh.controller.state,fresh.owner.status()['confirmed']),('STANDBY','STANDBY'))
        self.assertTrue(fresh.controller.synchronized);self.assertFalse(fresh.jio.world_enabled)
        self.assertIs(fresh.owner,f.owner);self.assertIsNone(fresh.sensor)
        run=fresh.request(2,'RUN');fresh.owner.submit(run);fresh.cycle()
        self.assertTrue(fresh.jio.world_enabled);self.assertFalse(fresh.compute.snapshot()['operation_standby'])
        fresh.compute.submit(compute_fixtures.item(3));compute_fixtures.event_of(fresh.compute,'GEOMETRY_COMPLETED')
        self.assertEqual(fresh.processor.accepted_jobs[-1],compute_fixtures.identifier(3))
        f.supervisor.close();self.assertTrue(fresh.jio.world_enabled)  # 옛 source close가 새 peer를 철회하지 않는다.
        other=SupervisorOperation(source_fixtures.uid(),clock=f.clock)
        with self.assertRaises(OperationRequestError):other.submit(raw)

    @unittest.skipUnless(SUPPORTED,'NOT_SUPPORTED: coherent B09 Gate required')
    def test_original_applied_before_e_is_preserved_after_late_delivery_and_does_not_erase_new_pending(self):
        f=self.rig();f.add_gate();raw=f.request(1,'STANDBY',lifetime=1000)
        f.owner.submit(raw)
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        f.mapper.pump(before_outputs=f.ingest)
        self.assertEqual(f.controller.state,'STANDBY')
        f.clock.set(2111)
        self.assertEqual(f.owner.query(compute_fixtures.identifier(101))['status'],'UNKNOWN')
        self.assertFalse(f.owner.status()['mapper_synchronized']);self.assertFalse(f.jio.world_enabled)
        f.owner.submit(f.request(2,'RUN'))
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        self.assertEqual(f.owner.status()['pending_request_id'],compute_fixtures.identifier(102))
        self.assertEqual(f.owner.status()['desired'],'RUN')
        first=f.owner.query(compute_fixtures.identifier(101))
        self.assertEqual(first['status'],'APPLIED');self.assertEqual(first['request_sha256'],sha256(raw).hexdigest())
        self.assertLess(int(first['completed_j_us']),int(first['expires_j_us']))
        f.cycle();self.assertEqual(f.owner.status()['confirmed'],'RUN')
        self.assertTrue(f.owner.status()['mapper_synchronized']);self.assertTrue(f.jio.world_enabled)
        self.assertEqual(f.owner.query(compute_fixtures.identifier(101)),first)

    def test_same_channel_config_and_request_retry_cannot_change_original_revision_or_bytes(self):
        f=self.rig();config=f.owner._config['selected'];controller=f.controller
        original=controller.process(config)
        serial=f.compute.snapshot()['operation_serial']
        self.assertEqual(controller.process(config),original)
        fields=json.loads(config.body);fields.update(state='STANDBY',config_revision='1',
            accepted_request_id=compute_fixtures.identifier(1),accepted_request_sha256='a'*64)
        packet=f.supervisor.link.send('SOURCE_OPERATION',{},canonical(fields),request_id=config.header['request_id'],
            issued_us=config.issued_us,expires_us=config.expires_us)
        self.assertIsNotNone(packet)
        with self.assertRaises(ValueError):controller.process(packet)
        f.supervisor.link.discard_operation(packet)
        raw=f.request(1,'RUN');f.owner.submit(raw);f.cycle()
        delivered=f.owner._records[compute_fixtures.identifier(101)]['selected']
        expected=controller.process(delivered)
        changed=f.supervisor.link.send('SOURCE_OPERATION',{'config_revision':'999'},raw,
            request_id=delivered.header['request_id'],issued_us=delivered.issued_us,expires_us=delivered.expires_us)
        with self.assertRaises(ValueError):controller.process(changed)
        f.supervisor.link.discard_operation(changed)
        self.assertEqual(controller.process(delivered),expected)
        self.assertEqual(f.compute.snapshot()['operation_serial'],serial)
        with self.assertRaises(OperationRequestError):f.owner.submit(raw+b' ')
        # RAW ACK가 선행해도 실제 consumer 설치 전에는 CONFIG transaction 시간이 흐르지 않는다.
        delayed=self.rig(installed=False)
        self.assertIsNone(delayed.owner._consumer_ready);self.assertIsNone(delayed.owner._config)
        self.assertIsNone(delayed.mapper.publication)
        delayed.clock.set(6001000);delayed.cycle()
        self.assertIsNone(delayed.owner._config);self.assertFalse(delayed.owner.status()['mapper_synchronized'])
        delayed.mapper.install_operation(delayed.controller);delayed.cycle()
        self.assertGreaterEqual(delayed.owner._config['issued'],6001000)
        self.assertTrue(delayed.owner.status()['mapper_synchronized'])
        self.assertEqual(delayed.controller.state,'RUN');self.assertIsNone(delayed.mapper.publication)

    def test_unsent_expiry_is_normal_terminal_and_history16_full_does_not_reset_source(self):
        f=self.rig();endpoint=f.supervisor.link.endpoint
        class Blocked:
            def __getattr__(self,name):return getattr(endpoint,name)
            def send(self,raw):raise BlockingIOError()
        f.owner.submit(f.request(1,'RUN',lifetime=10));f.supervisor.link.endpoint=Blocked()
        self.addCleanup(lambda:setattr(f.supervisor.link,'endpoint',endpoint))
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        packet=f.supervisor.link.pending;self.assertEqual(packet.kind,'SOURCE_OPERATION')
        f.clock.set(1010)
        self.assertEqual(f.owner.query(compute_fixtures.identifier(101))['status'],'EXPIRED')
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        self.assertIsNone(f.supervisor.link.pending);self.assertTrue(f.jio.world_enabled)
        f.supervisor.link.endpoint=endpoint
        for number in range(2,17):
            f.owner.submit(f.request(number,'RUN'));f.cycle()
            self.assertEqual(f.owner.query(compute_fixtures.identifier(100+number))['status'],'APPLIED')
        denied=f.owner.submit(f.request(17,'RUN'))
        self.assertEqual(denied['reason'],'HISTORY_FULL');self.assertEqual(len(f.owner._records),16)
        self.assertTrue(f.jio.world_enabled);self.assertEqual(f.controller.state,'RUN')
        self.assertIsNotNone(f.mapper.publication)
        # 원 result slot/link pending이 막혀도 다음pending1을 보존하고 초과분은 적용하지 않는다.
        back=self.rig();m_endpoint=back.mapper.link.endpoint
        class BlockedMapper:
            def __getattr__(self,name):return getattr(m_endpoint,name)
            def send(self,raw):raise BlockingIOError()
        back.mapper.link.endpoint=BlockedMapper()
        self.addCleanup(lambda:setattr(back.mapper.link,'endpoint',m_endpoint))
        config,ack=back.controller._config_original
        back.mapper.link.send('OPERATION_RESULT',{},ack,request_id=config[1],issued_us=1000,expires_us=5001000)
        back.owner.submit(back.request(1,'RUN',lifetime=10))
        back.supervisor.pump(back.material,bind_ready=True,on_publication=lambda *_:None)
        back.mapper.pump(before_outputs=back.ingest)
        self.assertIsNotNone(back.mapper._operation_result)
        back.clock.set(1011);back.owner.query(compute_fixtures.identifier(101))
        second=back.request(2,'RUN',lifetime=10);back.owner.submit(second)
        back.supervisor.pump(back.material,bind_ready=True,on_publication=lambda *_:None)
        back.mapper.pump(before_outputs=back.ingest)
        self.assertEqual(back.mapper._operation_pending.body,second)
        self.assertEqual(back.controller.revision,1)
        back.clock.set(1022);back.owner.query(compute_fixtures.identifier(102))
        back.owner.submit(back.request(3,'RUN',lifetime=10))
        back.supervisor.pump(back.material,bind_ready=True,on_publication=lambda *_:None)
        back.mapper.pump(before_outputs=back.ingest)
        self.assertEqual(back.mapper.counts['operation_backpressure_rejected'],1)
        self.assertEqual(back.mapper._operation_pending.body,second)
        back.clock.set(1033);back.owner.query(compute_fixtures.identifier(103))
        back.mapper.link.endpoint=m_endpoint;back.cycle()
        self.assertEqual(back.owner.query(compute_fixtures.identifier(101))['status'],'APPLIED')
        self.assertEqual(back.owner.query(compute_fixtures.identifier(102))['status'],'REJECTED')
        self.assertEqual(back.owner.query(compute_fixtures.identifier(103))['status'],'UNKNOWN')
        self.assertFalse(back.owner.status()['mapper_synchronized']);self.assertFalse(back.jio.world_enabled)
        self.assertEqual(back.owner.status()['last_reason'],'ORIGINAL_ACK_NOT_RECEIVED')
        back.owner.submit(back.request(4,'RUN'));back.cycle()
        self.assertTrue(back.owner.status()['mapper_synchronized']);self.assertTrue(back.jio.world_enabled)

    def test_static_consumer_marker_can_retry_without_restarting_the_mapper(self):
        for pending in (False,True):
            with self.subTest(pending=pending):
                f=self.rig(installed=False)
                f.mapper.install_operation(f.controller)
                ready=f.mapper._operation_result
                original_endpoint=f.mapper.link.endpoint
                if pending:
                    class Blocked:
                        def __getattr__(self,name):return getattr(original_endpoint,name)
                        def send(self,raw):raise BlockingIOError()
                    f.mapper.link.endpoint=Blocked()
                    self.addCleanup(lambda f=f,endpoint=original_endpoint:setattr(f.mapper.link,'endpoint',endpoint))
                    f.mapper.pump(before_outputs=f.ingest)
                    packet=f.mapper.link.pending
                    self.assertEqual((packet.body,packet.header['request_id'],packet.issued_us,packet.expires_us),ready)
                    self.assertIsNone(f.mapper._operation_result)
                f.clock.set(ready[3])
                f.mapper.pump(before_outputs=f.ingest)
                fresh=f.mapper._operation_ready
                self.assertEqual(fresh[0],ready[0])
                self.assertNotEqual(fresh[1],ready[1])
                self.assertGreaterEqual(fresh[2],ready[3])
                self.assertFalse(f.controller.synchronized);self.assertFalse(f.jio.world_enabled)
                self.assertFalse(f.owner.status()['mapper_synchronized'])
                self.assertIsNone(f.owner._config)

    def test_late_static_consumer_marker_allows_a_fresh_config_transaction(self):
        for expires_during_poll in (False,True):
            with self.subTest(expires_during_poll=expires_during_poll):
                f=self.rig(installed=False);f.mapper.install_operation(f.controller)
                ready=f.mapper._operation_result
                f.mapper.pump(before_outputs=f.ingest)
                self.assertIsNone(f.mapper.link.pending);self.assertIsNone(f.mapper._operation_result)
                self.assertEqual(f.mapper.link.counts['sent'],1)
                if expires_during_poll:
                    endpoint=f.supervisor.link.endpoint
                    class DelayedReceive:
                        def __getattr__(self,name):return getattr(endpoint,name)
                        def recvmsg(self,*args):
                            value=endpoint.recvmsg(*args)
                            if value[0]:f.clock.set(ready[3])
                            return value
                    f.supervisor.link.endpoint=DelayedReceive()
                    self.addCleanup(lambda f=f,endpoint=endpoint:setattr(f.supervisor.link,'endpoint',endpoint))
                else:f.clock.set(ready[3])
                f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
                self.assertEqual(f.owner._consumer_ready,ready)
                self.assertIsNotNone(f.owner._config)
                self.assertGreaterEqual(f.owner._config['issued'],ready[3])
                self.assertFalse(f.owner.status()['mapper_synchronized']);self.assertFalse(f.jio.world_enabled)
                self.assertEqual(f.mapper._operation_ready,ready)

    def test_expired_config_retries_with_a_new_request_and_synchronizes_the_same_mapper(self):
        f=self.rig(installed=False);f.mapper.install_operation(f.controller)
        f.mapper.pump(before_outputs=f.ingest)
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        config=f.owner._config;packet=config['selected']
        self.assertIsNotNone(config['sent']);self.assertFalse(f.controller.synchronized)
        f.clock.set(config['expires'])
        self.assertEqual(f.owner.status()['last_reason'],'CONFIG_EXPIRED')
        self.assertEqual(f.owner.query(compute_fixtures.identifier(999))['reason'],'UNKNOWN_REQUEST')
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        fresh=f.owner._config
        self.assertIsNot(fresh,config)
        self.assertNotEqual(fresh['request_id'],config['request_id'])
        self.assertEqual((packet.issued_us,packet.expires_us),(config['issued'],config['expires']))
        self.assertFalse(f.owner.status()['mapper_synchronized']);self.assertFalse(f.jio.world_enabled)
        f.cycle()
        self.assertTrue(f.owner.status()['mapper_synchronized'])
        self.assertTrue(f.controller.synchronized)

    def test_polled_late_config_ack_applied_inside_original_e_wins_before_bootstrap_expiry(self):
        f=self.rig(installed=False);f.mapper.install_operation(f.controller)
        f.mapper.pump(before_outputs=f.ingest)
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        config=f.owner._config;packet=config['selected']
        f.clock.set(packet.issued_us+1000)
        f.mapper.pump(before_outputs=f.ingest)
        self.assertTrue(f.controller.synchronized)
        original,raw=f.controller._config_original
        ack=json.loads(raw)
        self.assertLess(int(ack['completed_j_us']),packet.expires_us)
        f.clock.set(packet.expires_us)
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        self.assertTrue(f.owner.status()['mapper_synchronized']);self.assertTrue(f.jio.world_enabled)
        self.assertIs(f.owner._config,config);self.assertEqual(config['result'],ack)
        self.assertEqual(original,(packet.body,packet.header['request_id'],packet.issued_us,packet.expires_us))
        self.assertEqual(f.controller._config_original[1],raw)

    def test_original_user_result_survives_local_delivery_expiry_without_reapplication(self):
        for blocked_at_mapper in (False,True):
            with self.subTest(blocked_at_mapper=blocked_at_mapper):
                f=self.rig();config=f.owner._config
                calls=[];apply=f.controller.apply
                def counted_apply(action,expires,gate):
                    calls.append((action,expires,gate))
                    return apply(action,expires,gate)
                f.controller.apply=counted_apply
                original_endpoint=f.mapper.link.endpoint
                if blocked_at_mapper:
                    class Blocked:
                        def __getattr__(self,name):return getattr(original_endpoint,name)
                        def send(self,raw):raise BlockingIOError()
                    f.mapper.link.endpoint=Blocked()
                    self.addCleanup(lambda f=f,endpoint=original_endpoint:setattr(f.mapper.link,'endpoint',endpoint))
                raw=f.request(1,'RUN',lifetime=10);f.owner.submit(raw)
                f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
                f.clock.set(1001);f.mapper.pump(before_outputs=f.ingest)
                record=f.owner._records[compute_fixtures.identifier(101)]
                self.assertEqual(f.controller.state,'RUN')
                original_ack=f.controller._history[compute_fixtures.identifier(101)][1]
                serial=f.compute.snapshot()['operation_serial']
                f.clock.set(5001001)
                self.assertEqual(f.owner.query(compute_fixtures.identifier(101))['status'],'UNKNOWN')
                if blocked_at_mapper:
                    f.mapper.pump(before_outputs=f.ingest)
                    self.assertEqual(f.mapper.link.pending.body,original_ack)
                    self.assertEqual(f.mapper.link.pending.issued_us,f.clock())
                    self.assertEqual(f.mapper.counts['operation_result_expired'],1)
                    f.mapper.link.endpoint=original_endpoint;f.cycle()
                else:
                    f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
                    self.assertEqual(f.supervisor.counts['operation_rejected'],0)
                self.assertIs(f.owner._config,config);self.assertTrue(f.owner._synchronized)
                self.assertEqual(record['request'].raw_bytes,raw)
                result=f.owner.query(compute_fixtures.identifier(101))
                self.assertEqual(canonical(result),original_ack)
                self.assertEqual(result['status'],'APPLIED');self.assertTrue(f.jio.world_enabled)
                self.assertEqual(f.compute.snapshot()['operation_serial'],serial)
                self.assertEqual(len(calls),1)

    def test_late_config_ack_from_previous_delivery_attempt_can_confirm_same_current_setting(self):
        f=self.rig(installed=False);f.mapper.install_operation(f.controller)
        f.mapper.pump(before_outputs=f.ingest)
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        old=f.owner._config
        endpoint=f.mapper.link.endpoint
        class Blocked:
            def __getattr__(self,name):return getattr(endpoint,name)
            def send(self,raw):raise BlockingIOError()
        f.mapper.link.endpoint=Blocked()
        try:
            f.mapper.pump(before_outputs=f.ingest)
            original_ack=f.mapper.link.pending.body
            serial=f.compute.snapshot()['operation_serial']
            f.clock.set(old['expires'])
            f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
            self.assertIsNot(f.owner._config,old)
            f.mapper._flush()
        finally:f.mapper.link.endpoint=endpoint
        f.mapper.pump(before_outputs=f.ingest)
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        self.assertEqual(canonical(old['result']),original_ack)
        self.assertTrue(f.owner.status()['mapper_synchronized'])
        self.assertEqual(f.compute.snapshot()['operation_serial'],serial)

    def test_historical_ack_cannot_roll_back_a_newer_revision_or_change_original_completion(self):
        f=self.rig();f.owner.submit(f.request(1,'RUN'))
        f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        f.mapper.pump(before_outputs=f.ingest)
        held,=f.supervisor.link.poll()
        old=json.loads(held.body)
        f.clock.set(int(old['expires_j_us']))
        self.assertEqual(f.owner.query(old['request_id'])['status'],'UNKNOWN')
        f.owner.submit(f.request(2,'RUN'));f.cycle()
        revision=f.owner.status()['config_revision']
        f.clock.set(f.clock()+5_000_000)
        def deliver(value):
            f.mapper.link.send('OPERATION_RESULT',{},canonical(value),request_id=value['request_id'],
                issued_us=f.clock(),expires_us=f.clock()+5_000_000)
            f.mapper.link.flush()
            f.supervisor.pump(f.material,bind_ready=True,on_publication=lambda *_:None)
        deliver(old)
        self.assertEqual(f.owner.status()['config_revision'],revision)
        self.assertEqual(f.owner.query(old['request_id']),old)
        invalid=dict(old,completed_j_us=old['expires_j_us']);deliver(invalid)
        self.assertEqual(f.supervisor.counts['operation_rejected'],1)
        self.assertEqual(f.owner.query(old['request_id']),old)
        self.assertEqual(f.owner.status()['config_revision'],revision)

    def test_real_compute_cancel_is_normal_mapper_terminal_and_lock_wait_cannot_hide_expiry(self):
        actor=phase_fixtures.Actor();clock=compute_fixtures.Clock();entered=threading.Event();release=threading.Event()
        processor=gate_fixtures.OperationProcessor(enter=entered,geometry_gate=release)
        compute=MapperCompute(lambda _:processor,source_generation=actor.binding.sha256,clock=clock)
        control=MapperControl(actor,compute,AbsoluteDeadline(1000,15001000),clock=clock);compute.start()
        try:
            compute.submit(compute_fixtures.item(1,generation=actor.binding.sha256));self.assertTrue(entered.wait(2))
            control.tick()
            apply_compute_operation(compute,None,action='STANDBY',expires_us=20000,gate_expires_us=9000,clock=clock)
            release.set()
            compute_fixtures.wait_until(lambda:compute.snapshot()['active'] is None)
            control.tick();watch=MapperJobWatch()
            for stage,sequence,_,fields in actor.progresses:
                if stage.startswith('MAPPER_'):
                    watch.observe(fields['request_id'],stage,sequence,fields['completed'],fields['reason'],now_us=clock())
            self.assertIsNone(watch.active);self.assertEqual(watch.last_terminal[-1],'OPERATION_FENCE')
            self.assertIsNone(control.latest_summary);self.assertIsNone(compute.terminal_error)
            locked=threading.Event();release_lock=threading.Event();requested=threading.Event();read_after=[]
            def holder():
                with compute._condition:
                    locked.set();release_lock.wait(2)
            def expiry():
                requested.wait(2);time.sleep(.02);clock.set(3000);release_lock.set()
            one=threading.Thread(target=holder,daemon=False);two=threading.Thread(target=expiry,daemon=False)
            one.start();self.assertTrue(locked.wait(2));two.start()
            def checked_clock():read_after.append(release_lock.is_set());return clock()
            try:
                requested.set()
                with self.assertRaises(OperationRequestError):
                    apply_compute_operation(compute,None,action='RUN',expires_us=2000,clock=checked_clock)
            finally:
                release_lock.set();one.join(2);two.join(2)
            self.assertFalse(one.is_alive());self.assertFalse(two.is_alive())
            self.assertEqual(read_after,[True]);self.assertTrue(compute.snapshot()['operation_standby'])
        finally:release.set();compute.request_stop();compute.join(2)


if __name__=='__main__':unittest.main()
