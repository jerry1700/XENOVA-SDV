"""원 Pi gate와 M의 독립 기하 serial을 검산한다.

Gate/STOP는 명시 software protobuf fixture이며 실제 Pi STOP 또는 이동 수락이 아니다.
원 SensorState admission, compute thread/handshake, Linux seqpacket consumer를 사용한다.
구 common에는 Gate가 없으므로 해당 양성만 NOT_SUPPORTED로 제외한다. 이 제외는 PASS가 아니다.
"""

from dataclasses import replace
from hashlib import sha256
import socket
import threading
import unittest
from uuid import UUID

import test_jetson_jio as jio_fixtures
import test_mapper_compute as compute_fixtures
import test_mapper_sensor_state as sensor_fixtures
import test_scene_source_ports as scene_fixtures
from test_world_adapter import template
from xenova_drive_world import decode, encode, schema
from xenova_vehicle.runtime.mapper_compute import MapperCompute
from xenova_vehicle.runtime.mapper_operation import MapperOperationGate, mapper_operation_supported
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.transport.jetson_jio import JetsonJio, PREFIX


SUPPORTED = mapper_operation_supported()


class GateFixture:
    def __init__(self):
        self.f = sensor_fixtures.MapperSensorStateTests(methodName='runTest')
        self.f.setUp()
        self.owner = MapperOperationGate()
        self.now = self.f.now
        self.probe = decode(self.f.probe_raw, schema.MapperClockProbe)
        self.probe.operation_gate.CopyFrom(schema.PiOperationGate(
            gateway_instance=UUID(self.f.origin['gateway_instance']).bytes,
            control_generation=7, operation_revision=3, mode=0, wait_confirmed=True,
            manual_local_admitted=False, vehicle_sequence=1, stop_command_sequence=17,
            valid_until_pi_us=1_200_000))

    def ingest(self):
        raw = encode(self.probe); key = sha256(raw).hexdigest()
        snapshot = self.f.snapshot()
        snapshot['probes'] = {key: self.f.row(raw, key=key, received=1_010_000)}
        snapshot['active_probe_sha256'] = key
        # 원 Vehicle은 첫 ingest의 이 Probe를 참조한다. 이후 replay는 재-admit되지 않는다.
        for row in snapshot['vehicles'] + list(snapshot['tof'].values()):
            row['clock_probe_sha256'] = key
        self.inputs = self.f.state.ingest(canonical(snapshot), now_us=self.now)
        return self.inputs

    def observe(self, **changes):
        args = dict(sensor_state=self.f.state, inputs=self.inputs, origin=self.f.origin, now_us=self.now)
        args.update(changes)
        lease = self.inputs.clock_lease
        return self.owner.observe(lease.raw_probe_bytes, lease, **args)


