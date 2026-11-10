"""같은 Linux uid의 명시 운영 CLI와 S-main을 잇는 유한 관리 transport다.

새 thread·ResourceStore lock·파일 socket을 만들지 않는다. 현재 protected manifest와
실제 peer birth를 검산하며 callback의 원 결과만 운반한다. 설정 적용/READY/추진 권위는
S/M actor 책임이다. import/constructor는 socket이나 파일을 열지 않는다.
"""

from collections import Counter
from hashlib import sha256
import errno
import json
import math
import os
import selectors
import socket
import stat
import struct
import sys
import threading
import time
from uuid import UUID, uuid4

from .procfs_formats import parse_boot_id, parse_proc_stat
from .resource_manifest import ResourceManifest


PACKET_CAP = 4096
TRANSPORT_US = 5_000_000
U64_MAX = 2**64 - 1
HELLO = 'jetson-operation-management-1'
CLIENT = 'jetson-operation-client-1'
RESPONSE = 'jetson-operation-response-1'
KINDS = frozenset(('STATUS', 'QUERY', 'REQUEST'))


class OperationManagementError(RuntimeError):
    """관리 통로/원 형식/보호 서비스 신원을 검산하지 못했다. 운영 결과가 아니다."""


def _j_us():
    return time.monotonic_ns() // 1000


def _uint(value):
    if type(value) is not int or not 0 <= value <= U64_MAX:
        raise OperationManagementError('actual bounded monotonic J time required')
    return value


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        raise OperationManagementError('canonical nonzero management UUID required')
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise OperationManagementError('invalid management UUID') from error
    if str(parsed) != value or not parsed.int:
        raise OperationManagementError('canonical nonzero management UUID required')
    return value


def _keys(value, names):
    if (type(value) is not dict or len(value) != len(names)
            or any(type(key) is not str for key in value) or set(value) != set(names)):
        raise OperationManagementError('unknown or missing management fields')


def _tree(value):
    remaining = PACKET_CAP
    def visit(item, depth):
        nonlocal remaining
        if remaining < 0 or depth > 6:
            raise OperationManagementError('management JSON work bound exceeded')
        kind = type(item)
        if kind is dict or kind is list:
            if len(item) > PACKET_CAP:
                raise OperationManagementError('management container exceeds packet bound')
            remaining -= 2 + max(0, len(item)-1)
            if kind is dict:
                remaining -= len(item)
                for key, child in item.items():
                    if type(key) is not str:
                        raise OperationManagementError('management keys must be strings')
                    visit(key, depth+1); visit(child, depth+1)
            else:
                for child in item: visit(child, depth+1)
        elif kind is str:
            if len(item) > PACKET_CAP or any(0xd800 <= ord(c) <= 0xdfff for c in item):
                raise OperationManagementError('invalid management string')
            remaining -= len(json.dumps(item, ensure_ascii=True))
        elif item is None or kind is bool:
            remaining -= 5 if item is False else 4
        elif kind is int:
            if item.bit_length() > 64: raise OperationManagementError('management integer exceeds uint64 width')
            remaining -= len(str(item))
        elif kind is float and math.isfinite(item):
            remaining -= len(json.dumps(item, allow_nan=False))
        else:
            raise OperationManagementError('management accepts finite native JSON only')
        if remaining < 0: raise OperationManagementError('management packet exceeds4096 bytes')
    visit(value, 0)


def _encode(value):
    _tree(value)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    if not 0 < len(raw) <= PACKET_CAP:
        raise OperationManagementError('management packet exceeds4096 bytes')
    return raw


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise OperationManagementError('duplicate management key')
        result[key] = value
    return result


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= PACKET_CAP:
        raise OperationManagementError('bounded original management packet required')
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs,
            parse_int=lambda token: int(token) if len(token) <= 21 else (_ for _ in ()).throw(ValueError('integer length')),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError('nonfinite constant')))
        _tree(value)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise OperationManagementError('invalid finite ASCII management JSON') from error
    return value


def _read_path(path, cap):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        raw = bytearray()
        while len(raw) <= cap:
            part = os.read(fd, cap+1-len(raw))
            if not part: break
            raw.extend(part)
        if not raw or len(raw) > cap: raise OperationManagementError('kernel identity read exceeded bound')
        return bytes(raw)
    finally:
        os.close(fd)


def _identity(value):
    return value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode)


