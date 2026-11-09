"""운영 설정·입력 preflight의 유한 형식. 현재 권위·주행 READY를 발급하지 않는다.

preflight의 원 clock/source/E는 caller가 실제 owner의 원 기록과 대조한다.
이 모듈은 clock·transport·장치·저장소를 읽지 않는다.
"""
from uuid import UUID

STATE_OPERATION=32
MAPPER_OPERATION=1
NIL=bytes(16)
ZERO=bytes(32)
U63=(1<<63)-1


def _positive(value,*names):
    if any(not 0<getattr(value,name)<=U63 for name in names):
        raise ValueError('operation positive original key/time')


def operation_binding_equal(left,right):
    """같은 서비스의 실제 설정·원 departure 결박만 비교한다. 권위 검사는 별도다."""
    return all(getattr(left,name)==getattr(right,name) for name in (
        'service_epoch','operation_revision','departure_nonce','permission_revision','parking_enabled'))


def preflight_source_matches(source,world):
    """원 검사에 사용한 World를 exact 식별한다. 최신 표본으로 바꾸지 않는다."""
    return (all(getattr(source,key)==getattr(world,key) for key in (
        'vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch',
        'clock_epoch','configuration','calibration','reference_id','reference_revision'))
        and source.primitive.hex()==world.primitive and source.world_sequence==world.sequence
        and source.observation_sequence==world.observation_seq)


def validate_one(value):
    """typed/wire 공통 구조·부모 결박. stale의 현재성 판정은 owner가 한다."""
    name=value.DESCRIPTOR.name
    if name=='OperationBinding':
        _positive(value,'operation_revision','permission_revision')
        if value.service_epoch==NIL:raise ValueError('operation service epoch')
    elif name=='OperationStatus':
        _positive(value,'ready_revision','valid_until_pi_us')
        if (value.vehicle_phase not in range(1,6) or value.desired_mode>2 or value.blocked_reason>31
                or value.auto_ready and value.blocked_reason or value.vehicle_phase>=3 and value.binding.departure_nonce==NIL):
            raise ValueError('operation status state')
        if value.vehicle_phase==3:
            _positive(value,'countdown_until_pi_us')
            if value.active_request_nonce==NIL or value.desired_mode!=1:raise ValueError('original AUTO countdown')
        elif value.countdown_until_pi_us:raise ValueError('inactive countdown sentinel')
    elif name=='AutoInputPreflight':
        _positive(value,'vehicle_sequence','control_generation','checked_guest_us','valid_until_pi_us','clock_probe_sequence')
        if value.clock_reply_sha256==ZERO:raise ValueError('preflight original confirmed clock SHA')
    elif name=='PiOperationGate':
        _positive(value,'control_generation','operation_revision','vehicle_sequence','valid_until_pi_us')
        if (value.gateway_instance==NIL or value.mode>2 or value.wait_confirmed and not value.stop_command_sequence
                or value.manual_local_admitted and value.mode!=2):raise ValueError('Pi operation gate origin')
    elif name=='ClockProbe':
        if value.HasField('supported_state_features') and value.supported_state_features&~63:
            raise ValueError('unsupported declared state capabilities')
        if value.HasField('auto_input_preflight'):
            report=value.auto_input_preflight
            if (not value.HasField('supported_state_features') or not value.supported_state_features&STATE_OPERATION
                    or report.clock_probe_sequence>=value.sequence or report.checked_guest_us>value.guest_send_us):
                raise ValueError('preflight prior confirmed clock/order/support')
    elif name=='ClockReply' and value.HasField('operation_status'):
        status=value.operation_status
        if (not value.probe.HasField('supported_state_features') or not value.probe.supported_state_features&STATE_OPERATION
                or status.binding.service_epoch!=UUID(value.gateway_instance).bytes
                or not value.HasField('guest_clock_valid_until_pi_us')
                or status.valid_until_pi_us>value.guest_clock_valid_until_pi_us):
            raise ValueError('operation status parent clock/service')
        if (value.HasField('current_mission') and value.current_mission.admitted and value.current_mission.HasField('operation_binding')
                and not operation_binding_equal(status.binding,value.current_mission.operation_binding)):
            raise ValueError('operation status/current grant binding')
        if status.auto_ready and (not value.probe.HasField('auto_input_preflight')
                or value.probe.auto_input_preflight.valid_until_pi_us<=value.pi_send_us
                or status.valid_until_pi_us>value.probe.auto_input_preflight.valid_until_pi_us):
            raise ValueError('operation READY requires current original preflight expiry')
    elif name=='MapperClockProbe' and value.HasField('operation_gate'):
        if value.operation_gate.valid_until_pi_us<=value.pi_send_us:raise ValueError('mapper operation original expiry')
    elif name=='MapperClockReply':
        if value.HasField('supported_mapper_features') and value.supported_mapper_features&~MAPPER_OPERATION:
            raise ValueError('unsupported mapper operation capabilities')
    elif name=='MissionGrant' and value.HasField('operation_binding'):
        if value.operation_binding.service_epoch!=value.gateway_instance:
            raise ValueError('operation binding/current grant service')
