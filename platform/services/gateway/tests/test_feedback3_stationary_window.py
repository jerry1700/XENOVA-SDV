"""정지 확인에 쓸 표본 창을 자른 결과가 check_stationary_window 의 길이 검사에 모자라지 않는지 본다.

표본마다 시각 오차가 다르다(도착 시각으로 위쪽을 자르기 때문이다). 마지막 표본의 오차가 더 큰 창이 문제였다.
"""
from collections import OrderedDict
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from xenova_gateway.feedback3 import Feedback3SerialSink
from xenova_gateway.odometry import OdometryConfig, VehicleOdometry
from xenova_gateway.scene_authority import COMMAND3, FEEDBACK3, StationaryPolicy

VM = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
GENERATION = 7
START_US = 1_000_000
POLICY = StationaryPolicy(30000, 100000, 200000, .000164, .05, 1., 0., 0.)
# 실보드를 PC 의 게이트웨이 코드에 물렸을 때 본 창. (앞 표본에서의 Pi 시각 간격, 시각 오차)
SEEN = ((0, 5000), (20000, 5883), (21570, 6002), (20207, 6196))


def rig(rows, policy=POLICY):
    """멈춘 차의 표본 rows 를 받은 sink. 마지막 표본을 가리키는 STOP APPLIED 가 와 있다."""
    sink = object.__new__(Feedback3SerialSink)
    sink._vehicle_wire_cache = OrderedDict()
    odometry = VehicleOdometry(OdometryConfig(
        vehicle='rc-test-1', calibration='encoder-lab-1', metres_per_tick=.000164, track_width_m=.174,
        wheelbase_m=.167, maximum_speed_mps=1., maximum_clock_error_us=20000, maximum_sample_age_us=200000,
        sample_period_ms=20, encoder_bits=32, position_error_m=.005, heading_error_rad=.000001,
        distance_error_ratio=.02, heading_error_per_m=.02), str(uuid4()))
    pi, device, gateway = str(uuid4()), str(uuid4()), str(uuid4())
    odometry.device_session(device)
    # 첫 표본은 기준으로만 쓰이고 창에 들지 않는다
    assert odometry.observe_mapped(pi, device, 480000, 1, 0, 0, 1, 0, 0, START_US - 21000, START_US - 19000) is None
    at = START_US
    for index, (gap, error) in enumerate(rows):
        at += gap
        assert odometry.observe_mapped(pi, device, 500000 + 20000*index, index + 2, 0, 0, 1, 0, 0,
                                       at - error, at + error) is not None
    assert len(odometry.samples) == len(rows)
    last = odometry.latest
    command = COMMAND3.pack(UUID(pi).bytes, UUID(device).bytes, 1, bytes(16), 0, 0, 0, 0, GENERATION, 1, 2, 0, 0, 0, 0, 0)
    feedback = FEEDBACK3.pack(UUID(pi).bytes, UUID(device).bytes, 1, GENERATION, 1, 2, 2, 1, last['source_sample'],
                              last['source_us'], 5, 0, 0, 1, bytes(16), 0, 0, 0, 0)
    sink.odometry = odometry
    sink.stationary_policy = policy
    sink.pi_session, sink.device_session = pi, device
    sink.last_motion = False
    sink.stop_pin = dict(applied=True, feedback=feedback, command=command, submitted_us=START_US - 100000,
                         gateway=gateway, sequence=1, frame=SimpleNamespace(generation=GENERATION))
    return sink, gateway, last


def current(last):
    return dict(vehicle=last['vehicle'], calibration=last['calibration'], odom_epoch=last['odom_epoch'])


def now_after(last):
    return last['pi_sample_us'] + last['clock_error_us'] + 1000


