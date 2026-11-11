"""PC의 별도 관측 프로세스. 현재 run의 진단·기존 MQTT를 snapshot 파일로 투영한다.

주행 계산/Streamlit을 import하지 않으며 명령을 발행하지 않는다. 수집 실패는
해당 관측의 오류·나이로 표시하고, 없던 프레임·위치·장치 적용을 만들지 않는다.
"""

import argparse
import base64
from collections import OrderedDict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import queue
import signal
import struct
import subprocess
import threading
import time
from uuid import uuid4
import zlib


SOURCE_CAP = 2 * 1024 * 1024
SNAPSHOT_CAP = 256 * 1024
MEDIA_CAP = 1024 * 1024
STREAMS = ('pc', 'rgb', 'depth', 'mqtt', 'world', 'vehicle', 'path', 'operation')


def packed(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def read_json(path, cap=SOURCE_CAP):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:raise ValueError('duplicate diagnostic JSON key')
            result[key] = value
        return result
    with Path(path).open('rb') as stream:raw = stream.read(cap+1)
    if len(raw) > cap:raise ValueError('diagnostic file exceeds limit')
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite diagnostic JSON')))
    if type(value) is not dict:raise ValueError('diagnostic object required')
    return value


def atomic(path, raw):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def uint(value):
    if type(value) is str and value.isascii() and value.isdecimal() and len(value) <= 20:value = int(value)
    if type(value) is not int or not 0 <= value < 2**64:raise ValueError('diagnostic sequence required')
    return value


class ObservationCollector:
    """최신 stream8개·media4개·단계3개만 보관한다. 제품 입력·명령을 출력하지 않는다."""

    def __init__(self, output, *, clock=time.monotonic, wall_clock=time.time, stale_after_s=3.):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.clock, self.wall_clock = clock, wall_clock
        self.stale_after = float(stale_after_s)
        if not math.isfinite(self.stale_after) or self.stale_after <= 0:raise ValueError('positive display age required')
        self.id, self.epoch = str(uuid4()), str(uuid4())
        self.sequence = 0
        self.run_id = self.session_id = None
        self.stage, self.state = 'WAITING_FOR_RUN', 'WAITING'
        self.last_error = self.stop_reason = None
        self.streams = {name:dict(name=name, received_count=0, source_id=None, source_epoch=None,
            source_sequence=None, source_stamp=None, pc_received_stamp=None, error=None) for name in STREAMS}
        self.received = {}; self.heads = OrderedDict(); self.media = {}; self.layers = {}
        self.stages = {}; self.stage_received = {}; self.stage_heads = {}
        self.operation = self.pose = self.binding = None
        self.paths = {}
        self.metrics = {}; self.identities = {}; self.slot = 0
        self.started_wall_ns = time.time_ns()
        self._pc_version = None
        self._remote_versions = {}

    def stamp(self, now=None):
        return dict(domain='pc-observer-monotonic', epoch=self.epoch,
                    us=str(int((self.clock() if now is None else now)*1_000_000)))

    def error(self, stream, code):
        # 외부 예외 repr/명령 인자/자격정보는 진단 파일에 복제하지 않는다.
        text = str(code)[:256]
        self.last_error = dict(source=stream, reason=text, pc_received_stamp=self.stamp())
        if stream in self.streams:self.streams[stream]['error'] = text

    def _receive(self, name, source_id, epoch, sequence, source_stamp=None):
        sequence = uint(sequence)
        key = name, source_id, epoch
        previous = self.heads.get(key)
        if previous is not None and sequence <= previous:return False
        self.heads[key] = sequence;self.heads.move_to_end(key)
        while len(self.heads)>32:self.heads.popitem(last=False)
        now = self.clock(); self.received[name] = now
        row = self.streams[name]
        row.update(received_count=row['received_count']+1, source_id=source_id, source_epoch=epoch,
                   source_sequence=str(sequence), source_stamp=source_stamp, pc_received_stamp=self.stamp(now), error=None)
        return True

    def _new_run(self, run):
        if type(run) is not str or not 0 < len(run) <= 128:raise ValueError('explicit current run required')
        if self.run_id == run:return
        self.run_id, self.session_id = run, None
        self.epoch = str(uuid4())
        self.received.clear(); self.heads.clear(); self.media.clear(); self.layers.clear()
        self.stages.clear(); self.stage_received.clear(); self.stage_heads.clear()
        self.operation = self.pose = self.binding = None
        self.paths.clear()
        self.metrics.clear(); self.identities.clear()
        self.last_error = self.stop_reason = None
        for row in self.streams.values():
            row.update(received_count=0, source_id=None, source_epoch=None, source_sequence=None,
                       source_stamp=None, pc_received_stamp=None, error=None)

    def _stage(self, name, key, source_id, source_epoch, source_sequence, detail, source_stamp=None):
        if self.stage_heads.get(name) == key:return
        self.stage_heads[name] = key
        self.stage_received[name] = self.clock()
        self.stages[name] = dict(state='OBSERVED', source_id=source_id, source_epoch=source_epoch,
            source_sequence=str(source_sequence), source_stamp=source_stamp,
            pc_received_stamp=self.stamp(), detail=detail)

    def ingest_pc(self, body):
        if body.get('schema') != 'pc-live-source-1':raise ValueError('unsupported PC source')
        self._new_run(body.get('run_id'))
        source = body.get('source_id') or body['run_id']
        epoch = body.get('source_epoch') or body['run_id']
        if not self._receive('pc', source, epoch, body['sequence'], body.get('source_stamp')):return
        self.session_id = body.get('session_id')
        self.binding = body.get('binding')
        self.stage, self.state = body.get('stage') or 'PC_CONNECTED', body.get('state') or 'RUNNING'
        if body.get('error'):self.error('pc', body['error'])
        if body.get('stop_reason'):self.stop_reason = body['stop_reason']
        counters = body.get('counters') or {}
        self.metrics.update({'pc_'+key:value for key,value in counters.items() if type(value) in (int,float,bool,str)})
        self.metrics['pc_observer_write_failures']=body.get('write_failures',0)
        if body.get('io_error') is not None:self.identities['pc_observer_io_error']=body['io_error']
        producer = body.get('producer') or {}
        self.identities['pc_source'] = {key:producer.get(key) for key in ('binding_sha256','started_pc_us','deadline_pc_us')}
        for item in body.get('media') or []:
            try:
                panel = item['panel']
                if panel not in ('rgb','depth'):raise ValueError('unsupported PC panel')
                raw = base64.b64decode(item['data_base64'], validate=True)
                width, height = item['width'], item['height']
                if (type(width) is not int or type(height) is not int or not 0 < width <= 4096
                        or not 0 < height <= 4096 or not 0 < len(raw) <= MEDIA_CAP):raise ValueError('PC media bound')
                expected = width*height*(3 if panel == 'rgb' else 4)//2
                if len(raw) != expected or item['encoding'] != ('NV12' if panel == 'rgb' else 'RAW16_LE'):
                    raise ValueError('PC media shape')
                if self._receive(panel, source, epoch, item['source_sequence'], item.get('source_stamp')):
                    descriptor = dict(panel=panel, encoding=item['encoding'], width=width, height=height,
                        sha256=sha256(raw).hexdigest(), byte_length=len(raw), source_sequence=str(item['source_sequence']),
                        stamp=item.get('source_stamp'), original_stamp=item.get('original_stamp',item.get('source_stamp')))
                    if panel == 'depth':descriptor['depth_unit'] = 'mm'
                    self.media[panel] = descriptor, raw
            except (KeyError,ValueError,TypeError):self.error(item.get('panel','pc'), 'INVALID_PC_MEDIA')
        peer = body.get('peer') or {}
        device = peer.get('device_session')
        if device is not None:self.identities['device_session'] = device
        command, applied = peer.get('command_hwm',0), peer.get('applied',0)
        if command:
            self._stage('command',(device,command),device,device,command,
                dict(kind='VIRTUAL_DEVICE_COMMAND_RECEIVED', command_sequence=command,
                     note='가상 장치가 받은 명령 순번. 새 명령 수신은 적용·이동의 증거가 아님'))
        if applied:
            self._stage('applied',(device,applied),device,device,applied,
                dict(kind='VIRTUAL_DEVICE_TARGET_APPLIED', applied_count=applied, command_sequence=command,
                     note='가상 장치의 target 적용. 바퀴 이동과 별개'))
        for key in ('received','rejected','command_hwm','applied','sensor_samples','queued_frames'):
            if peer.get(key) is not None:self.metrics['peer_'+key] = peer[key]
        plant = body.get('plant') or {}
        if plant.get('invalid_reason'):self.stop_reason = plant['invalid_reason']
        if plant.get('pose') is not None:
            x,y,yaw = plant['pose']; frame = 'PLANT:'+str(plant['epoch'])
            self.layers['plant'] = [dict(kind='GT', frame=frame, points=[[x,y]], closed=False)]
            self.identities['plant_pose'] = dict(frame=frame,x=x,y=y,yaw=yaw,observed_us=plant.get('observed_us'))
            tick = plant.get('observed_us')
            velocity = plant.get('velocity_mps')
            if tick is not None:
                self._stage('movement',(plant['epoch'],tick),plant['epoch'],plant['epoch'],tick,
                    dict(kind='SIMULATED_WHEEL_MOTION', velocity_mps=velocity, pose=[x,y,yaw],
                         moved=velocity is not None and abs(velocity)>1e-6,
                         note='동일 plant의 실제 적분 상태. 실물 차체 이동 수락이 아님'),
                    dict(domain='pc-plant-monotonic',epoch=plant['epoch'],us=str(tick)))

    def ingest_event(self, event):
        """기존 wire를 읽은 표시 사본만 받는다. run 밖·다른 clock/장치 자료는 결합하지 않는다."""
        name = event['stream']
        if name not in ('world','vehicle','path','runtime'):raise ValueError('unsupported observer stream')
        if self.run_id is None:return
        payload = event.get('payload') or {}
        identities = payload.get('identities') or {}
        binding = self.binding or {}
        if name == 'world':
            if not binding.get('destination_clock_epoch'):return
            if identities.get('clock_epoch') != binding['destination_clock_epoch']:
                self.error(name,'SOURCE_RUN_CLOCK_MISMATCH');return
        if name == 'path':
            world = self.identities.get('world')
            if not binding.get('destination_clock_epoch') or world is None:return
            if any(identities.get(key) != world.get(key) for key in
                   ('map_epoch','frame_epoch','odom_epoch','configuration','calibration','primitive')):
                self.error(name,'PATH_WORLD_FRAME_MISMATCH');return
        if name == 'vehicle':
            device = self.identities.get('device_session')
            if device is None or identities.get('device_session') != device:return
        if name == 'runtime':
            # PC resident는 physical mapper-runtime을 발행하지 않는다. 다른 실물 실행을 합치지 않는다.
            if not binding.get('service_instance') or payload.get('service_instance') != binding['service_instance']:return
            self.identities['runtime'] = payload
            return
        if not self._receive(name,event['source_id'],event['source_epoch'],event['source_sequence'],event.get('source_stamp')):return
        if name == 'path':
            vm = identities.get('vm_id')
            if vm is not None:
                if vm not in self.paths and len(self.paths)>=2:self.paths.pop(next(iter(self.paths)))
                self.paths[vm] = payload.get('layers') or []
        elif payload.get('layers') is not None:self.layers[name] = payload['layers']
        if name == 'world':
            self.metrics['source_map_layers_omitted']=len(payload.get('omitted_layers') or [])
        if name == 'world' and payload.get('reference_anchor'):
            anchor=payload['reference_anchor'];pose=anchor['pose']
            self.layers['anchor']=[dict(kind='ESTIMATE',frame=pose['frame'],points=[[pose['x'],pose['y']]],
                closed=False,role='world-reference-vehicle',vehicle_sequence=anchor['vehicle_sequence'])]
            self.identities['world_reference_pose']=anchor
        if name == 'world' and payload.get('grid') is not None:
            grid = payload['grid'];width,height = grid['width'],grid['height']
            occupancy = grid['occupancy']
            if 0<width<=512 and 0<height<=512 and len(occupancy)==width*height:
                colors={0:bytes((82,133,119)),100:bytes((240,103,92)),255:bytes((27,34,48))}
                rows=b''.join(b'\0'+b''.join(colors.get(v,colors[255]) for v in
                    occupancy[y*width:(y+1)*width]) for y in reversed(range(height)))
                def chunk(tag,data):
                    return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
                raw=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
                     +chunk(b'IDAT',zlib.compress(rows,3))+chunk(b'IEND',b''))
                self.media['grid']=(dict(panel='grid',encoding='PNG',width=width,height=height,
                    sha256=sha256(raw).hexdigest(),byte_length=len(raw),source_sequence=str(event['source_sequence']),
                    stamp=event.get('source_stamp'),original_stamp=event.get('source_stamp')),raw)
                self.identities['grid']={key:grid[key] for key in ('frame','width','height','origin','resolution_m')}
        if name == 'vehicle' and payload.get('pose') is not None:
            self.pose = payload['pose']
            self.layers[name] = [dict(kind='ESTIMATE',frame=self.pose['frame'],points=[[self.pose['x'],self.pose['y']]],closed=False)]
        self.identities[name] = identities

    def ingest_operator(self, projected):
        source = projected['source']
        identity = source.get('gateway_instance') or source.get('gateway_instance_id') or 'gateway'
        epoch = source.get('host_boot') or source.get('host_boot_id') or identity
        sequence = source.get('pi_now_ms')
        if sequence is None:sequence = source.get('sequence')
        if sequence is None:raise ValueError('original Gateway status time missing')
        if self._receive('operation',identity,epoch,sequence):self.operation = projected['operation']
        self.identities['gateway']=dict(channel='OPERATOR2',operator_interface='AVAILABLE',**source)
        if source.get('stop_reason'):self.stop_reason=source['stop_reason']

    def ingest_gateway_context(self, projected):
        source=projected['source'];observer=source['observer_epoch']
        if self._receive('operation','g1-context-observer:'+observer,observer,source['sequence'],source['response_stamp']):
            self.operation=projected['operation']
            self.identities['gateway']=source

    def poll_pc(self, directory):
        path = Path(directory)/'pc.json'
        try:
            info = path.stat();version = info.st_size, info.st_mtime_ns
            if version == self._pc_version:return
            # 시작 전에 남아 있던 완료 기록은 LIVE로 인수하지 않는다. 새 원자 교체를 기다린다.
            if info.st_mtime_ns < self.started_wall_ns and self._pc_version is None:return
            self.ingest_pc(read_json(path));self._pc_version = version
        except FileNotFoundError:pass
        except (OSError,ValueError,TypeError,KeyError):self.error('pc','PC_SOURCE_READ_OR_FORMAT_ERROR')

    def poll_remote(self, directory):
        """별도 SSH relay의 최신 파일만 읽는다. 제품 MQTT·소켓을 열지 않는다."""
        from .sources import decode_mqtt,project_operator_status,project_gateway_context
        for path in sorted(Path(directory).glob('remote-*.json'))[:8]:
            try:
                info=path.stat();version=info.st_size,info.st_mtime_ns
                if version==self._remote_versions.get(path.name):continue
                if info.st_mtime_ns<self.started_wall_ns and path.name not in self._remote_versions:continue
                body=read_json(path);value=body['payload']
                if body.get('schema')!='pc-remote-observation-1':raise ValueError('remote relay schema')
                self.metrics['relay_write_errors']=body.get('write_errors',0)
                if value['kind']=='mqtt':
                    self._receive('mqtt','pc-ssh-relay',body['relay_epoch'],body['sequence'],
                        dict(domain='pc-relay-monotonic',epoch=body['relay_epoch'],us=str(body['received_pc_us'])))
                    self.metrics['remote_transport']='SSH_NDJSON'
                    event=decode_mqtt(value['topic'],base64.b64decode(value['payload_base64'],validate=True))
                    if event is not None:self.ingest_event(event)
                elif value['kind']=='operator':
                    status=value.get('status')
                    if type(status) is dict and status.get('ok') is False:
                        self.error('operation','GATEWAY_STATUS_'+str((status.get('error') or {}).get('code','UNAVAILABLE')))
                    else:self.ingest_operator(project_operator_status(status,value.get('operation_status')))
                elif value['kind']=='gateway_context':self.ingest_gateway_context(project_gateway_context(value))
                elif value['kind']=='error':self.error(value.get('source','remote'),value.get('reason','REMOTE_OBSERVER_ERROR'))
                self._remote_versions[path.name]=version
            except (OSError,ValueError,KeyError,TypeError):self.error('remote','REMOTE_OBSERVATION_INVALID')

    def publish(self):
        now = self.clock();self.sequence += 1
        streams = []
        for name,row in self.streams.items():
            elapsed = None if name not in self.received else max(0.,now-self.received[name])
            state = 'ERROR' if row['error'] else 'WAITING' if elapsed is None else 'STALE' if elapsed >= self.stale_after else 'FRESH'
            streams.append(dict(row,state=state,received_age_s=elapsed))
        stages = {}
        for name in ('command','applied','movement'):
            row = self.stages.get(name)
            stages[name] = dict(state='UNKNOWN',source_id=None,source_sequence=None,source_stamp=None,
                pc_received_stamp=None,detail=None) if row is None else dict(row,
                state='STALE' if now-self.stage_received[name]>=self.stale_after else 'OBSERVED')
        selected_vm = (self.operation or {}).get('active_vm')
        path = self.paths.get(selected_vm, [])
        layers = (self.layers.get('world',[])[:4]+path[:1]+self.layers.get('vehicle',[])[:1]
                  +self.layers.get('plant',[])[:1]+self.layers.get('anchor',[])[:1])
        self.metrics['map_layers_omitted'] = max(0,len(self.layers.get('world',[]))-4)
        self.slot = 1-self.slot
        media = []
        for panel,(descriptor,raw) in self.media.items():
            relative = f'media-{self.slot}-{panel}.bin'
            atomic(self.output/relative,raw)
            media.append(dict(descriptor,path=relative))
        body = dict(schema='jetson-viewer-snapshot-1',source=dict(id=self.id,epoch=self.epoch,
            sequence=str(self.sequence),kind='LIVE_HIL' if self.run_id is not None else 'DIAGNOSTIC',stamp=self.stamp(now)),
            operation=self.operation,map=dict(layers=layers),media=media,identities=self.identities,metrics=self.metrics,
            raw_payload=None,live=dict(schema='pc-live-observer-1',run_id=self.run_id,session_id=self.session_id,
                collector=dict(id=self.id,epoch=self.epoch,sequence=str(self.sequence),collected_stamp=self.stamp(now)),
                connection=dict(stage=self.stage,state=self.state,stop_reason=self.stop_reason,last_error=self.last_error),
                streams=streams,stages=stages,pose=self.pose))
        raw = packed(body)
        if len(raw)>SNAPSHOT_CAP:raise ValueError('viewer snapshot exceeds bound')
        atomic(self.output/'snapshot.json',raw)
        return body


