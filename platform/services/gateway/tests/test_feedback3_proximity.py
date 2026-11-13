"""근거리 제동을 하는 장치의 막힘 표시와 거절을 고장으로 묶지 않고 받는지 본다.

장치는 capability 0x1000 을 알리고, 표본 quality 의 bit 3(앞)·bit 4(뒤)로 막힌 방향을 알리며,
막힌 방향의 DRIVE 를 REJECTED 와 reason 21 로 거절한다.
"""
from collections import OrderedDict
from queue import Queue
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from xenova_gateway.feedback import FeedbackSerialSink, Framer
from xenova_gateway.feedback3 import (CLOCK3, CLOCK_REPLY, REPORT, SENSOR, SENSOR3, Feedback3SerialSink,
                                      PROXIMITY_BLOCKED, PROXIMITY_CAPABILITY, PROXIMITY_FRONT, PROXIMITY_REAR)
from xenova_gateway.odometry import OdometryConfig, VehicleOdometry
from xenova_gateway.scene_authority import COMMAND3, FEEDBACK3
from xenova_gateway.tof_feedback3 import TofInbox

REQUIRED = 0x01f7
GENERATION = 7
NOW_US = 5_000_000
CUTOFF_US = 4_900_000


def rig(capabilities=REQUIRED | PROXIMITY_CAPABILITY):
    """handshake 와 시각 설치가 끝난 sink. 장치 시각에 1 ms 안팎을 더하면 Pi 시각이다."""
    sink = object.__new__(Feedback3SerialSink)
    sink.pi_session, sink.device_session, sink.clock_epoch = str(uuid4()), str(uuid4()), str(uuid4())
    sink.profile = SimpleNamespace(drift_ppm=0)
    sink.odometry = VehicleOdometry(OdometryConfig(
        vehicle='rc-test-1', calibration='encoder-lab-1', metres_per_tick=.000164, track_width_m=.174,
        wheelbase_m=.167, maximum_speed_mps=1., maximum_clock_error_us=20000, maximum_sample_age_us=200000,
        sample_period_ms=20, encoder_bits=32, position_error_m=.005, heading_error_rad=.000001,
        distance_error_ratio=.02, heading_error_per_m=.02), str(uuid4()))
    sink.odometry.device_session(sink.device_session)
    sink.odometry.clock_profile3 = True
    sink.ready = True
    sink.capabilities = capabilities
    sink.fault = None
    sink._sensor_blocked_reason = None
    sink.proximity, sink.proximity_rejected = None, 0
    sink.leases = OrderedDict([(1, dict(sequence=1, probe=1, reference=NOW_US, lower=-1000, upper=1000,
                                        expires=NOW_US + 800000, cutoff=NOW_US + 700000, ack=True))])
    sink.pending = OrderedDict()
    sink.stop_pin = None
    sink.report_hwm = sink.duplicates = sink.rejected = sink.received = 0
    sink.latest_command = sink.last_report = None
    sink.events = Queue(8)
    sink.tof_inbox = TofInbox()
    return sink


def sensor(sink, sample, quality, at_us=None):
    """표본 하나를 SENSOR 로 받는다. 받은 시각의 10 ms 전에 장치가 뜬 표본이다."""
    at_us = NOW_US + 20000*sample if at_us is None else at_us
    payload = SENSOR3.pack(UUID(sink.pi_session).bytes, UUID(sink.device_session).bytes, sample, at_us - 10000,
                           0, 0, quality, 0, 0, UUID(sink.clock_epoch).bytes, 1)
    sink._receive(SENSOR, payload, at_us/1e6)


def drive(sink, sequence, kind=1, velocity=200, deadline_s=6.):
    """sink 가 이미 내보낸 명령 하나를 적용 대기로 넣는다."""
    command = COMMAND3.pack(UUID(sink.pi_session).bytes, UUID(sink.device_session).bytes, sequence,
                            UUID(sink.clock_epoch).bytes if kind != 2 else bytes(16), 1 if kind != 2 else 0,
                            NOW_US if kind != 2 else 0, NOW_US + 90000 if kind != 2 else 0,
                            CUTOFF_US if kind != 2 else 0, GENERATION, sequence, kind, 0, velocity if kind == 1 else 0,
                            0, 0, 0)
    row = dict(sequence=sequence, frame=SimpleNamespace(generation=GENERATION, counter=sequence), command=command,
               kind=kind, clock=1 if kind != 2 else 0, expires=NOW_US + 90000 if kind != 2 else 0, deadline=deadline_s,
               accepted=False, applied=False)
    sink.pending[sequence] = row
    sink.latest_command = sequence
    return row