class _Directory:
    def __init__(self, path):
        self.fd = None
        original = os.path.abspath(os.path.expanduser(os.fspath(path)))
        before = os.stat(original, follow_symlinks=False)
        self.path = os.path.realpath(original)
        self.uid = os.geteuid()
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.original = os.fstat(self.fd)
            if _identity(before) != _identity(self.original) or not stat.S_ISDIR(before.st_mode):
                raise OperationManagementError('protected directory alias or type changed')
            self.check()
        except BaseException:
            self.close(); raise

    def check(self):
        actual = os.fstat(self.fd)
        named = os.stat(self.path, follow_symlinks=False)
        if (not stat.S_ISDIR(actual.st_mode) or not stat.S_ISDIR(named.st_mode)
                or actual.st_uid != self.uid or actual.st_mode & 0o022
                or _identity(actual) != _identity(self.original) or _identity(actual) != _identity(named)):
            raise OperationManagementError('protected directory owner, mode or identity changed')

    @property
    def address(self):
        key = sha256(os.fsencode(self.path)).hexdigest()
        return b'\0xenova-operation.' + str(self.uid).encode('ascii') + b'.' + key.encode('ascii')

    def service(self):
        """기존8KiB 기록을 lock 없이 읽는다. 전체 backing/운영 admission을 재증명하지 않는다."""
        self.check()
        fd = os.open('resource-set.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != self.uid
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or not 0 < before.st_size <= 8192):
                raise OperationManagementError('unsafe current service manifest')
            raw = bytearray()
            while len(raw) <= 8192:
                part = os.read(fd, 8193-len(raw))
                if not part: break
                raw.extend(part)
            fingerprint = lambda value: (_identity(value), value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)
            if (fingerprint(before) != fingerprint(os.fstat(fd))
                    or fingerprint(before) != fingerprint(os.stat('resource-set.json', dir_fd=self.fd, follow_symlinks=False))):
                raise OperationManagementError('current service manifest changed during read')
            manifest = ResourceManifest(bytes(raw))
        finally:
            os.close(fd)
        self.check()
        owner = next(item for item in manifest.owners if item.role == 'S' and item.phase == 'CURRENT')
        actual = parse_proc_stat(_read_path('/proc/'+str(owner.pid)+'/stat', 4096), owner.pid)
        if (actual.start_ticks != owner.start_ticks or actual.state in ('Z', 'X')
                or manifest.runtime.boot_id != parse_boot_id(_read_path('/proc/sys/kernel/random/boot_id', 64))
                or manifest.runtime.pid_ns_inode != os.stat('/proc/self/ns/pid').st_ino):
            raise OperationManagementError('current supervisor birth, boot or PID namespace differs')
        return manifest.service_instance, owner.pid, owner.start_ticks

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


def _linux():
    if (sys.platform != 'linux' or not hasattr(socket, 'SOCK_SEQPACKET')
            or not hasattr(socket, 'SO_PEERCRED')):
        raise OperationManagementError('operation management requires Linux AF_UNIX seqpacket/peercred')


