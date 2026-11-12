"""선택형 route META의 bytes/roots/상한 회귀다. 실제 D membership 시험은 별도다.

B10 준비 fixture의 typed software capture·등록·allocator를 재사용한다. 실제
JIO 출처, COMMITTED/완전 LOAD, Pi/P4·주행 수락을 이 codec 시험으로 주장하지 않는다.
"""

import builtins
from base64 import b64decode, b64encode
from copy import deepcopy
from hashlib import sha256
import unittest
from unittest.mock import patch
from uuid import uuid4

from xenova_drive_world import decode, encode, schema
from xenova_vehicle.storage.contracts import MAX_U63, PersistenceError, canonical
from xenova_vehicle.storage.mapper_route_state import (
    MAX_ROUTE_STATE_BYTES, PROFILE, STATE_KEY, decode_route_state, encode_route_state,
    route_state_roots, validate_route_state)
from xenova_vehicle.storage.mapper_state import decode_mapper_state, encode_mapper_state, mapper_state_roots


B10 = hasattr(schema, 'RoadGeometryScope') and hasattr(schema, 'RoadSection')


def _b64(raw):
    return b64encode(raw).decode('ascii')


def _replace_record(fields, index, value):
    from xenova_drive_world.route import route_record_sha256
    record = fields['records'][index]
    raw = encode(value)
    record['body_b64'] = _b64(raw)
    record['sha256'] = route_record_sha256(record['kind'], raw).hex()


