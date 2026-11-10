"""Pi에서 실행하는 선택형 읽기 전용 SSH 관측 helper. 설치·명령 발행은 하지 않는다.

기존 owner-only uplink 자격증명은 Pi 메모리에서만 사용한다. stdout에는 관측
NDJSON만 쓰며 예외·프로필·인증 정보의 repr를 출력하지 않는다. stdout 정체는
이 진단 프로세스에만 머무르고 MQTT callback/제품 worker를 기다리게 하지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import queue
import re
import signal
import stat
import sys
import threading
import time
from uuid import uuid4

DEFAULT_PROFILE = '/etc/xenova/hil01/uplink.json'
DEFAULT_SOCKET = '/run/xenova-gateway/operator.sock'
TOPICS = ('xenova/v1/world/snapshot.pb', 'xenova/v1/vehicle/state.pb',
          'xenova/v1/vms/+/drive/link/out', 'xenova/v1/mapper/+/runtime')
_PATH = re.compile(r'^xenova/v1/vms/[^/+#\s]+/drive/link/out$')
_RUNTIME = re.compile(r'^xenova/v1/mapper/[a-z0-9][a-z0-9_-]{0,31}/runtime$')


def _pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:raise ValueError('duplicate profile field')
        value[key] = item
    return value


def load_mqtt_settings(path, *, uid=None):
    """기존 UplinkProfile.load의 저장·top-level 계약에서 MQTT 필드만 읽는다."""
    uid = os.geteuid() if uid is None else uid
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 8192):
            raise ValueError('private existing profile required')
        raw = stream.read(8193)
    if len(raw) > 8192:raise ValueError('profile size')
    value = json.loads(raw, object_pairs_hook=_pairs)
    keys = {'version', 'clock', 'host', 'port', 'username', 'password', 'tof_profiles', 'tof_max_age_us'}
    if (type(value) is not dict or set(value) not in (keys, keys | {'scene_policy'})
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('existing uplink profile fields required')
    host, port, username, password = (value[key] for key in ('host', 'port', 'username', 'password'))
    if type(host) is not str or str(ipaddress.IPv4Address(host)) != host:
        raise ValueError('existing MQTT IPv4 required')
    if type(port) is not int or not 1 <= port <= 65535:raise ValueError('MQTT port')
    if username is not None and (type(username) is not str or not 1 <= len(username) <= 128
                                or any(ord(char) < 33 or ord(char) > 126 for char in username)):
        raise ValueError('MQTT username')
    if password is not None and (type(password) is not str or len(password) > 1024):
        raise ValueError('MQTT password shape')
    return host, port, username, password


class Outbox:
    """관측용 queue32. 정체 시 오래된 관측을 버리고 callback은 block하지 않는다."""
    def __init__(self):
        self.queue = queue.Queue(maxsize=32)
        self.dropped = 0

    def put(self, value):
        try:self.queue.put_nowait(value);return
        except queue.Full:pass
        try:self.queue.get_nowait();self.dropped += 1
        except queue.Empty:pass
        try:self.queue.put_nowait(value)
        except queue.Full:self.dropped += 1

    def error(self, source, reason):
        self.put(dict(kind='error', source=source, reason=reason))


def _reason(value):
    try:return int(getattr(value, 'value', value))
    except (ValueError, TypeError):return 255


def mqtt_callbacks(client, outbox, stopped):
    """고정 네 종류만 구독한다. 어떤 경로에서도 MQTT publish를 호출하지 않는다."""
    def connected(client, _userdata, _flags, reason, _properties=None):
        if _reason(reason):outbox.error('mqtt', 'MQTT_CONNECT_UNAVAILABLE');return
        try:
            result, _mid = client.subscribe([(topic, 0) for topic in TOPICS])
            if result:outbox.error('mqtt', 'MQTT_SUBSCRIBE_UNAVAILABLE')
        except Exception:outbox.error('mqtt', 'MQTT_SUBSCRIBE_UNAVAILABLE')

    def subscribed(_client, _userdata, _mid, reasons, _properties=None):
        if any(_reason(reason) >= 128 for reason in reasons):
            outbox.error('mqtt', 'MQTT_SUBSCRIBE_UNAVAILABLE')

    def disconnected(*_args):
        if not stopped.is_set():outbox.error('mqtt', 'MQTT_DISCONNECTED')

    def message(_client, _userdata, value):
        try:
            if value.retain:return
            topic, raw = value.topic, bytes(value.payload)
            if (topic not in TOPICS[:2] and not _PATH.fullmatch(topic) and not _RUNTIME.fullmatch(topic)):
                return
            cap = 4096 if _RUNTIME.fullmatch(topic) else 32768
            if not 0 < len(raw) <= cap:outbox.error('mqtt', 'MQTT_OBSERVATION_SIZE');return
            outbox.put(dict(kind='mqtt', topic=topic, payload_base64=base64.b64encode(raw).decode('ascii')))
        except Exception:outbox.error('mqtt', 'MQTT_OBSERVATION_INVALID')

    client.on_connect = connected
    client.on_subscribe = subscribed
    client.on_disconnect = disconnected
    client.on_connect_fail = lambda *_: outbox.error('mqtt', 'MQTT_CONNECT_UNAVAILABLE')
    client.on_message = message


def start_mqtt(settings, outbox, stopped):
    import paho.mqtt.client as mqtt
    kwargs = dict(client_id='pc-observer-' + str(uuid4()), protocol=mqtt.MQTTv311)
    if hasattr(mqtt, 'CallbackAPIVersion'):kwargs['callback_api_version'] = mqtt.CallbackAPIVersion.VERSION2
    client = mqtt.Client(**kwargs)
    mqtt_callbacks(client, outbox, stopped)
    host, port, username, password = settings
    if username is not None:client.username_pw_set(username, password)
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect_async(host, port, keepalive=10)
    client.loop_start()
    return client


def _operator_once(client, outbox):
    values = []
    for action, source, reason in (('status', 'operation', 'GATEWAY_STATUS_UNAVAILABLE'),
                                   ('operation.status', 'operation_detail', 'GATEWAY_OPERATION_STATUS_UNAVAILABLE')):
        try:
            value = client.call(dict(version=2, action=action))
            if type(value) is not dict or value.get('ok') is not True:
                outbox.error(source, reason)
        except Exception:
            value = None;outbox.error(source, reason)
        values.append(value)
    if any(value is not None for value in values):
        outbox.put(dict(kind='operator', status=values[0], operation_status=values[1]))


def operator_worker(socket_path, outbox, stopped):
    try:
        from xenova_gateway.operator_client import OperatorClient
        client = OperatorClient(endpoint=socket_path, wait_ms=500, uid=os.geteuid())
    except Exception:
        outbox.error('operation', 'GATEWAY_STATUS_UNAVAILABLE');return
    while not stopped.is_set():
        due = time.monotonic() + 1.
        _operator_once(client, outbox)
        if stopped.wait(max(0., due - time.monotonic())):return


def write_record(stream, value):
    """원 관측 NDJSON만 쓴다. 프로필이나 예외 repr는 덧붙이지 않는다."""
    stream.write(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n')
    stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Existing Pi sources to read-only NDJSON; no control output.')
    parser.add_argument('--mqtt-profile', default=DEFAULT_PROFILE)
    parser.add_argument('--socket', default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    stopped = threading.Event();outbox = Outbox();client = None
    for number in (signal.SIGINT, signal.SIGTERM):signal.signal(number, lambda *_: stopped.set())
    worker = threading.Thread(target=operator_worker, args=(args.socket, outbox, stopped),
                              name='remote-observer-operator', daemon=True)
    worker.start()
    try:
        try:client = start_mqtt(load_mqtt_settings(args.mqtt_profile), outbox, stopped)
        except Exception:outbox.error('mqtt', 'MQTT_PROFILE_OR_CONNECTION_UNAVAILABLE')
        while not stopped.is_set():
            try:value = outbox.queue.get(timeout=.5)
            except queue.Empty:continue
            try:write_record(sys.stdout, value)
            except (BrokenPipeError, OSError):break
            except (TypeError, ValueError):write_record(sys.stdout, dict(kind='error', source='operation', reason='OBSERVATION_FORMAT_INVALID'))
    finally:
        stopped.set()
        if client is not None:
            try:client.disconnect();client.loop_stop()
            except Exception:pass
        worker.join(1.1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
