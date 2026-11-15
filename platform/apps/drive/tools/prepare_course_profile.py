#!/usr/bin/env python3
"""Bind a fixed clearance policy to new development metadata and primitive C."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import tempfile

TUNING_FILE = 'course-clearance.json'
REQUIRED = {'margin', 'tracking_guard', 'primitive_duration'}
LANE_TUNING = {'tape_width_m', 'narrow_lane_speed_mps'}
TAPE_OVERHANG = 'tape_overhang_m'
DIVIDER_OVERHANG = 'divider_overhang_m'


def tuned_profile(profile, tuning):
    fields = set(tuning)
    if not REQUIRED <= fields or fields - REQUIRED - LANE_TUNING - {'primitive_speed', 'avoid_policy', TAPE_OVERHANG, DIVIDER_OVERHANG}:
        raise ValueError('course clearance requires margin, tracking_guard and primitive_duration')
    nonnegative = {'tape_width_m', TAPE_OVERHANG, DIVIDER_OVERHANG}
    if any(type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (value == 0 and name not in nonnegative) for name, value in tuning.items()):
        raise ValueError('course settings must be positive and finite, except tape settings may be zero')
    result = dict(profile, **{name: tuning[name] for name in fields - LANE_TUNING - {TAPE_OVERHANG, DIVIDER_OVERHANG}})
    if 'avoid_policy' in tuning and (type(tuning['avoid_policy']) is not int or tuning['avoid_policy'] not in (1, 2)):
        raise ValueError('course avoidance policy must be return-home or keep-target')
    tape = tuning.get('tape_width_m', 0)
    overhang = tuning.get(TAPE_OVERHANG, 0)
    divider = tuning.get(DIVIDER_OVERHANG)
    if divider is not None and (tape <= 0 or tape / 2 + divider > result['half_width']):
        raise ValueError('divider allowance requires tape and may not exceed the body half width')
    speed = tuning.get('narrow_lane_speed_mps', 0)
    if tape + 2 * overhang > result['half_width'] or speed > result['max_velocity']:
        raise ValueError('course boundary allowance or narrow-lane speed exceeds its vehicle bound')
    if LANE_TUNING <= fields:
        # Keep the frozen primitive generator and its legacy asset identity.
        # Its canonical profile still binds the new compiled course settings.
        result['assumptions'] += f' Course tape width={tape:.9g} m, narrow-lane speed={speed:.9g} m/s.'
    else:
        if 'tape_width_m' in fields:
            result['assumptions'] += f' Course tape width={tape:.9g} m.'
        if 'narrow_lane_speed_mps' in fields:
            result['assumptions'] += f' Narrow-lane speed={speed:.9g} m/s.'
    if overhang:
        result['assumptions'] += f' Body overhang beyond tape={overhang:.9g} m.'
    if divider is not None:
        result['assumptions'] += f' Divider overhang beyond tape={divider:.9g} m.'
    spacing = min(result['resolution'] * .25, result['tracking_guard'] * .5)
    if result['primitive_speed'] * result['primitive_duration'] > 64 * spacing:
        raise ValueError('primitive exceeds the swept-check subdivisions; shorten primitive_duration')
    return result


def lane_source(tuning):
    """Initializers added to the development-only generated C input."""
    source = ''
    if 'tape_width_m' in tuning:
        source += f" .lane_tape_half_width_m={tuning['tape_width_m'] * .5:.9e}f,\n"
    if 'narrow_lane_speed_mps' in tuning:
        source += f" .narrow_lane_speed_mps={tuning['narrow_lane_speed_mps']:.9e}f,\n"
    if overhang := tuning.get(TAPE_OVERHANG, 0):
        source += f" .lane_tape_overhang_m={overhang:.9e}f,\n"
    if DIVIDER_OVERHANG in tuning:
        allowance = tuning['tape_width_m'] / 2 + tuning[DIVIDER_OVERHANG]
        source += f" .lane_divider_allowance_m={allowance:.9e}f,\n"
    return source.encode()


def prepare(metadata, grid, output, *, receipt_sha256, clearance):
    import prepare_development_profile as base

    if output.exists() or output.is_symlink():
        raise ValueError('new output directory required')
    tuning_raw, tuning = base.read(clearance)
    spec = importlib.util.spec_from_file_location('course_primitives', base.HOME / 'tools/generate_primitives.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / 'profile'
        receipt = base.prepare(metadata, grid, staged, receipt_sha256=receipt_sha256)
        profile_path = staged / 'profile.json'
        _, profile = base.read(profile_path)
        profile_path.write_bytes(base.canonical(tuned_profile(profile, tuning)) + b'\n')
        generator.generate(profile_path, staged)
        if extra := lane_source(tuning):
            primitive_path = staged / 'primitives.c'
            initializer = b'const struct lp_config lp_selected_config={\n'
            source = primitive_path.read_bytes().replace(initializer, initializer + extra, 1)
            primitive_path.write_bytes(source)
            manifest_path = staged / 'primitive-manifest.json'
            _, manifest = base.read(manifest_path)
            manifest['primitive_source_sha256'] = base.sha(source)
            manifest_path.write_bytes(json.dumps(manifest, indent=2).encode() + b'\n')
        (staged / 'sources' / TUNING_FILE).write_bytes(tuning_raw)
        receipt['source_files'][TUNING_FILE] = {'bytes': len(tuning_raw), 'sha256': base.sha(tuning_raw)}
        receipt['inherited_provisional_fields'] = sorted(set(receipt['inherited_provisional_fields']) - tuning.keys())
        (staged / 'source-receipt.json').write_bytes(json.dumps(receipt, indent=2).encode() + b'\n')
        staged.rename(output)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-root', type=Path, required=True)
    parser.add_argument('--metadata-receipt-sha256', required=True)
    parser.add_argument('--grid', type=Path, required=True)
    parser.add_argument('--clearance', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.metadata_root, args.grid, args.output,
        receipt_sha256=args.metadata_receipt_sha256, clearance=args.clearance), indent=2))


if __name__ == '__main__':
    main()