def report(sink, row, status, reason, report_sequence=1):
    command = COMMAND3.unpack(row['command'])
    payload = FEEDBACK3.pack(UUID(sink.pi_session).bytes, UUID(sink.device_session).bytes, row['sequence'], GENERATION,
                             row['frame'].counter, row['kind'], status, report_sequence, 1, NOW_US - 10000, 0, 0, 0, 0,
                             command[3], command[4], command[7], command[11], reason)
    sink._receive(REPORT, payload, (NOW_US + 1000)/1e6)


def poll_later(sink, now_s):
    """들어온 바이트 없이 기한 검사만 돌린다."""
    sink.started = 0.
    sink.timeout = 1.
    sink.framer = Framer()
    sink.clock = lambda: now_s
    sink._serial = SimpleNamespace(read=lambda size: b'')
    FeedbackSerialSink._poll(sink, now_s, publish_view=False)


def test_blocked_bits_are_kept_for_the_view_and_hidden_from_odometry():
    sink = rig()
    sensor(sink, 1, 1)
    assert sink.proximity_view == dict(proximity_front_blocked=False, proximity_rear_blocked=False, proximity_rejected=0)
    sensor(sink, 2, 1 | PROXIMITY_FRONT)
    assert sink.proximity_view == dict(proximity_front_blocked=True, proximity_rear_blocked=False, proximity_rejected=0)
    sensor(sink, 3, 1 | PROXIMITY_FRONT | PROXIMITY_REAR)
    assert sink.proximity_view == dict(proximity_front_blocked=True, proximity_rear_blocked=True, proximity_rejected=0)
    # 막힘 bit 가 켜진 표본으로도 odometry 가 이어지고, odometry 가 적어 두는 quality 에는 그 bit 가 없다
    assert [row['source_sample'] for row in sink.odometry.samples] == [2, 3]
    assert sink.odometry.first_error is None
    assert sink.odometry._source_payload[5] == 1
    sensor(sink, 4, 1 | PROXIMITY_REAR)
    assert sink.proximity_view == dict(proximity_front_blocked=False, proximity_rear_blocked=True, proximity_rejected=0)
    sensor(sink, 5, 1)
    assert sink.proximity_view == dict(proximity_front_blocked=False, proximity_rear_blocked=False, proximity_rejected=0)


def test_late_older_sample_does_not_move_the_blocked_state_back():
    sink = rig()
    sensor(sink, 1, 1)
    sensor(sink, 3, 1 | PROXIMITY_FRONT)
    # 순번이 앞선 표본이 늦게 와도 막힘 상태는 최신 표본의 것이다
    with pytest.raises(ValueError, match='ODOM_SAMPLE_ORDER'):
        sensor(sink, 2, 1, at_us=NOW_US + 20000*3 + 1000)
    assert sink.proximity == PROXIMITY_FRONT


@pytest.mark.parametrize('quality', [0, PROXIMITY_REAR, 1 | 2 | 4, 1 | 0x20])
def test_sample_refused_by_odometry_does_not_change_the_blocked_state(quality):
    sink = rig()
    sensor(sink, 1, 1)
    sensor(sink, 2, 1 | PROXIMITY_FRONT)
    # odometry 가 받지 않는 표본이다. 순번이 크게 뛰어도 그 표본의 막힘 bit 로 상태를 바꾸지 않는다
    with pytest.raises(ValueError, match='ODOM_SENSOR_QUALITY'):
        sensor(sink, 100, quality, at_us=NOW_US + 50000)
    assert sink.proximity == PROXIMITY_FRONT
    # 그 뒤의 정상 표본은 odometry 와 막힘 상태에 모두 반영된다
    sensor(sink, 3, 1 | PROXIMITY_REAR)
    assert sink.odometry.samples[-1]['source_sample'] == 3
    assert sink.proximity == PROXIMITY_REAR
    sensor(sink, 4, 1)
    assert sink.proximity == 0


def test_sample_outside_its_lease_window_does_not_change_the_blocked_state():
    sink = rig()
    sensor(sink, 1, 1)
    sensor(sink, 2, 1 | PROXIMITY_FRONT)
    # lease 의 기한 뒤에 받은 표본은 odometry 에 닿기 전에 버려진다
    with pytest.raises(ValueError, match='sensor3 lease/counter'):
        sensor(sink, 3, 1, at_us=NOW_US + 800000)
    assert sink.proximity == PROXIMITY_FRONT


