"""HELLO_ACK를 받기 전까지 같은 Pi session의 HELLO를 다시 보내고, handshake 기한과 고장 유지는 그대로인지 확인한다."""
from collections import OrderedDict
from types import SimpleNamespace
from uuid import UUID

from xenova_gateway.feedback import Framer, HELLO, HELLO_ACK, packet
from xenova_gateway.feedback3 import ACK3, CLOCK_PROBE, HELLO3, HELLO_RETRY_US, REQUIRED_CAPS, Feedback3SerialSink

PI = '10000000-0000-4000-8000-000000000001'
DEVICE = '20000000-0000-4000-8000-000000000002'
START = 1.


def rig():
    sink = object.__new__(Feedback3SerialSink)
    sink.pi_session = PI
    sink.device_session = None
    sink.capabilities = 0
    sink.profile = SimpleNamespace(vehicle_config_sha256='a'*64, firmware_sha256='b'*64, probe_timeout_us=200000)
    sink.odometry = SimpleNamespace(config=SimpleNamespace(encoder_bits=32), device_session=lambda session: None,
        probe=lambda *args: None)
    sink.started = None
    sink.ready = False
    sink.fault = None
    sink._sensor_blocked_reason = None
    sink.timeout = .5
    sink.hello_sent_us = 0
    sink.outstanding = None
    sink.last_probe_us = 0
    sink.probe_sequence = 0
    sink.install = None
    sink.pending = OrderedDict()
    sink.framer = Framer()
    sink.received = sink.rejected = 0
    sink.tof_inbox = SimpleNamespace(reject=lambda **kwargs: None)
    sink._view = lambda: {}
    now = [START]
    sink.clock = lambda: now[0]
    incoming = []
    sink._serial = SimpleNamespace(read=lambda size: incoming.pop(0) if incoming else b'')
    sent = []
    decoder = Framer()
    sink._send = lambda raw: sent.extend(decoder.feed(raw))

    def poll(after_ms):
        now[0] = START+after_ms/1000
        sink._poll(now[0])

    return sink, sent, incoming, poll


def hello_ack():
    return packet(HELLO_ACK, ACK3.pack(3, UUID(PI).bytes, UUID(DEVICE).bytes, REQUIRED_CAPS,
        bytes.fromhex('a'*64), bytes.fromhex('b'*64), 32))


def test_lost_hello_is_sent_again_with_the_same_session_until_the_ack():
    sink, sent, incoming, poll = rig()
    poll(0)
    assert [kind for kind, _ in sent] == [HELLO]
    poll(HELLO_RETRY_US/1000-1)
    assert len(sent) == 1
    poll(HELLO_RETRY_US/1000+1)
    assert [kind for kind, _ in sent] == [HELLO, HELLO]
    assert sent[0] == sent[1] and HELLO3.unpack(sent[1][1])[1] == UUID(PI).bytes

    incoming.append(hello_ack())
    poll(60)
    assert sink.ready and sink.fault is None and sink.device_session == DEVICE
    for after_ms in (120, 180, 400, 700):
        poll(after_ms)
    assert [kind for kind, _ in sent].count(HELLO) == 2
    assert CLOCK_PROBE in [kind for kind, _ in sent]


def test_retries_stop_at_the_original_handshake_deadline_and_the_fault_stays():
    sink, sent, incoming, poll = rig()
    for step in range(10):
        poll(step*51)
    assert not sink.ready and sink.fault is None
    assert [kind for kind, _ in sent] == [HELLO]*10

    poll(510)
    assert sink.fault == 'DEVICE_HANDSHAKE_TIMEOUT'
    for after_ms in (570, 640, 900):
        poll(after_ms)
    assert len(sent) == 10

    incoming.append(hello_ack())
    poll(950)
    assert sink.ready and sink.fault == 'DEVICE_HANDSHAKE_TIMEOUT' and not sink._sensor_available()
    poll(1200)
    assert [kind for kind, _ in sent] == [HELLO]*10
