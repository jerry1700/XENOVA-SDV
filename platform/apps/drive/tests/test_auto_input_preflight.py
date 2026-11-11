"""Actual C current-input/body geometry preparation, without a future grant/path.

The engine fixture supplies declared World/Vehicle transport data. Actual P4,
report submit/Probe omission and Gateway Enter are exercised separately through
the persistent actual app/input bridge; these tests do not manufacture READY.
"""

import ctypes as C

import pytest

from test_engine import load, modern_world, send_vehicle, vehicle
from test_lattice import CFG, library, obstacle
from world_codec import decode, schema


def inspect(lib, engine, now=1220000, *, current=False):
    method = lib.engine_preflight_current if current else lib.engine_preflight
    method.argtypes = [C.c_void_p, C.c_uint64, C.c_void_p, C.c_size_t, C.POINTER(C.c_uint64)]
    raw = C.create_string_buffer(768)
    facts = (C.c_uint64 * 8)()
    result = method(engine, now, raw, len(raw), facts)
    source = decode(raw.raw[:-result], schema.EpisodeSourceContext) if result < -2 else None
    return result, source, list(facts)


@pytest.mark.parametrize('sample_at,localization_us', ((1220000, 1000000),
                                                    (1200001, 1000000), (1220000, 200000)))
def test_current_geometry_preflight_does_not_require_or_create_mission_path_or_apply(library, sample_at, localization_us):
    engine = library.engine_new()
    world = modern_world()
    world.localization.valid_for_us = localization_us
    try:
        send_vehicle(library, engine, vehicle())
        send_vehicle(library, engine, vehicle(2, at=sample_at))
        load(library, engine, world)
        result, source, facts = inspect(library, engine)
        assert result < -2 and source is not None
        assert source.world_sequence == world.sequence
        assert source.observation_sequence == world.observation_seq
        assert source.reference_id == world.reference_id
        assert source.reference_revision == world.reference_revision
        assert source.mapper_boot == world.mapper_boot and source.odom_epoch == world.odom_epoch
        assert facts[0] == 2
        # Independent original Pi lower observation from the fixture's 100ms
        # mapper offset and declared capture error; a fresh check mints no time.
        observed_pi_lower = world.observed_us + 100000 - world.localization.timestamp_error_us
        expected_end = min(observed_pi_lower + CFG['live_age_us'],
                           observed_pi_lower + localization_us,
                           sample_at + CFG['pose_age_us'] - 500)
        assert 1220000 < facts[1] == expected_end
        assert facts[2] == 1 and facts[3] > 0
        assert facts[4:] == [0, 0, 0, 0]  # no path/grant/search/worker completion
        _, again, repeated = inspect(library, engine, 1220010)
        assert again == source and repeated[1] == facts[1]
        assert repeated[4:] == [0, 0, 0, 0]
    finally:
        library.engine_free(engine)


@pytest.mark.parametrize('fault', ('steering_absent', 'error_absent', 'steering_limit',
                                  'error_limit', 'velocity_limit'))
def test_preflight_rejects_current_vehicle_input_faults_without_planning(library, fault):
    engine = library.engine_new()
    world = modern_world()
    latest = vehicle(2, at=1220000)
    if fault == 'steering_absent':
        latest.ClearField('estimated_steering_rad')
    elif fault == 'error_absent':
        latest.ClearField('steering_error_rad')
    elif fault == 'steering_limit':
        latest.estimated_steering_rad = 1.0
    elif fault == 'error_limit':
        latest.steering_error_rad = .5
    else:
        latest.velocity_mps = 2.0
    try:
        send_vehicle(library, engine, vehicle())
        send_vehicle(library, engine, latest)
        load(library, engine, world)
        result, source, facts = inspect(library, engine)
        assert result == 2 and source is None  # DE_INPUT
        assert facts[1] == 0 and facts[2] == 0 and facts[4:] == [0, 0, 0, 0]
    finally:
        library.engine_free(engine)


@pytest.mark.parametrize('fault', ('occupied', 'unknown', 'permission'))
def test_preflight_checks_actual_current_body_coverage(library, fault):
    engine = library.engine_new()
    world = modern_world()
    if fault == 'permission':
        world.drivable_permission = bytes(len(world.drivable_permission))
    else:
        obstacle(world, 0, 9, 0, world.height, unknown=fault == 'unknown')
    try:
        send_vehicle(library, engine, vehicle())
        send_vehicle(library, engine, vehicle(2, at=1220000))
        load(library, engine, world)
        result, source, facts = inspect(library, engine)
        assert result == 6 and source is None  # DE_PATH, no fake ready on UNKNOWN
        assert facts[1] == 0 and facts[2] == 1 and facts[4:] == [0, 0, 0, 0]
    finally:
        library.engine_free(engine)


def test_preflight_does_not_renew_expired_world_when_vehicle_is_fresh(library):
    engine = library.engine_new()
    world = modern_world()
    try:
        send_vehicle(library, engine, vehicle())
        send_vehicle(library, engine, vehicle(2, at=1220000))
        load(library, engine, world)
        # load stages reception; this actual owner poll must publish it while
        # valid before an already-published World's original E can be tested.
        ready, original_source, original = inspect(library, engine, 1220000)
        assert ready < -2 and original_source.world_sequence == world.sequence
        world_end = world.observed_us + 100000 - world.localization.timestamp_error_us + CFG['live_age_us']
        assert 1220000 < original[1] == world_end < 1500000
        assert original[0] == 2 and original[2] == 1 and original[4:] == [0, 0, 0, 0]
        fresh = vehicle(3, at=1500000)
        assert 1500000 - fresh.pi_sample_us + fresh.clock_error_us < CFG['pose_age_us']
        send_vehicle(library, engine, fresh)
        result, source, facts = inspect(library, engine, 1500000)
        assert result == 7 and source is None  # DE_EXPIRED
        assert facts[1] == 0 and facts[2] == 0 and facts[4:] == [0, 0, 0, 0]
    finally:
        library.engine_free(engine)


def test_preflight_has_no_current_map_when_staged_world_expires_before_owner_poll(library):
    engine = library.engine_new()
    try:
        send_vehicle(library, engine, vehicle())
        send_vehicle(library, engine, vehicle(2, at=1220000))
        load(library, engine, modern_world())
        send_vehicle(library, engine, vehicle(3, at=1500000))
        # Preserve the original failed fixture's distinct boundary: its first
        # owner poll drains the expired staged snapshot before publication.
        result, source, facts = inspect(library, engine, 1500000)
        assert result == 4 and source is None  # DE_MAP, not a geometry verdict
        assert facts == [0] * 8
    finally:
        library.engine_free(engine)


def test_cheap_current_input_probe_is_not_a_geometry_ready_substitute(library):
    engine = library.engine_new()
    world = modern_world()
    obstacle(world, 0, 9, 0, world.height, unknown=True)
    try:
        send_vehicle(library, engine, vehicle())
        send_vehicle(library, engine, vehicle(2, at=1220000))
        load(library, engine, world)
        for now in (1220000, 1220010, 1220020):
            result, source, facts = inspect(library, engine, now, current=True)
            assert result < -2 and source is not None
            assert facts[2:4] == [0, 0]
        result, source, facts = inspect(library, engine)
        assert result == 6 and source is None and facts[2] == 1
    finally:
        library.engine_free(engine)
