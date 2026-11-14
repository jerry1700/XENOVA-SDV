"""실제 JIO owner/seqpacket/snapshot의 원 R2 생성 trace를 검사한다.

FakeClient와 software Pi clock owner의 네 사건은 broker 송신·실물 Pi P4 수락이나
물리 FREE를 증명하지 않는다. capture/R1 trust와 생성 Reply R2를 구분한다.
"""

from dataclasses import FrozenInstanceError
from hashlib import sha256
import socket
import json
import struct
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_jetson_jio as jio_fixtures
import test_mapper_runtime_profile as runtime_fixtures
import test_mapper_sensor_state as sensor_fixtures
import test_source_identity as source_fixtures
from xenova_drive_world import decode, encode, schema
from xenova_drive_world.mapper_clock import ClockPolicy, MapperClockOwner, build_probe, candidate_body
from xenova_vehicle.perception.inputs import FrameStamp
from xenova_vehicle.runtime.data_channel import DataLink, DataPacket, data_pair
from xenova_vehicle.runtime.mapper_compute import MapperInput
from xenova_vehicle.runtime.mapper_operation import MapperOperationController
from xenova_vehicle.runtime.mapper_reply_trace import MapperReplyTrace
from xenova_vehicle.runtime.mapper_sensor_state import MapperSensorState, SensorSnapshotUnavailable
from xenova_vehicle.runtime.resident_workers import attach_sensor_snapshot
from xenova_vehicle.runtime.source_runtime import MapperSource, SupervisorSource
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.transport.jetson_jio import JetsonJio, PREFIX, VEHICLE_TOPIC
from xenova_vehicle.transport.jio_profile import JetsonJioProfile


def wait_for(predicate):
    end = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= end: raise AssertionError('actual software owner did not progress')
        time.sleep(.002)


def trace_for(f, *, generated=None, source=None, origin=None, probe=None):
    """기존 원 wire fixture의 명시 software 생성 비교다. JIO 실행 양성은 별도로 검사한다."""
    probe_raw = f.probe_raw if probe is None else probe
    value = decode(probe_raw, schema.MapperClockProbe)
    fields = f.fixture.source if source is None else source
    received = 1_010_000
    generated = f.now - 1 if generated is None else generated
    reply = schema.MapperClockReply(major=1, pi_host_boot=value.pi_host_boot,
        pi_client_instance=value.pi_client_instance, probe_seq=value.probe_seq, nonce=value.nonce,
        pi_send_us=value.pi_send_us, j2=received, j3=generated,
        **{key: data for key, data in fields.items() if key != 'vehicle'})
    return MapperReplyTrace.from_original(probe_raw, encode(reply), source=fields,
        origin=f.origin if origin is None else origin, probe_received_j_us=received,
        expires_j_us=received + 200_000)


