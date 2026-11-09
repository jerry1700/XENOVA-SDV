"""원 route 준비/Collector/SQL/Processor membership hook의 좁은 연결 회귀다.

기존 route fixture의 typed geometry leaf와 실제 임시 SQLite를 composition으로 쓴다.
실제 Processor ctor를 통과하지만 미자격 profile의 motion/target flag를 켜거나 CV
결과를 새로 승인하지 않는다. factory hook에는 원 typed draft만 명시 선택한다.
membership 소비 hook의 collector 연결은 이미 생성한 원 collector 참조의 단일
control-boundary handoff다. 실제 M actor/OAK/현재 route 권한/Pi D 수락 시험은 아니다.
"""

from base64 import b64decode
from dataclasses import replace
from hashlib import sha256
import os
import unittest
from uuid import uuid4

from xenova_drive_world import schema
from xenova_vehicle.runtime.map_checkpoint import MapCheckpointNotReady
from xenova_vehicle.runtime.mapper_mapping import MappingContextPort
from xenova_vehicle.runtime.mapper_processor import ResidentMapperProcessor
from xenova_vehicle.runtime.mapper_storage import MapperStorageMailbox
from xenova_vehicle.runtime.source_identity import SourcePolicy
from xenova_vehicle.storage.contracts import StorageJob, canonical, json_bytes
from xenova_vehicle.storage.mapper_state import decode_mapper_state


