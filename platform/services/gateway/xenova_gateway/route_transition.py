"""관측 window 전환의 단일 PREPARE/ACTIVATE/COMMIT/ABORT RAM owner.

M의 실제 newer World witness와 C의 현재 재검증을 결박한다. PREPARE로 World HWM을
움직이거나 receipt를 DRIVE/실제 pair commit으로 취급하지 않는다. 원문 전달만 수행한다.
"""
from copy import deepcopy

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.route import RouteReason,route_transition_request_sha256
from .route_policy import RouteError,require,ZERO
from .route_owner import peer_key
from .route_episode import inactive_episode_history


class RouteTransitionOwner:
    def __init__(self,route):
        self.route=route;self.data=route.data;self.current=None;self.last=None;self.terminal=None
        self.pending_receipt=None

    def request(self,raw,peer):
        value=decode(raw,schema.WindowTransitionRequest);digest=route_transition_request_sha256(raw)
        for saved in (self.last,self.terminal):
            if saved is not None and saved['nonce']==value.request_nonce:
                require(saved['sha']==digest,RouteReason.HASH_OR_BODY_MISMATCH)
                return saved.get('reply')
        route=self.route;registry=route.registry
        require(registry is not None and not route.failed and registry['snapshot'].state==2,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        route._current_grant(peer);route._witness(value.owner,value.witness,peer)
        current_world=self.data.world(value.witness.world_raw_sha256,route.now)
        require(value.expected_source_binding_revision==current_world.mapper.source_binding_revision,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        query=route.query
        require(query is not None and value.query_id==query['body'].query_id and value.query_sha256==query['sha']
            and value.route_id==registry['snapshot'].core.route_id and value.expected_registry_epoch==route.epoch
            and value.expected_registry_revision==route.revision
            and value.phase_revision==registry['snapshot'].core.phase_revision,RouteReason.REQUEST_CONTEXT_CHANGED)
        if value.action==1:
            require(self.current is None or self.current['state'] in (3,4,5),RouteReason.POOL_BUSY)
            inactive_episode_history(route.o,value.witness.source)
            before=self.data.get(5,value.from_window_sha256).message()
            after=self.data.get(5,value.to_window_sha256).message();count=after.overlap_point_count
            require(value.from_window_sha256==registry['snapshot'].current_window_sha256
                and after.from_window_sha256==value.from_window_sha256 and after.from_window_id==before.window_id
                and after.transition_id==value.transition_id and before.geometry_scope_sha256==after.geometry_scope_sha256
                and after.overlap_prefix_sha256!=ZERO and 2<=count<=32,RouteReason.GEOMETRY_CONFLICT)
            # Shared original F32 seam, with no snapping or independent reprojection.
            for name in ('reference','left_boundary','right_boundary'):
                a=getattr(before,name);b=getattr(after,name)
                require(len(a)>=count and len(b)>=count
                    and all(encode(x)==encode(y) for x,y in zip(a[-count:],b[:count])),RouteReason.GEOMETRY_CONFLICT)
            route._extent(value.extent,registry['snapshot'].task_space_kind,value.to_window_sha256,
                registry['snapshot'].exit_space_sha256,value.witness.vehicle_sequence,full=True)
            require(value.from_world_sha256==value.witness.world_raw_sha256,RouteReason.HASH_OR_BODY_MISMATCH)
            original_world=self.data.world(value.from_world_sha256,route.now)
            self.current=dict(id=bytes(value.transition_id),state=0,request=bytes(raw),sha=digest,
                nonce=bytes(value.request_nonce),from_window=bytes(value.from_window_sha256),
                to_window=bytes(value.to_window_sha256),generation=None,reply=None,
                from_world=bytes(value.from_world_sha256),from_sequence=original_world.source.world_sequence,
                from_observation=original_world.source.observation_sequence)
        else:
            require(self.current is not None and self.current['id']==value.transition_id
                and self.current['from_window']==value.from_window_sha256 and self.current['to_window']==value.to_window_sha256,
                RouteReason.REQUEST_CONTEXT_CHANGED)
            require(value.from_world_sha256==self.current['from_world'],RouteReason.HASH_OR_BODY_MISMATCH)
            if value.action==2:
                require(self.current['state']==1 and registry['state'] is not None,RouteReason.ACTIVATION_NOT_READY)
                inactive_episode_history(route.o,value.witness.source)
            elif value.action==5:
                require(self.current['state']==2 and registry['state'] is not None
                    and registry['snapshot'].current_window_sha256==value.to_window_sha256,
                    RouteReason.ACTIVATION_NOT_READY)
            else:require(value.action in (3,4),RouteReason.REQUEST_CONTEXT_CHANGED)
            self.current.update(request=bytes(raw),sha=digest,nonce=bytes(value.request_nonce),reply=None)
        self.last=dict(nonce=bytes(value.request_nonce),sha=digest,reply=None)
        return bytes(raw)  # Existing authenticated management transport forwards exact original bytes.

    def receive_receipt(self,raw,mapper_peer):
        value=decode(raw,schema.WindowTransitionReceipt);row=self.current
        require(row is not None and value.request_nonce==row['nonce'] and value.request_sha256==row['sha']
            and value.transition_id==row['id'] and value.from_window_sha256==row['from_window']
            and value.to_window_sha256==row['to_window'],RouteReason.HASH_OR_BODY_MISMATCH)
        require(self.data.mapper_current(value.mapper,mapper_peer,self.route.now)
            and self.route.now<value.valid_until_pi_us,RouteReason.CLOCK_OR_EXPIRY)
        request=decode(row['request'],schema.WindowTransitionRequest)
        self.route._current(request.owner,peer_key(request.owner))
        allowed={1:(1,5),2:(2,4,5),3:(4,5),4:(0,1,2,3,4,5),5:(3,4,5)}
        require(value.state in allowed[request.action],RouteReason.REQUEST_CONTEXT_CHANGED)
        if row['generation'] is None:
            require(value.transition_generation>0,RouteReason.REQUEST_CONTEXT_CHANGED)
            row['generation']=value.transition_generation  # Actual mapper-owned transition generation.
        else:require(value.transition_generation==row['generation'],RouteReason.REQUEST_CONTEXT_CHANGED)
        if value.state in (2,3):
            require(value.active_window_sha256==row['to_window'],RouteReason.GEOMETRY_CONFLICT)
            try:world=self.data.world(value.activated_world_sha256,self.route.now)
            except RouteError:
                self.pending_receipt=(bytes(raw),mapper_peer);return None
            require(world.source.world_sequence>row['from_sequence']
                and world.source.observation_sequence>=row['from_observation']
                and value.activated_world_sequence==world.source.world_sequence
                and encode(value.mapper)==encode(world.mapper)
                and value.source_binding_revision>=request.expected_source_binding_revision,
                RouteReason.ACTIVATION_NOT_READY)
            published=self.data.world_snapshot(value.activated_world_sha256,self.route.now)
            require(published.route_view.window_sha256==row['to_window']
                and published.route_view.from_window_sha256==row['from_window']
                and published.route_view.transition_id==row['id'],RouteReason.GEOMETRY_CONFLICT)
        row['state']=value.state;row['reply']=bytes(raw)
        self.last=dict(nonce=row['nonce'],sha=row['sha'],reply=bytes(raw));self.pending_receipt=None
        if value.state in (3,4,5):self.terminal=deepcopy(self.last)
        return bytes(raw)

    def tick(self):
        if self.pending_receipt is None:return
        raw,peer=self.pending_receipt;value=decode(raw,schema.WindowTransitionReceipt)
        if self.route.now>=value.valid_until_pi_us:
            self.pending_receipt=None;self.route.cancel(RouteReason.ACTIVATION_NOT_READY);return
        return self.receive_receipt(raw,peer)
