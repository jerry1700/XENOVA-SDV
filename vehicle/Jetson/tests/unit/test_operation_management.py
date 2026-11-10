"""실제 Linux seqpacket/보호 디렉터리/peercred/CLI의 관리 transport 회귀다.

callback 결과는 불투명한 시험 값이며 실제 SupervisorOperation/M/Pi gate 적용 증거가 아니다.
manifest는 이 시험이 배타 생성한 임시 파일이고 S 인증용 실제 PID/birth/boot만 결합한다.
client thread는 시험 구동용이다. 제품 S-main에 thread를 추가하지 않는다.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from resource_guard_fixtures import encode, minimal_fields
from xenova_vehicle.runtime import operation_management as management
from xenova_vehicle.runtime.operation_management import OperationManagementError, OperationServer, operation_command
from xenova_vehicle.runtime.operation_request import OperationRequest
from xenova_vehicle.runtime.procfs_formats import parse_boot_id, parse_proc_stat


def j_us():
    return time.monotonic_ns() // 1000


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


@unittest.skipUnless(sys.platform == 'linux', 'actual Linux abstract seqpacket/peercred required')
class OperationManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xenova-operation-')
        self.directory = Path(self.temp.name)
        os.chmod(self.directory, 0o700)
        self.service = str(uuid4())
        self.fields = minimal_fields()
        self.fields['service_instance'] = self.service
        self.fields['runtime']['boot_id'] = parse_boot_id(Path('/proc/sys/kernel/random/boot_id').read_bytes())
        self.fields['runtime']['pid_ns_inode'] = str(os.stat('/proc/self/ns/pid').st_ino)
        actual = parse_proc_stat(Path('/proc/self/stat').read_bytes(), os.getpid())
        self.fields['owners'] = [dict(role='S', phase='CURRENT', pid=os.getpid(), start_ticks=str(actual.start_ticks))]
        self._write_manifest()
        self.servers, self.clients = [], []
        self.received, self.results, self.fingerprints = [], {}, {}
        self.apply_count = 0
        self.pending = False

    def _write_manifest(self):
        path = self.directory/'resource-set.json'
        if path.exists(): path.write_bytes(encode(self.fields))
        else:
            with path.open('xb') as output: output.write(encode(self.fields))
        os.chmod(path, 0o600)

    def tearDown(self):
        for endpoint in self.clients:
            endpoint.close()
        for server in self.servers:
            server.close()
        self.temp.cleanup()

    def status(self):
        return dict(status='OPAQUE_OWNER_STATUS', desired='RUN', confirmed='RUN')

    def submit(self, raw):
        self.received.append(raw)
        request = OperationRequest(raw)
        previous = self.fingerprints.get(request.request_id)
        if previous is not None:
            return self.results.get(request.request_id) if previous == sha256(raw).hexdigest() else dict(status='OPAQUE_CONFLICT')
        self.fingerprints[request.request_id] = sha256(raw).hexdigest()
        self.apply_count += 1
        if self.pending: return None
        result = dict(status='OPAQUE_OWNER_RESULT', request_id=request.request_id, action=request.action,
                      original_issued=str(request.issued_us), original_expires=str(request.expires_us))
        self.results[request.request_id] = result
        return result

    def query(self, request_id):
        return self.results.get(request_id)

    def server(self, *, clock=j_us, status=None, service=None):
        server = OperationServer(str(self.directory), self.service if service is None else service,
            status=self.status if status is None else status, submit=self.submit, query=self.query, clock=clock)
        self.servers.append(server)
        server.start()
        return server

    def call(self, server, action, **kwargs):
        outcome = {}
        def client():
            try: outcome['result'] = operation_command(str(self.directory), action, **kwargs)
            except BaseException as error: outcome['error'] = error
        thread = threading.Thread(target=client, name='operation-test-client', daemon=False)
        thread.start()
        deadline = time.monotonic()+3
        while thread.is_alive() and time.monotonic() < deadline:
            server.poll()
            time.sleep(.001)
        thread.join(.1)
        self.assertFalse(thread.is_alive(), 'bounded client must return without a runtime worker/thread')
        server.poll()
        return outcome

    def connect(self, server):
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.clients.append(endpoint)
        endpoint.settimeout(1)
        endpoint.connect(server.address)
        server.poll()
        hello = json.loads(endpoint.recv(4096))
        self.assertEqual(set(hello), {'profile', 'service_instance', 'status'})
        self.assertEqual(hello['service_instance'], self.service)
        return endpoint

    def request(self, *, request_id=None, service=None, pretty=False, issued=None, expires=None):
        issued = j_us() if issued is None else issued
        value = dict(profile='jetson-operation-request-1', service_instance=self.service if service is None else service,
            request_id=str(uuid4()) if request_id is None else request_id, action='STANDBY',
            issued_j_us=str(issued), expires_j_us=str(issued+5_000_000 if expires is None else expires))
        return json.dumps(value, indent=2 if pretty else None, separators=None if pretty else (',', ':'), sort_keys=True).encode('ascii')

    def send_request(self, server, raw):
        endpoint = self.connect(server)
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=raw.decode('ascii'))))
        server.poll(); server.poll()
        return json.loads(endpoint.recv(4096))['result']

    def assert_peer_closed(self, endpoint):
        try: self.assertEqual(endpoint.recv(1), b'')
        except ConnectionResetError: pass

    def test_real_status_and_explicit_actions_return_only_owner_result_and_no_new_resources(self):
        server = self.server()
        before = set(self.directory.iterdir())
        self.assertEqual(self.call(server, 'status')['result'], self.status())
        for action in ('run', 'standby'):
            result = self.call(server, action)['result']
            self.assertEqual(result['status'], 'OPAQUE_OWNER_RESULT')
            self.assertEqual(result['action'], action.upper())
        self.assertEqual(self.apply_count, 2)
        self.assertEqual(set(self.directory.iterdir()), before)
        self.assertTrue(server.address.startswith(b'\0xenova-operation.'))
        self.assertFalse(server.client_active)

    def test_original_ascii_request_whitespace_and_same_key_fingerprint_replay_are_preserved(self):
        server = self.server()
        raw = self.request(pretty=True)
        first = self.send_request(server, raw)
        repeated = self.send_request(server, raw)
        self.assertEqual(first, repeated)
        self.assertEqual(self.apply_count, 1)
        self.assertEqual(self.received, [raw, raw])
        changed = wire(json.loads(raw))
        self.assertNotEqual(sha256(raw).digest(), sha256(changed).digest())
        self.assertEqual(self.send_request(server, changed), dict(status='OPAQUE_CONFLICT'))
        self.assertEqual(self.received[-1], changed)
        self.assertEqual(self.apply_count, 1)

    def test_pending_submit_is_not_repeated_and_query_uses_original_request_id(self):
        self.pending = True
        server = self.server()
        query_ids = []
        def query(identity):
            query_ids.append(identity)
            if len(query_ids) < 3: return None
            return dict(status='OPAQUE_LATE_OWNER_RESULT', request_id=identity)
        server._query = query
        identity = str(uuid4())
        outcome = self.call(server, 'standby', request_id=identity)
        self.assertEqual(outcome['result'], dict(status='OPAQUE_LATE_OWNER_RESULT', request_id=identity))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(query_ids, [identity]*len(query_ids))
        self.assertGreaterEqual(len(query_ids), 3)

    def test_timeout_keeps_original_request_id_expiry_and_pending_owner_for_explicit_query(self):
        self.pending = True
        server = self.server()
        identity = str(uuid4())
        outcome = self.call(server, 'standby', request_id=identity, timeout_s=.1)
        self.assertIsInstance(outcome.get('error'), TimeoutError)
        self.assertEqual(outcome['error'].request_id, identity)
        self.assertIn(identity, str(outcome['error']))
        self.assertEqual(len(self.received), 1)
        original = OperationRequest(self.received[0])
        self.assertEqual(original.request_id, identity)
        self.assertLessEqual(original.expires_us-original.issued_us, 100_000)
        self.results[identity] = dict(status='OPAQUE_HISTORICAL_RESULT', original_expires=str(original.expires_us))
        self.assertEqual(self.call(server, 'query', request_id=identity)['result'], self.results[identity])
        self.assertEqual(OperationRequest(self.received[0]).expires_us, original.expires_us)
        self.assertEqual(self.apply_count, 1)

    def test_client_one_pending_connection_and_eof_are_bounded_without_consuming_next_request(self):
        server = self.server()
        first = self.connect(server)
        second = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.clients.append(second); second.settimeout(1); second.connect(server.address)
        for _ in range(3): server.poll()
        self.assertEqual(server.counts['accepted_clients'], 1)
        first.close(); server.poll(); server.poll()
        self.assertEqual(json.loads(second.recv(4096))['profile'], 'jetson-operation-management-1')
        self.assertEqual(server.counts['accepted_clients'], 2)
        second.close(); server.poll()
        self.assertFalse(server.client_active)

    def test_oversized_truncated_duplicate_unknown_and_pipelined_records_do_not_call_owner(self):
        server = self.server()
        invalid = [b'x'*4097, b'{"profile":"jetson-operation-client-1","kind":"STATUS","kind":"STATUS"}',
            wire(dict(profile='jetson-operation-client-1', kind='NEW_KIND', request='{}')),
            wire(dict(profile='jetson-operation-client-1', kind='STATUS', unknown=None))]
        padded = self.request()
        padded += b' '*(4096-len(padded))
        self.assertEqual(OperationRequest(padded).raw_bytes, padded)
        wrapped = wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=padded.decode('ascii')))
        self.assertGreater(len(wrapped), 4096)
        invalid.append(wrapped)
        for raw in invalid:
            endpoint = self.connect(server); endpoint.send(raw); server.poll()
            self.assert_peer_closed(endpoint)
            self.assertFalse(server.client_active)
        self.pending = True
        endpoint = self.connect(server)
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=self.request().decode('ascii'))))
        server.poll()
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='STATUS')))
        server.poll()
        self.assert_peer_closed(endpoint)
        self.assertEqual(len(self.received), 1)

    def test_original_request_wrong_service_or_expired_interval_is_not_submitted(self):
        clock = lambda: 1_000_000
        server = self.server(clock=clock)
        for raw in (self.request(service=str(uuid4()), issued=1_000_000, expires=2_000_000),
                    self.request(issued=1, expires=999_999)):
            endpoint = self.connect(server)
            endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=raw.decode('ascii'))))
            server.poll()
            self.assert_peer_closed(endpoint)
        self.assertEqual(self.received, [])

    def test_idle_and_pending_peer_deadlines_close_only_client_and_do_not_refresh_original_e(self):
        clock = [1_000_000]
        server = self.server(clock=lambda: clock[0])
        idle = self.connect(server)
        clock[0] += 5_000_000
        server.poll(); self.assert_peer_closed(idle)
        clock[0] = 7_000_000; self.pending = True
        endpoint = self.connect(server)
        raw = self.request(issued=clock[0], expires=clock[0]+10)
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=raw.decode('ascii'))))
        server.poll(); clock[0] += 10; server.poll()
        self.assert_peer_closed(endpoint)
        self.assertEqual(self.received, [raw])
        self.assertFalse(server.closed)

    def test_unsafe_directory_or_manifest_mode_symlink_and_stale_birth_reject_without_fd_leak(self):
        baseline = len(os.listdir('/proc/self/fd'))
        os.chmod(self.directory, 0o770)
        with self.assertRaises(OperationManagementError): self.server()
        os.chmod(self.directory, 0o700)
        path = self.directory/'resource-set.json'; os.chmod(path, 0o644)
        with self.assertRaises(OperationManagementError): self.server()
        os.chmod(path, 0o600)
        self.fields['owners'][0]['start_ticks'] = str(int(self.fields['owners'][0]['start_ticks'])+1)
        self._write_manifest()
        with self.assertRaises(OperationManagementError): self.server()
        alias = self.directory/'alias'; alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OperationManagementError):
            OperationServer(str(alias), self.service, status=self.status, submit=self.submit, query=self.query, clock=j_us).start()
        self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)

    def test_manifest_service_and_actual_peer_credential_mismatch_are_not_path_hash_authentication(self):
        server = self.server()
        self.fields['service_instance'] = str(uuid4()); self._write_manifest()
        result = self.call(server, 'status')
        self.assertIn('error', result)
        self.assertEqual(self.received, [])
        server.close()
        self.fields['service_instance'] = self.service; self._write_manifest()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(server.address); listener.listen(1); listener.settimeout(1)
        result = {}
        def client():
            try: operation_command(str(self.directory), 'status', timeout_s=.5)
            except BaseException as error: result['error'] = error
        thread = threading.Thread(target=client, daemon=False)
        thread.start()
        try:
            accepted, _ = listener.accept()
            try:
                # 실제 같은 uid/PID 연결이어도 잘못된 service hello는 거절한다.
                accepted.send(wire(dict(profile='jetson-operation-management-1',
                    service_instance=str(uuid4()), status=self.status())))
            finally: accepted.close()
        finally:
            listener.close(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get('error'), OperationManagementError)
        class ForeignUid:
            def getsockopt(self, level, option, length):
                self.arguments = level, option, length
                return struct.pack('=iII', os.getpid(), os.geteuid()+1, os.getegid())
        peer = ForeignUid()
        with self.assertRaises(OperationManagementError): management._peer(peer, os.geteuid())
        self.assertEqual(peer.arguments, (socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        # foreign uid는 원 ucred bytes의 음성 주입이다. 실제 setuid process 시험은 아니다.

    def test_address_collision_and_normal_close_preserve_existing_server_and_release_owned_fds(self):
        baseline = len(os.listdir('/proc/self/fd'))
        first = self.server(); address = first.address
        with self.assertRaises(OSError): self.server()
        self.assertEqual(self.call(first, 'status')['result'], self.status())
        first.close()
        second = self.server(); self.assertEqual(second.address, address)
        second.close()
        self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)
        self.assertEqual(set(item.name for item in self.directory.iterdir()), {'resource-set.json'})

    def test_pending_request_current_service_replacement_closes_client_before_query_without_resubmit(self):
        self.pending = True
        server = self.server()
        query_ids = []
        server._query = lambda identity: query_ids.append(identity) or dict(status='OPAQUE_READY_RESULT')
        endpoint = self.connect(server)
        raw = self.request()
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=raw.decode('ascii'))))
        server.poll()
        self.assertEqual(self.received, [raw])
        fingerprints = dict(self.fingerprints)
        self.fields['service_instance'] = str(uuid4()); self._write_manifest()
        server.poll()
        self.assert_peer_closed(endpoint)
        self.assertEqual(query_ids, [])
        self.assertEqual(self.received, [raw])
        self.assertEqual(self.fingerprints, fingerprints)
        self.assertEqual(self.apply_count, 1)
        self.assertEqual(server.counts['responses_sent'], 0)
        self.assertFalse(server.closed)

    def test_queued_response_current_birth_replacement_does_not_send_or_change_original_history(self):
        server = self.server()
        endpoint = self.connect(server)
        raw = self.request(); original = OperationRequest(raw)
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='REQUEST', request=raw.decode('ascii'))))
        server.poll()
        result = self.results[original.request_id]
        self.assertEqual(server.counts['responses_sent'], 0)
        birth = self.fields['owners'][0]['start_ticks']
        self.fields['owners'][0]['start_ticks'] = str(int(birth)+1); self._write_manifest()
        server.poll()
        self.assert_peer_closed(endpoint)
        self.assertEqual(server.counts['responses_sent'], 0)
        self.assertIs(self.results[original.request_id], result)
        self.assertEqual(self.received, [raw])
        self.assertEqual(self.apply_count, 1)
        self.fields['owners'][0]['start_ticks'] = birth; self._write_manifest()
        self.assertEqual(self.call(server, 'query', request_id=original.request_id)['result'], result)

    def test_client_rechecks_current_service_after_valid_original_response_before_return(self):
        # 원 peer/hello는 유효한 실제 socket이다. 응답 직전의 metadata 교체만 음성 주입한다.
        address = (b'\0xenova-operation.'+str(os.geteuid()).encode('ascii')+b'.'
                   +sha256(os.fsencode(os.path.realpath(self.directory))).hexdigest().encode('ascii'))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(address); listener.listen(1); listener.settimeout(1)
        result = {}; identity = str(uuid4())
        def client():
            try: result['value'] = operation_command(str(self.directory), 'run', request_id=identity, timeout_s=.5)
            except BaseException as error: result['error'] = error
        thread = threading.Thread(target=client, daemon=False)
        thread.start()
        try:
            accepted, _ = listener.accept()
            accepted.settimeout(1)
            try:
                accepted.send(wire(dict(profile='jetson-operation-management-1', service_instance=self.service, status=self.status())))
                request = json.loads(accepted.recv(4096))
                self.assertEqual(request['kind'], 'REQUEST')
                self.assertEqual(OperationRequest(request['request'].encode('ascii')).request_id, identity)
                self.fields['service_instance'] = str(uuid4()); self._write_manifest()
                accepted.send(wire(dict(profile='jetson-operation-response-1', service_instance=self.service,
                    kind='REQUEST', result=dict(status='OPAQUE_OLD_SERVICE_RESULT'))))
            finally: accepted.close()
        finally:
            listener.close(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('value', result)
        self.assertIsInstance(result.get('error'), OperationManagementError)
        self.assertEqual(self.received, [])

    def test_constructor_no_io_owner_guard_and_oversized_callback_result_cleanup(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('constructor socket I/O')):
            unopened = OperationServer(str(self.directory), self.service,
                status=self.status, submit=self.submit, query=self.query, clock=j_us)
        unopened.close()
        server = self.server()
        with patch.object(server._directory, 'service', side_effect=AssertionError('idle poll must not read metadata')) as metadata:
            for _ in range(10): self.assertEqual(server.poll(), 0)
            self.assertEqual(metadata.call_count, 0)
        errors = []
        def foreign_owner():
            try: server.poll()
            except RuntimeError as error: errors.append(error)
        thread = threading.Thread(target=foreign_owner); thread.start(); thread.join(1)
        self.assertEqual(len(errors), 1)
        endpoint = self.connect(server)
        server._status = lambda: dict(status='OPAQUE_TOO_LARGE', text='x'*4096)
        endpoint.send(wire(dict(profile='jetson-operation-client-1', kind='STATUS')))
        server.poll()
        self.assert_peer_closed(endpoint)
        self.assertFalse(server.client_active)
        self.assertFalse(server.closed)
        self.assertIn('OperationManagementError', server.last_error)