@unittest.skipUnless(os.name == 'posix', 'actual existing SQLite owner requires POSIX')
class MapperRouteCheckpointHookTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(schema, 'RoadSection'):
            self.skipTest('NOT_SUPPORTED: coherent B10 route contract required; this is not a paired PASS')
        # 구 common의 기본 discovery에서도 없는 route module을 강제 import하지 않는다.
        import test_mapper_route_checkpoint as fixtures
        self.fx = fixtures.MapperRouteCheckpointTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.r = self.fx.route
        self.scope = self.fx.scope
        # 원 pixels/clock/R2 시간을 바꾸지 않고 준비 뒤 실제 commit 사건을 진행한다.
        self.fx.now += 20
        self.port = MappingContextPort()
        self.mailbox = MapperStorageMailbox(self.scope.context.sha256)
        binding = dict(service_instance=self.r.world.fixture.raw.fields['service_instance'],
            storage_worker=str(uuid4()), storage_profile_sha256=self.fx.db.profile.sha256,
            database_uuid=self.scope.context.database_uuid, db_schema_version=1)
        bootstrap = dict(status='EMPTY', allocator_ready=True, schema_version=1,
            database_uuid=self.scope.context.database_uuid,
            allocator_highwater=self.fx.db.connection.execute(
                'SELECT allocator_highwater FROM storage_metadata WHERE singleton=1').fetchone()[0],
            selected_checkpoint=None)
        self.port.publish(self.scope.context, material=canonical(dict(binding=binding, bootstrap=bootstrap)))
        self.processor = self.factory()
        self.assertEqual(self.processor.maintenance(), 'PROFILE_NOT_READY')
        self.assertIsNotNone(self.processor._mapping)
        self.assertIsNone(self.processor._mapping.collector)
        # geometry/admission은 fixture의 원 producer에 남긴다. hook의 원 객체 수명만 연결한다.
        self.processor._mapping.collector = self.r.draft.collector

    def factory(self, *, configured=True):
        f = self.r.world; source = f.source
        args = dict(clock=lambda: self.fx.now)
        if configured:
            args.update(mapper_profile_bytes=self.r.profile.canonical_bytes,
                source_policy=SourcePolicy(source['vehicle'], source['configuration'], source['calibration'],
                    source['primitive'], f.calibration.sha256), clock_policy=f.fixture.policy,
                mapper_worker=self.scope.context.mapper_worker, mapper_instance=self.scope.context.origin_mapper_instance,
                mapping_context_port=self.port, mapping_mailbox=self.mailbox)
        return ResidentMapperProcessor(canonical(f.calibration.to_dict()), f.fixture.raw, **args)

    def consume_membership(self):
        self.processor._maintain_route_membership()
        return self.processor.route_checkpoint_membership

    def commit_and_accept(self):
        request, receipt = self.fx.checkpoint()
        self.assertTrue(self.r.draft.collector.accept_commit(request, receipt, now_us=self.fx.now))
        return request, receipt

    def pending_checkpoint(self, *, route=True):
        context = self.scope.context
        job = StorageJob(context, str(uuid4()), self.fx.now, self.fx.now + 150_000)
        return self.r.draft.collector.capture(self.r.world.pipeline, job, now_us=self.fx.now,
            catalog_evidence=self.r.state.evidence, subsystem_state=self.fx.meta if route else None)

    def test_factory_prepare_uses_frozen_original_reply_and_missing_trace_only_defers_route(self):
        p = self.processor; r = self.r
        p._mapping_draft = r.draft
        original_grid = r.draft.pipeline.grid
        original_clock = r.sensors.metric_inputs.clock_lease
        candidate = p._prepare_route_candidate(r.sensors)
        self.assertIsNotNone(candidate)
        self.assertEqual(p._route_status, 'PREPARED_NOT_DURABLE')
        self.assertIsNone(p._route_candidate)
        self.assertIsNone(p.route_checkpoint_membership)
        self.assertIs(candidate.scope, self.scope)
        value = json_bytes(candidate.state_bytes, 16384)
        self.assertEqual(b64decode(value['clock']['reply_b64'], validate=True), r.sensors.mapper_reply_bytes)
        self.assertEqual(b64decode(value['clock']['probe_b64'], validate=True), original_clock.raw_probe_bytes)
        self.assertEqual(b64decode(value['clock']['trust_b64'], validate=True), original_clock.trust_bytes)
        self.assertEqual(tuple(sorted({pin['provenance_id'] for pin in value['roots'].values()})), candidate.roots)
        self.assertEqual(r.draft.collector._route_roots, candidate.roots)
        # 준비 자료만으로는 public CP 입구가 채택 route로 승격하지 않는다.
        with self.assertRaisesRegex(MapCheckpointNotReady, 'original adopted frame candidate'):
            p.queue_mapping_checkpoint(self.fx.job(), subsystem_state={'route_records': value})
        item = r.draft.original_job
        missing = r.world.fixture.state.frame_snapshot(r.sensors.capture_stamp, now_us=self.fx.now,
            expires_us=item.expires_us, job_id=item.job_id, source_generation=item.source_generation,
            source_key=item.source_key)
        self.assertIsNone(p._prepare_route_candidate(missing))
        self.assertEqual(p._route_status, 'WAIT_FROZEN_REPLY')
        self.assertIs(r.draft.pipeline.grid, original_grid)
        self.assertEqual(missing.metric_inputs.clock_lease, original_clock)
        self.assertEqual(missing.metric_inputs.reasons, r.sensors.metric_inputs.reasons)
        self.assertEqual((missing.capture_stamp, missing.expires_us), (r.sensors.capture_stamp, r.sensors.expires_us))
        self.assertIsNone(missing.p4_evidence)
        self.assertEqual(p._route_checkpoint_state(), {})

    def test_actual_sql_commit_requires_collector_acceptance_before_processor_membership(self):
        collector = self.r.draft.collector
        self.assertIsNone(collector.route_checkpoint_membership)
        self.assertIsNone(self.consume_membership())
        request, receipt = self.fx.checkpoint()
        self.assertEqual(receipt.status, 'COMMITTED')
        self.assertIs(collector._pending, request)
        self.assertIsNone(collector.route_checkpoint_membership)
        self.assertIsNone(self.consume_membership())
        self.assertTrue(collector.accept_commit(request, receipt, now_us=self.fx.now))
        member = collector.route_checkpoint_membership
        self.assertIsNotNone(member)
        self.assertIs(self.consume_membership(), member)
        self.assertEqual(member.checkpoint_payload_sha256, request.payload.sha256)
        raw = bytes(self.fx.db.connection.execute('SELECT receipt_json FROM persistence_jobs WHERE job_id=?',
            (request.job.job_id,)).fetchone()[0])
        self.assertEqual(member.receipt_bytes, raw)
        self.assertEqual(member.receipt_sha256, sha256(raw).hexdigest())
        for record in self.fx.prepared.records:
            self.assertTrue(member.contains(member.checkpoint_ref, record.kind, record.body))
        self.assertFalse(hasattr(member, 'authority'))
        self.assertIsNone(self.processor._route_candidate)

    def test_unknown_and_actual_duplicate_never_create_new_route_membership(self):
        collector = self.r.draft.collector
        request, receipt = self.fx.checkpoint()
        unknown = replace(receipt, status='UNKNOWN', durable_revision=None, committed_j_us=None)
        self.assertFalse(collector.accept_commit(request, unknown, now_us=self.fx.now))
        self.assertIs(collector._pending, request)
        self.assertIsNone(self.consume_membership())
        duplicate = self.fx.store.commit(request.job, request.payload,
            current_context_sha256=self.scope.context.sha256)
        self.assertEqual(duplicate.status, 'DUPLICATE')
        # 기존 CP baseline 처리와 새 route membership factory의 COMMITTED 조건은 별도다.
        self.assertTrue(collector.accept_commit(request, duplicate, now_us=self.fx.now))
        self.assertIsNone(collector.route_checkpoint_membership)
        self.assertEqual(collector._route_membership_issue, 'ROUTE_MEMBERSHIP_NOT_READY')
        self.assertIsNone(self.consume_membership())

    def test_unoffered_checkpoint_retirement_preserves_original_committed_membership(self):
        first, receipt = self.commit_and_accept()
        member = self.consume_membership(); collector = self.r.draft.collector
        original = (member.checkpoint_ref, member.receipt_bytes, member.route_state_bytes,
            first.job.job_id, first.job.expires_us, receipt.committed_j_us)
        count = self.fx.db.connection.execute('SELECT COUNT(*) FROM persistence_jobs').fetchone()[0]
        pending = self.pending_checkpoint()
        self.assertIsNot(pending, first)
        self.assertIs(collector._pending, pending)
        self.assertIs(self.consume_membership(), member)
        collector.retire_checkpoint(pending)
        self.assertIsNone(collector._pending)
        self.assertIs(self.consume_membership(), member)
        self.assertEqual((member.checkpoint_ref, member.receipt_bytes, member.route_state_bytes,
            first.job.job_id, first.job.expires_us, receipt.committed_j_us), original)
        self.assertEqual(self.fx.db.connection.execute('SELECT COUNT(*) FROM persistence_jobs').fetchone()[0], count)

    def test_unconfigured_factory_and_legacy_checkpoint_do_not_add_route_metadata(self):
        old = self.factory(configured=False)
        self.assertEqual(old.maintenance(), 'MAPPING_DISABLED')
        self.assertIsNone(old._prepare_route_candidate(None))
        self.assertEqual(old._route_checkpoint_state(), {})
        request = self.pending_checkpoint(route=False)
        state, _ = decode_mapper_state(json_bytes(request.payload.manifest, 16384)['scene'], request.payload.evidence.contexts)
        self.assertNotIn('route_records', state)
        self.assertEqual(state['profile'], 'mapper-pipeline-state-1')
        receipt = self.fx.store.commit(request.job, request.payload,
            current_context_sha256=self.scope.context.sha256)
        self.assertEqual(receipt.status, 'COMMITTED')
        collector = self.r.draft.collector
        self.assertTrue(collector.accept_commit(request, receipt, now_us=self.fx.now))
        self.assertIsNone(collector.route_checkpoint_membership)
        self.assertEqual(collector._route_membership_issue, 'ABSENT')
        self.assertIsNone(self.consume_membership())


if __name__ == '__main__': unittest.main()
