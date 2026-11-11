"""지연된 새 probe가 원 ACK 시계를 악화시키거나 유효기간을 연장하지 않는지 검증한다."""
from collections import OrderedDict
from types import SimpleNamespace
from uuid import UUID

from xenova_gateway.feedback import FeedbackSerialSink
from xenova_gateway.feedback3 import Feedback3SerialSink, CLOCK_REPLY, CLOCK3


def rig(*, remaining=800000, width=2000, acknowledged=True):
    sink=object.__new__(Feedback3SerialSink)
    sink.pi_session='10000000-0000-4000-8000-000000000001'
    sink.device_session='20000000-0000-4000-8000-000000000002'
    sink.clock_epoch='30000000-0000-4000-8000-000000000003'
    sink.profile=SimpleNamespace(timestamp_error_us=0,drift_ppm=0,probe_timeout_us=200000,
        max_offset_width_us=100000,lease_us=1000000,j_cutoff_us=100)
    sink.odometry=SimpleNamespace(config=SimpleNamespace(maximum_clock_error_us=20000))
    sink.ready=True;sink.clock_sequence=1;sink.outstanding=(3,1100000)
    sink.clock = lambda: 1.12
    sink.previous_interval=(-1000,1000,1000000);sink.streak=2;sink.pending={}
    current=dict(sequence=1,probe=2,reference=1000000,lower=-width//2,upper=width//2,
        expires=1120000+remaining,cutoff=1120000+remaining-100,ack=acknowledged)
    sink.install=current;sink.leases=OrderedDict([(1,current)])
    sink.samples=[];sink.wires=[]
    sink._sample=lambda *args:sink.samples.append(args)
    sink._send=lambda raw:sink.wires.append(raw)
    raw=CLOCK3.pack(UUID(sink.pi_session).bytes,UUID(sink.device_session).bytes,
        3,1100000,1101000,1102000,1103000,20,0,0,1,0,0)
    return sink,current,raw


def test_slower_probe_keeps_original_ack_clock_and_expiry():
    sink,current,raw=rig()
    original=current.copy()
    sink._receive(CLOCK_REPLY,raw,1.12)
    assert sink.install is current and current==original
    assert sink.clock_sequence==1 and not sink.wires
    assert sink.samples[-1][-2] is current
    assert sink.previous_interval==(-1000,17000,1120000)


def test_better_probe_installs_its_exact_new_interval():
    sink,current,raw=rig(width=30000)
    sink._receive(CLOCK_REPLY,raw,1.12)
    assert sink.clock_sequence==2 and sink.install is not current
    assert (sink.install['lower'],sink.install['upper'])==(-1000,17000)
    assert sink.install['expires']==2100000
    assert not sink.install['ack'] and len(sink.wires)==1


def test_clock_near_expiry_renews_without_extending_old_lease():
    sink,current,raw=rig(remaining=399999)
    original_expiry=current['expires']
    sink._receive(CLOCK_REPLY,raw,1.12)
    assert sink.install is not current and sink.clock_sequence==2
    assert current['expires']==original_expiry


def test_near_expiry_keeps_precise_ack_when_replacement_cannot_map_sensor():
    sink, current, raw = rig(remaining=250000, width=20362)
    original = current.copy()
    # Request delay lowers the new bound. Even the earliest plausible
    # capture under the old lease leaves more than 40 ms of uncertainty.
    fields = list(CLOCK3.unpack(raw))
    fields[4:7] = (1165000, 1166000, 1167000)
    sink._receive(CLOCK_REPLY, CLOCK3.pack(*fields), 1.168)
    assert sink.install is current and current == original
    assert sink.clock_sequence == 1 and not sink.wires
    assert sink.samples[-1][-2] is current
    assert sink._lease(current['expires']) is None


def test_near_expiry_can_renew_when_only_the_reply_widens_the_upper_bound():
    sink, current, raw = rig(remaining=250000, width=20362)
    sink.clock = lambda: 1.165
    sink._receive(CLOCK_REPLY, raw, 1.165)
    assert sink.install is not current and sink.clock_sequence == 2
    assert sink.install['upper'] - sink.install['lower'] > 40000
    assert len(sink.wires) == 1


def test_install_waits_for_ack_before_retrying_original_bytes(monkeypatch):
    sink, _, raw = rig(width=30000)
    sink.started = 1.0
    sink.framer = SimpleNamespace(invalid=0, oversize=0)
    sink.last_probe_us = 1100000
    sink._sensor_available = lambda: True
    sink._view = lambda: {}
    monkeypatch.setattr(FeedbackSerialSink, '_poll', lambda *args, **kwargs: None)
    sink._receive(CLOCK_REPLY, raw, 1.12)
    original, = sink.wires
    expiry = sink.install['expires']

    for now in (1.12, 1.169999):
        sink.clock = lambda: now
        sink._poll(now)
        assert sink.wires == [original]

    sink.clock = lambda: 1.17
    sink._poll(1.17)
    assert sink.wires == [original, original]
    assert sink.install['expires'] == expiry


def test_unacknowledged_clock_cannot_be_reused():
    sink,current,raw=rig(acknowledged=False)
    sink._receive(CLOCK_REPLY,raw,1.12)
    assert sink.install is not current and sink.clock_sequence==2


def test_expired_clock_cannot_be_reused():
    sink,current,raw=rig(remaining=0)
    sink._receive(CLOCK_REPLY,raw,1.12)
    assert sink.install is not current and sink.clock_sequence==2


def test_reuse_cannot_hide_clock_discontinuity_against_original_ack():
    sink,current,raw=rig()
    current.update(lower=-50000,upper=-48000)
    faults=[];sink._fail=lambda reason:faults.append(reason)
    sink._receive(CLOCK_REPLY,raw,1.12)
    assert faults==['DEVICE_CLOCK_DISCONTINUITY']
    assert not sink.leases and not sink.samples and not sink.wires
