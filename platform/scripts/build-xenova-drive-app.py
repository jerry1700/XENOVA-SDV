#!/usr/bin/env python3
"""Snapshot/build/package an existing drive app with the frozen Xen network profile.

No broker secret, VM identity, host mutation, image assembly or catalog overwrite.
The candidate's source digest includes common runtime/Diagnostics/Publisher code.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "platform/guests/zephyr"
LATTICE_PROFILES = ('warm-lattice-lab', 'warm-lattice-development')
DEVELOPMENT_BUNDLE = Path('platform/apps/drive/profiles/course-development-1')
DEVELOPMENT_SOURCE_FILES = frozenset(('motion.canonical.json', 'development.canonical.json',
    'calibration-bound.json', 'motion-metadata-receipt.json', 'grid.json', 'lattice-lab-policy.json'))
COURSE_CLEARANCE = 'course-clearance.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lattice_inputs(name, bundle=None):
    """선택한 C/config/generated asset과 원 development 입력의 지문을 검산한다."""
    home = ROOT / 'platform/apps/drive'
    development = name == 'warm-lattice-development'
    if bundle is not None and (not development or not bundle.is_absolute() or bundle.resolve() != bundle):
        raise ValueError('canonical bundle path requires warm-lattice-development')
    folder = (bundle or ROOT / DEVELOPMENT_BUNDLE) if development else home / 'planner/generated'
    members = bundle_members(folder) if development else {}
    profile_path = folder / 'profile.json' if development else home / 'profiles/lattice-lab-1.json'
    profile = json.loads(profile_path.read_text())
    generated = json.loads((folder / 'primitive-manifest.json').read_text())
    canonical = json.dumps(profile, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    generator_sha = hashlib.sha256((home / 'tools/generate_primitives.py').read_text(encoding='utf-8').encode()).hexdigest()
    if (generated['profile'] != profile or generated['profile_sha256'] != hashlib.sha256(canonical).hexdigest()
            or generated['generator_sha256'] != generator_sha
            or generated['asset_sha256'] != hashlib.sha256(canonical + b'\0' + generator_sha.encode()).hexdigest()
            or generated['primitive_source_sha256'] != sha(folder / 'primitives.c')):
        raise ValueError('selected vehicle profile/primitive generation differs; regenerate with the fixed tool')
    provenance = None
    if development:
        provenance = json.loads((folder / 'source-receipt.json').read_text())
        if (profile['kind'] != 'test' or provenance['scope'] != 'RPi_DEVELOPMENT_PRIMITIVE_INPUT_ONLY'
                or provenance['physical_admission'] is not False):
            raise ValueError('explicit development-only profile required')
        source_names = {path.name for path in members.values() if path.parent.name == 'sources'}
        if set(provenance['source_files']) != source_names:
            raise ValueError('complete original development sources required')
        for member, expected in provenance['source_files'].items():
            if Path(member).name != member or member in ('.', '..'):
                raise ValueError('bounded development source member required')
            path = folder / 'sources' / member
            if path.is_symlink() or expected != {'bytes': path.stat().st_size, 'sha256': sha(path)}:
                raise ValueError('development source changed: ' + member)
        spec = importlib.util.spec_from_file_location('development_profile_inputs', home / 'tools/prepare_development_profile.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        if provenance['prepare_tool_sha256'] != hashlib.sha256((home / 'tools/prepare_development_profile.py').read_text(encoding='utf-8').encode()).hexdigest():
            raise ValueError('development preparation tool changed; regenerate explicitly')
        (_, _, _, _, grid, motion, development_model, motion_sha, calibration_sha) = tool.inputs(
            folder / 'sources', folder / 'sources/grid.json', provenance['metadata_receipt_sha256'])
        policy_raw, policy = tool.read(folder / 'sources/lattice-lab-policy.json')
        expected, _ = tool.profile_values(motion, development_model, grid, policy, motion_sha)
        if COURSE_CLEARANCE in source_names:
            spec = importlib.util.spec_from_file_location('course_clearance', home / 'tools/prepare_course_profile.py')
            course_tool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(course_tool)
            _, tuning = tool.read(folder / 'sources' / COURSE_CLEARANCE)
            expected = course_tool.tuned_profile(expected, tuning)
            if extra := course_tool.lane_source(tuning):
                source = (folder / 'primitives.c').read_bytes()
                if not source.partition(b'const struct lp_config lp_selected_config={\n')[2].startswith(extra):
                    raise ValueError('compiled course lane settings differ from their input')
        if (expected != profile or provenance['motion_bundle_sha256'] != motion_sha
                or provenance['local_calibration_sha256'] != calibration_sha
                or provenance['grid'] != grid or provenance['policy_sha256'] != hashlib.sha256(policy_raw).hexdigest()
                or policy != json.loads((home / 'profiles/lattice-lab-1.json').read_text())):
            raise ValueError('selected development model/policy binding differs')
    return profile, generated, provenance


def bundle_members(folder):
    """대체 bundle은 snapshot 내 기존 CMake 경로에 있는 동일 역할의 파일만 공급한다."""
    names = {'profile.json', 'primitive-manifest.json', 'primitives.c', 'source-receipt.json'}
    names.update('sources/' + name for name in DEVELOPMENT_SOURCE_FILES)
    if (folder / 'sources' / COURSE_CLEARANCE).exists():
        names.add('sources/' + COURSE_CLEARANCE)
    paths = {name: folder / name for name in sorted(names)}
    if any(path.resolve() != path or not path.is_file() for path in paths.values()):
        raise ValueError('complete plain development bundle required')
    if ({path.name for path in folder.iterdir()} != {Path(name).parts[0] for name in names}
            or {path.name for path in (folder / 'sources').iterdir()} != {Path(name).name for name in names if '/' in name}):
        raise ValueError('unexpected development bundle members')
    return {(DEVELOPMENT_BUNDLE / name).as_posix(): path for name, path in paths.items()}


def selected_primitive_source(commands, home, profile):
    """실제 ARM compile DB의 단 하나의 primitive TU가 선택한 profile과 일치해야 한다."""
    expected = (home / ('../profiles/course-development-1/primitives.c'
                       if profile == 'warm-lattice-development' else '../planner/generated/primitives.c')).resolve()
    sources = [Path(row['file']).resolve() for row in commands if Path(row['file']).name == 'primitives.c']
    if sources != [expected]:
        raise ValueError('actual compiler primitive input differs from the selected profile')
    return expected


def source_paths(app, bundle=None):
    groups = [f"platform/apps/{app}/zephyr", "platform/apps/common/xenova-mqtt-c",
              "platform/apps/common/xenova-drive-runtime"]
    groups += ["platform/guests/zephyr/" + name for name in ("xen-store", "xen-runtime", "diagnostics", "telemetry")]
    if app == "drive":
        groups += ["platform/apps/autonomous/zephyr/src", "platform/apps/manual/zephyr/src"]
        groups += ['platform/apps/drive/'+name for name in ('world','planner','profiles','tools')]
        groups += ['docs/contracts/drive-world']
    # Interpreter caches are generated output, never firmware source inputs.
    paths = [p for group in groups for p in sorted((ROOT / group).rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")]
    paths += [ROOT / name for name in (
        "platform/scripts/build-xenova-drive-app.py", "platform/scripts/package-xenova-firmware.py",
        "platform/scripts/build-xenova-network-starter.py", "platform/guests/zephyr/network-xen.cfg.in",
        "platform/guests/zephyr/starter.lock.json", "platform/first-install/artifact_io.py",
        "platform/first-install/first_install_contract.py",
        "platform/yocto/meta-xenova/recipes-core/xenova-platform/files/xenova_zephyr.py",
        "platform/yocto/meta-xenova/recipes-core/xenova-platform/files/xenova_catalog.py",
        "platform/yocto/meta-xenova/recipes-core/xenova-platform/files/xenova_guest_profile.py",
        "platform/yocto/meta-xenova/recipes-core/xenova-platform/files/xenova_runtime.py",
        "platform/yocto/meta-xenova/recipes-core/xenova-platform/files/xenova_trace.py")]
    if any(p.is_symlink() for p in paths):
        raise ValueError("source snapshot must not contain linked inputs")
    inputs = {p.relative_to(ROOT).as_posix(): p for p in paths}
    if bundle is not None:
        inputs = {name: path for name, path in inputs.items() if not Path(name).is_relative_to(DEVELOPMENT_BUNDLE)}
        inputs.update(bundle_members(bundle))
    return inputs


def fingerprint(app, bundle=None):
    return source_fingerprint(source_paths(app, bundle))


def source_fingerprint(paths):
    return {name: {"sha256": sha(path), "bytes": path.stat().st_size}
            for name, path in paths.items()}


def source_member(root, name):
    """manifest 소유 member만 허용하며 symlink/상위 경로로 작업공간을 벗어나지 않는다."""
    part = Path(name)
    if (not isinstance(name, str) or not name.startswith(('platform/', 'docs/'))
            or part.is_absolute() or '..' in part.parts or '\\' in name
            or part.as_posix() != name):
        raise ValueError('plain repository-relative source member required')
    path = root / part
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError('linked workspace source member rejected')
    return path


def verify_sources(root, source):
    """수정·미등록 파일이 있는 mutable input은 채택하지 않고 원 소유 목록을 검산한다."""
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('plain owned workspace source tree required')
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('linked workspace source file rejected')
            actual.add(path.relative_to(root).as_posix())
    if actual != set(source):
        raise ValueError('workspace input contains missing or unowned files')
    for name, expected in source.items():
        path = source_member(root, name)
        if expected != {'sha256': sha(path), 'bytes': path.stat().st_size}:
            raise ValueError('workspace source differs from its last owned manifest: ' + name)


@contextmanager
def incremental_workspace(work, identity, snapshot, source):
    """개발 전용 marker/동일 profile의 cache만 사용한다. 사용자/LAB 출력은 채택하지 않는다."""
    import fcntl
    marker = work / 'WORKSPACE.json'
    if work.exists():
        if (not work.is_dir() or marker.is_symlink() or not marker.is_file()
                or marker.stat().st_nlink != 1 or json.loads(marker.read_text()) != identity):
            raise ValueError('matching development workspace marker required; existing directories are not adopted')
    else:
        work.mkdir(mode=0o755)
        with marker.open('x') as stream:
            stream.write(json.dumps(identity, sort_keys=True, indent=2) + '\n')
    allowed = {'WORKSPACE.json', '.build.lock', 'SOURCE_MANIFEST.json', 'input', 'build'}
    if any(p.name not in allowed or p.is_symlink() for p in work.iterdir()):
        raise ValueError('unowned or linked workspace entry rejected')
    lock_path = work / '.build.lock'
    if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
        raise ValueError('plain workspace lock required')
    with lock_path.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('development workspace already in use') from error
        inputs, manifest = work / 'input', work / 'SOURCE_MANIFEST.json'
        old = {}
        if manifest.exists():
            if not manifest.is_file() or manifest.stat().st_nlink != 1:
                raise ValueError('plain owned source manifest required')
            record = json.loads(manifest.read_text())
            if set(record) != {'schema', 'files'} or record['schema'] != 1 or type(record['files']) is not dict:
                raise ValueError('owned source manifest shape differs')
            old = record['files']
            if not inputs.is_dir():
                raise ValueError('owned workspace input missing')
            verify_sources(inputs, old)
        elif inputs.exists() or (work / 'build').exists():
            raise ValueError('workspace cache without owned source manifest rejected')
        cache_file = work / 'build/CMakeCache.txt'
        synchronization = dict(previous_source_manifest_sha256=sha(manifest) if manifest.exists() else None,
            cache_was_configured=cache_file.exists(),
            updated_source_members=sum(old.get(name) != expected for name, expected in source.items()),
            removed_source_members=len(old.keys() - source.keys()))
        if cache_file.exists():
            if cache_file.is_symlink() or not cache_file.is_file():
                raise ValueError('plain development CMake cache required')
            cache = {line.partition('=')[0].partition(':')[0]: line.partition('=')[2]
                     for line in cache_file.read_text().splitlines() if '=' in line and not line.startswith(('#', '//'))}
            home = inputs / f"platform/apps/{identity['app']}/zephyr"
            expected = dict(APPLICATION_SOURCE_DIR=str(home), ZEPHYR_BASE=identity['zephyr_source'],
                BOARD='xenvm', CMAKE_C_COMPILER=str(Path(identity['tools']) / 'aarch64-zephyr-elf/bin/aarch64-zephyr-elf-gcc'),
                EXTRA_CONF_FILE=str(home / 'profiles' / (identity['profile'] + '.conf')),
                XENOVA_DRIVE_DEVELOPMENT_PROFILE='ON')
            if any(cache.get(key) != value for key, value in expected.items()):
                raise ValueError('existing CMake cache does not match development workspace identity')
        verify_sources(snapshot, source)
        for name, expected in source.items():
            target = source_member(inputs, name)
            if old.get(name) != expected:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_member(snapshot, name), target)
        # 재귀 삭제 없이 이전 manifest로 소유와 원 bytes를 검산한 stale member만 제거한다.
        for name in old.keys() - source.keys():
            source_member(inputs, name).unlink()
        verify_sources(inputs, source)
        temporary = work / 'SOURCE_MANIFEST.next'
        with temporary.open('x') as stream:
            stream.write(json.dumps({'schema': 1, 'files': source}, sort_keys=True, indent=2) + '\n')
        temporary.replace(manifest)
        yield inputs, work / 'build', synchronization


def verify_compile_sources(commands, inputs, source, zephyr, build):
    """실제 compile DB가 현재 manifest·Zephyr·해당 build의 원 파일만 가리켜야 한다."""
    compiled = {}
    for row in commands:
        path = Path(row['file']).resolve()
        if not Path(row['directory']).resolve().is_relative_to(build):
            raise ValueError('compiler working directory differs from selected build')
        if path.is_relative_to(inputs):
            name = path.relative_to(inputs).as_posix()
            actual = {'sha256': sha(path), 'bytes': path.stat().st_size}
            if source.get(name) != actual:
                raise ValueError('compiler source is not the current frozen input: ' + name)
            compiled[name] = actual
        elif not (path.is_relative_to(zephyr) or path.is_relative_to(build)):
            raise ValueError('compiler source belongs to another input/cache: ' + str(path))
    if not compiled:
        raise ValueError('actual application compiler sources required')
    return compiled


def build_candidate(resources):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--app", choices=("manual", "autonomous", "drive"), required=True)
    p.add_argument("--profile", choices=("operations", "experiment", "warm-operations", "warm-experiment", *LATTICE_PROFILES), default="operations",
                   help="operations excludes raw capture; experiment explicitly includes it")
    p.add_argument("--zephyr-source", type=Path, required=True)
    p.add_argument("--tools", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument('--development-bundle', type=Path,
                   help='prepared course bundle for explicit drive/warm-lattice-development')
    p.add_argument('--incremental-workspace', type=Path,
                   help='optional owned stable workspace; only warm-lattice-development, every --output remains new')
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    if args.incremental_workspace is not None and (args.app, args.profile) != ('drive', 'warm-lattice-development'):
        p.error('incremental workspace requires explicit drive/warm-lattice-development')
    if args.development_bundle is not None and (args.app, args.profile) != ('drive', 'warm-lattice-development'):
        p.error('development bundle requires explicit drive/warm-lattice-development')
    if args.profile.startswith('warm-') and args.app!='drive':p.error('warm profiles require the common drive firmware')
    is_lattice = args.profile in LATTICE_PROFILES
    if is_lattice:
        profile, generated, provenance = lattice_inputs(args.profile, args.development_bundle)
    for path in (args.zephyr_source, args.tools, args.output, *([args.incremental_workspace] if args.incremental_workspace else [])):
        if not path.is_absolute() or path.resolve() != path or path.is_symlink():
            raise ValueError("canonical absolute paths required")
    if args.output.exists() or any(args.output.is_relative_to(path) for path in (ROOT, args.zephyr_source, args.tools)):
        raise ValueError("new output outside repository/source/toolchain required")
    if args.incremental_workspace is not None and any(
            args.incremental_workspace.is_relative_to(path) or path.is_relative_to(args.incremental_workspace)
            for path in (ROOT, args.zephyr_source, args.tools, args.output)):
        raise ValueError('workspace must be separate from repository/source/tools/output')
    lock = json.loads((BASE / "starter.lock.json").read_text())
    commit = subprocess.check_output(["git", "-C", str(args.zephyr_source), "rev-parse", "HEAD"], text=True).strip()
    if commit != lock["zephyr_commit"]:
        raise ValueError("frozen Zephyr commit required")
    api_lock = json.loads((BASE / "xen-store/references.lock.json").read_text())
    for name, digest in api_lock["zephyr"]["api_files"].items():
        if sha(args.zephyr_source / name) != digest:
            raise ValueError("frozen Zephyr API differs: " + name)
    cmake = args.tools / "python/bin/cmake"
    ninja = args.tools / "python/bin/ninja"
    compiler = args.tools / "aarch64-zephyr-elf/bin/aarch64-zephyr-elf-gcc"
    if not all(path.is_file() for path in (cmake, ninja, compiler)):
        raise ValueError("existing fixed CMake/Ninja/AArch64 toolchain required")
    paths = source_paths(args.app, args.development_bundle)
    source = source_fingerprint(paths)
    digest = hashlib.sha256(json.dumps({"profile": args.profile, "files": source},
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan = {"action": "build-drive-candidate" if args.execute else "plan", "app": args.app,
                      "source_sha256": digest, "profile": args.profile,
                      "output": str(args.output), "board_tested": False}
    if args.incremental_workspace is not None:
        plan['incremental_workspace'] = str(args.incremental_workspace)
    print(json.dumps(plan), flush=True)
    if not args.execute:
        return
    args.output.mkdir(mode=0o755)
    snapshot = args.output / "input"
    for name in source:
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths[name], target)
        if sha(target) != source[name]["sha256"]:
            raise ValueError("source changed during snapshot: " + name)
    if args.development_bundle is not None and lattice_inputs(args.profile, snapshot / DEVELOPMENT_BUNDLE) != (
            profile, generated, provenance):
        raise ValueError('selected development bundle changed during snapshot')
    frozen_home = snapshot / f"platform/apps/{args.app}/zephyr"
    inputs, build = snapshot, args.output / 'build'
    workspace_identity = None
    if args.incremental_workspace is not None:
        workspace_identity = dict(schema=1, purpose='xenova-drive-development-incremental-1',
            app=args.app, profile=args.profile, repository=str(ROOT), board='xenvm',
            zephyr_source=str(args.zephyr_source), zephyr_commit=commit, tools=str(args.tools),
            sdk_version=lock['sdk_version'], compiler_sha256=sha(compiler), cmake_sha256=sha(cmake), ninja_sha256=sha(ninja))
        inputs, build, synchronization = resources.enter_context(incremental_workspace(
            args.incremental_workspace, workspace_identity, snapshot, source))
    home = inputs / f'platform/apps/{args.app}/zephyr'
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(args.tools / "python"),
               ZEPHYR_BASE=str(args.zephyr_source), ZEPHYR_TOOLCHAIN_VARIANT="cross-compile",
               CROSS_COMPILE=str(args.tools / "aarch64-zephyr-elf/bin/aarch64-zephyr-elf-"),
               PATH=str(args.tools / "python/bin") + ":" + os.environ["PATH"])

    def run(label, argv, *, cwd=None):
        with (args.output / (label + ".stdout")).open("wb") as stream:
            result = subprocess.run(list(map(str, argv)), env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=300, cwd=cwd)
        (args.output / (label + ".command.json")).write_text(json.dumps(
            {"argv": list(map(str, argv)), "rc": result.returncode, "timeout_s": 300}, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError(label + " failed; full output preserved")

    profile_args = ["-DCONFIG_XENOVA_DRIVE_RECORDER=" + ("y" if args.profile == "experiment" else "n"),
                    "-DCONFIG_XENOVA_DIAGNOSTICS_THREADS=" + ("y" if args.profile == "experiment" else "n")]
    if args.app == "drive":
        profile_args = ["-DEXTRA_CONF_FILE=" + str(home / "profiles" / (args.profile + ".conf"))]
    if args.profile=='warm-lattice-development':
        profile_args+=['-DXENOVA_DRIVE_DEVELOPMENT_PROFILE=ON']
    if is_lattice:
        # This frozen toolchain contains picolibc. Legacy cross-compile does not
        # advertise it without an explicit path/flag; do not change old profiles.
        header=args.tools/'aarch64-zephyr-elf/aarch64-zephyr-elf/include/picolibc.h'
        if not header.is_file():raise ValueError('fixed picolibc headers required for the selected math profile')
        profile_args+=['-DTOOLCHAIN_HAS_PICOLIBC=ON','-DCONFIG_TOOLCHAIN_CROSS_COMPILE_SUPPORTS_THREAD_LOCAL_STORAGE=y']
    run("configure", [cmake, "-S", home, "-B", build, "-GNinja", "-DBOARD=xenvm",
                      "-DCMAKE_MAKE_PROGRAM=" + str(ninja), "-DDTC_OVERLAY_FILE=" + str(home / "boards/xenvm.overlay"),
                      *profile_args])
    run("compile", [cmake, "--build", build, "-j", "4"])

    # .send may run on the kernel's small TCP workqueue, rather than app main.
    # Audit its direct frame with the same fixed compiler/config, not a host estimate.
    stack_dir = args.output / "stack-audit"
    stack_dir.mkdir()
    commands = json.loads((build / "compile_commands.json").read_text())
    compiled_sources = verify_compile_sources(commands, inputs, source, args.zephyr_source, build) if workspace_identity else None
    selected_primitive = selected_primitive_source(commands, home, args.profile) if is_lattice else None
    command = next(v for v in commands if v["file"].endswith("/xen-runtime/src/netfront.c"))
    audit_args = shlex.split(command["command"])
    if set(audit_args) & {"-MF", "-MD", "-MMD"}:
        raise ValueError("stack audit must not overwrite original dependency output")
    audit_args[audit_args.index("-o") + 1] = str(stack_dir / "netfront.o")
    run("stack-audit", audit_args + ["-fstack-usage"], cwd=command["directory"])
    rows = [v.split("\t") for v in (stack_dir / "netfront.su").read_text().splitlines()]
    send = [v for v in rows if len(v) == 3 and v[0].endswith(":send")]
    if len(send) != 1 or send[0][2] != "static" or int(send[0][1]) > 256:
        raise ValueError("netfront send exceeds the direct 256-byte stack-frame budget")
    config = (build / "zephyr/.config").read_text()
    for name in ("XENOVA_DRIVE_RECORDER", "XENOVA_DIAGNOSTICS_THREADS"):
        expected=args.profile.endswith("experiment") or (name=="XENOVA_DIAGNOSTICS_THREADS" and is_lattice)
        if ("CONFIG_" + name + "=y\n" in config) != expected:
            raise ValueError("resolved configuration differs from selected profile: " + name)
    if ("CONFIG_XENOVA_DRIVE_WARM=y\n" in config)!=args.profile.startswith('warm-'):
        raise ValueError('resolved readiness profile differs from requested profile')
    if is_lattice and 'CONFIG_PICOLIBC=y\n' not in config:
        raise ValueError('actual target lacks the selected fixed math library')
    if is_lattice and 'CONFIG_THREAD_LOCAL_STORAGE=y\n' not in config:
        raise ValueError('toolchain picolibc errno requires thread-local storage')
    if "CONFIG_STACK_SENTINEL=y\n" not in config:
        raise ValueError("drive target must retain stack boundary detection")
    tcp_workqueue_stack_audit = None
    if args.app == "drive":
        tcp_rows = [line.partition("=")[2] for line in config.splitlines()
                    if line.startswith("CONFIG_NET_TCP_WORKQ_STACK_SIZE=")]
        if len(tcp_rows) != 1 or not tcp_rows[0].isdigit() or int(tcp_rows[0]) < 2048:
            raise ValueError("drive TCP workqueue must reserve at least 2048 bytes")
        tcp_workqueue_stack_audit = {
            "reserved_bytes": int(tcp_rows[0]),
            "minimum_reserved_bytes": 2048,
            "reference_c_chain_lower_bound_bytes": 1152,
            "reference_build_id": "sha256:78500a046176f1155dadc24f7d00d0f6c69decebeff47ae500a50a356f351600",
            "reference_chain": "tcp_resend_data -> TCP/Ethernet -> ARP allocation (including work_queue_main)",
            "scope": "matched 1.0.4 ARM C frames with tail calls removed; excludes thread entry/architecture saves; not a worst-case upper bound",
            "target_high_water_required": True}
    netfront_stack_audit = {"send_frame_bytes": int(send[0][1]), "direct_frame_budget_bytes": 256,
                            "stack_sentinel": True,
                            "scope": "adapter direct frame; target tests still validate the caller chain"}
    calculation_stack_audit = None
    if args.app == "drive":
        frames = {}
        basenames=["workers.c", "adapter.c", "autonomous.c", "manual.c", "xm_payload.c", "xm_json.c"]
        if args.profile.startswith('warm-'):basenames += ['context.c','runtime.c','main.c','xen_bootstrap.c','xm_zclient.c']
        if is_lattice:basenames += ['lattice_app.c','lattice_input.c','lattice_memory.c','engine.c','lattice.c','world.c','pb_decode.c','pb_encode.c']
        for basename in basenames:
            item = next(v for v in commands if v["file"].endswith("/" + basename))
            argv = shlex.split(item["command"])
            if set(argv) & {"-MF", "-MD", "-MMD"}:
                raise ValueError("calculation audit must preserve original dependency output")
            argv[argv.index("-o") + 1] = str(stack_dir / (basename + ".o"))
            run("stack-" + basename, argv + ["-fstack-usage"], cwd=item["directory"])
            frames[basename] = [{"function": row[0].rsplit(":", 1)[-1],
                                "frame_bytes": int(row[1]), "kind": row[2]}
                               for line in (stack_dir / (basename + ".su")).read_text().splitlines()
                               if len(row := line.split("\t")) == 3]
        worker_stack = int(next(line.split("=", 1)[1] for line in config.splitlines()
                                if line.startswith("CONFIG_XENOVA_DRIVE_WORKER_STACK_SIZE=")))
        if any(row["frame_bytes"] >= worker_stack for rows in frames.values() for row in rows):
            raise ValueError("a calculation/parser direct frame exceeds the reserved worker stack")
        calculation_stack_audit = {"worker_stack_bytes": worker_stack, "direct_frames": frames,
            "scope": "direct compiler frames, not full call-chain peak or on-board stack high-water"}
    spec = importlib.util.spec_from_file_location("xenova_network_build", snapshot / "platform/scripts/build-xenova-network-starter.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify_event_irq(build / "zephyr/zephyr.dts")
    segments = verifier.verify_elf(build / "zephyr/zephyr.elf")
    license_file=args.zephyr_source/'LICENSE'
    if is_lattice:
        license_file=args.output/'THIRD_PARTY_LICENSES.txt'
        nanopb=snapshot/'platform/apps/drive/world/nanopb/LICENSE.txt'
        license_file.write_text('Zephyr / Xenova application (Apache-2.0)\n\n'+
            (args.zephyr_source/'LICENSE').read_text()+'\n\nNanopb runtime (zlib)\n\n'+nanopb.read_text())
    if workspace_identity is not None:
        # packager는 실제 CMake home을 검사한다. 별도 불변 snapshot과 원 bytes는 여기서 대조한다.
        verify_sources(inputs, source)
        verify_sources(snapshot, source)
    run("package", [sys.executable, "-B", snapshot / "platform/scripts/package-xenova-firmware.py",
                    "--build-dir", build / "zephyr", "--license", license_file,
                    "--version", "0.1.0-drive-" + digest[:12], "--app", "xenova-" + args.app,
                    "--source-revision", "sha256:" + digest, "--build-id", args.output.name,
                    "--app-source", home, "--networked", "--output", args.output / "firmware", "--execute"])
    if workspace_identity is not None:
        verify_sources(inputs, source)
        verify_sources(snapshot, source)
        if fingerprint(args.app) != source:
            raise ValueError('repository source changed during incremental build')
        evidence = args.output / 'build-evidence'
        evidence.mkdir()
        for name in ('compile_commands.json', 'CMakeCache.txt'):
            shutil.copyfile(build / name, evidence / name)
    receipt = {"netfront_stack_audit": netfront_stack_audit, "app": args.app, "profile": args.profile,
               "calculation_stack_audit": calculation_stack_audit,
               "tcp_workqueue_stack_audit": tcp_workqueue_stack_audit,
               "config_sha256": sha(build / "zephyr/.config"),
               "source_files": source, "source_sha256": digest, "zephyr_commit": commit,
               "compiler_sha256": sha(compiler), "compiler_version": subprocess.check_output([str(compiler), "--version"], text=True).splitlines()[0],
               "sdk_version": lock["sdk_version"], "elf_segments": segments,
               "files": {p.name: {"sha256": sha(p), "bytes": p.stat().st_size}
                         for p in sorted((args.output / "firmware").iterdir()) if p.is_file()},
               "acceptance": {"M1_arm_build": True, "M2_board": False, "M3_performance": False}}
    if is_lattice:
        receipt['drive_contract']={'kind':'test','world_profile':profile['id'],
            'schema_sha256':sha(snapshot/'docs/contracts/drive-world/world.proto'),
            'primitive_sha256':generated['asset_sha256'],
            'app_state_models':{'autonomous':'stateful','manual':'stateless'},
            'bootstrap_profile':'warm-drive-1','map_slots':3,'map_cells':4096,
            'odom_slots':128,'path_points':512,'node_limit':profile['node_limit'],
            'output_authority':'Gateway','operating_vehicle_limits_verified':False}
        if provenance is not None:
            receipt['drive_contract']['development_inputs']=provenance
        if args.development_bundle is not None:
            receipt['drive_contract']['explicit_development_bundle'] = True
        receipt['drive_contract']['compiled_primitive_source']={
            'path':str(selected_primitive.relative_to(inputs)).replace('\\','/'),
            'sha256':sha(selected_primitive)}
    if workspace_identity is not None:
        receipt['incremental_workspace'] = dict(path=str(args.incremental_workspace), identity=workspace_identity,
            synchronization=synchronization,
            source_snapshot_binding=dict(actual_app_source=str(home), immutable_app_source=str(frozen_home),
                verified_source_sha256=digest),
            source_manifest_sha256=sha(args.incremental_workspace / 'SOURCE_MANIFEST.json'),
            compiled_sources=compiled_sources, mutable_cache_is_not_release=True,
            immutable_build_evidence={name:sha(args.output / 'build-evidence' / name)
                for name in ('compile_commands.json', 'CMakeCache.txt')})
    (args.output / "BUILD_RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"candidate": str(args.output / "firmware"), "firmware_sha256": receipt["files"]["zephyr.bin"]["sha256"]}))


def main():
    with ExitStack() as resources:
        build_candidate(resources)


if __name__ == "__main__":
    main()