class MapperReplyTraceTests(unittest.TestCase):
    def fixture(self):
        f = sensor_fixtures.MapperSensorStateTests('runTest'); f.setUp()
        f.state.ingest(canonical(f.snapshot()), now_us=f.now)
        return f

    def freeze(self, f, replies=(), *, now=None):
        return f.state.frame_snapshot(f.stamp, now_us=f.now if now is None else now,
            expires_us=f.now + 300_000, job_id=source_fixtures.uid(), source_generation=f.raw.sha256,
            source_key=('rgb_video', 12, 401000, 999000), clock_replies=replies)

    def test_snapshot_freezes_exact_probe_reply_and_original_clock_without_p4_or_restamping(self):
        f = self.fixture(); before = canonical(f.snapshot()); trace = trace_for(f)
        plain = self.freeze(f); frozen = self.freeze(f, (trace,))
        self.assertIsNone(plain.mapper_reply_bytes)
        self.assertIs(frozen.mapper_reply, trace)
        self.assertIs(frozen.mapper_reply_bytes, trace.raw_reply_bytes)
        self.assertEqual(frozen.clock_proofs, plain.clock_proofs)
        self.assertEqual(frozen.metric_inputs.clock_lease.trust_bytes, f.fixture.lease.trust_bytes)
        self.assertEqual(frozen.metric_inputs.clock_lease.raw_probe_bytes, f.probe_raw)
        self.assertEqual((frozen.capture_stamp, frozen.expires_us), (plain.capture_stamp, plain.expires_us))
        self.assertEqual(frozen.source_context_bytes, before)
        self.assertGreater(frozen.byte_length, plain.byte_length)
        with patch('xenova_vehicle.runtime.mapper_sensor_state.MAX_SENSOR_SNAPSHOT_BYTES', frozen.byte_length - 1):
            self.freeze(f)
            with self.assertRaises(SensorSnapshotUnavailable): self.freeze(f, (trace,))
        self.assertIsNone(frozen.p4_evidence)
        with self.assertRaises(FrozenInstanceError): frozen.mapper_reply = None
        changed = trace.source; changed['map_epoch'] = source_fixtures.uid()
        self.assertNotEqual(trace.source, changed)
        self.assertIsNone(plain.mapper_reply_bytes)  # 이후 snapshot은 과거 snapshot을 보강하지 않는다.

    def test_future_foreign_probe_origin_and_ambiguous_reply_never_replace_geometry_clock(self):
        f = self.fixture(); trace = trace_for(f)
        foreign_origin = dict(f.origin, gateway_instance=source_fixtures.uid())
        foreign = trace_for(f, origin=foreign_origin)
        foreign_source = trace_for(f, source=dict(f.fixture.source, configuration='foreign-configuration'))
        probe = decode(f.probe_raw, schema.MapperClockProbe); probe.probe_seq += 1
        other_probe = trace_for(f, probe=encode(probe))
        future = trace_for(f, generated=f.now + 1)
        different = trace_for(f, generated=f.now - 2)
        baseline = self.freeze(f).metric_inputs
        callback = MapperReplyTrace.from_original(f.probe_raw, trace.raw_reply_bytes, source=f.fixture.source,
            origin=f.origin, probe_received_j_us=trace.probe_received_j_us, expires_j_us=trace.expires_j_us)
        # 원 context의 callback과 다른 J2/J3를 갖는 별도 생성 비교도 선택하지 않는다.
        other = decode(callback.raw_reply_bytes, schema.MapperClockReply); other.j2 -= 1
        changed_callback = MapperReplyTrace.from_original(f.probe_raw, encode(other), source=f.fixture.source,
            origin=f.origin, probe_received_j_us=other.j2, expires_j_us=trace.expires_j_us - 1)
        for rows in ((foreign,), (foreign_source,), (other_probe,), (future,), (changed_callback,), (trace, different)):
            with self.subTest(rows=tuple(row.probe_sha256 for row in rows)):
                snapshot = self.freeze(f, rows)
                self.assertIsNone(snapshot.mapper_reply_bytes)
                self.assertEqual(snapshot.metric_inputs.clock_lease, baseline.clock_lease)
                self.assertEqual(snapshot.metric_inputs.reasons, baseline.reasons)
        # 원 local relay E 이후에도 이미 받은 생성 사실을 새 clock/관측으로 바꾸지 않는다.
        later = self.freeze(f, (trace,), now=trace.expires_j_us + 1)
        self.assertIs(later.mapper_reply, trace)
        self.assertEqual(later.mapper_reply.expires_j_us, trace.expires_j_us)
        with self.assertRaises(SensorSnapshotUnavailable): self.freeze(f, (trace,) * 9)

    def test_factory_rejects_changed_nonce_callback_source_and_payload_cap(self):
        f = self.fixture(); trace = trace_for(f)
        fields = dict(source=f.fixture.source, origin=f.origin,
            probe_received_j_us=trace.probe_received_j_us, expires_j_us=trace.expires_j_us)
        for changes in (dict(nonce=b'x' * 16), dict(j2=trace.probe_received_j_us + 1),
                        dict(mapper_instance=source_fixtures.uid()), dict(j3=trace.expires_j_us)):
            raw = decode(trace.raw_reply_bytes, schema.MapperClockReply)
            for key, value in changes.items(): setattr(raw, key, value)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MapperReplyTrace.from_original(f.probe_raw, encode(raw), **fields)
        for raw in (b'', bytes(1025)):
            with self.assertRaises(ValueError): MapperReplyTrace.from_original(f.probe_raw, raw, **fields)
        with self.assertRaises(TypeError): MapperReplyTrace()

    def test_actual_worker_attachment_preserves_owned_pixels_and_freezes_reply_port(self):
        f = runtime_fixtures.MapperRuntimeProfileTests('runTest'); f.setUp()
        self.addCleanup(f.doCleanups)
        item = f.item()
        original = MapperInput(item.job_id, item.source_generation, item.sample, item.expires_us,
            context_bytes=item.context_bytes)
        f.sensor_owner.ingest(original.context_bytes, now_us=f.now)
        trace = trace_for(f.fixture)
        actual = attach_sensor_snapshot(original, f.sensor_owner, f.config.capture_timing,
            now_us=f.now, clock_replies=(trace,))
        self.assertIs(actual.sample.rgb.payload, original.sample.rgb.payload)
        self.assertIs(actual.sample.depth.payload, original.sample.depth.payload)
        self.assertEqual((actual.job_id, actual.source_key, actual.expires_us),
            (original.job_id, original.source_key, original.expires_us))
        self.assertIs(actual.sensor_snapshot.mapper_reply, trace)


