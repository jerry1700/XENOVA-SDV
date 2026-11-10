"""PC 관측기의 읽기 전용 wire/표시 투영. 네트워크·SDK·주행 권한을 소유하지 않는다.

원 시각은 원 clock에 남긴다. 특히 DriveState에는 timestamp가 없으며 Vehicle의
ODOMETRY pose를 정확한 anchor 없이 MAP pose로 바꾸지 않는다. 수신된 사본은
Gateway 수락·장치 APPLIED·차체 이동의 증거가 아니다.
"""
from __future__ import annotations

import json
import re
from uuid import UUID

WORLD_TOPIC = 'xenova/v1/world/snapshot.pb'
VEHICLE_TOPIC = 'xenova/v1/vehicle/state.pb'
_PATH_TOPIC = re.compile(r'^xenova/v1/vms/([^/+#\s]+)/drive/link/out$')
_RUNTIME_TOPIC = re.compile(r'^xenova/v1/mapper/([a-z0-9][a-z0-9_-]{0,31})/runtime$')
_WORLD_IDENTITIES = ('vehicle', 'mapper_boot', 'mapper_instance', 'map_epoch', 'frame_epoch',
                     'odom_epoch', 'clock_epoch', 'configuration', 'calibration', 'primitive')
_WORLD_LAYER_LIMIT = 7  # 기존 UI의 8개 층 중 경로 한 층을 남긴다. 생략 원자료는 보존한다.
_U64 = (1 << 64) - 1


class SourceDecodeError(ValueError):
    """현재 표시용 계약으로 읽을 수 없는 입력. 제품 상태나 권한은 변경하지 않는다."""


def _codec():
    try:
        from xenova_drive_world import decode, schema
    except (ImportError,AttributeError) as error:
        raise SourceDecodeError('shared drive-world codec unavailable') from error
    return decode, schema


def _uuid(value):
    if type(value) is not str:
        raise SourceDecodeError('canonical source UUID required')
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise SourceDecodeError('invalid source UUID') from error
    if not parsed.int or str(parsed) != value:
        raise SourceDecodeError('canonical nonzero source UUID required')
    return value


def _uint(value, *, positive=False):
    if type(value) is str and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not (1 if positive else 0) <= value <= _U64:
        raise SourceDecodeError('original unsigned sequence/time required')
    return value


def _pose(value, frame):
    return dict(frame=frame, x=float(value.x), y=float(value.y), yaw=float(value.yaw))


def _points(values):
    return [[float(point.x), float(point.y)] for point in values]


def _layer(role, values, frame, *, closed=False, kind='MODEL', **metadata):
    return dict(kind=kind, frame=frame, points=_points(values), closed=closed, role=role, **metadata)


def _event(stream, topic, epoch, sequence, stamp, payload):
    return dict(stream=stream, source_id=topic, source_epoch=epoch,
                source_sequence=_uint(sequence, positive=True), source_stamp=stamp, payload=payload)


