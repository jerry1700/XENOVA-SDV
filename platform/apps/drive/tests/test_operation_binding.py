"""Actual C production/apply/reuse keys for an issuer-supplied operation stamp.

Issuer/transport values below are explicit software fixtures. They do not prove
Gateway permission, physical STOP, P4 or a new Enter. Geometry/track and immutable
origin are exercised in the actual C engine; actor/P4 uses the persistent bridge.
"""

import ctypes as C

from test_engine import exported_state, lane_grant, load, modern_world, send_vehicle, vehicle
from test_lattice import encode, library
from world_codec import decode, schema


def grant(*,enabled=False, permission=1, revision=3, kind=1):
    value = lane_grant(revision=revision)
    current = value.mission_grant
    current.kind = kind
    current.operation_binding.CopyFrom(schema.OperationBinding(
        service_epoch=current.gateway_instance, operation_revision=1,
        departure_nonce=bytes.fromhex('d1' * 16), permission_revision=permission,
        parking_enabled=enabled))
    if kind == 2:
        current.strategy = 1
        current.mission_deadline_pi_us = 121000000
        current.search_deadline_pi_us = 31000000
        current.max_execution_travel_m = 5
        current.max_search_travel_m = 5
    return value


def admit(lib, engine, value):
    raw = encode(value)
    assert lib.engine_grant(engine, raw, len(raw), 1220000) == 0
    return raw


def ready_inputs(lib, engine):
    send_vehicle(lib, engine, vehicle())
    send_vehicle(lib, engine, vehicle(2, at=1220000))
    load(lib, engine, modern_world())


def original_mission(lib, engine):
    lib.engine_origin_mission.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
    data = C.create_string_buffer(512)
    count = lib.engine_origin_mission(engine, data, len(data))
    assert count > 0
    return decode(data.raw[:count], schema.MissionExecution)


def test_actual_metadata_exports_binding_and_same_revision_permission_change_closes_old_key(library):
    engine = library.engine_new()
    old = grant()
    changed = grant(enabled=True, permission=2)  # same mission/grant revision on purpose
    try:
        ready_inputs(library, engine)
        old_wire = admit(library, engine, old)
        assert library.engine_plan(engine, 1220000, 4096) == 0
        command = (C.c_float * 4)()
        assert library.engine_track_grant(engine, old_wire, len(old_wire), 1220000, command) == 0
        before = exported_state(library, engine)
        assert before.state_features == 47 and before.mission.operation_binding == old.mission_grant.operation_binding
        new_wire = admit(library, engine, changed)
        assert library.engine_track_grant(engine, new_wire, len(new_wire), 1220000, command) == 9
        assert command[0] == 0
        assert exported_state(library, engine).mission == before.mission  # no in-place relabel
        # The current geometry becomes a separate validated candidate, not a grant mutation.
        assert library.engine_plan(engine, 1220000, 4096) == 1
        library.engine_finish_apply.argtypes = [C.c_void_p, C.c_uint64]
        assert library.engine_finish_apply(engine, 1220000) == 0
        assert library.engine_track_grant(engine, new_wire, len(new_wire), 1220000, command) == 0
        after = exported_state(library, engine)
        assert after.mission.operation_binding == changed.mission_grant.operation_binding
        assert after.calculation_sequence > before.calculation_sequence
        assert after.path == before.path
        assert after.mission.mission_deadline_pi_us == before.mission.mission_deadline_pi_us == 0
    finally:
        library.engine_free(engine)


def test_actual_import_revalidates_geometry_but_keeps_original_copy_mission(library):
    source = library.engine_new()
    target = library.engine_new()
    old = grant()
    changed = grant(enabled=True, permission=2)
    try:
        for engine in (source, target):
            ready_inputs(library, engine)
        old_wire = admit(library, source, old)
        assert library.engine_plan(source, 1220000, 4096) == 0
        command = (C.c_float * 4)()
        assert library.engine_track_grant(source, old_wire, len(old_wire), 1220000, command) == 0
        state = exported_state(library, source)
        original = encode(state)
        new_wire = admit(library, target, changed)
        assert library.engine_apply(target, original, len(original), 1220000) == 0
        assert original_mission(library, target) == state.mission
        assert encode(state) == original
        assert library.engine_track_grant(target, new_wire, len(new_wire), 1220000, command) == 0
        after = exported_state(library, target)
        assert after.mission.operation_binding == changed.mission_grant.operation_binding
        assert original_mission(library, target).operation_binding == old.mission_grant.operation_binding
        assert library.engine_track_grant(target, old_wire, len(old_wire), 1220000, command) == 9
        assert command[0] == 0
    finally:
        library.engine_free(source)
        library.engine_free(target)


def test_pending_geometry_candidate_rechecks_same_revision_binding_before_commit(library):
    engine = library.engine_new()
    try:
        ready_inputs(library, engine)
        old_wire = admit(library, engine, grant())
        assert library.engine_plan(engine, 1220000, 4096) == 0
        command = (C.c_float * 4)()
        assert library.engine_track_grant(engine, old_wire, len(old_wire), 1220000, command) == 0
        original = exported_state(library, engine)
        admit(library, engine, grant(enabled=True, permission=2))
        assert library.engine_plan(engine, 1220000, 4096) == 1
        admit(library, engine, grant(enabled=False, permission=3))
        library.engine_finish_apply.argtypes = [C.c_void_p, C.c_uint64]
        assert library.engine_finish_apply(engine, 1220000) == 9
        assert exported_state(library, engine).mission == original.mission
    finally:
        library.engine_free(engine)


def test_follow_search_permission_switch_preserves_original_parking_attempt_budget(library):
    engine = library.engine_new()
    library.engine_behavior_enable_for_budget.argtypes = [C.c_void_p]
    library.engine_parking_original_budget.argtypes = [C.c_void_p, C.POINTER(C.c_uint64)]
    try:
        # Enable the actual budget owner only; no facts, NONE receipt or READY are fabricated.
        library.engine_behavior_enable_for_budget(engine)
        initial = grant(enabled=True, kind=2)
        admit(library, engine, initial)
        before = (C.c_uint64 * 8)()
        library.engine_parking_original_budget(engine, before)
        assert list(before) == [1, 121000000, 31000000, 0, 0, 2, 1, 3]
        admit(library, engine, grant(enabled=False, permission=2, revision=4, kind=1))
        admit(library, engine, grant(enabled=True, permission=3, revision=5, kind=2))
        after = (C.c_uint64 * 8)()
        library.engine_parking_original_budget(engine, after)
        assert list(after) == list(before)
    finally:
        library.engine_free(engine)
