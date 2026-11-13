#!/usr/bin/env python3
"""개발용 RPi raw PTY↔단일 TCP 중계. Feedback3를 해석하거나 권한을 발급하지 않는다.

Gateway는 출력된 PTY slave를 기존 SERIAL_PORT로 연다. 한 번의 연결만 사용하며
EOF/지연/과부하는 세션을 닫는다. 원 packet·시각·순번·기한을 고치거나 재송신하지 않는다.
Linux/POSIX 전용이며 이미지 설치·서비스 변경은 이 도구의 책임이 아니다.
"""

import argparse
from collections import deque
import errno
import ipaddress
import json
import math
import os
import select
import socket
import time


QUEUE_BYTES = 8192
QUEUE_CHUNKS = 32
READ_BYTES = 4096


class TransportClosed(RuntimeError):
    """원 세션을 더 이어갈 수 없어 양 끝을 닫아야 한다."""


def seconds(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError('finite positive bounded transport time required')
    return float(value)


def loopback(value):
    address = ipaddress.ip_address(value)
    if not address.is_loopback or address.version != 4:
        raise ValueError('IPv4 loopback required; use an explicit SSH port forward')
    return str(address)


class PendingBytes:
    """8KiB/32 chunk 상한과 각 원 수신 시각을 유지하는 partial-write 큐다."""

    def __init__(self, maximum_age_s):
        self.maximum_age_s = seconds(maximum_age_s, .2)
        self.rows = deque()
        self.size = 0

    def append(self, raw, received):
        if type(raw) is not bytes or not raw:
            raise ValueError('nonempty original bytes required')
        if self.size + len(raw) > QUEUE_BYTES or len(self.rows) >= QUEUE_CHUNKS:
            raise TransportClosed('TRANSPORT_QUEUE_FULL')
        self.rows.append([raw, 0, received])
        self.size += len(raw)

    def check(self, now):
        if self.rows and not 0 <= now - self.rows[0][2] < self.maximum_age_s:
            raise TransportClosed('TRANSPORT_PENDING_EXPIRED_OR_CLOCK_REGRESSED')

    def flush(self, fd, now):
        self.check(now)
        if not self.rows:
            return 0
        raw, offset, _ = self.rows[0]
        try:
            sent = os.write(fd, memoryview(raw)[offset:])
        except BlockingIOError:
            return 0
        if sent <= 0:
            raise TransportClosed('TRANSPORT_WRITE_EOF')
        self.rows[0][1] += sent
        self.size -= sent
        if self.rows[0][1] == len(raw):
            self.rows.popleft()
        return sent


def relay(left_fd, right_fd, *, duration_s, pending_age_s, startup_timeout_s,
          first_left=None, clock=time.monotonic, cancelled=lambda: False):
    """caller 소유 nonblocking FD 두 개를 중계한다. 실패 뒤 재연결·queue 재사용은 없다.

    left는 PTY master다. 최초 slave 데이터가 오면 first_left로 보관 slave를 닫는다.
    보관 slave는 Gateway가 처음 열기 전 EIO만 막으며 startup_timeout 안에 폐기된다.
    반환은 유한 실행 종료이며 장치 APPLIED/실제 정지 수락을 뜻하지 않는다.
    """
    duration_s = seconds(duration_s, 3600)
    startup_timeout_s = seconds(startup_timeout_s, 120)
    to_right, to_left = PendingBytes(pending_age_s), PendingBytes(pending_age_s)
    start = clock()
    opened = False
    counts = {'pty_to_tcp_bytes': 0, 'tcp_to_pty_bytes': 0}
    while not cancelled():
        now = clock()
        if now < start:
            raise TransportClosed('TRANSPORT_CLOCK_REGRESSED')
        if now - start >= duration_s:
            return counts
        if not opened and now - start >= startup_timeout_s:
            raise TransportClosed('GATEWAY_PTY_STARTUP_TIMEOUT')
        to_right.check(now)
        to_left.check(now)
        ready, _, _ = select.select([left_fd, right_fd], [], [], .002)
        for fd, pending in ((left_fd, to_right), (right_fd, to_left)):
            if fd not in ready:
                continue
            try:
                raw = os.read(fd, READ_BYTES)
            except BlockingIOError:
                continue
            except OSError as error:
                if error.errno == errno.EIO:
                    raise TransportClosed('GATEWAY_PTY_CLOSED') from error
                raise
            if not raw:
                raise TransportClosed('TRANSPORT_READ_EOF')
            pending.append(raw, clock())
            if fd == left_fd and not opened:
                opened = True
                if first_left is not None:
                    first_left()
        counts['pty_to_tcp_bytes'] += to_right.flush(right_fd, clock())
        counts['tcp_to_pty_bytes'] += to_left.flush(left_fd, clock())
    return counts


def run_bridge(*, host, port, connect_timeout_s, duration_s, pending_age_s,
               startup_timeout_s, ready, cancelled=lambda: False):
    """TCP 연결 후 새 raw PTY 한 개를 만들고 모든 caller FD를 반드시 회수한다."""
    if os.name != 'posix':
        raise RuntimeError('Linux/POSIX PTY required; native Windows is unsupported')
    import tty
    host = loopback(host)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('explicit TCP port required')
    seconds(duration_s, 3600)
    seconds(pending_age_s, .2)
    seconds(startup_timeout_s, 120)
    connect_timeout_s = seconds(connect_timeout_s, 30)
    master = guard_slave = None
    connection = None
    try:
        connection = socket.create_connection((host, port), connect_timeout_s)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, QUEUE_BYTES)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, QUEUE_BYTES)
        connection.setblocking(False)
        master, guard_slave = os.openpty()
        tty.setraw(guard_slave)
        os.set_blocking(master, False)
        slave_path = os.ttyname(guard_slave)

        def release_guard():
            nonlocal guard_slave
            if guard_slave is not None:
                os.close(guard_slave)
                guard_slave = None

        ready({'kind': 'hil-pty-ready-1', 'serial_port': slave_path,
               'tcp_peer': [host, port], 'single_session': True,
               'physical_qualified': False})
        return relay(master, connection.fileno(), duration_s=duration_s,
                     pending_age_s=pending_age_s, startup_timeout_s=startup_timeout_s,
                     first_left=release_guard, cancelled=cancelled)
    finally:
        if connection is not None:
            connection.close()
        for fd in (guard_slave, master):
            if fd is not None:
                os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--connect-timeout-s', type=float, required=True)
    parser.add_argument('--startup-timeout-s', type=float, required=True)
    parser.add_argument('--duration-s', type=float, required=True)
    parser.add_argument('--pending-age-s', type=float, required=True)
    args = parser.parse_args(argv)
    import signal
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        counts = run_bridge(**vars(args), ready=lambda row: print(json.dumps(row), flush=True))
        print(json.dumps({'kind': 'hil-pty-closed-1', 'counts': counts}), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'kind': 'hil-pty-failed-1', 'error': str(error)[:200]}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
