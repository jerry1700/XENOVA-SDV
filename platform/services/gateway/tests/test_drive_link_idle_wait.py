"""Drive link I/O owner wait: with nothing queued the select sleeps until the MQTT
socket, an offer() or close() wakes it; with replies queued it keeps BUSY_WAIT_S.

Uses the real Paho client against a minimal local MQTT 3.1.1 peer, no broker
binary. The owner's wake pair is separate from Paho's own wake sockets.
"""
import select as select_module
import socket
import threading
import time
from uuid import uuid4

import paho.mqtt.client as mqtt

from xenova_gateway import drive_link
from xenova_gateway.drive_link import BUSY_WAIT_S, IDLE_WAIT_S, DriveLinkIO, inbound

A, B = str(uuid4()), str(uuid4())


class ExternalClient:
    def __init__(self, sock):
        self.sock = sock
        self.calls = []

    def socket(self):
        return self.sock

    def want_write(self):
        return False

    def loop_read(self):
        self.calls.append('read')
        return mqtt.MQTT_ERR_SUCCESS

    def loop_write(self):
        self.calls.append('write')
        return mqtt.MQTT_ERR_SUCCESS

    def loop_misc(self):
        self.calls.append('misc')
        return mqtt.MQTT_ERR_SUCCESS


class Peer:
    """One MQTT 3.1.1 connection: CONNACK, SUBACK, PINGRESP; records PUBLISH bodies."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.server.settimeout(3)
        self.port = self.server.getsockname()[1]
        self.subscribed = 0
        self.published = []
        self.errors = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _read(stream, count):
        data = b''
        while len(data) < count:
            part = stream.recv(count - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    def _packet(self, stream):
        header = self._read(stream, 1)[0]
        length, factor = 0, 1
        while True:
            byte = self._read(stream, 1)[0]
            length += (byte & 127) * factor
            factor *= 128
            if not byte & 128:
                break
        return header, self._read(stream, length)

    def _serve(self):
        try:
            with self.server.accept()[0] as stream:
                stream.settimeout(5)
                self._packet(stream)
                stream.sendall(b'\x20\x02\x00\x00')
                while True:
                    try:
                        header, body = self._packet(stream)
                    except (EOFError, OSError):
                        return
                    kind = header >> 4
                    if kind == 8:  # SUBSCRIBE: one topic per call
                        stream.sendall(b'\x90\x03' + body[:2] + b'\x00')
                        self.subscribed += 1
                    elif kind == 3:  # PUBLISH QoS0: topic length + topic + payload
                        size = int.from_bytes(body[:2], 'big')
                        self.published.append((time.monotonic(), body[2:2 + size].decode(), body[2 + size:]))
                    elif kind == 12:
                        stream.sendall(b'\xd0\x00')
        except BaseException as error:  # noqa: BLE001 - reported by the test
            self.errors.append(error)

    def close(self):
        self.server.close()
        self.thread.join(5)


def until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def test_wait_bound_follows_queued_replies():
    io = DriveLinkIO('127.0.0.1', 1, None, None, (A, B))
    assert io._select_wait() == IDLE_WAIT_S
    io.offer(inbound(A), b'state', 'state')
    assert io._select_wait() == BUSY_WAIT_S


def test_offer_wakes_an_idle_select_without_reading_the_mqtt_socket():
    io = DriveLinkIO('127.0.0.1', 1, None, None, (A, B))
    wake = socket.socketpair()
    for end in wake:
        end.setblocking(False)
    io._wake_read, io._wake_write = wake
    mqtt_end, peer_end = socket.socketpair()
    try:
        client = ExternalClient(mqtt_end)
        timer = threading.Timer(.05, lambda: io.offer(inbound(A), b'state', 'state'))
        began = time.monotonic()
        timer.start()
        assert io._network_once(client, mqtt, IDLE_WAIT_S) == mqtt.MQTT_ERR_SUCCESS
        assert time.monotonic() - began < IDLE_WAIT_S / 2
        assert client.calls == ['misc']
        assert not select_module.select([wake[0]], [], [], 0)[0]  # the wake bytes were drained
    finally:
        timer.join()
        io._close_wake()
        mqtt_end.close()
        peer_end.close()


def test_connected_idle_owner_sleeps_and_an_offer_is_sent_at_once(monkeypatch):
    peer = Peer()
    waits = []
    real_select = select_module.select

    def counted(read, write, error, timeout):
        waits.append(timeout)
        return real_select(read, write, error, timeout)

    io = DriveLinkIO('127.0.0.1', peer.port, None, None, (A, B))
    try:
        io.start()
    except BaseException:
        peer.close()
        raise
    try:
        assert until(lambda: io.connected and peer.subscribed == 2)
        monkeypatch.setattr(drive_link.select, 'select', counted)
        time.sleep(1.0)
        idle = len(waits)
        # At most one select per IDLE_WAIT_S while idle (and a few around the
        # patch boundary); the earlier 2 ms poll made about 500 per second.
        assert idle <= 1.0 / IDLE_WAIT_S + 3
        assert all(wait == IDLE_WAIT_S for wait in waits)
        offered = time.monotonic()
        io.offer(inbound(A), b'state-bytes', 'state')
        assert until(lambda: peer.published)
        sent_at, topic, payload = peer.published[0]
        assert (topic, payload) == (inbound(A), b'state-bytes')
        assert sent_at - offered < IDLE_WAIT_S / 2
        assert io.sent == 1 and io.failure is None
    finally:
        began = time.monotonic()
        io.close()
        assert time.monotonic() - began < .5 and not io.thread.is_alive()
        peer.close()
    assert io._wake_read is None and io._wake_write is None
    io.offer(inbound(B), b'late', 'state')  # a stopped owner needs no wakeup
    assert not peer.errors
