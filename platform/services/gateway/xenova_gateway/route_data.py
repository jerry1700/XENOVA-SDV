"""인증된 RoadData 원문과 실제 mapper publication의 유한 RAM 보관.

D receipt membership·현재 mapper/guest clock의 사실 검산은 기존 owner의 유한 포트가
담당한다. 이 모듈은 새 D I/O·thread·DB·권위 복원·geometry 재인코딩을 만들지 않는다.
"""
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.route import (RouteReason,route_record_sha256,road_record_body,
    route_world_witness_sha256,route_witness_sha256,route_source_key_sha256)
from .route_policy import RouteError,require,ZERO


KINDS={'start_goal':1,'road_topology':2,'road_section':3,'road_connection':4,'road_window':5,
       'exit_space':6,'route_plan':7,'road_geometry_scope':8}
# Current/staged/original references are pinned. Pool pressure is explicit BUSY,
# never eviction of an active source or an unbounded queued read.
COUNTS={1:1,2:1,3:2,4:2,5:3,6:1,7:1,8:1}


@dataclass(frozen=True)
class RoadRecord:
    kind: int
    digest: bytes
    original_body: bytes
    original_envelope: bytes
    delivery_envelope: bytes
    durable: bool

    def message(self):
        envelope=decode(self.delivery_envelope,schema.RoadData)
        return getattr(envelope,envelope.WhichOneof('payload'))