class MqttReader:
    """독립 PC collector의 수신만 소유한다. publish/명령용 client는 만들지 않는다."""

    def __init__(self, profile, *, device=None):
        self.inputs = queue.Queue(maxsize=32);self.dropped = 0;self.error = None
        self.client = None;self.thread = None;self.stop = threading.Event()
        self.profile,self.device = profile,device

    def start(self):
        self.thread = threading.Thread(target=self._run,name='pc-observer-mqtt',daemon=True)
        self.thread.start()

    def _run(self):
        try:
            import paho.mqtt.client as mqtt
            config = read_json(self.profile,8192)
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id='pc-observer-'+str(uuid4()),protocol=mqtt.MQTTv311)
            self.client = client
            if config.get('username') is not None:client.username_pw_set(config['username'],config.get('password'))
            def connected(c,_,flags,reason,properties):
                if reason.is_failure:self.error='MQTT_CONNECTION_REJECTED';return
                self.error = None
                c.subscribe([('xenova/v1/world/snapshot.pb',0),('xenova/v1/vehicle/state.pb',0),
                    ('xenova/v1/vms/+/drive/link/out',0),('xenova/v1/mapper/+/runtime',0)])
            def message(c,_,m):
                if m.retain or len(m.payload)>SNAPSHOT_CAP:return
                try:self.inputs.put_nowait((m.topic,bytes(m.payload)))
                except queue.Full:self.dropped += 1
            client.on_connect,client.on_message = connected,message
            client.connect(config['host'],int(config['port']),keepalive=15)
            while not self.stop.is_set():
                if client.loop(timeout=.2) != 0:
                    self.error='MQTT_DISCONNECTED';self.stop.wait(1.)
        except (OSError,ValueError,KeyError,ImportError):self.error='MQTT_OBSERVER_UNAVAILABLE'
        finally:
            if self.client is not None:self.client.disconnect()


