"""선택형 1×1 ToF의 bounded RX 인계와 actor 원 시각 mapping.

관측 하나가 serial 패킷 하나이며 조립·부분 관측·조립 기한은 없다.
센서별 원 source/reset/CRC/시각을 보존하고 FREE나 주행 권한은 만들지 않는다.
"""
from dataclasses import dataclass
from collections import Counter
import queue
import struct
from uuid import UUID
from binascii import crc32

from xenova_drive_world import schema,decode,encode
from xenova_drive_world.mapper_clock import drift_pad


TOF_SAMPLE=0x0f17
TOF_CAPABILITY=0x0800
TOF_SAMPLE3=struct.Struct('<16s16sQQI16sQB16sII32sBBHHBBI')
TOF_COMMON3=struct.Struct('<16s16sQQI16sQB16sII32sBBH')
NIL=bytes(16)
U63=(1<<63)-1


@dataclass(frozen=True)
class TofSelection:
    profiles: tuple
    maximum_age_us: int
    maximum_clock_error_us: int
    vehicle: str
    host_boot: str
    gateway_instance: str

    def __post_init__(self):
        if type(self.profiles) is not tuple or len(self.profiles)>2:raise ValueError('TOF profile count')
        ids=set()
        for row in self.profiles:
            if (type(row) is not tuple or len(row)!=3 or type(row[0]) is not int or row[0] not in (0,1)
                    or row[0] in ids or type(row[1]) is not str or len(row[1])!=64
                    or any(c not in '0123456789abcdef' for c in row[1])
                    or type(row[2]) is not int or not 0<row[2]<1<<32):raise ValueError('TOF selected calibration')
            ids.add(row[0])
        if (type(self.maximum_age_us) is not int or not 0<self.maximum_age_us<=1000000
                or type(self.maximum_clock_error_us) is not int or not 0<self.maximum_clock_error_us<=2**31-1):
            raise ValueError('TOF original time bounds')
        if type(self.vehicle) is not str or not 1<=len(self.vehicle)<=64:raise ValueError('TOF vehicle')
        for value in (self.host_boot,self.gateway_instance):
            if str(UUID(value))!=value:raise ValueError('TOF source UUID')


@dataclass(frozen=True)
class TofClock:
    epoch: bytes
    sequence: int
    lower: int
    upper: int
    reference_pi_us: int
    expires_pi_us: int
    drift_ppm: int


@dataclass(frozen=True)
class TofReceipt:
    payload: bytes
    received_pi_us: int
    clock: TofClock


class TofInbox:
    """RX1 producer/actor1 consumer. loss snapshot은 원 source fence2개뿐이다."""
    def __init__(self):
        self.inputs=queue.Queue(maxsize=8);self.loss=(0,((0,None),(0,None)));self.counts=Counter()

    @staticmethod
    def source(payload):
        if type(payload) is not bytes or len(payload)!=TOF_SAMPLE3.size:return None
        sensor=payload[76]
        if sensor not in (0,1):return None
        sample=struct.unpack_from('<Q',payload,32)[0];generation=struct.unpack_from('<I',payload,93)[0]
        return sensor,(payload[16:32],generation,payload[77:93],sample)

    @staticmethod
    def merge_fence(old,new):
        if old is None or old[0]!=new[0] or new[1]>old[1]:return new
        if new[1]<old[1]:return old
        # NIL here is an internal conflict tombstone, never a sensor UUID or
        # outgoing observation. That generation cannot choose either claimant.
        instance=old[2] if old[2]==new[2] else NIL
        return old[0],old[1],instance,max(old[3],new[3])

    def reject(self,payload=None,reason='invalid'):
        source=self.source(payload);revision,fences=self.loss;values=list(fences)
        if source is None:values=[(revision+1,row[1]) for row in values]
        else:
            fence=self.merge_fence(values[source[0]][1],source[1])
            values[source[0]]=(revision+1,fence)
        self.loss=(revision+1,tuple(values));self.counts[reason]+=1
        if source is None:
            # An outer-frame/source loss has no trustworthy sensor key. Retire
            # every already queued original sample as well as actor observations.
            for _ in range(8):
                try:old=self.inputs.get_nowait()
                except queue.Empty:break
                self.reject(old.payload,reason)

    def offer(self,payload,received_pi_us,clock):
        if type(payload) is not bytes or len(payload)!=TOF_SAMPLE3.size or type(clock) is not TofClock:
            self.reject(payload);return False
        try:self.inputs.put_nowait(TofReceipt(payload,received_pi_us,clock))
        except queue.Full:
            # A saturated low-priority stream cannot reserve the serial owner.
            # Drop the whole queued batch and fence every affected source.
            for _ in range(8):
                try:old=self.inputs.get_nowait()
                except queue.Empty:break
                self.reject(old.payload,'overflow')
            self.reject(payload,'overflow');return False
        return True

    def drain(self):
        before=self.loss;items=[]
        for _ in range(8):
            try:items.append(self.inputs.get_nowait())
            except queue.Empty:break
        after=self.loss
        if before!=after:
            # Actor-local loss snapshot also retires the batch taken during
            # a producer loss. Do not mutate the producer's loss slot here.
            values=list(after[1])
            for item in items:
                source=self.source(item.payload)
                if source is not None:
                    values[source[0]]=(after[0],self.merge_fence(values[source[0]][1],source[1]))
            return (after[0],tuple(values)),()
        return after,tuple(items)