class MapperOperationTests(unittest.TestCase):
    def test_absent_optional_schema_is_explicit_unsupported_run(self):
        owner = MapperOperationGate(); owner.supported = False
        decision = owner.observe(None, None, sensor_state=None, inputs=None, origin=None, now_us=1)
        self.assertFalse(decision.supported); self.assertFalse(decision.standby)
        self.assertEqual(decision.reason, 'UNSUPPORTED_OPERATION_SCHEMA')
        self.assertEqual(decision.probe_bytes, b''); self.assertIsNone(decision.vehicle_admission)

    @unittest.skipUnless(SUPPORTED, 'NOT_SUPPORTED: coherent B09 operation schema/helper required')
    def test_wait_and_manual_bind_exact_original_vehicle_not_latest_and_do_not_renew_e(self):
        f = GateFixture(); inputs = f.ingest(); decision = f.observe()
        self.assertTrue(decision.standby); self.assertEqual(decision.reason, 'WAIT_CONFIRMED')
        self.assertEqual(inputs.vehicle.anchor.sequence, 2)
        record = next(r for r in f.f.state.vehicle_admissions if r.evidence.anchor.sequence == 1)
        self.assertIs(decision.vehicle_admission, record)
        self.assertEqual(decision.probe_bytes, inputs.clock_lease.raw_probe_bytes)
        self.assertEqual(decision.expires_j_us, 1_099_990)
        self.assertEqual((decision.control_generation, decision.operation_revision), (7, 3))
        original_pose_samples = tuple(f.f.state.poses._samples)
        f.now += 1
        self.assertEqual(f.observe().expires_j_us, decision.expires_j_us)
        f.probe.probe_seq += 1
        f.probe.operation_gate.mode = 2
        f.probe.operation_gate.wait_confirmed = False
        f.probe.operation_gate.manual_local_admitted = True
        f.probe.operation_gate.operation_revision += 1
        f.ingest(); manual = f.observe()
        self.assertTrue(manual.standby); self.assertEqual(manual.reason, 'MANUAL_LOCAL_ADMITTED')
        self.assertIs(manual.vehicle_admission, record)
        self.assertEqual(tuple(f.f.state.poses._samples), original_pose_samples)
        self.assertEqual(f.f.state.fence_revision, 0)
        f.probe.pi_client_instance = compute_fixtures.identifier(700)
        f.probe.probe_seq = 1
        f.ingest()
        self.assertTrue(f.observe().standby)  # 실제 새 uplink client의 seq는 별도이며 운영 revision은 유지한다.

    @unittest.skipUnless(SUPPORTED, 'NOT_SUPPORTED: coherent B09 operation schema/helper required')
    def test_new_probe_absence_expiry_foreign_or_regressed_gate_is_run(self):
        f = GateFixture(); f.ingest(); original = f.observe(); self.assertTrue(original.standby)
        expired = f.observe(now_us=original.expires_j_us)
        self.assertFalse(expired.standby); self.assertEqual(expired.reason, 'OPERATION_GATE_EXPIRED')
        f.probe.probe_seq += 1; saved = encode(f.probe.operation_gate)
        f.probe.ClearField('operation_gate'); f.ingest()
        self.assertEqual(f.observe().reason, 'OPERATION_GATE_ABSENT')
        f.probe.probe_seq += 1; f.probe.operation_gate.CopyFrom(decode(saved, schema.PiOperationGate))
        f.probe.operation_gate.operation_revision -= 1; f.ingest()
        self.assertEqual(f.observe().reason, 'OPERATION_REVISION_REGRESSED')
        f.probe.probe_seq += 1; f.probe.operation_gate.operation_revision = 4
        f.probe.operation_gate.gateway_instance = UUID(compute_fixtures.identifier(999)).bytes
        f.ingest(); self.assertEqual(f.observe().reason, 'OPERATION_GATEWAY_MISMATCH')
        self.assertFalse(f.observe(origin={**f.f.origin, 'pi_host_boot': compute_fixtures.identifier(999)}).standby)

    @unittest.skipUnless(SUPPORTED, 'NOT_SUPPORTED: coherent B09 operation schema/helper required')
    def test_mode_alone_missing_original_seq_and_missing_current_admission_do_not_standby(self):
        f = GateFixture(); f.probe.operation_gate.wait_confirmed = False
        f.probe.operation_gate.mode = 2; f.ingest()
        self.assertEqual(f.observe().reason, 'OPERATION_GATE_NOT_PERMITTING')
        f.probe.probe_seq += 1; f.probe.operation_gate.manual_local_admitted = True
        f.probe.operation_gate.vehicle_sequence = 999; f.ingest()
        self.assertEqual(f.observe().reason, 'OPERATION_ORIGINAL_VEHICLE_UNAVAILABLE')
        self.assertFalse(f.observe(inputs=replace(f.inputs, vehicle=None)).standby)
        self.assertFalse(f.owner.observe(None, None, sensor_state=f.f.state,
            inputs=f.inputs, origin=f.f.origin, now_us=f.now).standby)