class StatusReader:
    """허용된 읽기 전용 Gateway CLI를 독립 worker에서 최대1Hz로 호출한다."""

    def __init__(self, command_file):
        config = read_json(command_file,8192)
        self.argv = config['argv']
        if (type(self.argv) is not list or not all(type(x) is str for x in self.argv)
                or not self.argv or self.argv[-1]!='status' or 'xenova-drive' not in self.argv):
            raise ValueError('explicit read-only xenova-drive status argv required')
        self.latest = None;self.error = None;self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run,name='pc-observer-status',daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try:
                result = subprocess.run(self.argv,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    timeout=2.,check=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode or len(result.stdout)>8192:raise ValueError('status unavailable')
                value = json.loads(result.stdout)
                self.latest = value;self.error = None
            except (OSError,ValueError,subprocess.TimeoutExpired):self.error='GATEWAY_STATUS_UNAVAILABLE'
            self.stop.wait(1.)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir',required=True)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--mqtt-profile')
    parser.add_argument('--gateway-status-command')
    parser.add_argument('--remote-input-dir',help='기존 SSH read-only helper를 연결한 PC relay 폴더')
    parser.add_argument('--interval-s',type=float,default=.5)
    parser.add_argument('--stale-after-s',type=float,default=3.)
    args = parser.parse_args(argv)
    if not .1<=args.interval_s<=5:parser.error('display poll interval must be 0.1..5s')
    owner = ObservationCollector(args.output_dir,stale_after_s=args.stale_after_s)
    stopped = threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_:stopped.set())
    mqtt = MqttReader(args.mqtt_profile) if args.mqtt_profile else None
    status = StatusReader(args.gateway_status_command) if args.gateway_status_command else None
    if mqtt:mqtt.start()
    if status:status.thread.start()
    owner.publish()
    print(json.dumps(dict(kind='pc-observer-started-1',output=str(owner.output),control_enabled=False)),flush=True)
    try:
        while not stopped.is_set():
            owner.poll_pc(args.input_dir)
            if args.remote_input_dir:
                try:owner.poll_remote(args.remote_input_dir)
                except (OSError,ImportError):owner.error('remote','REMOTE_OBSERVER_UNAVAILABLE')
            if mqtt is not None:
                for _ in range(8):
                    try:topic,raw = mqtt.inputs.get_nowait()
                    except queue.Empty:break
                    try:
                        from .sources import decode_mqtt
                        event = decode_mqtt(topic,raw)
                        if event is not None:owner.ingest_event(event)
                    except (ValueError,ImportError,KeyError,TypeError):owner.error('mqtt','MQTT_OBSERVATION_INVALID')
                if mqtt.error:owner.error('mqtt',mqtt.error)
                owner.metrics['mqtt_dropped'] = mqtt.dropped
            if status is not None:
                if status.latest is not None:
                    try:
                        from .sources import project_operator_status
                        owner.ingest_operator(project_operator_status(status.latest))
                    except (ValueError,KeyError,TypeError,ImportError):owner.error('operation','GATEWAY_STATUS_INVALID')
                if status.error:owner.error('operation',status.error)
            try:owner.publish()
            except (OSError,ValueError):pass  # collector I/O 자체 실패도 제품에 신호를 보내지 않는다.
            stopped.wait(args.interval_s)
    finally:
        if mqtt:mqtt.stop.set()
        if status:status.stop.set()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
