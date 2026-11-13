"""커밋된 Jetson 코스를 PC HIL이 받는 장면과 개발 프로필로 내보낸다."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from xenova_vehicle.simulation.profile import DevelopmentProfile, SCENE_SOURCE_PATH
from xenova_vehicle.simulation.scene import Scene, canonical


def prepare_course(repository, revision, track_id, development, output):
    """Git 원본에서 선택한 코스의 SHA와 PC 실행 인자를 만든다."""
    commit = subprocess.check_output(
        ['git', 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}'],
        cwd=repository, text=True,
    ).strip()
    catalog = subprocess.check_output(
        ['git', 'show', f'{commit}:{SCENE_SOURCE_PATH}'], cwd=repository,
    )
    tracks = [track for track in json.loads(catalog) if track['id'] == track_id]
    if len(tracks) != 1:
        raise ValueError(f'expected one committed Jetson course with ID {track_id!r}')
    track, = tracks
    source_sha256 = sha256(catalog).hexdigest()
    scene = Scene(canonical(dict(track=track, source_commit=commit,
        source_path=SCENE_SOURCE_PATH, source_sha256=source_sha256)))

    fields = development.fields
    scene_name = track_id + '.json'
    fields['source']['scene'] = dict(file=scene_name, size_bytes=len(scene.canonical_bytes),
        sha256=scene.sha256, base_sha256=scene.sha256, derived_from_profile_sha256=development.sha256,
        source_commit=commit, source_path=SCENE_SOURCE_PATH, source_sha256=source_sha256, track_id=track_id)
    profile = DevelopmentProfile.from_bytes(canonical(fields))
    profile.check_scene(scene)

    output.mkdir(parents=True, exist_ok=False)
    scene_path, profile_path = output / scene_name, output / 'development.canonical.json'
    scene_path.write_bytes(scene.canonical_bytes)
    profile_path.write_bytes(profile.canonical_bytes)
    return [
        '--development-profile', profile_path.as_posix(), '--development-sha256', profile.sha256,
        '--scene', scene_path.as_posix(), '--scene-sha256', scene.sha256,
        '--initial-pose', *map(str, track['layout']['start_pose']),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    parser.add_argument('--revision', required=True)
    parser.add_argument('--track', required=True)
    parser.add_argument('--development-profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    development = DevelopmentProfile.from_bytes(args.development_profile.read_bytes())
    print(json.dumps(prepare_course(args.repository, args.revision, args.track, development, args.output)))


if __name__ == '__main__':
    main()