def _world(topic, value):
    frame = f'MAP:{value.map_epoch}:{value.frame_epoch}'
    layers = [_layer(role, getattr(value, role), frame)
              for role in ('reference', 'left_boundary', 'right_boundary')]
    if value.HasField('lane_model'):
        for lane in value.lane_model.lanes:
            layers.append(_layer('lane:' + str(lane.lane_id), lane.centerline, frame,
                                 lane_id=lane.lane_id, point_state=list(lane.point_state)))
        layers.append(_layer('divider', value.lane_model.divider, frame,
                             crossing=list(value.lane_model.crossing),
                             point_state=list(value.lane_model.divider_state)))
    slots = []
    for slot in value.parking_slots:
        layers.append(_layer('parking:' + str(slot.slot_id), slot.corners, frame, closed=True,
                             slot_id=slot.slot_id, geometry_state=slot.geometry_state,
                             occupancy_state=slot.occupancy_state))
        slots.append(dict(slot_id=slot.slot_id, geometry_revision=slot.geometry_revision,
                          geometry_state=slot.geometry_state, occupancy_state=slot.occupancy_state,
                          occupancy_revision=slot.occupancy_revision, corners=_points(slot.corners),
                          entry_edge=slot.entry_edge, approach_lane_id=slot.approach_lane_id,
                          geometry_complete=slot.geometry_complete, goal_mask=slot.goal_mask))
    identities = {key: getattr(value, key) for key in _WORLD_IDENTITIES}
    payload = dict(layers=layers[:_WORLD_LAYER_LIMIT], identities=identities,
                   omitted_layers=[layer['role'] for layer in layers[_WORLD_LAYER_LIMIT:]],
                   model_geometry=layers, parking_slots=slots, sequence=value.sequence,
                   reference_id=value.reference_id, reference_revision=value.reference_revision,
                   observation_seq=value.observation_seq, required_features=value.required_features,
                   observed_us=value.observed_us, completed_us=value.completed_us,
                   published_us=value.published_us, complete=value.complete, truncated=value.truncated,
                   reference_anchor=dict(vehicle_sequence=value.reference_odom_seq,
                                         pose=_pose(value.reference_vehicle_pose, frame)),
                   grid=dict(frame=frame, width=value.width, height=value.height,
                             origin=_pose(value.origin, frame), resolution_m=float(value.resolution_m),
                             occupancy=list(value.occupancy), cell_flags=list(value.cell_flags),
                             age_ms=[int.from_bytes(value.age_le32[i:i+4], 'little')
                                     for i in range(0, len(value.age_le32), 4)]),
                   protocol_stage='WORLD_SNAPSHOT_OBSERVED')
    epoch = '/'.join((value.mapper_boot, value.mapper_instance, value.map_epoch, value.frame_epoch))
    return _event('world', topic, epoch, value.sequence,
                  dict(domain='JETSON_MONOTONIC', epoch=value.clock_epoch, us=value.observed_us), payload)


def _vehicle(topic, value):
    identities = {key: getattr(value, key) for key in
                  ('vehicle', 'host_boot', 'device_session', 'odom_epoch', 'calibration')}
    for key in ('gateway_instance', 'pi_session'):
        identities[key] = getattr(value, key) if value.HasField(key) else None
    frame = 'ODOMETRY:' + value.odom_epoch
    payload = dict(identities=identities, pose=_pose(value.odom_pose, frame),
                   velocity_mps=float(value.velocity_mps), sequence=value.sequence,
                   source_sample=value.source_sample, source_us=value.source_us,
                   pi_sample_us=value.pi_sample_us, clock_error_us=value.clock_error_us,
                   position_error_m=float(value.position_error_m),
                   heading_error_rad=float(value.heading_error_rad), quality=value.quality,
                   left_ticks=value.left_ticks, right_ticks=value.right_ticks,
                   measured_steering_rad=float(value.measured_steering_rad) if value.HasField('measured_steering_rad') else None,
                   estimated_steering_rad=float(value.estimated_steering_rad) if value.HasField('estimated_steering_rad') else None,
                   sensor_stamp=dict(domain='MCU_MONOTONIC', epoch=value.device_session, us=value.source_us),
                   protocol_stage='VEHICLE_SAMPLE_OBSERVED')
    epoch = '/'.join((value.host_boot, identities['gateway_instance'] or 'unreported',
                      value.device_session, value.odom_epoch))
    return _event('vehicle', topic, epoch, value.sequence,
                  dict(domain='PI_MONOTONIC', epoch=value.host_boot, us=value.pi_sample_us), payload)


def _path(topic, vm, value):
    if value.source_vm != vm:
        raise SourceDecodeError('DriveState source VM does not match topic')
    frame = f'MAP:{value.map_epoch}:{value.frame_epoch}'
    identities = {key: getattr(value, key) for key in
                  ('source_vm', 'source_execution', 'source_app_instance', 'source_generation',
                   'map_epoch', 'frame_epoch', 'odom_epoch', 'configuration', 'calibration', 'primitive')}
    identities['vm_id'] = vm
    payload = dict(identities=identities,
                   layers=[_layer('planned_path', [point.pose for point in value.path], frame, kind='PATH')],
                   state_revision=value.state_revision, message_id=value.message_id,
                   path_revision=value.path_revision, maneuver_revision=value.maneuver_revision,
                   vehicle_sequence=value.vehicle_sequence, calculation_sequence=value.calculation_sequence,
                   observation_seq=value.observation_seq, tracker_index=value.tracker_index,
                   reference_id=value.reference_id, reference_revision=value.reference_revision,
                   merge_goal=_pose(value.merge_goal, frame), complete=value.complete,
                   path=[dict(pose=_pose(point.pose, frame), velocity_mps=float(point.velocity_mps),
                              steering_rad=float(point.steering_rad)) for point in value.path],
                   protocol_stage='VM_SNAPSHOT_OBSERVED')
    epoch = '/'.join((value.source_execution, value.source_app_instance, str(value.source_generation)))
    # DriveState has sequence references but no original snapshot timestamp.
    return _event('path', topic, epoch, value.state_revision, None, payload)


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise SourceDecodeError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise SourceDecodeError('nonfinite JSON number')