def _peer(endpoint, uid):
    pid, actual_uid, _ = struct.unpack('=iII', endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid <= 0 or actual_uid != uid:
        raise OperationManagementError('management peer uid differs from protected state owner')
    return pid


def _receive(endpoint):
    raw, _, flags, _ = endpoint.recvmsg(PACKET_CAP+1)
    if not raw: raise EOFError('management peer closed')
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(raw) > PACKET_CAP:
        raise OperationManagementError('oversized or truncated management record')
    return _decode(raw)


class OperationServer:
    """S-main listener1/client1이다. 네트워크는 비차단이고 poll당 accept/RX/TX 각1회다.

    연결된 요청의 원 service 검사는 유한 metadata read다. callback 대기/fsync는 하지 않는다.
    """

    def __init__(self, state_directory, service_instance, *, status, submit, query, clock):
        _uuid(service_instance)
        if not all(callable(value) for value in (status, submit, query, clock)):
            raise ValueError('operation owner callbacks and actual J clock required')
        self._path, self.service_instance = state_directory, service_instance
        self._status, self._submit, self._query, self._clock = status, submit, query, clock
        self._directory = self._listener = self._client = None
        self._owner = self._address = self._tx = self._waiting = None
        self._service_identity = None
        self._phase = None
        self._deadline = 0
        self._started = self._closed = False
        self.last_error = None
        self.counts = Counter()

    @property
    def address(self): return self._address

    @property
    def closed(self): return self._closed

    @property
    def client_active(self): return self._client is not None

    def _owned(self):
        if self._owner is not None and self._owner != (os.getpid(), threading.get_ident()):
            raise RuntimeError('operation server belongs to original S-main owner')

    def start(self):
        self._owned()
        if self._started or self._closed: raise OperationManagementError('operation server is single-start')
        self._owner = os.getpid(), threading.get_ident()
        self._started = True
        try:
            _linux()
            self._directory = _Directory(self._path)
            self._service_identity = self._directory.service()
            service, pid, _ = self._service_identity
            if (service, pid) != (self.service_instance, os.getpid()):
                raise OperationManagementError('protected CURRENT S differs from server owner')
            self._address = self._directory.address
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self._listener.setblocking(False)
            self._listener.set_inheritable(False)
            self._listener.bind(self._address)
            self._listener.listen(1)
            return self
        except BaseException:
            self.close(); raise

    def _drop_client(self):
        endpoint, self._client = self._client, None
        self._tx = self._waiting = self._phase = None
        if endpoint is not None:
            endpoint.close(); self.counts['closed_clients'] += 1

    def _current_service(self):
        if self._directory.service() != self._service_identity:
            raise OperationManagementError('original supervisor service or birth changed')

    def _current_client(self):
        self._current_service()
        if _uint(self._clock()) >= self._deadline:
            self.counts['expired_clients'] += 1
            self._drop_client()
            return False
        return True

    def _result(self, result):
        if type(result) is not dict: raise OperationManagementError('operation callback must return an original result dict')
        self._tx = _encode(dict(profile=RESPONSE, service_instance=self.service_instance, kind=self._phase, result=result))

    def poll(self):
        """반환1은 관리 응답 송신 완료이며 운영 설정 적용/READY가 아니다."""
        self._owned()
        if not self._started or self._closed: return 0
        now = _uint(self._clock())
        try:
            if self._client is None:
                try: endpoint, _ = self._listener.accept()
                except BlockingIOError: return 0
                self._client = endpoint
                endpoint.setblocking(False); endpoint.set_inheritable(False)
                _peer(endpoint, self._directory.uid)
                self._current_service()
                if now > U64_MAX-TRANSPORT_US: raise OperationManagementError('management deadline overflow')
                self._deadline, self._phase = now+TRANSPORT_US, 'HELLO'
                status = self._status()
                if type(status) is not dict: raise OperationManagementError('operation status must be a dict')
                self._tx = _encode(dict(profile=HELLO, service_instance=self.service_instance, status=status))
                self.counts['accepted_clients'] += 1
            if now >= self._deadline:
                self.counts['expired_clients'] += 1; self._drop_client(); return 0
            if self._tx is not None:
                if not self._current_client(): return 0
                try: sent = self._client.send(self._tx)
                except BlockingIOError: return 0
                if sent != len(self._tx): raise OperationManagementError('management seqpacket send was partial')
                self._tx = None
                if self._phase != 'HELLO':
                    self.counts['responses_sent'] += 1; self._drop_client(); return 1
                self._phase = 'READ'
            if self._waiting is not None:
                try:
                    queued = self._client.recv(1, socket.MSG_PEEK)
                except BlockingIOError:
                    queued = None
                if queued is not None:
                    if queued: raise OperationManagementError('one management command per connection required')
                    self._drop_client(); return 0
                if not self._current_client(): return 0
                result = self._query(self._waiting)
                if result is not None: self._result(result)
                return 0
            try: value = _receive(self._client)
            except BlockingIOError: return 0
            _keys(value, ('profile', 'kind') if value.get('kind') == 'STATUS' else
                  ('profile', 'kind', 'request_id') if value.get('kind') == 'QUERY' else ('profile', 'kind', 'request'))
            if value['profile'] != CLIENT or value['kind'] not in KINDS:
                raise OperationManagementError('unsupported operation client profile or kind')
            self._phase = value['kind']
            self.counts['requests_received'] += 1
            self._current_service()
            if self._phase == 'STATUS':
                result = self._status()
                if type(result) is not dict: raise OperationManagementError('operation status must be a dict')
            elif self._phase == 'QUERY':
                self._waiting = _uuid(value['request_id']); result = self._query(self._waiting)
            else:
                from .operation_request import OperationRequest
                if type(value['request']) is not str: raise OperationManagementError('original request ASCII text required')
                request = OperationRequest(value['request'].encode('ascii'))
                now = _uint(self._clock())
                if request.service_instance != self.service_instance or not request.issued_us <= now < request.expires_us:
                    raise OperationManagementError('original request service or lifetime differs')
                self._deadline = min(self._deadline, request.expires_us)
                self._waiting = request.request_id
                result = self._submit(request.raw_bytes)
            if result is not None: self._result(result)
            return 0
        except Exception as error:
            self.last_error = type(error).__name__ + ': ' + str(error)[:256]
            self.counts['rejected_clients'] += 1
            self._drop_client()
            return 0

    def close(self):
        self._owned()
        self._closed = True
        try:
            self._drop_client()
        finally:
            try:
                if self._listener is not None:
                    listener, self._listener = self._listener, None
                    listener.close()
            finally:
                if self._directory is not None:
                    directory, self._directory = self._directory, None
                    directory.close()


def operation_command(state_directory, action, *, request_id=None, timeout_s=2.0):
    """같은 host의 원 service를 확인하고 원 result dict만 반환한다. timeout은 같은 ID로 조회한다."""
    _linux()
    if type(action) is not str or action not in ('status', 'run', 'standby', 'query'):
        raise ValueError('operation action must be status, run, standby or query')
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= TRANSPORT_US/1_000_000):
        raise ValueError('operation timeout must be finite and within five seconds')
    budget = int(timeout_s*1_000_000)
    if not budget: raise ValueError('operation timeout is below one microsecond')
    if action == 'query' and request_id is None: raise ValueError('query requires the original request ID')
    if request_id is not None: _uuid(request_id)
    if action in ('run', 'standby') and request_id is None: request_id = str(uuid4())
    began = _j_us(); deadline = _uint(began+budget)
    directory = endpoint = selector = None

    def timeout():
        detail = 'operation timed out'
        if request_id is not None: detail += '; query original request_id=' + request_id
        error = TimeoutError(detail)
        error.request_id = request_id
        return error

    def wait(event):
        now = _j_us()
        if now >= deadline: raise timeout()
        selector.modify(endpoint, event)
        if not selector.select((deadline-now)/1_000_000): raise timeout()

    def receive():
        while True:
            if _j_us() >= deadline: raise timeout()
            try: return _receive(endpoint)
            except BlockingIOError: wait(selectors.EVENT_READ)
            except EOFError: raise timeout()

    try:
        directory = _Directory(state_directory)
        identity = directory.service()
        service, pid, _ = identity
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        endpoint.setblocking(False); endpoint.set_inheritable(False)
        selector = selectors.DefaultSelector()
        selector.register(endpoint, selectors.EVENT_WRITE)
        result = endpoint.connect_ex(directory.address)
        if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            raise OperationManagementError('current operation endpoint is unavailable')
        if result:
            wait(selectors.EVENT_WRITE)
            if endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                raise OperationManagementError('operation endpoint connection failed')
        if _peer(endpoint, directory.uid) != pid:
            raise OperationManagementError('connected server is not the protected CURRENT S')
        hello = receive()
        _keys(hello, ('profile', 'service_instance', 'status'))
        if hello['profile'] != HELLO or hello['service_instance'] != service or type(hello['status']) is not dict:
            raise OperationManagementError('management hello differs from current service')
        if directory.service() != identity:
            raise OperationManagementError('current service changed during handshake')
        kind = 'STATUS' if action == 'status' else 'QUERY' if action == 'query' else 'REQUEST'
        value = dict(profile=CLIENT, kind=kind)
        if kind == 'QUERY': value['request_id'] = request_id
        elif kind == 'REQUEST':
            from .operation_request import OperationRequest
            issued = _j_us()
            if issued >= deadline: raise timeout()
            raw = _encode(dict(profile='jetson-operation-request-1', service_instance=service,
                request_id=request_id, action=action.upper(), issued_j_us=str(issued), expires_j_us=str(deadline)))
            request = OperationRequest(raw)
            value['request'] = request.raw_bytes.decode('ascii')
        raw = _encode(value)
        while True:
            if _j_us() >= deadline: raise timeout()
            try:
                if endpoint.send(raw) != len(raw): raise OperationManagementError('client seqpacket send was partial')
                break
            except BlockingIOError: wait(selectors.EVENT_WRITE)
        response = receive()
        _keys(response, ('profile', 'service_instance', 'kind', 'result'))
        if (response['profile'] != RESPONSE or response['service_instance'] != service
                or response['kind'] != kind or type(response['result']) is not dict):
            raise OperationManagementError('management response differs from original current service/kind')
        if directory.service() != identity:
            raise OperationManagementError('current service changed before final response acceptance')
        if _j_us() >= deadline: raise timeout()
        return response['result']
    except TimeoutError:
        raise
    except (OSError, ValueError) as error:
        raise OperationManagementError('current operation endpoint identity unavailable ('+type(error).__name__+')') from error
    finally:
        try:
            if selector is not None: selector.close()
        finally:
            try:
                if endpoint is not None: endpoint.close()
            finally:
                if directory is not None: directory.close()
