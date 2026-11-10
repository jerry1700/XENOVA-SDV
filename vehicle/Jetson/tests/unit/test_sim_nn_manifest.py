"""SIM 사전 선언·작은 원본 bytes·start 반환값 비교의 software 계약을 검사한다.

작은 fake asset은 private 순수 비교기에만 전달한다. production from_current는 이를
거절한다. 실제 고정15.97MB blob/SDK/Device/start/inference/pixel 동치 시험이 아니다.
구조-only manifest나 fake start dict를 실제 A 등록·READY·주행 권위로 만들지 않는다.
"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from xenova_vehicle.oak import sim_nn_manifest as manifests
from xenova_vehicle.oak.sim_nn import SimNnProfile
from xenova_vehicle.oak.yolop_asset import ValidatedYolopAsset, BLOB_NAME
from xenova_vehicle.yolop_profile import YolopBenchProfile

from test_yolop_asset import pinned_manifest


CONTEXT = 'c' * 64


def profile():
    return SimNnProfile(YolopBenchProfile('explicit-sim-oak', Path('owned-assets'), Path('owned-manifest.json')),
        'sim-wall:original-capture', 'jetson-monotonic:original-epoch', 'a' * 64)


def structural_fields():
    """실파일 SHA를 읽는 사전 선언이다. 고정 blob bytes 검증을 대체하지 않는다."""
    return manifests._fields(profile(), manifests._asset_summary())


def small_asset():
    """명시 fake 자기 일관성용이며 production pin은 한 개도 patch하지 않는다."""
    body = pinned_manifest()
    raw = b'small-explicit-unit-only-compiled-blob'
    digest = hashlib.sha256(raw).hexdigest()
    for item in body['files']:
        if item['name'] == BLOB_NAME:
            item['bytes'], item['sha256'] = len(raw), digest
    manifest_sha = hashlib.sha256(manifests._canonical(body)).hexdigest()
    expected = dict(expected_asset_id=body['asset_id'], expected_blob_bytes=len(raw),
        expected_blob_sha256=digest, expected_manifest_sha256=manifest_sha)
    receipt = dict(asset_id=body['asset_id'], manifest_canonical_sha256=manifest_sha,
        blob_sha256=digest, blob_bytes=len(raw),
        files=[{key: row[key] for key in ('name', 'bytes', 'sha256')} for row in body['files']],
        device_readback_verified=False, on_device_inference_verified=False)
    return ValidatedYolopAsset(raw, body, receipt), expected


def original_start(fields):
    """순수 receipt comparator용 고정 fixture다. SDK의 실제 반환 사건은 아니다."""
    asset = pinned_manifest()
    original = dict(**manifests._asset_summary(),
        files=[{key: row[key] for key in ('name', 'bytes', 'sha256')} for row in asset['files']],
        device_readback_verified=False, on_device_inference_verified=False)
    metadata = {key: value for key, value in asset['sdk_blob_metadata'].items() if key != 'host_graph_required_version'}
    graph = dict(profile_id='oak-host-sim-yolop-v1', input_kind='SYNTHETIC', camera_nodes=0,
        scope='host_SIM_node_connection_declaration', device_config_readback=False,
        nodes=[dict(id=key, name=name) for key, name in ((71, 'XLinkIn'), (5, 'ImageManip'),
            (19, 'NeuralNetwork'), (80, 'XLinkOut'), (4, 'XLinkOut'))],
        connections=[dict(output_id=left, input_id=right, output_name=out, input_name=into)
            for left, right, out, into in ((71, 5, 'out', 'inputImage'), (5, 19, 'out', 'in'),
                (19, 80, 'passthrough', 'in'), (19, 4, 'out', 'in'))],
        asset_storage_included=False,
        asset={key: fields['asset'][key] for key in ('asset_id', 'blob_sha256', 'blob_bytes')},
        declared_settings=deepcopy(fields['graph']['declared_settings']))
    value = dict(profile=deepcopy(fields['sim_profile']), graph_declaration=graph,
        device_mxid=fields['sim_profile']['approved_asset_device_settings']['device_mxid'],
        depthai_version='2.29.0.0', model_id=fields['asset']['asset_id'], model_asset=original,
        sdk_blob_metadata=metadata, blob_registration_count=1, pipeline_start_count=1,
        input_kind='SYNTHETIC', nn_context_key=CONTEXT, nn_ready=False, metric_ready=False, live_authority=False,
        model_residency_evidence='host_verified_asset_and_graph_start_only', startup_deadline_us='85000000')
    return value, dict(asset_receipt=deepcopy(original), sdk_metadata=deepcopy(metadata),
        context_key=CONTEXT, startup_deadline_us=90000000)


class SimNnManifestTests(unittest.TestCase):
    def test_structural_manifest_is_deterministic_owns_values_and_pins_all_sources(self):
        fields = structural_fields()
        raw = manifests._canonical(fields)
        first = manifests.SimNnManifest.from_bytes(raw)
        second = manifests.SimNnManifest.from_bytes(raw)
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertEqual(first.sha256, hashlib.sha256(raw).hexdigest())
        self.assertLessEqual(len(raw), 16 * 1024)
        self.assertEqual(set(first.fields), {'profile', 'sim_profile', 'asset', 'graph', 'parser', 'scope'})
        fields['sim_profile']['source_id'] = 'different-original'
        copied = first.fields
        copied['graph']['declared_settings']['input_type'] = 'RGB'
        self.assertEqual(first.fields['sim_profile']['source_id'], profile().source_id)
        self.assertEqual(first.fields['graph']['declared_settings']['input_type'], 'NV12')
        parser_sources = first.fields['parser']['source_sha256']
        self.assertIn('perception/decoders.py', parser_sources)
        self.assertIn('oak/sim_nn.py', parser_sources)
        for section in ('graph', 'parser'):
            for name, digest in first.fields[section]['source_sha256'].items():
                actual = (manifests._PACKAGE / name).read_bytes()
                self.assertEqual(digest, hashlib.sha256(actual).hexdigest())
        self.assertIsNone(first._asset_receipt)
        with self.assertRaises(TypeError):
            manifests.SimNnManifest(raw)

    def test_small_asset_pure_seam_checks_actual_bytes_and_cannot_enter_production_factory(self):
        asset, expected = small_asset()
        summary, receipt, metadata = manifests._checked_asset(asset, **expected)
        self.assertEqual(summary['blob_bytes'], len(asset.blob_bytes))
        self.assertEqual(summary['blob_sha256'], hashlib.sha256(asset.blob_bytes).hexdigest())
        self.assertEqual(json.loads(receipt), asset.receipt)
        self.assertEqual(json.loads(metadata)['num_shaves'], 6)
        with self.assertRaisesRegex(manifests.SimNnManifestError, 'actual blob bytes'):
            manifests.SimNnManifest.from_current(profile(), asset)
        asset.manifest['sdk_blob_metadata']['num_shaves'] = 8
        asset.receipt['files'].clear()
        self.assertEqual(json.loads(metadata)['num_shaves'], 6)
        self.assertTrue(json.loads(receipt)['files'])

    def test_small_asset_seam_rejects_blob_manifest_receipt_and_inventory_tampering(self):
        for kind in ('blob', 'manifest', 'receipt', 'inventory'):
            asset, expected = small_asset()
            if kind == 'blob':
                asset = replace(asset, blob_bytes=b'x' * len(asset.blob_bytes))
            elif kind == 'manifest':
                asset.manifest['input']['color_order'] = 'RGB'
            elif kind == 'receipt':
                asset.receipt['device_readback_verified'] = True
            else:
                asset.receipt['files'][0]['bytes'] = True
            with self.subTest(kind=kind), self.assertRaises(manifests.SimNnManifestError):
                manifests._checked_asset(asset, **expected)

    def test_structural_reader_rejects_unknown_missing_alias_nonfinite_and_unbounded_inputs(self):
        original = structural_fields()
        mutations = []
        value = deepcopy(original); value['unexpected'] = None; mutations.append(value)
        value = deepcopy(original); del value['parser']['configuration']; mutations.append(value)
        value = deepcopy(original); value['sim_profile']['camera_nodes'] = False; mutations.append(value)
        value = deepcopy(original); value['asset']['blob_bytes'] = True; mutations.append(value)
        value = deepcopy(original); value['graph']['source_sha256']['other.py'] = 'd' * 64; mutations.append(value)
        value = deepcopy(original); value['graph']['declared_settings']['passthrough_metadata_only'] = 1; mutations.append(value)
        value = deepcopy(original); value['parser']['configuration']['confidence'] = .25; mutations.append(value)
        value = deepcopy(original); value['parser']['source_sha256']['oak/sim_nn.py'] = 'D' * 64; mutations.append(value)
        for value in mutations:
            with self.subTest(value=str(value)[:120]), self.assertRaises(manifests.SimNnManifestError):
                manifests.SimNnManifest.from_bytes(manifests._canonical(value))
        for raw in (b'{"profile":1,"profile":2}', b'{"value":NaN}', b'[' * 1100 + b']' * 1100,
                b' ' * (16 * 1024 + 1), b'\xff', b'[]'):
            with self.subTest(raw=raw[:30]), self.assertRaises(manifests.SimNnManifestError):
                manifests.SimNnManifest.from_bytes(raw)

    def test_pure_start_comparison_preserves_original_smaller_deadline_and_two_output_edges(self):
        fields = structural_fields()
        receipt, args = original_start(fields)
        before = manifests._canonical(receipt)
        manifests._receipt(receipt, fields, **args)
        self.assertEqual(manifests._canonical(receipt), before)
        self.assertEqual(receipt['startup_deadline_us'], '85000000')
        receipt['graph_declaration']['nodes'].reverse()
        receipt['graph_declaration']['connections'].reverse()
        manifests._receipt(receipt, fields, **args)
        structural = manifests.SimNnManifest.from_bytes(manifests._canonical(fields))
        with self.assertRaisesRegex(manifests.SimNnManifestError, 'from_current'):
            manifests.validate_start_receipt(structural, receipt, context_key=CONTEXT, startup_deadline_us=90000000)
        with self.assertRaises(TypeError):
            manifests.SimNnStartEvidence()

    def test_pure_start_comparison_rejects_same_count_wrong_topology_and_physical_graph(self):
        fields = structural_fields()
        for kind in ('physical', 'bypass', 'same_output', 'reverse', 'duplicate', 'foreign', 'bool_id', 'bad_port'):
            receipt, args = original_start(fields)
            graph = receipt['graph_declaration']
            if kind == 'physical': graph['nodes'][0]['name'] = 'ColorCamera'
            elif kind == 'bypass': graph['connections'][0]['input_id'] = 19
            elif kind == 'same_output': graph['connections'][3]['input_id'] = 80
            elif kind == 'reverse': graph['connections'][1]['output_id'], graph['connections'][1]['input_id'] = 19, 5
            elif kind == 'duplicate': graph['connections'][3] = deepcopy(graph['connections'][2])
            elif kind == 'foreign': graph['connections'][0]['input_id'] = 99
            elif kind == 'bool_id': graph['nodes'][0]['id'] = True
            else: graph['connections'][0]['output_name'] = []
            with self.subTest(kind=kind), self.assertRaises(manifests.SimNnManifestError):
                manifests._receipt(receipt, fields, **args)

    def test_pure_start_comparison_rejects_python_input_property_as_native_port_alias(self):
        # 원 SDK 실행을 대신하지 않는 literal 비교 반례다. 세 native input은 각각 exact다.
        fields = structural_fields()
        for edge in (1, 2, 3):
            receipt, args = original_start(fields)
            receipt['graph_declaration']['connections'][edge]['input_name'] = 'input'
            with self.subTest(edge=edge), self.assertRaisesRegex(manifests.SimNnManifestError, 'topology'):
                manifests._receipt(receipt, fields, **args)

    def test_pure_start_comparison_rejects_changed_original_profile_asset_sdk_context_and_cap(self):
        fields = structural_fields()
        for kind in ('profile', 'model', 'sdk', 'asset', 'context', 'deadline', 'decimal', 'counter', 'ready', 'unknown'):
            receipt, args = original_start(fields)
            if kind == 'profile': receipt['profile']['calibration_id'] = 'e' * 64
            elif kind == 'model': receipt['model_id'] = 'physical-model'
            elif kind == 'sdk': receipt['sdk_blob_metadata']['outputs']['det_out']['offset'] += 2
            elif kind == 'asset': receipt['model_asset']['blob_sha256'] = 'e' * 64
            elif kind == 'context': receipt['nn_context_key'] = 'e' * 64
            elif kind == 'deadline': receipt['startup_deadline_us'] = '90000001'
            elif kind == 'decimal': receipt['startup_deadline_us'] = '085000000'
            elif kind == 'counter': receipt['blob_registration_count'] = True
            elif kind == 'ready': receipt['nn_ready'] = True
            else: receipt['extra'] = None
            with self.subTest(kind=kind), self.assertRaises(manifests.SimNnManifestError):
                manifests._receipt(receipt, fields, **args)


if __name__ == '__main__':
    unittest.main()
