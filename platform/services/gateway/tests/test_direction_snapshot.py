"""ClockReply 방향 증거는 함께 선택한 원 Vehicle의 snapshot을 사용한다."""
from types import SimpleNamespace
from uuid import uuid4

from xenova_gateway.feedback3 import Feedback3SerialSink
from test_feedback3_adapter import source
from test_verified import A


def test_direction_witness_does_not_read_newer_serial_snapshot_or_sessions():
    def snapshot(sequence):
        sample=dict(sequence=sequence,pi_session=str(uuid4()),device_session=str(uuid4()))
        pin=dict(witness=source(),phase_end=2000000,frame=SimpleNamespace(generation=7),
                 applied=False,sequence=11)
        return dict(clock_ready=True,moving=False,pin=pin,sample=sample,matched=None,gate_revision=3)
    original=snapshot(12);newer=snapshot(13)
    sink=object.__new__(Feedback3SerialSink)
    sink._direction_snapshot=newer
    sink.pi_session=newer['sample']['pi_session'];sink.device_session=newer['sample']['device_session']
    witness=sink.direction_witness(A,'autonomous',SimpleNamespace(generation=7),1000000,
        view=dict(stop_facts_source=original))
    assert witness.vehicle_sequence==12
    assert witness.pi_session==original['sample']['pi_session']
    assert witness.device_session==original['sample']['device_session']
    assert witness.gate.state==1  # 미적용 STOP을 준비 완료로 올리지 않는다.


def test_missing_original_direction_snapshot_does_not_use_newer_serial_snapshot():
    sink=object.__new__(Feedback3SerialSink)
    sink._direction_snapshot={'unexpected':'newer snapshot must not be read'}
    assert sink.direction_witness(A,'manual',SimpleNamespace(generation=7),1000000,
        view=dict(stop_facts_source=None)) is None