def _runtime(topic, device, raw):
    if len(raw) > 4096:
        raise SourceDecodeError('mapper runtime envelope exceeds4096 bytes')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise SourceDecodeError('invalid mapper runtime envelope') from error
    # xenova_mqtt.envelope의 원 필드를 읽는다. package __init__의 Paho client를
    # decoder에 import하지 않아 실제 MQTT owner와 독립적으로 사용할 수 있다.
    if (type(value) is not dict or value.get('schema_version') != '1.0'
            or type(value.get('payload')) is not dict
            or type(value.get('sent_at')) is not str or not value['sent_at']):
        raise SourceDecodeError('invalid mapper runtime envelope fields')
    _uuid(value.get('message_id'))
    _uint(value.get('seq'), positive=True)
    payload = value['payload']
    if (value.get('type') != 'mapper.runtime' or value.get('source_id') != 'jetson'
            or payload.get('profile') != 'mapper-runtime-1' or payload.get('device') != device):
        raise SourceDecodeError('mapper runtime type/device/profile mismatch')
    boot = _uuid(value.get('boot_id')); instance = _uuid(value.get('agent_instance_id'))
    if payload.get('mapper_boot') != boot or payload.get('service_instance') != instance:
        raise SourceDecodeError('mapper runtime original source mismatch')
    stamp = _uint(payload.get('snapshot_j_us'))
    epoch = payload.get('clock_epoch')
    if epoch is not None:
        _uuid(epoch)
    payload['envelope'] = {key: value[key] for key in
                           ('schema_version', 'message_id', 'source_id', 'boot_id',
                            'agent_instance_id', 'type', 'seq', 'sent_at')}
    return _event('runtime', topic, boot + '/' + instance, value.get('seq'),
                  dict(domain='JETSON_MONOTONIC', epoch=epoch or boot, us=stamp), payload)


def decode_mqtt(topic: str, raw: bytes) -> dict | None:
    """기존 4종 topic의 표시 event. 다른 topic은 None, 잘못된 해당 입력은 예외다.

    공용 codec이 검사한 형식은 관측 표시용이며 현재 주행 허가가 아니다. retained,
    수신 나이·순서·현재 active VM/같은 frame 선택은 collector가 별도로 표시한다.
    """
    path = _PATH_TOPIC.fullmatch(topic)
    runtime = _RUNTIME_TOPIC.fullmatch(topic)
    if topic not in (WORLD_TOPIC, VEHICLE_TOPIC) and path is None and runtime is None:
        return None
    if type(raw) is not bytes or not raw:
        raise SourceDecodeError('nonempty original wire bytes required')
    if runtime:
        return _runtime(topic, runtime[1], raw)
    decode, schema = _codec()
    try:
        if topic == WORLD_TOPIC:
            return _world(topic, decode(raw, schema.WorldSnapshot))
        if topic == VEHICLE_TOPIC:
            return _vehicle(topic, decode(raw, schema.VehicleSample))
        vm = _uuid(path[1])
        link = decode(raw, schema.DriveLink)
        return _path(topic, vm, link.snapshot) if link.WhichOneof('payload') == 'snapshot' else None
    except ValueError as error:
        if isinstance(error, SourceDecodeError):
            raise
        raise SourceDecodeError('invalid shared drive-world wire') from error


def _response(value):
    if value is None:
        return None
    if type(value) is not dict:
        raise SourceDecodeError('operator response must be a dict or null')
    if 'ok' not in value:
        return value
    if value.get('version') != 2 or type(value.get('ok')) is not bool:
        raise SourceDecodeError('invalid OperatorClient response version')
    return value.get('data') if value['ok'] and type(value.get('data')) is dict else None