def test_window_kept_for_matched_stop_passes_when_the_last_sample_is_less_certain():
    sink, gateway, last = rig(SEEN)
    matched = sink._make_matched_stop()
    assert matched is not None
    # 뒤의 세 표본만으로는 41777 - 5883 - 6196 = 29698 us 라 30 ms 에 모자란다. 앞 표본을 남겨야 한다
    assert len(matched.vehicle_window) == 4
    assert matched.check(current(last), sink.odometry.host_boot, gateway, GENERATION, sink.pi_session,
                         sink.device_session, POLICY, now_after(last))


def test_window_is_still_cut_to_the_shortest_one_that_passes():
    sink, gateway, last = rig(((0, 1000), (20000, 1000), (20000, 1000), (20000, 1000), (20000, 1000)))
    matched = sink._make_matched_stop()
    # 40 ms - 2 x 1 ms 이면 넉넉하다. 더 오래된 표본은 버린다
    assert len(matched.vehicle_window) == 3
    assert matched.check(current(last), sink.odometry.host_boot, gateway, GENERATION, sink.pi_session,
                         sink.device_session, POLICY, now_after(last))


@pytest.mark.parametrize('first,last_error,kept', [(4000, 6000, 3), (4000, 6001, 4), (6000, 4000, 3), (6001, 4000, 4)])
def test_cut_uses_the_errors_of_both_ends(first, last_error, kept):
    # 뒤의 세 표본은 40 ms 에 걸친다. 두 끝의 오차를 더해 10 ms 를 넘으면 한 표본을 더 남긴다
    sink, gateway, last = rig(((0, 1000), (20000, first), (20000, 1000), (20000, last_error)))
    matched = sink._make_matched_stop()
    assert len(matched.vehicle_window) == kept
    assert matched.check(current(last), sink.odometry.host_boot, gateway, GENERATION, sink.pi_session,
                         sink.device_session, POLICY, now_after(last))


def hold_snapshot(sink, gateway, last):
    """방향을 바꾸는 자리의 STOP 뒤에 HOLD_STEER 가 APPLIED 된 때의 snapshot"""
    command = dict(next_direction=-1, calculation_sequence='1', plan_revision='1', phase_revision='1', segment_index=0,
                   cusp_index=0, mission_id=str(uuid4()), mission_revision='1', grant_revision='1',
                   driver_session=str(UUID(int=0)), driver_action_seq='0', driver_state_seq='0')
    witness = dict(vm_id=VM, app_id='autonomous', generation=GENERATION, direction_intent_admitted=True,
                   gateway_instance=gateway, command=command)
    pin = dict(sink.stop_pin, witness=witness, phase_end=now_after(last) + 1_000_000)
    hold = dict(applied=True, feedback=sink.stop_pin['feedback'], frame=SimpleNamespace(steer_rad=.1))
    return dict(pin=pin, rows=tuple(dict(row) for row in sink.odometry.samples), clock_ready=True, sample=dict(last),
                hold=hold, hold_start=START_US, gate_revision=3, matched=None, moving=False,
                clock_end=now_after(last) + 1_000_000)


def test_hold_steer_gate_is_offered_when_the_last_sample_is_less_certain():
    sink, gateway, last = rig(SEEN)
    sink._direction_snapshot = hold_snapshot(sink, gateway, last)
    witness = sink.direction_witness(VM, 'autonomous', SimpleNamespace(generation=GENERATION), now_after(last))
    # 조향 측정이 없으므로 준비 완료(4)는 아니다. 정지 확인은 통과해 gate 가 나온다
    assert witness is not None and witness.gate.state == 2 and witness.gate.requested_direction == -1


def test_hold_steer_gate_is_withheld_when_the_wheels_moved():
    sink, gateway, last = rig(SEEN)
    snapshot = hold_snapshot(sink, gateway, last)
    moved = [dict(row) for row in snapshot['rows']]
    moved[-1]['left_ticks'] += 40
    snapshot['rows'] = tuple(moved)
    sink._direction_snapshot = snapshot
    assert sink.direction_witness(VM, 'autonomous', SimpleNamespace(generation=GENERATION), now_after(last)) is None
