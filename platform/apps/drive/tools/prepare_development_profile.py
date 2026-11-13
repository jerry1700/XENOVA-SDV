#!/usr/bin/env python3
"""원 개발 모델 metadata에서 RPi용 test primitive 입력을 새 디렉터리에 생성한다.

제품 모듈·시뮬레이터를 실행하지 않는다. 모델의 물리 상한과 LAB의 임시
제어 정책을 구분하며, 생성된 설정은 실차 승인·출력 권한을 부여하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
INPUTS = ('motion.canonical.json', 'development.canonical.json', 'calibration-bound.json')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError('bounded regular metadata file required: ' + str(path))
    raw = path.read_bytes()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate metadata key: ' + key)
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite metadata')))
    if not isinstance(value, dict):
        raise ValueError('metadata object required')
    return raw, value


def positive(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError('positive finite model dimension required')
    return value


def inputs(metadata, grid, receipt_sha256):
    receipt_raw, receipt = read(metadata / 'motion-metadata-receipt.json')
    if len(receipt_sha256) != 64 or sha(receipt_raw) != receipt_sha256:
        raise ValueError('original metadata receipt SHA differs')
    if (receipt.get('scope') != 'RPi_FIRMWARE_INPUT_DEVELOPMENT_MODEL_METADATA_ONLY'
            or receipt.get('physical_admission') is not False
            or receipt.get('device_started') is not False or receipt.get('app_started') is not False):
        raise ValueError('explicit development-only metadata receipt required')
    rows = receipt.get('files')
    if (not isinstance(rows, list) or len(rows) != len(INPUTS)
            or {row.get('path') for row in rows} != set(INPUTS)):
        raise ValueError('original metadata members differ')
    records = {}
    for row in rows:
        raw, value = read(metadata / row['path'])
        if row.get('bytes') != len(raw) or row.get('sha256') != sha(raw):
            raise ValueError('original metadata member hash/length differs: ' + row['path'])
        records[row['path']] = (raw, value)
    motion_raw, motion = records[INPUTS[0]]
    development_raw, development = records[INPUTS[1]]
    _, calibration = records[INPUTS[2]]
    if canonical(motion) != motion_raw or canonical(development) != development_raw:
        raise ValueError('original canonical model bytes differ')
    motion_sha = sha(motion_raw)
    calibration_sha = sha(canonical(calibration))
    if (motion_sha != receipt.get('motion_bundle_sha256')
            or calibration_sha != receipt.get('local_calibration_sha256')
            or calibration.get('vehicle_bundle_sha256') != motion_sha):
        raise ValueError('model/calibration binding differs')
    if (motion.get('identity') != development.get('identity')
            or calibration.get('provenance', {}).get('identity') != motion.get('identity')
            or motion.get('qualification') != {'input_kind': 'SYNTHETIC', 'measured': False, 'physical_admission': False}
            or development.get('qualification') != {'measured': False, 'physical_admission': False}
            or calibration.get('kind') != 'SYNTHETIC'):
        raise ValueError('explicit matching synthetic model identities required')
    vehicle = development['vehicle']
    odometry = motion['odometry']
    if vehicle.get('pose_origin') != 'REAR_AXLE_FLU':
        raise ValueError('rear axle pose origin required')
    for name in ('wheelbase', 'front', 'rear', 'width', 'length'):
        if positive(calibration['body'][name]) != positive(vehicle[name + '_m']):
            raise ValueError('body/calibration dimension differs: ' + name)
    if positive(odometry['wheelbase_m']) != vehicle['wheelbase_m']:
        raise ValueError('odometry wheelbase differs')
    grid_raw, grid_value = read(grid)
    if (set(grid_value) != {'width', 'height', 'resolution_m', 'origin_xy'}
            or any(type(grid_value[name]) is not int or not 1 <= grid_value[name] <= 256 for name in ('width', 'height'))
            or grid_value['width'] * grid_value['height'] > 4096
            or not isinstance(grid_value['origin_xy'], list) or len(grid_value['origin_xy']) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in grid_value['origin_xy'])):
        raise ValueError('bounded explicit World GridSpec required')
    positive(grid_value['resolution_m'])
    return records, receipt_raw, receipt, grid_raw, grid_value, motion, development, motion_sha, calibration_sha


def profile_values(motion, development, grid_value, policy, motion_sha):
    """정본 모델 필드만 대체하고 나머지 유한 LAB 정책은 원값으로 보존한다."""
    profile = dict(policy)
    vehicle = development['vehicle']
    changes = {
        'kind': 'test', 'id': motion['identity']['configuration'],
        'vehicle': motion['identity']['vehicle'], 'calibration': motion_sha,
        'assumptions': 'Unmeasured development model; no physical admission. Physical dimensions and limits are bound to the original motion/development/calibration metadata. Remaining collision, clock, search, avoidance and parking policies are provisional LAB software limits; device-clock and guest-clock bounds are distinct. World GridSpec is an initial development setting, not measured performance.',
        'resolution': grid_value['resolution_m'], 'wheelbase': vehicle['wheelbase_m'],
        'front': vehicle['front_m'], 'rear': vehicle['rear_m'], 'half_width': vehicle['width_m'] / 2,
        'max_steer': positive(vehicle['max_steer_rad']), 'steer_rate': positive(vehicle['max_steer_rate_rps']),
        'max_accel': positive(vehicle['max_accel_mps2']), 'max_brake': positive(vehicle['max_decel_mps2']),
        'max_velocity': positive(motion['odometry']['maximum_speed_mps']),
    }
    if policy['max_reverse_velocity'] > changes['max_velocity']:
        raise ValueError('explicit reverse policy exceeds model speed')
    profile.update(changes)
    return profile, changes


def prepare(metadata, grid, output, *, receipt_sha256):
    if output.exists() or output.is_symlink():
        raise ValueError('new output directory required; existing profiles are immutable')
    (records, receipt_raw, receipt, grid_raw, grid_value, motion,
     development, motion_sha, calibration_sha) = inputs(metadata, grid, receipt_sha256)
    policy_path = HOME / 'profiles/lattice-lab-1.json'
    policy_raw, policy = read(policy_path)
    profile, changes = profile_values(motion, development, grid_value, policy, motion_sha)
    # The fixed generator validates resource/rate/terminal hard caps before creating C.
    output.mkdir(parents=True)
    (output / 'profile.json').write_bytes(canonical(profile) + b'\n')
    spec = importlib.util.spec_from_file_location('development_primitive_generator', HOME / 'tools/generate_primitives.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.generate(output / 'profile.json', output)
    sources = output / 'sources'
    sources.mkdir()
    for name, (raw, _) in records.items():
        (sources / name).write_bytes(raw)
    (sources / 'motion-metadata-receipt.json').write_bytes(receipt_raw)
    (sources / 'grid.json').write_bytes(grid_raw)
    (sources / 'lattice-lab-policy.json').write_bytes(policy_raw)
    provenance = {
        'schema': 1, 'scope': 'RPi_DEVELOPMENT_PRIMITIVE_INPUT_ONLY',
        'physical_admission': False, 'arm_build_executed': False, 'board_test_executed': False,
        'metadata_receipt_sha256': sha(receipt_raw), 'source_manifest_sha256': receipt['source_manifest_sha256'],
        'motion_bundle_sha256': motion_sha, 'local_calibration_sha256': calibration_sha,
        'grid': grid_value, 'grid_sha256': sha(grid_raw), 'policy_sha256': sha(policy_raw),
        'derived_fields': sorted(changes), 'inherited_provisional_fields': sorted(set(policy) - set(changes)),
        'prepare_tool_sha256': sha(Path(__file__).read_text(encoding='utf-8').encode()),
        'source_files': {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p.read_bytes())}
                         for p in sorted(sources.iterdir())},
    }
    (output / 'source-receipt.json').write_bytes(json.dumps(provenance, indent=2).encode() + b'\n')
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-root', type=Path, required=True)
    parser.add_argument('--metadata-receipt-sha256', required=True)
    parser.add_argument('--grid', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.metadata_root, args.grid, args.output,
                     receipt_sha256=args.metadata_receipt_sha256)
    print(json.dumps({'output': str(args.output), 'scope': result['scope'],
                      'motion_bundle_sha256': result['motion_bundle_sha256'], 'physical_admission': False}))


if __name__ == '__main__':
    main()