@unittest.skipUnless(hasattr(socket, 'SOCK_SEQPACKET'), 'actual Linux seqpacket required')
class MapperReplyRelayTests(unittest.TestCase):
    def setUp(self):
        self.calibration, self.raw, self.policy, self.sample, self.bootstrap, self.peer, self.instance = source_fixtures.fixture()
        self.clock = jio_fixtures.Clock(1000); self.client = jio_fixtures.FakeClient()
        self.policy_clock = ClockPolicy(vehicle=self.policy.vehicle, device='oak-front',
            configuration=self.policy.configuration, calibration=self.policy.calibration, primitive=self.policy.primitive,
            timestamp_error_us=0, drift_ppm=0, max_offset_width_us=100, lease_us=1_000_000,
            vehicle_max_age_us=200_000, lookback_us=500_000)
        self.jio = JetsonJio(JetsonJioProfile(self.policy, self.policy_clock, 'oak-front', '127.0.0.1', 1883),
            clock=self.clock, client_factory=lambda *_: self.client)
        left, right = data_pair()
        self.mapper = MapperSource(DataLink(left, self.peer, 'M', clock=self.clock), self.raw, self.policy,
            mapper_instance=self.instance, device='oak-front', clock=self.clock)
        def apply_run(action, expires, gate):
            if action != 'RUN' or self.clock() >= expires: raise ValueError('software RUN-only fixture')
            return self.clock()
        self.mapper.install_operation(MapperOperationController(self.peer, apply=apply_run, clock=self.clock))
        self.supervisor = SupervisorSource(DataLink(right, self.peer, 'S', clock=self.clock), self.raw, self.jio,
            mapper_instance=self.instance, mapper_boot=source_fixtures.uid(),
            clock_epoch=self.raw.fields['clock_epoch'], clock=self.clock)
        self.material = (dict(service_instance=self.peer.service_instance, storage_worker=source_fixtures.uid(),
            storage_profile_sha256='c' * 64, database_uuid=self.bootstrap['database_uuid'], db_schema_version=1), self.bootstrap)
        self.addCleanup(self.close)
        jio_fixtures.deliver(self.jio, VEHICLE_TOPIC, self.sample)
        self.cycle()
        self.assertIsNotNone(self.mapper.publication)

    def close(self):
        self.jio.stop(); self.assertTrue(self.jio.join(2))
        self.mapper.close(); self.supervisor.close()

    def cycle(self):
        for _ in range(16):
            self.supervisor.pump(self.material, bind_ready=True, on_publication=lambda *_: None)
            self.mapper.pump()

    def send(self, kind, body, metadata, *, issued=None, expires=None):
        issued = self.clock() if issued is None else issued
        packet = self.supervisor.link.send(kind, metadata, body, issued_us=issued,
            expires_us=issued + 100_000 if expires is None else expires)
        self.assertIsNotNone(packet); self.assertIsNotNone(self.supervisor.link.flush())
        self.mapper.pump()
        return packet

    def test_actual_jio_generated_r2_relay_and_frame_freeze_keep_r1_clock_proof(self):
        self.jio.start()
        pi = MapperClockOwner(self.policy_clock, self.sample.host_boot)
        pi_client = source_fixtures.uid()
        topic = f'{PREFIX}/mapper/oak-front/clock/request.pb'
        raw_reply = raw_probe = None
        for number in range(1, 4):
            at = 1100 + (number - 1) * 200_000
            sample = schema.VehicleSample(); sample.CopyFrom(self.sample)
            sample.sequence = number + 1; sample.source_sample += number
            sample.source_us = at; sample.pi_sample_us = at
            wire = encode(sample); pi.set_vehicle(wire, at)
            plan = pi.plan(at + 100, pi_client)
            self.assertIsNotNone(plan)
            raw_probe = build_probe(plan, at + 101)
            self.clock.now = at + 110
            self.client.incoming.extend((SimpleNamespace(topic=VEHICLE_TOPIC, payload=wire, qos=0, retain=False),
                SimpleNamespace(topic=topic, payload=raw_probe, qos=0, retain=False)))
            wait_for(lambda: len(self.client.published) >= number)
            raw_reply = self.client.published[number - 1][1]
            self.clock.now = at + 120
            pi.accept(plan, candidate_body(raw_probe, raw_reply, at + 120), at + 120)
            self.cycle()
        self.assertIsNotNone(self.jio.current_lease)
        # returned R1 수신 뒤 새 actual callback에 결박된 Vehicle만 fresh 수락한다.
        sample.sequence += 1; sample.source_sample += 1; sample.source_us += 30; sample.pi_sample_us += 30
        self.clock.now += 20
        self.client.incoming.append(SimpleNamespace(topic=VEHICLE_TOPIC, payload=encode(sample), qos=0, retain=False))
        wait_for(lambda: self.jio._heads.get(('VEHICLE', sample.host_boot, sample.gateway_instance,
            sample.device_session, sample.pi_session, sample.odom_epoch)) == sample.sequence)
        self.cycle(); original_context = self.mapper.context_bytes()
        owner = MapperSensorState(self.calibration, self.raw, self.policy_clock, tof_leaves={},
            vehicle_max_age_us=200_000, tof_max_age_us=200_000, max_speed_mps=.5, max_yaw_rate_rps=2.,
            max_gap_us=100_000, capacity=16, mapper_worker=self.peer.mapper_worker, mapper_instance=self.instance)
        inputs = owner.ingest(original_context, now_us=self.clock())
        self.assertIsNotNone(inputs.clock_lease); self.assertIsNotNone(inputs.vehicle)
        stamp = FrameStamp(self.raw.fields['acquisition_worker'], self.raw.fields['stream_instances']['rgb_video'],
            10, self.raw.fields['clock_epoch'], (self.clock() - 2, self.clock() - 1), self.clock())
        frozen = owner.frame_snapshot(stamp, now_us=self.clock(), expires_us=self.clock() + 100_000,
            job_id=source_fixtures.uid(), source_generation=self.raw.sha256,
            source_key=('rgb_video', 10, 100, 200), clock_replies=self.mapper.clock_replies)
        self.assertEqual(frozen.mapper_reply_bytes, raw_reply)
        self.assertEqual(frozen.mapper_reply.raw_probe_bytes, raw_probe)
        self.assertEqual(frozen.metric_inputs.clock_lease.raw_probe_bytes, raw_probe)
        self.assertIn((sha256(raw_probe).hexdigest(), raw_probe,
            sha256(inputs.clock_lease.trust_bytes).hexdigest(), inputs.clock_lease.trust_bytes), frozen.clock_proofs)
        self.assertIsNone(frozen.p4_evidence)
        self.assertEqual(self.mapper.context_bytes(), original_context)
        self.assertLessEqual(len(self.mapper.clock_replies), 8)

    def test_reply_before_probe_is_bounded_conflicts_rejected_and_expired_pending_dropped(self):
        source, origin = self.mapper.source, self.mapper.origin
        for number in range(10):
            probe = schema.MapperClockProbe(major=1, vehicle=source['vehicle'], device=source['device'],
                pi_host_boot=origin['pi_host_boot'], pi_client_instance=source_fixtures.uid(), probe_seq=number + 1,
                nonce=bytes([number + 1]) * 16, pi_send_us=900)
            raw = encode(probe)
            reply = schema.MapperClockReply(major=1, pi_host_boot=probe.pi_host_boot,
                pi_client_instance=probe.pi_client_instance, probe_seq=probe.probe_seq, nonce=probe.nonce,
                pi_send_us=probe.pi_send_us, j2=999, j3=1000,
                **{key: value for key, value in source.items() if key != 'vehicle'})
            trace = MapperReplyTrace.from_original(raw, encode(reply), source=source, origin=origin,
                probe_received_j_us=999, expires_j_us=1100)
            self.send('SOURCE_CLOCK_REPLY', trace.raw_reply_bytes, trace.metadata, expires=1100)
        self.assertEqual(len(self.mapper._clock_reply_packets), 8)
        self.assertEqual(self.mapper.clock_replies, ())
        metadata = dict(received_j_us='999', sensor_id=None, clock_probe_sha256=trace.probe_sha256)
        self.send('SOURCE_CLOCK', raw, metadata)
        self.assertEqual(self.mapper.clock_replies[0].raw_reply_bytes, trace.raw_reply_bytes)
        reply.j3 = 1001; self.clock.now = 1001
        self.send('SOURCE_CLOCK_REPLY', encode(reply), trace.metadata, issued=1001, expires=1100)
        self.assertEqual(self.mapper.counts['clock_reply_rejected'], 1)
        self.assertEqual(self.mapper.clock_replies[0].raw_reply_bytes, trace.raw_reply_bytes)
        foreign = trace.metadata; foreign['origin']['gateway_instance'] = source_fixtures.uid()
        self.send('SOURCE_CLOCK_REPLY', trace.raw_reply_bytes, foreign, issued=1000, expires=1100)
        self.assertEqual(self.mapper.counts['clock_reply_rejected'], 2)
        packet = next(reversed(self.mapper._clock_reply_packets.values()))
        header = packet.header; header['service_instance'] = source_fixtures.uid()
        encoded = json.dumps(header, sort_keys=True, separators=(',', ':')).encode('ascii')
        foreign_packet = DataPacket(struct.pack('<II', len(encoded), len(packet.body)) + encoded + packet.body)
        self.supervisor.link.endpoint.send(foreign_packet.raw)
        with self.assertRaisesRegex(ValueError, 'registered source/channel'): self.mapper.pump()
        self.assertEqual(self.mapper.clock_replies[0].raw_reply_bytes, trace.raw_reply_bytes)
        with self.assertRaises(ValueError): self.supervisor.link.send('SOURCE_CLOCK_REPLY', trace.metadata, bytes(1025))
        pending = self.supervisor.link.send('SOURCE_CLOCK_REPLY', trace.metadata, trace.raw_reply_bytes,
            issued_us=1000, expires_us=1100)
        next_sequence = self.supervisor.link._next
        self.clock.now = 1100; self.supervisor._flush()
        self.assertIsNone(self.supervisor.link.pending)
        self.assertFalse(self.supervisor.link.discard_clock_reply(pending))
        self.assertEqual(self.supervisor.link._next, next_sequence)
        self.assertEqual(self.mapper.clock_replies[0].raw_reply_bytes, trace.raw_reply_bytes)
        # pump의 최초 now는 E 전이지만 실제 cache 삽입 경계에서는 원 E에 도달한다.
        probe.probe_seq += 100; probe.nonce = b'z' * 16; probe.pi_send_us = 1000
        reply.probe_seq = probe.probe_seq; reply.nonce = probe.nonce; reply.pi_send_us = probe.pi_send_us
        reply.j2 = 1099; reply.j3 = 1100
        late = MapperReplyTrace.from_original(encode(probe), encode(reply), source=source, origin=origin,
            probe_received_j_us=1099, expires_j_us=1200)
        packet = self.supervisor.link.send('SOURCE_CLOCK_REPLY', late.metadata, late.raw_reply_bytes,
            issued_us=1100, expires_us=1200)
        self.assertIsNotNone(packet); self.assertIsNotNone(self.supervisor.link.flush())
        before = tuple(self.mapper._clock_reply_packets)
        actual = self.mapper._clock_reply
        def cross_original_expiry(original):
            self.clock.now = original.expires_us
            return actual(original)
        with patch.object(self.mapper, '_clock_reply', side_effect=cross_original_expiry): self.mapper.pump()
        self.assertEqual(tuple(self.mapper._clock_reply_packets), before)
        self.assertEqual(self.mapper.counts['clock_reply_expired'], 1)
        self.assertEqual((self.mapper.source, self.mapper.origin), (source, origin))
        self.assertEqual(self.mapper.clock_replies[0].raw_reply_bytes, trace.raw_reply_bytes)
        self.mapper.close()
        self.assertEqual(self.mapper.clock_replies, ())

    def test_actual_source_generation_fences_created_and_late_old_reply_without_clearing_clock_on_same_source(self):
        source, origin = self.mapper.source, self.mapper.origin
        probe = schema.MapperClockProbe(major=1, vehicle=source['vehicle'], device=source['device'],
            pi_host_boot=origin['pi_host_boot'], pi_client_instance=source_fixtures.uid(), probe_seq=1,
            nonce=b'q' * 16, pi_send_us=900, admission_revision=1)
        jio_fixtures.deliver(self.jio, f'{PREFIX}/mapper/oak-front/clock/request.pb', probe)
        pending = self.jio._probe
        raw = self.jio._reply(pending)
        token = self.jio._token
        self.assertFalse(self.jio.replace_source(source, **origin))
        self.assertIs(self.jio._token, token)
        self.assertEqual([row.raw for row in self.jio.take_inputs() if row.kind == 'CLOCK_REPLY'], [raw])
        self.jio._reply(pending)
        changed = dict(source, mapper_instance=source_fixtures.uid())
        self.assertTrue(self.jio.replace_source(changed, **origin))
        self.assertEqual(self.jio.take_inputs(), ())
        self.jio._reply(pending)  # 생성이 끝난 옛 token은 새 source cache에 들어갈 수 없다.
        self.assertEqual(self.jio.take_inputs(), ())
        self.jio.fence_source()
        self.assertEqual(self.jio.take_inputs(), ())


if __name__ == '__main__': unittest.main()