def test_clock_reply_sample_carries_the_blocked_bits_too():
    sink = rig()
    sensor(sink, 1, 1)
    sink.outstanding = (9, NOW_US + 30000)
    sink.profile = SimpleNamespace(drift_ppm=0, timestamp_error_us=0, probe_timeout_us=200000, max_offset_width_us=100000,
                                   lease_us=1000000, j_cutoff_us=100)
    sink.previous_interval = (-1000, 7000, NOW_US)
    sink.streak = 1
    sink.install = sink.leases[1]
    at = NOW_US + 40000
    reply = CLOCK3.pack(UUID(sink.pi_session).bytes, UUID(sink.device_session).bytes, 9, NOW_US + 30000,
                        at - 9000, at - 8000, at - 7000, 2, 0, 0, 1 | PROXIMITY_REAR, 0, 0)
    sink.clock = lambda: at/1e6
    sink._send = lambda raw: None
    sink._receive(CLOCK_REPLY, reply, at/1e6)
    assert sink.proximity == PROXIMITY_REAR
    assert sink.odometry.first_error is None


def test_device_without_the_capability_cannot_use_the_bits():
    sink = rig(REQUIRED)
    sensor(sink, 1, 1)
    with pytest.raises(ValueError, match='ODOM_SENSOR_QUALITY'):
        sensor(sink, 2, 1 | PROXIMITY_FRONT)
    assert sink.proximity is None
    assert sink.proximity_view == dict(proximity_front_blocked=None, proximity_rear_blocked=None, proximity_rejected=0)
    assert sink.odometry.first_error == 'ODOM_SENSOR_QUALITY'


def test_unknown_quality_bits_are_still_refused_with_the_capability():
    sink = rig()
    sensor(sink, 1, 1)
    with pytest.raises(ValueError, match='ODOM_SENSOR_QUALITY'):
        sensor(sink, 2, 1 | 0x20)


def test_proximity_rejection_of_a_drive_is_not_a_device_fault():
    sink = rig()
    row = drive(sink, 5)
    report(sink, row, 3, PROXIMITY_BLOCKED)
    assert sink.fault is None and sink._sensor_blocked_reason is None
    assert sink.proximity_rejected == 1
    assert sink.last_report['status'] == 'REJECTED' and sink.last_report['rejection'] == 'PROXIMITY_BLOCKED'
    assert sink.last_report['accepted'] is False and sink.last_report['applied'] is False
    assert sink.events.empty()
    # 적용을 기다리던 줄에서 빠져, 기한이 지나도 적용 기한 초과가 되지 않는다
    assert 5 not in sink.pending
    poll_later(sink, 7.)
    assert sink.fault is None


def test_a_drive_that_gets_no_report_still_times_out():
    sink = rig()
    drive(sink, 5)
    poll_later(sink, 7.)
    assert sink.fault == 'DEVICE_APPLY_TIMEOUT'


def test_later_drive_is_judged_on_its_own_after_a_proximity_rejection():
    sink = rig()
    report(sink, drive(sink, 5), 3, PROXIMITY_BLOCKED, report_sequence=1)
    row = drive(sink, 6)
    report(sink, row, 2, 0, report_sequence=2)
    assert row['applied'] and sink.fault is None
    assert sink.last_report['status'] == 'APPLIED' and 'rejection' not in sink.last_report
    # APPLIED 증거는 근거리 처리를 넣기 전과 같은 키만 가진다
    assert set(sink.events.get_nowait()) == set(sink.last_report)


@pytest.mark.parametrize('reason', [0, 9, 10, 14, 18, 20])
def test_other_rejections_of_a_drive_stay_device_faults(reason):
    sink = rig()
    report(sink, drive(sink, 5), 3, reason)
    assert sink.fault == 'DEVICE_REJECTED' and sink.proximity_rejected == 0
    assert 5 in sink.pending


@pytest.mark.parametrize('kind', [2, 3])
def test_stop_and_hold_cannot_be_refused_for_proximity(kind):
    sink = rig()
    report(sink, drive(sink, 5, kind=kind), 3, PROXIMITY_BLOCKED)
    assert sink.fault == 'DEVICE_REJECTED' and sink.proximity_rejected == 0


def test_fault_status_with_the_proximity_reason_is_still_a_fault():
    sink = rig()
    report(sink, drive(sink, 5), 4, PROXIMITY_BLOCKED)
    assert sink.fault == 'DEVICE_FAULT'


def test_device_without_the_capability_cannot_refuse_for_proximity():
    sink = rig(REQUIRED)
    report(sink, drive(sink, 5), 3, PROXIMITY_BLOCKED)
    assert sink.fault == 'DEVICE_REJECTED' and sink.proximity_rejected == 0


def test_wire_values_match_the_contract():
    assert (PROXIMITY_CAPABILITY, PROXIMITY_FRONT, PROXIMITY_REAR, PROXIMITY_BLOCKED) == (0x1000, 0x08, 0x10, 21)
