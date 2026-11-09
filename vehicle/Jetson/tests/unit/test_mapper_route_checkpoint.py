"""원 route META의 실제 SQLite CP/LOAD membership 회귀다.

기하·센서·R2 생성은 명시 software leaf다. 실제 CheckpointRequest/Collector/SQL/모든
선택 LOAD page를 사용하지만 JIO 생성, 실제 장치/주행, Pi의 독립 D owner는 수락하지 않는다.
PC에서 실행하지 않으며 coherent B10 source의 Nano selector다.
"""

from dataclasses import fields, replace
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_mapper_route_records as route_fixture
from xenova_drive_world import encode, schema
from xenova_vehicle.runtime.map_checkpoint import CheckpointPages, SourceComplete
from xenova_vehicle.runtime.mapper_reply_trace import MapperReplyTrace
from xenova_vehicle.runtime.mapper_route_checkpoint import (
    RouteCheckpointMembership, RouteCheckpointNotReady, from_prepared, from_committed, from_complete)
from xenova_vehicle.runtime.resident_storage import _Database, StorageProfile
from xenova_vehicle.runtime.scene import SceneEvidenceScope
from xenova_vehicle.storage.contracts import StorageJob, LoadRequest, canonical, json_bytes
from xenova_vehicle.storage.mapper_state import decode_mapper_state
from xenova_vehicle.storage.mapper_route_state import STATE_KEY, encode_route_state
from xenova_vehicle.storage.records import CheckpointCommitOrigin, ContextRecord, EvidenceTable
from xenova_vehicle.storage.repository import PersistenceStore


def uid():
    return str(uuid4())


def corrupted_complete(original, **changes):
    """음성 변조 전용이다. 양성은 실제 CheckpointPages에서만 만든다."""
    value = object.__new__(SourceComplete)
    for field in fields(SourceComplete):
        object.__setattr__(value, field.name, changes.get(field.name, getattr(original, field.name)))
    return value