def project_operator_status(status: dict | None, operation_status: dict | None = None) -> dict:
    """기존 status/operation.status의 원 보고를 투영한다. 누락을 READY로 채우지 않는다.

    서로 다른 host/instance/generation의 응답은 섞지 않는다. status에는 standby
    VM 정보가 없으므로 추정하지 않는다. APPLIED/실제 이동 판단도 하지 않는다.
    """
    current = _response(status); operating = _response(operation_status)
    operation = dict(mode='UNKNOWN', vehicle_phase='UNKNOWN', parking_enabled=None,
                     active_vm=None, standby_vm=None, auto_ready=None, blocked_reason='UNKNOWN')
    source = dict(host_boot=None, gateway_instance=None, generation=None,
                  protocol_stage='OPERATOR_STATUS_OBSERVED', context_match=None, stop_reason=None)
    if current is not None:
        operation['mode'] = current.get('mode') if current.get('mode') in ('AUTO', 'MANUAL', 'AUTO_READY', 'SAFE_STOP') else 'UNKNOWN'
        operation['active_vm'] = current.get('owner')
        source.update(host_boot=current.get('host_boot_id'), gateway_instance=current.get('gateway_instance_id'),
                      generation=current.get('generation'), pi_now_ms=current.get('pi_now_ms'),
                      output_permitted=current.get('output_permitted'))
        hold = current.get('hold')
        if type(hold) is dict:
            source['hold'] = dict(active=hold.get('active'), reason=hold.get('reason'))
            if hold.get('active') is True and type(hold.get('reason')) is str and hold['reason']:
                source['stop_reason'] = hold['reason']
    if operating is not None and operating.get('contract') == 'operation-status-1' and operating.get('supported') is True:
        keys = ('host_boot_id', 'gateway_instance_id', 'generation')
        same = all(operating.get(key) is not None for key in keys) and (
            current is None or all(operating.get(key) == current.get(key) and key in current for key in keys))
        source['context_match'] = same
        if same:
            binding = operating.get('binding')
            operation.update(vehicle_phase=operating.get('vehicle_phase') if type(operating.get('vehicle_phase')) is str else 'UNKNOWN',
                             auto_ready=operating.get('auto_ready') if type(operating.get('auto_ready')) is bool else None,
                             blocked_reason=operating.get('blocked_reason', 'UNKNOWN'),
                             parking_enabled=binding.get('parking_enabled') if type(binding) is dict and type(binding.get('parking_enabled')) is bool else None)
            source.update(host_boot=operating.get('host_boot_id'), gateway_instance=operating.get('gateway_instance_id'),
                          generation=operating.get('generation'), pi_now_us=operating.get('pi_now_us'),
                          valid_until_pi_us=operating.get('valid_until_pi_us'), ready_revision=operating.get('ready_revision'),
                          operation_revision=None if type(binding) is not dict else binding.get('operation_revision'),
                          permission_revision=None if type(binding) is not dict else binding.get('permission_revision'))
    return dict(operation=operation, source=source)


def project_gateway_context(record: dict) -> dict:
    """기본 Gateway G1의 원 응답. 조회 관측 시각과 제품 상태의 시각을 구분한다."""
    current=record['context'];observed=record['observation']
    if type(current) is not dict or type(observed) is not dict:
        raise SourceDecodeError('provider context observation required')
    host=_uuid(current.get('host_boot_id'));observer=_uuid(observed.get('observer_epoch'))
    sequence=_uint(observed.get('sequence'),positive=True)
    received=_uint(observed.get('response_pi_us'))
    mode=current.get('mode');owner=current.get('owner')
    if mode not in ('SAFE_STOP','AUTO','AUTO_READY','MANUAL'):
        raise SourceDecodeError('original provider mode required')
    if owner is not None:_uuid(owner)
    _uint(current.get('generation'))
    if type(current.get('output_permitted')) is not bool:
        raise SourceDecodeError('original provider output state required')
    interface=record.get('operator_interface')
    if interface not in ('ABSENT','UNREACHABLE'):raise SourceDecodeError('operator interface observation required')
    return dict(operation=dict(mode=mode,active_vm=None if mode=='SAFE_STOP' else owner,
        standby_vm=None,vehicle_phase='UNKNOWN',parking_enabled=None,auto_ready=None,blocked_reason=None),
        source=dict(channel='G1_CONTEXT',observer_epoch=observer,sequence=sequence,
            response_stamp=dict(domain='PI_OBSERVER_RESPONSE',epoch=observer,us=str(received)),
            host_boot=host,selected_owner=owner,generation=current['generation'],phase=current.get('phase'),
            output_permitted=current['output_permitted'],operator_interface=interface))