@unittest.skipUnless(B10, 'NOT_SUPPORTED: coherent B10 route contract required')
class MapperRouteStateTests(unittest.TestCase):
    def setUp(self):
        # TestCase를 전역 alias/상속하지 않아 원 11개가 이 module에서 중복 수집되지 않는다.
        import test_mapper_route_records as route_fixture
        case = route_fixture.MapperRouteRecordsTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        prepared = case.prepare()
        self.prepared = prepared
        self.snapshot = case.sensors
        self.evidence = case.state.evidence
        records = {record.provenance_id: record for record in self.evidence.provenance}
        identities = dict(frame=prepared.frame_provenance_id, registration=prepared.alignment_provenance_id,
                          anchor=case.draft.collector._propagation_anchor_record[1])
        roots = {role: dict(provenance_id=identity, record_sha256=sha256(records[identity].encode()).hexdigest(),
                            payload_sha256=sha256(records[identity].payload).hexdigest())
                 for role, identity in identities.items()}
        lease = self.snapshot.metric_inputs.clock_lease
        self.fields = dict(profile=PROFILE,
            records=[dict(kind=record.kind, body_b64=_b64(record.body), sha256=record.sha256.hex())
                     for record in prepared.records],
            clock=dict(probe_b64=_b64(lease.raw_probe_bytes), trust_b64=_b64(lease.trust_bytes),
                       reply_b64=_b64(prepared.original_mapper_reply)), roots=roots)

    def test_roundtrip_owns_native_fields_and_preserves_original_bodies_clock_and_roots(self):
        raw = encode_route_state(self.fields)
        owned = validate_route_state(self.fields)
        self.assertIsNot(owned, self.fields)
        self.assertEqual(decode_route_state(raw), owned)
        self.assertLessEqual(len(raw), MAX_ROUTE_STATE_BYTES)
        self.assertEqual(tuple(b64decode(row['body_b64']) for row in owned['records']),
                         tuple(record.body for record in self.prepared.records))
        lease = self.snapshot.metric_inputs.clock_lease
        self.assertEqual(tuple(b64decode(owned['clock'][key]) for key in ('probe_b64', 'trust_b64', 'reply_b64')),
                         (lease.raw_probe_bytes, lease.trust_bytes, self.prepared.original_mapper_reply))
        expected = tuple(sorted({root['provenance_id'] for root in owned['roots'].values()}))
        self.assertEqual(route_state_roots(owned), expected)
        self.fields['records'][0]['body_b64'] = ''
        self.fields['roots']['frame']['provenance_id'] = 0
        self.assertEqual(encode_route_state(owned), raw)
        self.assertNotIn('durable', owned)
        self.assertNotIn('now_us', owned)

    def test_exact_shape_scalars_base64_and_canonical_json_reject_without_repair(self):
        def invalid(change):
            value = deepcopy(self.fields)
            change(value)
            with self.assertRaises(PersistenceError):validate_route_state(value)
        for change in (
                lambda v: v.update(extra=True), lambda v: v.update(profile='unknown'),
                lambda v: v['records'].reverse(), lambda v: v['records'][0].update(kind=True),
                lambda v: v['roots']['frame'].update(provenance_id=True),
                lambda v: v['roots']['frame'].update(provenance_id=MAX_U63+1),
                lambda v: v['roots']['frame'].update(extra=1),
                lambda v: v['records'][0].update(sha256='A'*64),
                lambda v: v['clock'].update(reply_b64=v['clock']['reply_b64']+'='),
                lambda v: v['clock'].update(probe_b64='A'*1369)):
            with self.subTest(change=change):invalid(change)
        raw = encode_route_state(self.fields)
        for bad in (b' '+raw, raw[:-1]+b',"profile":"mapper-route-records-1"}',
                    b'{"profile":NaN}', b'x'*(MAX_ROUTE_STATE_BYTES+1)):
            with self.subTest(raw_length=len(bad)):
                with self.assertRaises(PersistenceError):decode_route_state(bad)
        with self.assertRaises(PersistenceError):encode_mapper_state({STATE_KEY: None})

    def test_shared_record_domains_scope_and_role_hashes_cannot_be_substituted(self):
        for role, field in (('frame', 'record_sha256'), ('registration', 'record_sha256'), ('anchor', 'payload_sha256')):
            value = deepcopy(self.fields)
            value['roots'][role][field] = 'a'*64
            with self.subTest(role=role):
                with self.assertRaises(PersistenceError):validate_route_state(value)
        value = deepcopy(self.fields)
        value['records'][0]['sha256'] = sha256(self.prepared.records[0].body).hexdigest()
        with self.assertRaises(PersistenceError):validate_route_state(value)
        value = deepcopy(self.fields)
        section = decode(self.prepared.records[1].body, schema.RoadSection)
        section.geometry_scope_sha256 = b'z'*32
        _replace_record(value, 1, section)
        with self.assertRaises(PersistenceError):validate_route_state(value)
        value = deepcopy(self.fields)
        scope = decode(self.prepared.records[0].body, schema.RoadGeometryScope)
        scope.map_epoch = uuid4().bytes
        _replace_record(value, 0, scope)
        section = decode(self.prepared.records[1].body, schema.RoadSection)
        section.geometry_scope_sha256 = bytes.fromhex(value['records'][0]['sha256'])
        _replace_record(value, 1, section)
        with self.assertRaises(PersistenceError):validate_route_state(value)

    def test_noncanonical_or_unknown_proto_bytes_are_rejected_instead_of_reencoded(self):
        for suffix in (b'\xf8\x07\x01', b'\x08\x01'):
            value = deepcopy(self.fields)
            value['records'][0]['body_b64'] = _b64(self.prepared.records[0].body+suffix)
            with self.subTest(suffix=suffix):
                with self.assertRaises(PersistenceError):validate_route_state(value)
        self.assertEqual(b64decode(self.fields['records'][0]['body_b64']), self.prepared.records[0].body)

    def test_original_clock_nonce_source_and_returned_trust_cannot_be_repaired_by_new_hash(self):
        for change in ('nonce', 'source', 'future'):
            value = deepcopy(self.fields)
            reply = decode(self.prepared.original_mapper_reply, schema.MapperClockReply)
            section = decode(self.prepared.records[1].body, schema.RoadSection)
            if change == 'nonce':reply.nonce = bytes((reply.nonce[0] ^ 1,))+reply.nonce[1:]
            elif change == 'source':reply.mapper_boot = str(uuid4())
            else:reply.j3 = section.origin.completed_mapper_us+1
            raw = encode(reply)
            value['clock']['reply_b64'] = _b64(raw)
            section.origin.mapper_clock_reply_sha256 = sha256(raw).digest()
            _replace_record(value, 1, section)
            with self.subTest(change=change):
                with self.assertRaises(PersistenceError):validate_route_state(value)
        value = deepcopy(self.fields)
        trust = decode(b64decode(value['clock']['trust_b64']), schema.WorldTrust)
        trust.map_epoch = str(uuid4())
        value['clock']['trust_b64'] = _b64(encode(trust))
        with self.assertRaises(PersistenceError):validate_route_state(value)

    def test_shared_registration_anchor_identity_deduplicates_only_exact_original_hashes(self):
        roots = self.fields['roots']
        self.assertEqual(roots['registration'], roots['anchor'])
        expected = tuple(sorted({roots['frame']['provenance_id'], roots['registration']['provenance_id']}))
        self.assertEqual(route_state_roots(self.fields), expected)
        changed = deepcopy(self.fields)
        changed['roots']['anchor']['record_sha256'] = 'b'*64
        with self.assertRaises(PersistenceError):validate_route_state(changed)

    def test_optional_meta_roundtrip_roots_and_global_caps_still_apply(self):
        state = {'profile': 'route-state-unit-1', STATE_KEY: self.fields}
        ref, pages = encode_mapper_state(state)
        loaded, originals = decode_mapper_state({'mapper_state': ref}, pages)
        self.assertEqual(loaded, state)
        self.assertEqual(originals, pages)
        self.assertEqual(mapper_state_roots(loaded), route_state_roots(self.fields))
        with self.assertRaises(PersistenceError):
            encode_mapper_state(dict(state, padding='x'*(512*1024)))
        noise = ''.join(sha256(str(index).encode('ascii')).hexdigest() for index in range(6000))
        self.assertLess(len(canonical(dict(state, padding=noise))), 512*1024)
        with self.assertRaises(PersistenceError):encode_mapper_state(dict(state, padding=noise))


class MapperRouteStateLegacyTests(unittest.TestCase):
    def test_absent_route_keeps_old_canonical_state_and_never_imports_route_codec(self):
        legacy = {'profile': 'legacy-mapper-unit-1', 'value': [1, 'old']}
        raw = b'{"profile":"legacy-mapper-unit-1","value":[1,"old"]}'
        original_import = builtins.__import__
        attempted = []
        def guard(name, *args, **kwargs):
            if name.endswith('mapper_route_state') or name == 'xenova_drive_world.route':
                attempted.append(name)
                raise AssertionError('absent optional route must not import B10')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guard):
            ref, pages = encode_mapper_state(legacy)
            loaded, selected = decode_mapper_state({'mapper_state': ref}, pages)
            self.assertEqual(mapper_state_roots(loaded), ())
        self.assertEqual(attempted, [])
        self.assertEqual(canonical(loaded), raw)
        self.assertEqual(ref['state_sha256'], sha256(raw).hexdigest())
        self.assertEqual(selected, pages)
        self.assertNotIn(STATE_KEY, loaded)
