"""Prepare matching synthetic Scene, Jetson mapper/calibration and Pi model metadata."""
import argparse
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
from pathlib import Path

from generate_jetson_tracks import VARIANTS, generate, read_geometry
from tools.hil_observation.resident import load_motion_profile
from xenova_vehicle.perception.calibration import canonical as canonical_calibration
from xenova_vehicle.perception.tape import TapeProfile
from xenova_vehicle.runtime.mapper_profile import MapperProfile
from xenova_vehicle.simulation.profile import DevelopmentProfile
from xenova_vehicle.simulation.scene import Scene, canonical, demo_scene


def prepare(geometry_path, metadata, mapper_path, output, *, metadata_receipt_sha256,
            position_drift_mps=None, yaw_drift_rps=None):
    """Use the frozen demo model as the parent of an explicitly derived synthetic run."""
    geometry = read_geometry(geometry_path)
    if 'lane_width_tolerance_m' not in geometry:
        raise ValueError('explicit lane_width_tolerance_m required for the Jetson road profile')
    geometry_raw = canonical(geometry)
    geometry_sha = sha256(geometry_raw).hexdigest()
    track_id, radius = VARIANTS[0]
    track = generate(track_id, radius, geometry)
    development_raw = (metadata / 'development.canonical.json').read_bytes()
    motion_raw = (metadata / 'motion.canonical.json').read_bytes()
    old_receipt_raw = (metadata / 'motion-metadata-receipt.json').read_bytes()
    if sha256(old_receipt_raw).hexdigest() != metadata_receipt_sha256:
        raise ValueError('parent metadata receipt SHA differs')
    old_receipt = json.loads(old_receipt_raw)
    files = {row['path']: row for row in old_receipt['files']}
    for name, raw in (('development.canonical.json', development_raw), ('motion.canonical.json', motion_raw)):
        if files[name]['bytes'] != len(raw) or files[name]['sha256'] != sha256(raw).hexdigest():
            raise ValueError('parent model bytes differ from the receipt: ' + name)
    model_pins = dict(expected_development_sha256=files['development.canonical.json']['sha256'],
        expected_motion_sha256=old_receipt['motion_bundle_sha256'],
        expected_calibration_sha256=old_receipt['local_calibration_sha256'])
    parent = DevelopmentProfile.from_bytes(development_raw)
    base_scene = demo_scene()
    parent.check_scene(base_scene)
    scene_fields = base_scene.fields
    scene_fields.update(track=track, geometry_sha256=geometry_sha)
    scene = Scene(canonical(scene_fields))

    fields = parent.fields
    body = track['layout']['vehicle']
    fields['vehicle'].update(body, front_m=(body['length_m'] + body['wheelbase_m']) / 2,
        rear_m=(body['length_m'] - body['wheelbase_m']) / 2)
    # The original scene contract accepts a derived demo. Preserve its parent pins.
    fields['vehicle']['max_steer_rad'] = math.atan(body['wheelbase_m'] / body['min_turn_radius_m'])
    fields['camera']['height_m'] = track['layout']['camera']['height_m']
    fields['source']['scene'].update(sha256=scene.sha256, size_bytes=len(scene.canonical_bytes),
        derived_from_profile_sha256=parent.sha256)
    development = DevelopmentProfile.from_bytes(canonical(fields))
    development.check_scene(scene)
    motion = load_motion_profile(development_raw, motion_raw, **model_pins, derived_development=development)
    identity = motion.fields['identity']
    original_mapper = MapperProfile.from_bytes(mapper_path.read_bytes())
    if (original_mapper.vehicle_configuration != identity['configuration']
            or original_mapper.calibration_profile_sha256 != model_pins['expected_calibration_sha256']):
        raise ValueError('base mapper must be bound to the parent calibration')
    mapper_fields = original_mapper.to_dict()
    mapper_fields['calibration_profile_sha256'] = motion.bound_calibration.sha256
    drift_overrides = {name: value for name, value in (
        ('position_drift_mps', position_drift_mps), ('yaw_drift_rps', yaw_drift_rps)) if value is not None}
    if drift_overrides:
        if original_mapper.localization is None:
            raise ValueError('drift override requires a parent localization policy')
        mapper_fields['localization'] = asdict(replace(original_mapper.localization, **drift_overrides))
    tape = TapeProfile()
    mapper_fields['pipeline']['tape'].update(
        value_min=tape.value_min, min_strip_length_m=tape.min_strip_length_m)
    # R45 replay acceptance limit for the measured-course development profile.
    pipeline = mapper_fields['pipeline']
    pipeline['registration']['max_position_error_m'] = .065
    if pipeline.get('road_geometry_limits') is None:
        pipeline['road_geometry_limits'] = {
            name: pipeline['registration'][name] for name in ('max_position_error_m','max_yaw_error_rad')}
    width, tolerance = geometry['lane_width_m'], geometry['lane_width_tolerance_m']
    width_bounds = (width-tolerance,width+tolerance)
    pipeline['road'].update(lane_width_min_m=width_bounds[0],lane_width_max_m=width_bounds[1],
                            missing_outer_width_bounds_m=width_bounds)
    mapper = MapperProfile.from_dict(mapper_fields)
    mapper.check(motion.bound_calibration, vehicle_configuration=identity['configuration'],
        vehicle_calibration=motion.sha256)

    artifacts = {'development.canonical.json': development.canonical_bytes,
        'motion.canonical.json': motion.canonical_bytes,
        'calibration-bound.json': canonical_calibration(motion.bound_calibration.to_dict())}
    receipt = dict(scope='RPi_FIRMWARE_INPUT_DEVELOPMENT_MODEL_METADATA_ONLY',
        physical_admission=False, device_started=False, app_started=False,
        source_manifest_sha256=geometry_sha, scene_sha256=scene.sha256, parent_profile_sha256=parent.sha256,
        motion_bundle_sha256=motion.sha256, local_calibration_sha256=motion.bound_calibration.sha256,
        files=[dict(path=name, bytes=len(raw), sha256=sha256(raw).hexdigest()) for name, raw in artifacts.items()])
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in dict(artifacts, **{'scene.json': scene.canonical_bytes,
            'mapper-profile.json': mapper.canonical_bytes, 'geometry.json': geometry_raw,
            'motion-metadata-receipt.json': canonical(receipt)}).items():
        (output / name).write_bytes(raw)
    return dict(scene_sha256=scene.sha256, development_sha256=development.sha256, motion_sha256=motion.sha256,
        calibration_sha256=motion.bound_calibration.sha256, mapper_sha256=mapper.sha256,
        metadata_receipt_sha256=sha256(canonical(receipt)).hexdigest(), initial_pose=track['layout']['start_pose'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geometry', type=Path, required=True)
    parser.add_argument('--metadata-root', type=Path, required=True)
    parser.add_argument('--metadata-receipt-sha256', required=True)
    parser.add_argument('--mapper-profile', type=Path, required=True)
    parser.add_argument('--position-drift-mps', type=float, help='position drift budget for this synthetic profile')
    parser.add_argument('--yaw-drift-rps', type=float, help='yaw drift budget for this synthetic profile')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.geometry, args.metadata_root, args.mapper_profile, args.output,
        metadata_receipt_sha256=args.metadata_receipt_sha256, position_drift_mps=args.position_drift_mps,
        yaw_drift_rps=args.yaw_drift_rps), indent=2))


if __name__ == '__main__':
    main()