class TofReceiver:
    """Gateway actor-only. 센서별 latest 원 관측141B 하나만 유지한다."""
    def __init__(self,selection):
        if type(selection) is not TofSelection:raise ValueError('TOF installed selection')
        self.selection=selection;self.scope=None;self.loss_revision=0;self.loss_seen=[0,0]
        self.sensors={row[0]:dict(profile=row,identity=None,blocked_generation=0,sample=0,stamp=0,
            latest=None,fence=None) for row in selection.profiles}
        self.counts=Counter();self.revoked=set()

    def _clear(self,sensor,*,sample=0,latest=False,identity=None):
        state=self.sensors[sensor]
        identity=state['identity'] if identity is None else identity
        if identity is not None and sample>0:
            state['fence']=TofInbox.merge_fence(state['fence'],(*identity,sample))
        if latest:state['latest']=None
        self.revoked.add(sensor)

    def _loss(self,loss):
        revision,fences=loss
        if revision==self.loss_revision:return
        self.loss_revision=revision
        for sensor,state in self.sensors.items():
            version,fence=fences[sensor]
            if version==self.loss_seen[sensor]:continue
            self.loss_seen[sensor]=version
            sample=state['sample'] if fence is None else fence[3]
            self._clear(sensor,sample=sample,latest=True,identity=None if fence is None else fence[:3])

    def _context(self,context):
        # Published by the one serial owner; no live mutable clock dict is read.
        if context is None or not context[3]:
            for sensor,state in self.sensors.items():self._clear(sensor,sample=state['sample'],latest=True)
            return False
        scope=context[:3]
        if self.scope!=scope:
            first=self.scope is None
            self.scope=scope
            for sensor,state in self.sensors.items():
                fence=state['fence'] if first and state['fence'] is not None and state['fence'][0]==scope[1] else None
                self._clear(sensor,latest=True);state.update(identity=None,blocked_generation=0,sample=0,stamp=0,fence=fence)
        return True

    def process(self,loss,items,context,now):
        """현재 source 손실/원E를 먼저 확인한다. 반환 raw는 arrival로 시각을 바꾸지 않는다."""
        self.revoked.clear();self._loss(loss)
        current=self._context(context)
        for sensor,state in self.sensors.items():
            latest=state['latest']
            if latest is not None and now>=latest.get('valid_until',1<<63):
                state['latest']=None;self.revoked.add(sensor);self.counts['expired']+=1
        if current:
            for item in items:
                try:self._sample(item,now)
                except (ValueError,struct.error):self.counts['rejected']+=1
        else:
            # Loss after RX admission also retires that original batch. A new
            # ACK/source observation cannot lend its validity to queued bytes.
            for item in items:
                source=TofInbox.source(item.payload)
                if source is not None and source[0] in self.sensors:
                    self._clear(source[0],sample=source[1][3],latest=True,identity=source[1][:3])
        result=[]
        for sensor,state in self.sensors.items():
            latest=state['latest']
            if latest is None or latest['published']:continue
            try:raw=self._mapped(latest,now)
            except ValueError:
                self._clear(sensor,sample=state['sample'],latest=True);self.counts['mapping_rejected']+=1
            else:latest['published']=True;latest['wire']=raw;result.append(raw)
        return tuple(result),tuple(sorted(self.revoked))

    def current_ranges(self,vehicle,now):
        """Return only selected, mapped observations in the current device session."""
        answer={}
        for sensor,state in self.sensors.items():
            latest=state['latest']
            if latest is None or 'wire' not in latest or now>=latest['valid_until']:continue
            row=decode(latest['wire'],schema.TofObservation)
            if (row.pi_session,row.device_session)!=(vehicle['pi_session'],vehicle['device_session']):continue
            if not row.frame_status&1:continue
            if row.status==b'\x01' and row.quality[0]&1:
                distance=int.from_bytes(row.distance_mm_le,'little')
            elif sensor==1 and row.status in (b'\x02',b'\x03'):
                distance=65535
            else:continue
            answer[sensor]=(distance,row.pi_sample_us,row.clock_error_us+row.sensor_time_error_us,latest['valid_until'])
        return answer

    def _sample(self,item,now):
        if type(item) is not TofReceipt or not 0<=item.received_pi_us<=now:raise ValueError('TOF receipt')
        values=TOF_SAMPLE3.unpack(item.payload)
        pi,device,sample,stamp,error,epoch,seq,sensor,instance,generation,revision,calibration,rows,cols,flags,distance,status,quality,crc=values
        state=self.sensors.get(sensor)
        if state is None:raise ValueError('TOF unselected')
        if ((pi,device,epoch)!=self.scope or not 0<sample<=U63 or not 0<stamp<=U63 or not 0<seq<=U63
                or not 0<generation<1<<32 or instance==NIL or rows!=1 or cols!=1 or flags&~7
                or status>7 or quality&~31 or status==1 and distance in (0,65535)
                or quality&1 and status!=1):
            self._clear(sensor,sample=sample if 0<sample<=U63 else state['sample']);raise ValueError('TOF shape/source')
        body=item.payload[:-4]
        if crc32(body)&0xffffffff!=crc:
            self._clear(sensor,sample=sample);raise ValueError('TOF sample CRC32')
        identity=(device,generation,instance);previous=state['identity']
        if (calibration.hex(),revision)!=state['profile'][1:]:
            self._clear(sensor,sample=sample,latest=True,identity=identity);raise ValueError('TOF calibration')
        if previous is not None and identity!=previous:
            if generation<=previous[1] or instance==previous[2]:
                if generation==previous[1]:state['blocked_generation']=generation;self._clear(sensor,sample=state['sample'],latest=True)
                raise ValueError('TOF generation conflict/replay')
            self._clear(sensor,latest=True);state.update(sample=0,blocked_generation=0)
        state['identity']=identity
        fence=state['fence']
        if fence is not None and fence[0]==device and (generation<fence[1]
                or generation==fence[1] and instance!=fence[2]):
            if generation==fence[1]:state['blocked_generation']=generation;self._clear(sensor,latest=True)
            raise ValueError('TOF retired generation/instance')
        if generation<=state['blocked_generation'] or fence is not None and fence[:3]==identity and sample<=fence[3]:
            raise ValueError('TOF dropped source')
        clock=item.clock
        if clock.epoch!=epoch or clock.sequence!=seq or now>=clock.expires_pi_us or item.received_pi_us>=clock.expires_pi_us:
            self._clear(sensor,sample=sample);raise ValueError('TOF original installed clock')
        latest=state['latest']
        if latest is not None and sample==latest['sample']:
            if latest['body']!=body or latest['crc']!=crc or latest['clock']!=clock:
                self._clear(sensor,sample=sample,latest=True);raise ValueError('TOF completed conflict')
            self.counts['duplicate']+=1;return
        if sample<=state['sample']:raise ValueError('TOF old sample')
        if stamp<=state['stamp']:
            self._clear(sensor,sample=sample);raise ValueError('TOF source time regression')
        self._clear(sensor,sample=state['sample'])
        state.update(sample=sample,stamp=stamp)
        state['latest']=dict(body=body,crc=crc,clock=clock,received=item.received_pi_us,sample=sample,published=False)
        self.counts['completed']+=1

    def _mapped(self,latest,now):
        values=TOF_COMMON3.unpack(latest['body'][:137])
        pi,device,sample,stamp,error,epoch,seq,sensor,instance,generation,revision,calibration,rows,cols,flags=values
        clock=latest['clock'];p=self.selection
        if not flags&2 or now>=clock.expires_pi_us:raise ValueError('TOF capture timing/clock')
        # The capture may predate P4. Include the entire allowed capture age and
        # sensor acquisition uncertainty in drift growth, never just RX age.
        pad=drift_pad(clock.drift_ppm,abs(latest['received']-clock.reference_pi_us)+p.maximum_age_us+error)
        lo=stamp+clock.lower-pad;hi=min(stamp+clock.upper+pad,latest['received'])
        if not 0<=lo<=hi<=latest['received']:raise ValueError('TOF conservative mapping')
        middle=(lo+hi)//2;uncertainty=(hi-lo+1)//2
        if (uncertainty>p.maximum_clock_error_us or middle-uncertainty-error<0
                or now>=middle+p.maximum_age_us-uncertainty-error):raise ValueError('TOF original capture expiry')
        measurement=latest['body'][137:141]
        value=schema.TofObservation(major=1,vehicle=p.vehicle,host_boot=p.host_boot,gateway_instance=p.gateway_instance,
            pi_session=str(UUID(bytes=pi)),device_session=str(UUID(bytes=device)),sensor_id=sensor,source_sample=sample,
            source_us=stamp,pi_sample_us=middle,clock_error_us=uncertainty,sensor_time_error_us=error,
            calibration=calibration.hex(),sensor_config_revision=revision,rows=rows,cols=cols,distance_mm_le=measurement[:2],
            status=measurement[2:3],quality=measurement[3:4],frame_status=flags,source_crc32=latest['crc'],clock_epoch=str(UUID(bytes=epoch)),
            clock_seq=seq,complete=True,sensor_instance=instance,sensor_generation=generation)
        raw=encode(value)
        if len(raw)>1024:raise ValueError('TOF observation cap')
        latest['valid_until']=middle+p.maximum_age_us-uncertainty-error
        return raw

    def payload_bytes(self):
        return sum(len(row['latest']['body']) for row in self.sensors.values() if row['latest'] is not None)