class RoadDataOwner:
    """단일 actor owner. callback은 이미 수신한 사실만 검산하고 동기 I/O를 하지 않는다."""
    def __init__(self,*,mapper_current,guest_current,durable_membership,world_current):
        self.mapper_current=mapper_current;self.guest_current=guest_current
        self.durable_membership=durable_membership
        # Returns the actual already-published original World bytes, never a
        # bool/management acknowledgement or a reconstructed current wrapper.
        self.world_current=world_current
        self.records=OrderedDict();self.pins=frozenset();self._pins={};self.worlds=OrderedDict();self.world_hwm={}
        self.lookup=None;self.registration=None

    def pin(self,digests,*,owner='default'):
        values=frozenset(digests)
        require(owner in ('default','route','goal') and len(values)<=sum(COUNTS.values())
            and all(type(v) is bytes and len(v)==32 for v in values),
            RouteReason.POOL_BUSY)
        combined=dict(self._pins);combined[owner]=values
        result=frozenset().union(*combined.values())
        require(len(result)<=sum(COUNTS.values()),RouteReason.POOL_BUSY)
        self._pins=combined;self.pins=result

    def pins_for(self,owner):return self._pins.get(owner,frozenset())

    def receive(self,raw,peer,now):
        """현재 delivery만 인증한다. static geometry는 현재 FREE/pose/출력 권위가 아니다."""
        body=decode(raw,schema.RoadData);name=body.WhichOneof('payload');kind=KINDS[name]
        record_raw=road_record_body(raw);digest=route_record_sha256(kind,record_raw)
        require(digest==body.record_sha256,RouteReason.HASH_OR_BODY_MISMATCH)
        if body.HasField('durable'):
            require(body.durable.status==1 and body.durable.record_sha256==digest,RouteReason.DURABLE_NOT_READY)
        if body.publisher_kind==1:
            require(self.mapper_current(body.mapper,peer,now),RouteReason.SOURCE_UNAVAILABLE)
            require(body.HasField('durable') and self.durable_membership(body.durable,kind,record_raw),
                RouteReason.DURABLE_NOT_READY)
            require(body.durable.status==1 and body.durable.record_sha256==digest,
                RouteReason.DURABLE_NOT_READY)
        else:
            require(body.publisher_kind==2 and kind==7
                and self.guest_current(body.guest_owner,body.guest_witness,peer,now),RouteReason.SOURCE_UNAVAILABLE)
            require(body.route_plan.origin_guest_witness_sha256==route_witness_sha256(body.guest_witness),
                RouteReason.HASH_OR_BODY_MISMATCH)
            if body.HasField('durable'):
                require(self.durable_membership(body.durable,kind,record_raw),RouteReason.DURABLE_NOT_READY)
        key=(kind,digest);old=self.records.get(key)
        if old is not None:
            require(old.original_body==record_raw,RouteReason.HASH_OR_BODY_MISMATCH)
            self.records[key]=RoadRecord(kind,digest,record_raw,old.original_envelope,bytes(raw),old.durable or body.HasField('durable'))
            return self.records[key]
        same=[k for k in self.records if k[0]==kind]
        if len(same)>=COUNTS[kind]:
            victim=next((k for k in same if k[1] not in self.pins),None)
            require(victim is not None,RouteReason.POOL_BUSY);del self.records[victim]
        saved=RoadRecord(kind,digest,record_raw,bytes(raw),bytes(raw),body.HasField('durable'))
        self.records[key]=saved
        return saved

    def get(self,kind,digest):
        value=self.records.get((kind,bytes(digest)))
        require(value is not None,RouteReason.TOPOLOGY_INCOMPLETE)
        return value

    def original_delivery(self,kind,digest):return self.get(kind,digest).delivery_envelope

    def receive_world(self,raw,peer,now):
        """실제 publication 뒤 원 World witness만 수락한다. 관리응답을 World로 만들지 않는다."""
        value=decode(raw,schema.MapperWorldWitness)
        require(self.mapper_current(value.mapper,peer,now),RouteReason.SOURCE_UNAVAILABLE)
        published=self.world_current(value,peer,now)
        require(type(published) is bytes and sha256(published).digest()==value.world_raw_sha256,
            RouteReason.SOURCE_UNAVAILABLE)
        world=decode(published,schema.WorldSnapshot);source=value.source
        require(world.HasField('route_view') and world.sequence==source.world_sequence
            and world.observation_seq==source.observation_sequence and all(getattr(world,k)==getattr(source,k)
                for k in ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch','clock_epoch',
                    'configuration','calibration','reference_id','reference_revision'))
            and world.primitive==source.primitive.hex(),RouteReason.HASH_OR_BODY_MISMATCH)
        key=bytes(value.world_raw_sha256);digest=route_world_witness_sha256(raw)
        old=self.worlds.get(key)
        if old is not None:
            require(old[0]==bytes(raw),RouteReason.HASH_OR_BODY_MISMATCH)
            return old[1]
        source=(bytes(value.mapper.mapper_boot),bytes(value.mapper.mapper_instance),bytes(value.mapper.clock_epoch))
        sequence=value.source.world_sequence
        require(sequence>self.world_hwm.get(source,0),RouteReason.REQUEST_CONTEXT_CHANGED)
        require(value.source.observation_sequence==value.mapper.observation_sequence,
            RouteReason.HASH_OR_BODY_MISMATCH)
        self.world_hwm[source]=sequence
        while len(self.world_hwm)>8:del self.world_hwm[next(iter(self.world_hwm))]
        self.worlds[key]=(bytes(raw),digest,value.mapper.valid_until_pi_us,published)
        while len(self.worlds)>8:self.worlds.popitem(last=False)
        return digest

    def world(self,digest,now):
        saved=self.worlds.get(bytes(digest))
        require(saved is not None and now<saved[2],RouteReason.CLOCK_OR_EXPIRY)
        return decode(saved[0],schema.MapperWorldWitness)

    def world_snapshot(self,digest,now):
        self.world(digest,now)
        return decode(self.worlds[bytes(digest)][3],schema.WorldSnapshot)

    def world_for_sequence(self,sequence,source_key,now):
        """기존 원 history8 조회만 한다. fresh frame마다 fullState/새 ACK를 요구하지 않는다."""
        matches=[]
        for digest,row in self.worlds.items():
            value=decode(row[0],schema.MapperWorldWitness)
            if value.source.world_sequence==sequence and route_source_key_sha256(value.source)==source_key:
                matches.append(digest)
        require(len(matches)==1,RouteReason.SOURCE_UNAVAILABLE)
        return self.world(matches[0],now)

    def request_lookup(self,raw,now):
        body=decode(raw,schema.RoadDataRequest)
        require(now<body.valid_until_pi_us,RouteReason.CLOCK_OR_EXPIRY)
        if self.lookup is not None:
            require(self.lookup==bytes(raw),RouteReason.POOL_BUSY)
        else:self.lookup=bytes(raw)
        return self.lookup

    def finish_lookup(self,request_nonce):
        if self.lookup is not None and decode(self.lookup,schema.RoadDataRequest).request_nonce==request_nonce:self.lookup=None

    def tick(self,now):
        if self.lookup is not None and now>=decode(self.lookup,schema.RoadDataRequest).valid_until_pi_us:self.lookup=None
        if self.registration is not None and now>=decode(self.registration,schema.RoadRegistrationRequest).valid_until_pi_us:self.registration=None