@unittest.skipUnless(os.name == 'posix', 'actual existing D SQLite owner requires POSIX')
@unittest.skipUnless(hasattr(schema, 'RoadSection'), 'NOT_SUPPORTED: coherent B10 route contract required')
class MapperRouteCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = _Database(StorageProfile(str(Path(temporary.name) / 'route.sqlite3'), 'CREATE_IF_ABSENT'),
            time.monotonic_ns() // 1000 + 5_000_000)
        self.addCleanup(self.db.close)
        # 실제 DB가 먼저 정한 UUID를 최초 fixture ingest 이전에 선택한다. SQL identity는 고치지 않는다.
        sensor_class = route_fixture.world_fixture.sensors_fixture.MapperSensorStateTests
        original_setup = sensor_class.setUp
        def sensor_setup(owner):
            original_setup(owner)
            owner.context = replace(owner.context, database_uuid=self.db.metadata['database_uuid'])
        def reserve(original_job, nonce, *_):
            self.now = original_job.issued_us + 100
            self.store = PersistenceStore(self.db.connection, self.db.metadata['database_uuid'],
                expires_us=self.now + 5_000_000, clock_us=lambda: self.now)
            self.store.reserve_provenance(StorageJob(original_job.context, uid(), self.now, self.now + 150_000),
                uid(), block_count=999, current_context_sha256=original_job.context.sha256)
            grant = self.store.reserve_provenance(original_job, nonce, block_count=256,
                current_context_sha256=original_job.context.sha256)
            self.assertEqual((grant.start_inclusive, grant.end_exclusive), (1000, 1256))
            return grant
        self.route = route_fixture.MapperRouteRecordsTests(methodName='runTest')
        self.addCleanup(self.route.doCleanups)
        with patch.object(sensor_class, 'setUp', sensor_setup), patch.object(route_fixture, 'ProvenanceRange', reserve):
            self.route.setUp()
        r = self.route; f = r.world
        # 이 test의 software R2를 최초 준비 전에 원 Probe row callback에 맞춘다. JIO 실행은 아니다.
        received = int(f.fixture.snapshot()['probes'][f.fixture.probe_key]['received_j_us'])
        r.reply.j2, r.reply.j3 = received, received + 1
        r.raw_reply = encode(r.reply)
        trace = MapperReplyTrace.from_original(f.lease.raw_probe_bytes, r.raw_reply,
            source=f.source, origin=f.fixture.origin, probe_received_j_us=received,
            expires_j_us=f.now + 150_000)
        snapshot = f.fixture.state.frame_snapshot(r.sensors.capture_stamp, now_us=f.now,
            expires_us=f.item.expires_us, job_id=f.item.job_id, source_generation=f.item.source_generation,
            source_key=f.item.source_key, clock_replies=(trace,))
        self.assertEqual(snapshot.mapper_reply_bytes, r.raw_reply)
        item = r.draft.original_job.with_sensor_snapshot(snapshot, now_us=f.now)
        r.sensors = snapshot; r.inputs = snapshot.metric_inputs
        r.draft = replace(r.draft, original_job=item, inputs=r.inputs,
            catalog_draft=replace(r.draft.catalog_draft, original_job=item))
        self.scope = r.draft.frame.scope
        self.prepared = r.prepare()
        self.meta = from_prepared(self.prepared, r.sensors, r.state.evidence, scope=self.scope)

    def job(self):
        return StorageJob(self.scope.context, uid(), self.now, self.now + 150_000)

    def checkpoint(self):
        # 원 CP에 저장되지만 LOAD의 route/root closure에는 필요 없는 원 context다.
        extra = ContextRecord(canonical(dict(kind='METRIC', body=dict(profile='unreferenced-software-diagnostic-1'))))
        evidence = self.route.state.evidence
        evidence = EvidenceTable(evidence.provenance, evidence.contexts + (extra,))
        self.request = self.route.draft.collector.capture(self.route.world.pipeline, self.job(), now_us=self.now,
            catalog_evidence=evidence, subsystem_state=self.meta)
        self.receipt = self.store.commit(self.request.job, self.request.payload,
            current_context_sha256=self.scope.context.sha256)
        self.assertEqual(self.receipt.status, 'COMMITTED')
        return self.request, self.receipt

    def member(self, request=None, receipt=None, **changes):
        args = dict(scope=self.scope, observed_us=self.now, current_context_sha256=self.scope.context.sha256,
            now_us=self.now)
        args.update(changes)
        return from_committed(self.request if request is None else request,
            self.receipt if receipt is None else receipt, **args)

    def complete(self):
        reference = canonical(self.store.latest_complete(expires_us=self.now + 150_000))
        request = LoadRequest(self.job(), reference, canonical(dict(kind='CHECKPOINT_PAGE', cursor=0)))
        loaded = self.store.load(request, current_context_sha256=self.scope.context.sha256)
        self.loaded = loaded
        complete = CheckpointPages(reference, self.scope.context).accept(loaded, now_us=self.now)
        self.assertIsInstance(complete, SourceComplete)
        return complete

    def test_prepared_metadata_keeps_original_pair_clock_root_domains_without_durable_success(self):
        state = self.meta[STATE_KEY]
        self.assertEqual([row['kind'] for row in state['records']], [8, 3])
        self.assertLessEqual(len(encode_route_state(state)), 16384)
        self.assertEqual(state['roots']['frame']['provenance_id'], self.prepared.frame_provenance_id)
        self.assertEqual(state['roots']['registration']['provenance_id'], self.prepared.alignment_provenance_id)
        self.assertNotIn('receipt', state)
        self.assertIsNone(from_prepared(None, None, None, scope=self.scope))
        state['roots']['frame']['provenance_id'] = 1
        self.assertNotEqual(state, from_prepared(self.prepared, self.route.sensors,
            self.route.state.evidence, scope=self.scope)[STATE_KEY])

    def test_missing_original_reply_trace_is_not_replaced_by_probe_trust_or_latest(self):
        r = self.route; item = r.draft.original_job; owner = r.world.fixture.state
        snapshot = owner.frame_snapshot(r.sensors.capture_stamp, now_us=self.now,
            expires_us=item.expires_us, job_id=item.job_id, source_generation=item.source_generation,
            source_key=item.source_key)
        self.assertIsNone(snapshot.mapper_reply_bytes)
        with self.assertRaisesRegex(RouteCheckpointNotReady, 'frozen snapshot trace'):
            from_prepared(self.prepared, snapshot, r.state.evidence, scope=self.scope)
        for raw in (b'', r.inputs.clock_lease.raw_probe_bytes):
            with self.subTest(raw=raw[:8]), self.assertRaises(RouteCheckpointNotReady):
                from_prepared(replace(self.prepared, original_mapper_reply=raw), r.sensors, r.state.evidence, scope=self.scope)

    def test_original_body_probe_context_and_root_bytes_cannot_be_relabelled(self):
        r = self.route
        altered = replace(self.prepared.records[1], body=self.prepared.records[1].body + b'\x00')
        changes = (dict(records=(self.prepared.records[0], altered)),
            dict(original_probe_sha256=b'x' * 32), dict(original_geometry_context_bytes=b'{}'),
            dict(context_sha256='f' * 64), dict(source_binding_revision=self.prepared.source_binding_revision + 1),
            dict(original_provenance_bytes=(self.prepared.original_provenance_bytes[0][:-1] + b'\x01',
                *self.prepared.original_provenance_bytes[1:])))
        for change in changes:
            with self.subTest(change=tuple(change)), self.assertRaises(RouteCheckpointNotReady):
                from_prepared(replace(self.prepared, **change), r.sensors, r.state.evidence, scope=self.scope)

    def test_missing_parent_or_context_is_not_a_complete_semantic_root_closure(self):
        r = self.route; evidence = r.state.evidence
        roots = {pin['provenance_id'] for pin in self.meta[STATE_KEY]['roots'].values()}
        parent = next(row for row in evidence.provenance if row.provenance_id not in roots)
        choices = (EvidenceTable(tuple(row for row in evidence.provenance if row is not parent), evidence.contexts),
            EvidenceTable(evidence.provenance, tuple(row for row in evidence.contexts if row.sha256 != parent.context_sha256)))
        for reduced in choices:
            with self.subTest(records=len(reduced.provenance)), self.assertRaises(RouteCheckpointNotReady):
                from_prepared(self.prepared, r.sensors, reduced, scope=self.scope)

    def test_actual_sql_committed_membership_uses_original_receipt_json_and_bounded_records(self):
        request, receipt = self.checkpoint()
        view = self.member()
        raw = bytes(self.db.connection.execute('SELECT receipt_json FROM persistence_jobs WHERE job_id=?',
            (request.job.job_id,)).fetchone()[0])
        self.assertEqual(view.receipt_bytes, raw)
        self.assertEqual(view.receipt_sha256, sha256(raw).hexdigest())
        self.assertEqual(view.checkpoint_payload_sha256, request.payload.sha256)
        self.assertEqual(view.route_state_bytes, encode_route_state(self.meta[STATE_KEY]))
        for record in self.prepared.records:
            self.assertTrue(view.contains(view.checkpoint_ref, record.kind, record.body))
            self.assertFalse(view.contains(view.checkpoint_ref, record.kind, record.body + b'\x00'))
        self.assertFalse(view.contains(b'{}', 8, self.prepared.records[0].body))
        self.assertFalse(any(hasattr(view, name) for name in ('payload', 'evidence', 'tiles', 'current', 'authority', 'capability')))
        with self.assertRaises(TypeError):
            RouteCheckpointMembership()
        self.assertEqual(receipt.status, 'COMMITTED')

    def test_duplicate_unknown_late_or_rebound_completion_cannot_create_current_membership(self):
        self.checkpoint()
        duplicate = self.store.commit(self.request.job, self.request.payload, current_context_sha256=self.scope.context.sha256)
        self.assertEqual(duplicate.status, 'DUPLICATE')
        unknown = replace(self.receipt, status='UNKNOWN', durable_revision=None, committed_j_us=None)
        for receipt in (duplicate, unknown, replace(self.receipt, job_digest='f' * 64)):
            with self.subTest(status=receipt.status), self.assertRaises(RouteCheckpointNotReady):
                self.member(receipt=receipt)
        for change in (dict(observed_us=self.request.job.expires_us), dict(now_us=self.request.job.expires_us),
                       dict(current_context_sha256='f' * 64), dict(observed_us=self.receipt.committed_j_us - 1)):
            with self.subTest(change=change), self.assertRaises(RouteCheckpointNotReady):
                self.member(**change)

    def test_actual_complete_load_preserves_original_cp_hash_when_load_closure_sha_is_different(self):
        self.checkpoint(); committed = self.member(); complete = self.complete()
        self.assertNotEqual(complete.evidence.sha256, self.request.payload.evidence.sha256)
        loaded = from_complete(complete, scope=self.scope, now_us=self.now)
        self.assertEqual(loaded, committed)
        self.assertEqual(loaded.checkpoint_payload_sha256, complete.original_commit.payload_sha256(complete.manifest))
        self.assertEqual(complete.original_commit.evidence_sha256, self.request.payload.evidence.sha256)
        self.assertEqual(loaded.receipt_bytes, canonical(complete.original_commit.receipt.fields))

    def test_partial_load_missing_meta_and_descriptor_tampering_are_not_complete_membership(self):
        self.checkpoint(); complete = self.complete()
        with self.assertRaises(RouteCheckpointNotReady):
            from_complete(self.loaded, scope=self.scope, now_us=self.now)
        state, metadata = decode_mapper_state(json_bytes(complete.manifest, 16384)['scene'], complete.evidence.contexts)
        missing = EvidenceTable(complete.evidence.provenance,
            tuple(row for row in complete.evidence.contexts if row.sha256 != metadata[0].sha256))
        bad_receipt = replace(complete.original_commit.receipt, job_digest='f' * 64)
        for change in (dict(original_commit=None), dict(evidence=missing), dict(state=canonical(dict(state, route_records={}))),
            dict(page_receipts=()), dict(original_commit=CheckpointCommitOrigin(bad_receipt,
                complete.original_commit.evidence_sha256, complete.original_commit.copy_tiles))):
            with self.subTest(change=tuple(change)), self.assertRaises(RouteCheckpointNotReady):
                from_complete(corrupted_complete(complete, **change), scope=self.scope, now_us=self.now)

    def test_complete_requires_last_original_head_expiry_and_exact_registered_current_map_context(self):
        self.checkpoint(); complete = self.complete()
        with self.assertRaises(RouteCheckpointNotReady):
            from_complete(complete, scope=self.scope, now_us=complete.page_receipts[-1][0].job.expires_us)
        context = self.scope.context; binding = json_bytes(context.source_binding, 2048)
        binding['mapper_worker'] = uid()
        context = replace(context, mapper_worker=binding['mapper_worker'], source_binding=canonical(binding))
        scope = SceneEvidenceScope(context, self.scope.raw_binding, self.scope.calibration, self.scope.method_manifest_sha256)
        with self.assertRaises(RouteCheckpointNotReady):
            from_complete(complete, scope=scope, now_us=self.now)
        with self.assertRaises(RouteCheckpointNotReady):
            self.member(scope=scope)


if __name__ == '__main__':
    unittest.main()
