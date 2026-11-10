"""RPi 개발 primitive 입력/빌드 선택의 순수 도구 회귀. ARM·보드 시험은 별도다."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'platform/apps/drive/profiles/course-development-1'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class DevelopmentPrimitiveInputsTest(unittest.TestCase):
    def setUp(self):
        self.tool = module('development_inputs', ROOT / 'platform/apps/drive/tools/prepare_development_profile.py')
        self.builder = module('drive_builder', ROOT / 'platform/scripts/build-xenova-drive-app.py')
        self.provenance = json.loads((BUNDLE / 'source-receipt.json').read_bytes())

    def test_recorded_model_grid_and_new_asset_are_exact(self):
        profile, asset, provenance = self.builder.lattice_inputs('warm-lattice-development')
        model = json.loads((BUNDLE / 'sources/development.canonical.json').read_bytes())
        motion_raw = (BUNDLE / 'sources/motion.canonical.json').read_bytes()
        self.assertEqual(profile['id'], model['identity']['configuration'])
        self.assertEqual(profile['vehicle'], model['identity']['vehicle'])
        self.assertEqual(profile['calibration'], hashlib.sha256(motion_raw).hexdigest())
        for target, source in (('wheelbase', 'wheelbase_m'), ('front', 'front_m'), ('rear', 'rear_m'),
                               ('max_steer', 'max_steer_rad'), ('steer_rate', 'max_steer_rate_rps'),
                               ('max_accel', 'max_accel_mps2'), ('max_brake', 'max_decel_mps2')):
            self.assertEqual(profile[target], model['vehicle'][source])
        self.assertEqual(profile['half_width'] * 2, model['vehicle']['width_m'])
        self.assertEqual(profile['resolution'], provenance['grid']['resolution_m'])
        legacy, old_asset, _ = self.builder.lattice_inputs('warm-lattice-lab')
        self.assertNotEqual(profile['id'], legacy['id'])
        self.assertNotEqual(asset['asset_sha256'], old_asset['asset_sha256'])
        self.assertEqual(profile['kind'], 'test')
        self.assertIs(provenance['physical_admission'], False)
        for key in provenance['inherited_provisional_fields']:
            self.assertEqual(profile[key], legacy[key], key)

    def test_repeated_generation_preserves_original_sources_and_assets(self):
        with tempfile.TemporaryDirectory(prefix='xenova-development-primitives-') as temporary:
            generated = Path(temporary) / 'new'
            self.tool.prepare(BUNDLE / 'sources', BUNDLE / 'sources/grid.json', generated,
                              receipt_sha256=self.provenance['metadata_receipt_sha256'])
            expected = {str(p.relative_to(BUNDLE)): p.read_bytes() for p in BUNDLE.rglob('*') if p.is_file()}
            actual = {str(p.relative_to(generated)): p.read_bytes() for p in generated.rglob('*') if p.is_file()}
            self.assertEqual(actual, expected)
            with self.assertRaisesRegex(ValueError, 'new output'):
                self.tool.prepare(BUNDLE / 'sources', BUNDLE / 'sources/grid.json', generated,
                                  receipt_sha256=self.provenance['metadata_receipt_sha256'])

    def test_partial_or_changed_original_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='xenova-development-invalid-') as temporary:
            source = Path(temporary) / 'sources'
            shutil.copytree(BUNDLE / 'sources', source)
            original = source / 'motion.canonical.json'
            original.write_bytes(original.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'hash/length'):
                self.tool.inputs(source, source / 'grid.json', self.provenance['metadata_receipt_sha256'])
            original.unlink()
            with self.assertRaisesRegex(ValueError, 'regular metadata'):
                self.tool.inputs(source, source / 'grid.json', self.provenance['metadata_receipt_sha256'])

    def test_calibration_body_or_original_receipt_cannot_be_relabelled(self):
        with self.assertRaisesRegex(ValueError, 'receipt SHA'):
            self.tool.inputs(BUNDLE / 'sources', BUNDLE / 'sources/grid.json', '0' * 64)
        with tempfile.TemporaryDirectory(prefix='xenova-development-body-') as temporary:
            source = Path(temporary) / 'sources'
            shutil.copytree(BUNDLE / 'sources', source)
            calibration = json.loads((source / 'calibration-bound.json').read_bytes())
            calibration['body']['width'] += .01
            raw = self.tool.canonical(calibration)
            (source / 'calibration-bound.json').write_bytes(raw)
            receipt = json.loads((source / 'motion-metadata-receipt.json').read_bytes())
            receipt['local_calibration_sha256'] = hashlib.sha256(raw).hexdigest()
            for row in receipt['files']:
                if row['path'] == 'calibration-bound.json':
                    row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            receipt_raw = self.tool.canonical(receipt)
            (source / 'motion-metadata-receipt.json').write_bytes(receipt_raw)
            with self.assertRaisesRegex(ValueError, 'body/calibration'):
                self.tool.inputs(source, source / 'grid.json', hashlib.sha256(receipt_raw).hexdigest())
        motion = json.loads((BUNDLE / 'sources/motion.canonical.json').read_bytes())
        development = json.loads((BUNDLE / 'sources/development.canonical.json').read_bytes())
        policy = json.loads((BUNDLE / 'sources/lattice-lab-policy.json').read_bytes())
        grid = json.loads((BUNDLE / 'sources/grid.json').read_bytes())
        motion['odometry']['maximum_speed_mps'] = .1
        with self.assertRaisesRegex(ValueError, 'reverse policy'):
            self.tool.profile_values(motion, development, grid, policy, '0' * 64)

    def test_builder_snapshot_includes_actual_model_and_generated_source(self):
        inputs = self.builder.fingerprint('drive')
        for member in ('profile.json', 'primitives.c', 'primitive-manifest.json', 'source-receipt.json',
                       'sources/motion.canonical.json', 'sources/grid.json'):
            name = 'platform/apps/drive/profiles/course-development-1/' + member
            self.assertEqual(inputs[name]['sha256'], hashlib.sha256((ROOT / name).read_bytes()).hexdigest())
        self.assertIn('platform/apps/drive/zephyr/profiles/warm-lattice-development.conf', inputs)

    def test_actual_compiler_input_cannot_silently_select_legacy_or_both_assets(self):
        home = ROOT / 'platform/apps/drive/zephyr'
        current = {'file': str(BUNDLE / 'primitives.c')}
        legacy = {'file': str(ROOT / 'platform/apps/drive/planner/generated/primitives.c')}
        self.assertEqual(self.builder.selected_primitive_source([current], home, 'warm-lattice-development'),
                         (BUNDLE / 'primitives.c').resolve())
        for sources in ([], [legacy], [current, legacy], [current, current]):
            with self.assertRaisesRegex(ValueError, 'compiler primitive input'):
                self.builder.selected_primitive_source(sources, home, 'warm-lattice-development')


if __name__ == '__main__':
    unittest.main()
