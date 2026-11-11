"""개발 transport의 원 바이트/partial write/EOF/유한 대기 회귀. 제품 주행 수락이 아니다.

Linux에서 XENOVA_HIL_SERIAL_BRIDGE를 검토된 Pi 도구의 절대경로로 지정한다.
이 파일의 import 대상은 stdlib 도구뿐이며 실제 C/Gateway/가상 ESP는 실행하지 않는다.
"""

from contextlib import closing
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import queue
import select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


def load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = load_tool('hil_feedback_peer_server', Path(__file__).with_name('feedback_peer_server.py'))


def receive_exact(fd, size, timeout=1.):
    raw = bytearray()
    deadline = time.monotonic() + timeout
    while len(raw) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise AssertionError('bounded byte receipt timed out')
        part = os.read(fd, size - len(raw))
        if not part:
            raise AssertionError('unexpected EOF')
        raw.extend(part)
    return bytes(raw)


@unittest.skipUnless(os.name == 'posix', 'POSIX PTY/FD required; Windows unsupported')
class HilTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('XENOVA_HIL_SERIAL_BRIDGE')
        if not path or not Path(path).is_absolute():
            raise RuntimeError('explicit XENOVA_HIL_SERIAL_BRIDGE absolute source path required')
        cls.bridge = load_tool('hil_serial_bridge', path)

    def test_partial_write_preserves_original_order_and_oldest_receive_time(self):
        pending = self.bridge.PendingBytes(.1)
        pending.append(b'\x00\x11\x13\xff', 10.)
        pending.append(b'next\x00', 10.02)
        observed = []
        def partial(_, raw):
            observed.append(bytes(raw[:1]))
            return 1
        with patch.object(self.bridge.os, 'write', partial):
            for _ in range(9):
                pending.flush(7, 10.03)
        self.assertEqual(b''.join(observed), b'\x00\x11\x13\xffnext\x00')
        self.assertEqual(pending.size, 0)
        self.assertFalse(pending.rows)

    def test_partial_or_new_input_does_not_renew_old_pending_age(self):
        pending = self.bridge.PendingBytes(.05)
        pending.append(b'old', 10.)
        with patch.object(self.bridge.os, 'write', return_value=1):
            pending.flush(7, 10.01)
        pending.append(b'new', 10.04)
        with self.assertRaisesRegex(self.bridge.TransportClosed, 'EXPIRED'):
            pending.check(10.051)
        with self.assertRaisesRegex(self.bridge.TransportClosed, 'CLOCK_REGRESSED'):
            pending.check(9.99)

    def test_queue_capacity_is_fail_closed_without_discarding_old_bytes(self):
        pending = self.bridge.PendingBytes(.1)
        pending.append(b'x' * self.bridge.QUEUE_BYTES, 10.)
        with self.assertRaisesRegex(self.bridge.TransportClosed, 'QUEUE_FULL'):
            pending.append(b'y', 10.)
        self.assertEqual(pending.size, self.bridge.QUEUE_BYTES)
        self.assertEqual(pending.rows[0][0], b'x' * self.bridge.QUEUE_BYTES)

    def begin_bridge(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(1.)
        ready, outcome = queue.Queue(1), queue.Queue(1)
        cancelled = threading.Event()
        def run():
            try:
                result = self.bridge.run_bridge(host='127.0.0.1', port=listener.getsockname()[1],
                    connect_timeout_s=1., startup_timeout_s=1., duration_s=2., pending_age_s=.1,
                    ready=ready.put_nowait, cancelled=cancelled.is_set)
                outcome.put_nowait(result)
            except BaseException as error:
                outcome.put_nowait(error)
        thread = threading.Thread(target=run, daemon=False)
        thread.start()
        connection, _ = listener.accept()
        listener.close()
        connection.settimeout(1.)
        row = ready.get(timeout=1.)
        slave = os.open(row['serial_port'], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        slave_owner = [slave]
        def close_slave():
            if slave_owner[0] is not None:
                os.close(slave_owner[0])
                slave_owner[0] = None
        def cleanup():
            cancelled.set()
            connection.close()
            thread.join(1.)
            self.assertFalse(thread.is_alive(), 'bounded bridge child did not exit')
            close_slave()
        self.addCleanup(cleanup)
        return connection, slave, outcome, thread, close_slave

    def test_real_raw_pty_tcp_duplex_preserves_binary_and_slave_close_ends_session(self):
        connection, slave, outcome, thread, close_slave = self.begin_bridge()
        original = bytes(range(256)) * 2
        os.write(slave, original)
        self.assertEqual(receive_exact(connection.fileno(), len(original)), original)
        reverse = original[::-1]
        connection.sendall(reverse[:3])
        connection.sendall(reverse[3:])
        self.assertEqual(receive_exact(slave, len(reverse)), reverse)
        close_slave()
        result = outcome.get(timeout=1.)
        self.assertIsInstance(result, self.bridge.TransportClosed)
        thread.join(1.)
        self.assertFalse(thread.is_alive())
        self.assertEqual(connection.recv(1), b'')

    def test_tcp_eof_closes_pty_without_reconnect_or_delayed_replay(self):
        connection, slave, outcome, thread, _ = self.begin_bridge()
        os.write(slave, b'first\x00')
        self.assertEqual(receive_exact(connection.fileno(), 6), b'first\x00')
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        self.assertIsInstance(outcome.get(timeout=1.), self.bridge.TransportClosed)
        thread.join(1.)
        self.assertFalse(thread.is_alive())
        self.assertTrue(select.select([slave], [], [], .5)[0])
        self.assertEqual(os.read(slave, 16), b'')

    def test_tcp_eof_before_gateway_first_write_closes_guard_and_both_endpoints(self):
        # 최초 slave 사용 전의 보관 FD도 상대 EOF를 숨겨서는 안 된다.
        connection, slave, outcome, thread, _ = self.begin_bridge()
        connection.shutdown(socket.SHUT_RDWR)
        self.assertIsInstance(outcome.get(timeout=1.), self.bridge.TransportClosed)
        thread.join(1.)
        self.assertFalse(thread.is_alive())
        self.assertEqual(connection.recv(1), b'')

    def test_peer_listener_returns_one_nonblocking_stream_and_closes_listener(self):
        with closing(socket.socket()) as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        ready, accepted = queue.Queue(1), queue.Queue(1)
        def run():
            try:
                accepted.put_nowait(SERVER.accept_once(host='127.0.0.1', port=port,
                    timeout_s=1., ready=ready.put_nowait))
            except BaseException as error:
                accepted.put_nowait(error)
        thread = threading.Thread(target=run, daemon=False)
        thread.start()
        try:
            ready.get(timeout=1.)
            with socket.create_connection(('127.0.0.1', port), 1.) as client:
                connection = accepted.get(timeout=1.)
                self.assertIsInstance(connection, socket.socket)
                with connection:
                    self.assertFalse(os.get_blocking(connection.fileno()))
                    client.sendall(b'\x00same original bytes\xff')
                    self.assertEqual(receive_exact(connection.fileno(), 21), b'\x00same original bytes\xff')
                    with self.assertRaises(OSError):
                        socket.create_connection(('127.0.0.1', port), .1)
        finally:
            thread.join(1.)
            self.assertFalse(thread.is_alive())

    def test_profile_exact_file_hash_caps_and_duplicate_json_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'profile.json'
            original = b'{"one":1}\n'
            path.write_bytes(original)
            raw, value = SERVER.read_profile(path, sha256(original).hexdigest(), 64)
            self.assertEqual((raw, value), (original, {'one': 1}))
            with self.assertRaises(ValueError):
                SERVER.read_profile(path, '0' * 64, 64)
            with self.assertRaises(ValueError):
                SERVER.read_profile(path, sha256(original).hexdigest(), 4)
            duplicate = b'{"one":1,"one":2}'
            path.write_bytes(duplicate)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                SERVER.read_profile(path, sha256(duplicate).hexdigest(), 64)


if __name__ == '__main__':
    unittest.main()