class OperationProcessor(compute_fixtures.Processor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.accepted_jobs = ['original-map']; self.cancelled_jobs = []
    def accepted(self, item): self.accepted_jobs.append(item.job_id)
    def cancelled(self, item, reason): self.cancelled_jobs.append((item.job_id, reason))


class MapperOperationComputeTests(unittest.TestCase):
    def cleanup(self, owner, *gates):
        owner.request_stop()
        for gate in gates: gate.set()
        owner.join(2)
        self.assertFalse(owner.is_alive); self.assertIsNone(owner.terminal_error)

    def test_standby_fences_active_and_pending_raw_but_keeps_factory_map_and_sensor_revision(self):
        entered = threading.Event(); gate = threading.Event(); factories = []
        processor = OperationProcessor(enter=entered, geometry_gate=gate)
        def factory(source): factories.append(source); return processor
        owner = MapperCompute(factory, source_generation='A', clock=compute_fixtures.Clock())
        owner.start()
        try:
            original = compute_fixtures.item(1); owner.submit(original); self.assertTrue(entered.wait(2))
            owner.submit(compute_fixtures.item(2)); self.assertTrue(owner.snapshot()['pending_raw'])
            self.assertTrue(owner.set_operation_standby(True)); self.assertFalse(owner.set_operation_standby(True))
            self.assertFalse(owner.snapshot()['pending_raw'])
            self.assertFalse(owner.submit(compute_fixtures.item(3)))
            gate.set(); cancelled = compute_fixtures.event_of(owner, 'CANCELLED')
            self.assertEqual(cancelled.reason, 'OPERATION_FENCE'); self.assertIsNone(owner.take_result())
            self.assertEqual(processor.accepted_jobs, ['original-map'])
            self.assertTrue(owner.set_operation_standby(False)); self.assertTrue(owner.submit(compute_fixtures.item(4)))
            compute_fixtures.event_of(owner, 'GEOMETRY_COMPLETED')
            self.assertEqual(factories, ['A']); self.assertEqual(owner._admission_fence, 0)
            self.assertEqual(owner.snapshot()['operation_serial'], 2)
        finally: self.cleanup(owner, gate)

    def test_parser_keeps_original_completed_fact_across_standby_run_aba(self):
        entered = threading.Event(); gate = threading.Event(); factories = []
        def parse(item, checkpoint):
            entered.set()
            if not gate.wait(2): raise AssertionError('bounded parser test gate expired')
            checkpoint('NN_DECODED')
            return item.sample.nn_output
        processor = OperationProcessor(parse=parse)
        def factory(source): factories.append(source); return processor
        owner = MapperCompute(factory, source_generation='A', clock=compute_fixtures.Clock())
        owner.start(); seen = []
        try:
            item = compute_fixtures.item(1, nn=True); owner.submit(item)
            start = compute_fixtures.event_of(owner, 'PARSER_START_REQUIRED', seen)
            owner.acknowledge_parser_start(item.job_id, compute_fixtures.sent(start))
            self.assertTrue(entered.wait(2))
            owner.set_operation_standby(True); owner.set_operation_standby(False); gate.set()
            cancelled = compute_fixtures.event_of(owner, 'CANCELLED', seen)
            self.assertEqual(cancelled.reason, 'OPERATION_FENCE')
            nn = next(value for value in seen if value.kind == 'NN_COMPLETED')
            self.assertTrue(nn.accepted); self.assertEqual(nn.nn_source_key, item.nn_source_key)
            self.assertEqual(processor.geometries, []); self.assertEqual(processor.accepted_jobs, ['original-map'])
            self.assertEqual(factories, ['A']); self.assertIsNone(owner.take_result())
        finally: self.cleanup(owner, gate)

    def test_queued_geometry_result_is_revoked_without_recalling_already_adopted_map_or_nn(self):
        processor = OperationProcessor()
        owner = MapperCompute(lambda _: processor, source_generation='A', clock=compute_fixtures.Clock())
        owner.start()
        try:
            item = compute_fixtures.item(1, nn=True); owner.submit(item)
            start = compute_fixtures.event_of(owner, 'PARSER_START_REQUIRED')
            owner.acknowledge_parser_start(item.job_id, compute_fixtures.sent(start))
            compute_fixtures.wait_until(lambda: owner.snapshot()['result_pending'] and owner.snapshot()['active'] is None)
            owner.set_operation_standby(True)
            self.assertIsNone(owner.take_result())
            events = owner.poll()
            self.assertTrue(next(e for e in events if e.kind == 'NN_COMPLETED').accepted)
            terminal = next(e for e in events if e.kind == 'GEOMETRY_COMPLETED')
            self.assertFalse(terminal.accepted); self.assertEqual(terminal.reason, 'OPERATION_FENCE')
            self.assertEqual(processor.accepted_jobs, ['original-map', item.job_id])
        finally: self.cleanup(owner)


class MapperOperationPortsTests(unittest.TestCase):
    def test_reply_support_requires_installed_consumer_and_coherent_schema_not_readiness(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                owner = JetsonJio(jio_fixtures.profile(), clock=jio_fixtures.Clock(1300200),
                    mapper_operation_installed=installed)
                jio_fixtures.bind(owner)
                raw = jio_fixtures.returned_probe()
                probe = decode(raw, schema.MapperClockProbe); probe.ClearField('mapper_trust')
                jio_fixtures.deliver(owner, f'{PREFIX}/mapper/oak-1/clock/request.pb', probe)
                self.assertIsNone(owner.current_lease)
                reply = decode(owner._reply(owner._probe), schema.MapperClockReply)
                if 'supported_mapper_features' in reply.DESCRIPTOR.fields_by_name:
                    self.assertEqual(reply.HasField('supported_mapper_features'), installed and SUPPORTED)
                    self.assertEqual(reply.supported_mapper_features, int(installed and SUPPORTED))
                else:
                    self.assertFalse(SUPPORTED)

    @unittest.skipUnless(hasattr(socket, 'SOCK_SEQPACKET'), 'actual Linux binary seqpacket required')
    def test_world_only_discard_preserves_original_negative_scene_stage_and_clock(self):
        fixture = scene_fixtures.SceneSourcePortTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups)
        runtime = fixture.runtime(); grant, lease = fixture.original_scene(runtime)
        mapper = runtime.mapper
        value = template(0)
        for key, data in mapper.source.items():
            if key != 'device': setattr(value, key, data)
        value.width = value.height = 2; value.resolution_m = .1
        value.occupancy = b'\xff'*4; value.cell_flags = b'\0'*4; value.age_le32 = b'\xff'*16
        value.observed_us = runtime.now-50; value.completed_us = value.published_us = runtime.now
        pub = sha256(canonical(mapper.publication)).hexdigest()
        self.assertTrue(mapper.offer_world(encode(value), publication_sha256=pub,
            issued_us=runtime.now, expires_us=runtime.now+100))
        negative = scene_fixtures.summary(grant, runtime.now); negative.status = 3; negative.failure_flags = 1
        raw = encode(negative)
        self.assertTrue(mapper.offer_survey_summary(raw, publication_sha256=pub,
            grant_sha256=sha256(encode(grant)).hexdigest(), issued_us=runtime.now, expires_us=runtime.now+100))
        summary = mapper._outputs['SURVEY_SUMMARY']; original_clock = runtime.jio.current_lease
        endpoint = mapper.link.endpoint
        class Blocked:
            def __getattr__(self, name): return getattr(endpoint, name)
            def send(self, raw): raise BlockingIOError()
        mapper.link.endpoint = Blocked()
        self.addCleanup(lambda: setattr(mapper.link, 'endpoint', endpoint))
        mapper.pump()
        self.assertEqual(mapper.link.pending.kind, 'WORLD_CANDIDATE')
        self.assertEqual(mapper.discard_world(), 1)
        self.assertNotIn('WORLD_CANDIDATE', mapper._outputs)
        self.assertIs(mapper._outputs['SURVEY_SUMMARY'], summary)
        mapper.pump(); pending_summary = mapper.link.pending
        self.assertEqual(pending_summary.kind, 'SURVEY_SUMMARY')
        self.assertEqual(mapper.discard_world(), 0)
        self.assertIs(mapper.link.pending, pending_summary)
        mapper.link.endpoint = endpoint
        runtime.cycle()
        self.assertEqual(runtime.jio.worlds, [])
        self.assertEqual(runtime.jio.summaries[-1][0], raw)
        self.assertIs(runtime.jio.current_lease, original_clock)
        self.assertIs(original_clock, lease)


if __name__ == '__main__': unittest.main()
