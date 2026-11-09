"""명시 PC 관측 A/S/M/D를 기존 S 자원·출생·종료 owner에 접합한다.

physical RawBinding/WorkerBoot와 capture30 준비를 사용하지 않는다. 실제 A NN start,
등록 M parser5, 실제 D EMPTY bootstrap으로 source를 승인하며 Scene/COMPLETE/parking
복원과 worker 교체는 미지원으로 닫는다. 입력 소켓·tools loop factory는 caller가 넘긴다.
이 모듈은 listen/reconnect·MQTT 자격 설정·clock/metric/주행 READY를 만들지 않는다.
"""

from collections import deque
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
import selectors
import signal
import socket
import threading

from ..oak.sim_nn import SimNnProfile
from ..oak.sim_nn_manifest import SimNnManifest
from ..oak.yolop_asset import load_yolop_asset
from ..perception.calibration import canonical as calibration_canonical
from ..simulation.pc_input import PcSourceBinding
from .channel import control_pair
from .data_channel import DataLink, DataPeer, data_pair
from .lifecycle_messages import LifecyclePeer
from .lifecycle_policy import AbsoluteDeadline, TIMING_US, REQUIRED_NN_PAIR_COUNT
from .pc_binding_messages import PcBindContext, PcBindMessage, PcBindAck
from .pc_channel import PcActorLink, pc_actor_pair, forward_received
from .pc_raw_broker import PcRawBroker
from .pc_resident_profile import PcResidentProfile
from .pc_source_identity import PcSourceIdentityOwner
from .pc_source_runtime import PcSupervisorSource
from .pc_workers import PcWorkerBoot, pc_worker_main, pc_worker_release
from .raw_leases import Endpoint, RawScope, SLOT_BYTES
from .resident_profile import canonical, publish_runtime_artifacts, release_sha256
from .resident_recovery import ResidentRecovery
from .resident_runtime import _Runtime, _identifier, _redirect_worker_output
from .resident_storage import StorageProfile
from .resident_transport import j_us


@dataclass(frozen=True)
class _PcLaunch:
    boot: PcWorkerBoot
    loop: object


def _pc_worker_entry(endpoint, launch):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _redirect_worker_output()
    pc_worker_main(endpoint, launch.boot, launch.loop)


def _observation_scope(binding):
    fields = binding.fields
    return json.dumps(dict(source_id=fields['source_id'], session_id=fields['jetson_session'],
        source_descriptor_sha256=binding.sha256, calibration_sha256=fields['calibration_profile_sha256'],
        motion_bundle_sha256=fields['motion_profile_sha256'], pc_clock_epoch=fields['pc_clock_epoch']),
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


class _PcRecovery(ResidentRecovery):
    """미구현 PC 복원을 physical recovery로 캐스팅하지 않는다. 원 STOP/reap는 재사용한다."""

    def fail(self, role, reason, retryable=True):
        if self.r.stopping:
            return False
        self.r._fence_pc('PC_'+role+'_FAILED')
        self.r.event('PC_RECOVERY_NOT_SUPPORTED', role=role, reason=str(reason)[:320])
        self.r.stop('pc_worker_failed', RuntimeError(str(reason)))
        return False

    def healthy(self):
        return False


class PcResidentRuntime(_Runtime):
    """실제 S-main 한 owner다. factory는 배포된 tools의 명시 A/M loop 생성자다.

    capture_clock_policy는 profile에 보존한 exact 다섯 필드와 같아야 한다. src에서 tools를
    import하지 않으며 실제 loop 생성자가 해당 tools exacttype을 다시 검사한다. caller는
    connected observation_endpoint를 넘긴 뒤 별도 사용하지 않는다. 이 owner가 닫는다.
    """

    def __init__(self, profile, state_directory, storage_profile, output, *, observation_endpoint,
                 acquisition_loop_factory, mapper_loop_factory, capture_clock_policy,
                 duration_s=None, service_scope=None, output_root=False):
        if type(profile) is not PcResidentProfile or type(storage_profile) is not StorageProfile:
            raise ValueError('explicit typed PC installation/storage profile required')
        if (type(observation_endpoint) is not socket.socket
                or observation_endpoint.type != socket.SOCK_STREAM
                or observation_endpoint.family not in (socket.AF_INET, socket.AF_UNIX)
                or not callable(acquisition_loop_factory) or not callable(mapper_loop_factory)):
            raise ValueError('one caller-connected stream socket and explicit A/M factories required')
        observation_endpoint.getpeername()
        if asdict(capture_clock_policy) != dict(profile.capture_clock_fields):
            raise ValueError('actual PC capture clock policy differs from original installation fields')
        if duration_s is not None and (type(duration_s) not in (int, float) or not 0.1 <= duration_s <= 300):
            raise ValueError('optional original resident duration must be in0.1..300seconds')
        self.pc_profile = profile
        self._observation_endpoint = observation_endpoint
        self._acquisition_factory, self._mapper_factory = acquisition_loop_factory, mapper_loop_factory
        self._capture_clock_policy = capture_clock_policy
        self._pc_links, self._pc_children, self._pc_peers = {}, {}, {}
        self._pc_relay = dict(A=deque(), M=deque())
        self._pc_owner = self._pc_manifest = self._pc_nn_profile = None
        self._pc_start = None
        self._pc_binds_observed = set()
        self._pc_storage_observed = False
        self._pc_source_state = None
        self._pc_storage_result = self._pc_storage_received = self._pc_discover_sent = None
        self._pc_storage_ack_received = None
        self._pc_bootstrap_ready_us = None
        self._pc_mapper_instance = _identifier()
        self._pc_epochs = (_identifier(), _identifier())
        super().__init__(profile.oak, state_directory, storage_profile, output, duration_s,
            service_scope, output_root,
            perception_profile_bytes=calibration_canonical(profile.motion.bound_calibration.to_dict()),
            jio_profile=profile.jio, mapper_profile_bytes=profile.mapper.canonical_bytes)
        self.recovery = _PcRecovery(self)

    def _prepare(self):
        """S-log에서 실제 파일 closure를 한 번 고정한다. Device나 물리 graph는 만들지 않는다."""
        self.output.mkdir(mode=0o700)
        self.output_owned = True
        profile = self.pc_profile
        nn = SimNnProfile(profile.oak, 'sim-pc:'+profile.source_id,
            'jetson-monotonic:'+self.clock_epoch, profile.motion.bound_calibration.sha256)
        manifest = SimNnManifest.from_current(nn,
            load_yolop_asset(profile.oak.asset_dir, profile.oak.asset_manifest))
        binding = PcSourceBinding(dict(profile='development-pc-source-1', service_instance=self.service,
            source_id=profile.source_id, pc_clock_epoch=profile.pc_clock_epoch,
            jetson_session=profile.jetson_session, destination_clock_epoch=self.clock_epoch,
            stream_instances=dict(rgb_video=profile.rgb_stream, depth_right=profile.depth_stream),
            calibration_profile_sha256=profile.motion.bound_calibration.sha256,
            motion_profile_sha256=profile.motion.sha256, nn_manifest_sha256=manifest.sha256))
        binding.check_profile(profile.motion)
        from .pc_slot import HEADER_BYTES, METADATA_BYTES, MAX_PAYLOAD_BYTES, PROFILE, MAGIC
        formats = canonical(dict(profile=PROFILE, magic_hex=MAGIC.hex(), slot_bytes=SLOT_BYTES,
            header_bytes=HEADER_BYTES, metadata_bytes=METADATA_BYTES,
            max_payload_bytes=MAX_PAYLOAD_BYTES, source_scope='PC_OWNED_NOT_PHYSICAL_RAW'))
        manifests = {'pc-installation.json': profile.canonical_bytes,
            'pc-nn-manifest.json': manifest.canonical_bytes,
            'pc-source-binding.json': binding.canonical_bytes,
            'pc-observation-scope.json': _observation_scope(binding),
            'pc-format-manifest.json': formats,
            'perception-calibration.json': self.perception_profile_bytes,
            'mapper-profile.json': profile.mapper.canonical_bytes,
            'development-motion.json': profile.motion.canonical_bytes}
        self._pc_manifest, self._pc_nn_profile = manifest, nn
        # D의 기존 package-only 검사와 A/M의 tools 포함 검사를 구분한다.
        artifacts = dict(release_sha256=release_sha256(), pc_worker_release_sha256=pc_worker_release(),
            model_id=manifest.fields['asset']['blob_sha256'],
            parser_id=sha256(json.dumps(manifest.fields['parser'], sort_keys=True,
                separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest(),
            graph_profile_sha256=manifest.sha256, format_table_sha256=sha256(formats).hexdigest(),
            calibration_bundle=profile.motion.bound_calibration.sha256,
            mapper_profile_sha256=profile.mapper.sha256, manifests=manifests)
        artifacts['published'] = publish_runtime_artifacts(self.output/'artifacts', artifacts)
        return artifacts

    def _configure_binding(self):
        self.binding = PcSourceBinding.from_bytes(self.artifacts['manifests']['pc-source-binding.json'])
        self.binding.check_profile(self.pc_profile.motion)
        self.scope = RawScope(self.service, self.handles.raw.arena_id,
            Endpoint(self.identities['A'][1], self.identities['A'][0]), self.binding.sha256,
            self.handles.raw.name, self.handles.raw.size_bytes)
        for role in ('A', 'M'):
            worker, channel = self.identities[role]
            peer = LifecyclePeer(self.service, channel, worker, role, self.artifacts['pc_worker_release_sha256'])
            parent, child = pc_actor_pair()
            self._pc_peers[role] = peer
            self._pc_children[role] = child
            self._pc_links[role] = PcActorLink(parent, peer, 'S', self.binding, clock=j_us)
            # model/slot 제어와 다른 FD도 S를 깨운다. 항상 쓰기 가능인 EVENT_WRITE는 등록하지 않는다.
            self.selector.register(parent, selectors.EVENT_READ, ('pc-metadata', role))

    @property
    def observation_scope_bytes(self):
        """S가 고정한 원 scope다. caller는 원 descriptor와 함께 PC producer에 전달한다."""
        if self.binding is None:
            return None
        return _observation_scope(self.binding)

    def _spawn(self, role, *, deadline_us=None):
        if role == 'D':
            return super()._spawn(role, deadline_us=deadline_us)
        if role not in ('A', 'M') or role in self.workers:
            raise ValueError('PC runtime supports exactly one original A and M')
        deadline = self.recovery.operation_cap if deadline_us is None else deadline_us
        peer = self._pc_peers[role]
        parent = child = data_parent = data_child = None
        handed = False
        metadata_child = self._pc_children[role]
        try:
            parent, child = control_pair()
            for endpoint in (parent, child):
                endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            if role == 'M':
                data_parent, data_child = data_pair()
                data_peer = DataPeer(self.service, peer.worker_instance, _identifier())
            boot = PcWorkerBoot(peer, self._pc_peers['A'], self._pc_nn_profile, self.scope,
                self.handles.raw, self.binding.canonical_bytes, self._pc_manifest.canonical_bytes,
                self.handles.raw_tokens, deadline, str(self.output/role/peer.worker_instance),
                writer_bind_raw=self.workers['A'].bind.raw if role == 'M' else None)
            if role == 'A':
                loop = self._acquisition_factory(observation_endpoint=self._observation_endpoint,
                    metadata_endpoint=metadata_child, mapper_peer=self._pc_peers['M'],
                    scope_bytes=self.observation_scope_bytes, clock_policy=self._capture_clock_policy,
                    duration_s=self.pc_profile.observation_duration_s,
                    probe_interval_us=self.pc_profile.probe_interval_us, slot_limits=self.pc_profile.slot_limits)
            else:
                loop = self._mapper_factory(metadata_endpoint=metadata_child, data_endpoint=data_child,
                    data_peer=data_peer, copy_handles=(self.handles.db_copy, self.handles.db_tokens),
                    motion_profile=self.pc_profile.motion, mapper_profile_bytes=self.mapper_profile_bytes,
                    source_policy=self.jio_profile.source_policy, mapper_instance=self._pc_mapper_instance,
                    device=self.jio_profile.device, epochs=self._pc_epochs,
                    capture_clock_policy=self._capture_clock_policy,
                    slot_limits=self.broker.limits, cleanup_limits=self.broker.cleanup_limits)
            if not callable(loop):
                raise ValueError('explicit PC loop factory must return its callable settings')
            extras = (metadata_child, data_child, self._observation_endpoint if role == 'A' else None)
            handed = True
            host = self._start_worker(role, peer, parent, child, _PcLaunch(boot, loop),
                deadline_us=deadline, entry=_pc_worker_entry, source_endpoint=data_parent,
                extra_child_endpoints=extras, extra_parent_endpoints=(self._pc_links[role].endpoint,))
            self._pc_children.pop(role)
            if role == 'A':
                self._observation_endpoint = None
            else:
                self._create_source_owner(host, data_parent, data_peer)
            return host
        except BaseException:
            if not handed:
                for endpoint in (parent, child, data_parent, data_child):
                    if endpoint is not None:
                        endpoint.close()
            elif role == 'M' and data_parent is not None and self.workers.get('M', None) is not None:
                if self.workers['M'].source is None:
                    data_parent.close()
            raise

    def _create_source_owner(self, mapper, data_endpoint, data_peer):
        acquisition, storage = self.workers['A'], self.workers['D']
        self._pc_owner = PcSourceIdentityOwner(self._pc_links['A'], self._pc_links['M'], self.binding,
            self.jio_profile.source_policy, self.pc_profile.motion, self._pc_manifest, self.broker,
            acquisition_receiver=acquisition.receiver, mapper_receiver=mapper.receiver,
            storage_receiver=storage.receiver, storage_peer=storage.peer, storage_profile=self.storage_profile,
            mapper_instance=self._pc_mapper_instance, mapper_boot=self.manifest.runtime.boot_id,
            device=self.jio_profile.device, map_epoch=self._pc_epochs[0], frame_epoch=self._pc_epochs[1],
            startup_deadline_us=self.startup_deadline, revision_floor=self.source_revision)
        mapper.source = PcSupervisorSource(DataLink(data_endpoint, data_peer, 'S'), self._pc_owner, self.jio,
            mapper_instance=self._pc_mapper_instance, mapper_boot=self.manifest.runtime.boot_id,
            epochs=self._pc_epochs, revision_floor=self.source_revision,
            operation_owner=self.operation_owner, clock=j_us)
        self._observe_bootstrap_facts(j_us())

    def _sent(self, host):
        # base의 원 successful-send 소비를 보존하고 D DISCOVER 원 ticket만 추가로 보관한다.
        for sent in host.link.take_sent():
            self.recovery.sent(host, sent)
            draft = self.db_selected.pop((host.peer.channel_epoch, sent.packet.fields['message_sequence']), None)
            if draft is not None:
                self.db_broker.note_sent(draft, sent.packet, sent.completed_us)
            if sent.packet.kind == 'BIND' and sent.packet.fields['binding_domain'] != 'PUBLICATION':
                host.bind_sent = sent
            elif sent.packet.kind == 'STOP':
                host.receiver.record_first_sent(sent.packet, sent.started_us, now_us=j_us())
            elif (host.peer.role == 'D' and host.discovery is sent.packet):
                self._pc_discover_sent = sent

    def _bind(self, role):
        if role == 'D':
            return super()._bind(role)
        host = self.workers[role]
        if not host.hello or host.bind is not None or not host.admitted or self.stopping:
            return
        if role == 'M' and (self.workers['A'].bind_ack is None or self._pc_start is None):
            return
        if host.bind_draft is None:
            now = j_us()
            host.bind_draft = dict(kind='BIND', payload=dict(binding_revision='1',
                binding_sha256=self.binding.sha256, binding=self.binding.fields), domain='RAW', revision=1,
                request_id=_identifier(), issued_us=now, expires_us=min(now+5000000, host.startup_deadline_us))
        packet = host.link.send(**host.bind_draft)
        if packet is None:
            return
        host.bind = PcBindMessage(packet, PcBindContext(host.peer, self.scope.writer,
            self.binding, self.scope, self.handles.raw), cap_us=host.startup_deadline_us)
        if role == 'A':
            self.broker = PcRawBroker(host.bind, self.scope,
                Endpoint(self.identities['M'][1], self.identities['M'][0]), self.handles.raw_tokens,
                limits=self.pc_profile.slot_limits, cleanup_limits=self.pc_profile.cleanup_limits)

    def _dispatch(self, role, packet):
        host, now = self.workers[role], j_us()
        if (role in ('A', 'M') and packet.kind == 'BIND_ACK'
                and packet.fields['binding_domain'] == 'RAW' and not self.stopping):
            if not host.admitted:
                if len(host.held) == 8:
                    raise RuntimeError('pre-admission PC control mailbox is full')
                host.held.append(packet)
                return
            if not host.hello or host.bind is None or host.bind_sent is None:
                raise RuntimeError('PC BIND ACK requires actual current HELLO and original fullsend')
            ack = PcBindAck(packet, host.bind)
            if not ack.accepted or not host.bind_sent.started_us <= ack.issued_us <= now < ack.expires_us:
                raise ValueError('PC BIND ACK rejected or outside original send/lifetime')
            if host.bind_ack is not None and host.bind_ack.raw != ack.raw:
                raise ValueError('one original PC BIND ACK cannot change')
            host.bind_ack, host.pc_bind_received_us = ack, now
            if role == 'M':
                self.broker.bind_reader(host.bind)
            self.event('BIND_ACK', role=role)
            self._observe_bootstrap_facts(now)
            return
        super()._dispatch(role, packet)
        if self.stopping:
            return
        if role == 'D' and packet.kind == 'DB_JOB' and host.bootstrap is not None and self._pc_storage_result is None:
            self._pc_storage_result, self._pc_storage_received = packet, now
            if host.bootstrap['status'] != 'EMPTY':
                raise RuntimeError('PC_COMPLETE_RESTORE_NOT_READY')
        elif role == 'D' and packet.kind == 'BIND_ACK' and packet.fields['binding_domain'] == 'STORAGE':
            self._pc_storage_ack_received = now
        self._observe_bootstrap_facts(now)
        if packet.kind == 'SLOT':
            self._drain_filled()

    def _observe_bootstrap_facts(self, now):
        owner = self._pc_owner
        if owner is None or self.stopping:
            return
        for role in ('A', 'M'):
            host = self.workers[role]
            if role not in self._pc_binds_observed and host.bind_ack is not None:
                owner.observe_bind(role, host.bind_ack, host.bind_sent,
                    received_j_us=host.pc_bind_received_us, now_us=now)
                self._pc_binds_observed.add(role)
        storage = self.workers['D']
        if (not self._pc_storage_observed and storage.bind_ack is not None
                and self._pc_discover_sent is not None and self._pc_storage_result is not None):
            owner.observe_storage(self._pc_discover_sent, self._pc_storage_result,
                storage.bind_sent, storage.bind_ack, result_received_j_us=self._pc_storage_received,
                ack_received_j_us=self._pc_storage_ack_received, now_us=now)
            self._pc_storage_observed = True
        if self._pc_start is not None and 'M' in self._pc_binds_observed and owner.parser_deadline is None:
            anchor = max(self._pc_start.packet.issued_us, self.workers['M'].bind_ack.issued_us)
            deadline = AbsoluteDeadline(anchor, min(anchor+TIMING_US['warmup'], self.startup_deadline))
            owner.install_parser_deadline(deadline)
            self.parser_deadline = deadline.expires_us

    def _drain_filled(self):
        if self.broker is None or self._pc_owner is None or self.stopping:
            return
        for _ in range(2):
            value = self.broker.take_filled_metadata()
            if value is None:
                break
            if value.phase == 'NN':
                self._pc_owner.observe_nn_result(value, now_us=j_us())

    def _queue_forward(self, received, role):
        queue = self._pc_relay[role]
        if len(queue) >= 8:
            raise RuntimeError('bounded original PC relay mailbox is full')
        queue.append(('FORWARD', received))

    def _pc_message(self, role, received):
        owner, link, now = self._pc_owner, self._pc_links[role], j_us()
        link.require_received(received)
        if owner is None or now >= received.packet.expires_us:
            raise TimeoutError('PC actor message has no current original source owner/lifetime')
        packet = received.packet
        if packet.kind == 'PC_SOURCE_FENCE':
            raise RuntimeError('PC_'+role+'_SOURCE_FENCE')
        if role == 'A' and packet.kind == 'PC_NN_START':
            owner.observe_nn_start(received, now_us=now)
            self._pc_start = received
            self.a_phase = 'WARMUP'
            self._observe_bootstrap_facts(now)
        elif role == 'A' and packet.kind in ('PC_EARLY_META', 'PC_NN_RESULT_META'):
            self.broker.observe_metadata(received, origin_link=link, now_us=now)
            self._drain_filled()
            self._queue_forward(received, 'M')
        elif role == 'M' and packet.kind == 'PC_PARSE_COMPLETE':
            owner.observe_parse_complete(received, now_us=now)
            self._queue_forward(received, 'A')
        elif role == 'M' and packet.kind == 'PC_SNAPSHOT_ACK':
            self._queue_forward(received, 'A')
        else:
            raise ValueError('unsupported actual PC actor statement')

    def _drive_pc(self, *, receive=True):
        if self.stopping:
            return
        for role, link in tuple(self._pc_links.items()):
            host = self.workers.get(role)
            if host is None or not host.admitted or not host.hello:
                continue
            if receive and self._pc_owner is not None:
                for received in link.poll():
                    self._pc_message(role, received)
            self._flush_pc_relay(link)
            queue = self._pc_relay[role]
            while queue and queue[0][0] == 'FORWARD':
                original = queue[0][1]
                if j_us() < original.packet.expires_us:break
                other = 'M' if role == 'A' else 'A'
                self._pc_links[other].require_received(original)
                if original.packet.kind not in ('PC_EARLY_META', 'PC_NN_RESULT_META', 'PC_SNAPSHOT_ACK', 'PC_PARSE_COMPLETE'):
                    raise TimeoutError('expired non-frame PC relay cannot be retired')
                queue.popleft();link.counts['expired_relay_before_selection'] += 1
            if queue and link.pending is None:
                kind, original = queue[0]
                if kind == 'FORWARD':
                    other = 'M' if role == 'A' else 'A'
                    packet = forward_received(original, link, origin_link=self._pc_links[other])
                else:
                    packet = link.send(**original)
                if packet is not None:
                    queue.popleft()
                    self._flush_pc_relay(link)

    @staticmethod
    def _flush_pc_relay(link):
        """원 frame relay만 E 뒤 폐기한다. Source state와 unknown 송신은 원 실패를 보존한다."""
        try:return link.flush()
        except TimeoutError as error:
            packet = link.pending
            if packet is None or packet.kind != 'PC_FORWARD':raise
            try:link.discard_expired_work(packet)
            except ValueError:raise error
            return None

    def _drive_workers(self):
        self._drive_pc(receive=False)
        super()._drive_workers()
        self._drain_filled()
        self._drive_pc()

    def set_publication(self, binding, storage_context):
        original = None if self._pc_owner is None else self._pc_owner.admission
        if (original is None or binding != original.publication
                or storage_context.sha256 != original.storage_context.sha256):
            raise RuntimeError('PC publication requires the actual original S admission/context')
        super().set_publication(binding, storage_context)
        self.broker.set_publication(original.publication_bytes)
        if self._pc_source_state != original.publication_sha256:
            now = j_us()
            draft = dict(kind='PC_SOURCE_STATE', metadata=dict(profile='pc-source-state-1', state='BOUND',
                publication_sha256=original.publication_sha256), body=original.publication_bytes,
                request_id=_identifier(), issued_us=now, expires_us=now+5000000)
            if len(self._pc_relay['A']) >= 8:
                raise RuntimeError('bounded PC source-state relay is full')
            self._pc_relay['A'].append(('STATE', draft))
            self._pc_source_state = original.publication_sha256

    def _a_stage(self, state, event_us):
        # PC A 모델 기동은 기존 원 asset/Device/graph phase만 감시한다. capture30은 만들지 않는다.
        if state in ('ASSET_CHECK', 'DEVICE_BOOT', 'PIPELINE_START'):
            super()._a_stage(state, event_us)
        elif state in ('WARMUP', 'STREAMING'):
            self.a_phase = state

    def _progress(self, role, packet, now):
        if role == 'A' and packet.payload.get('stage') in ('CAPTURE_PAIR', 'CAPTURE_READY'):
            raise ValueError('PC actor cannot claim physical capture readiness')
        super()._progress(role, packet, now)

    def _readiness_timers(self, now):
        if self.a_phase in self.a_phase_deadlines and now >= self.a_phase_deadlines[self.a_phase]:
            self.recovery.fail('A', 'original PC A '+self.a_phase+' budget elapsed')
        host = self.workers.get('A')
        if (self._pc_start is not None and host is not None and host.last_heartbeat_us is not None
                and now-host.last_heartbeat_us >= 1000000):
            self.recovery.fail('A', 'actual PC A heartbeat stalled')
        if self.parser_deadline is not None and self.nn_ready_us is None and now >= self.parser_deadline:
            self.recovery.fail('M', 'original PC parser5 readiness deadline elapsed')
        if (self.nn_ready_us is not None and self.last_fresh_parser_us is not None
                and now-self.last_fresh_parser_us >= 5000000):
            self.recovery.fail('M', 'actual PC parser progress stalled')
        admission = None if self._pc_owner is None else self._pc_owner.admission
        if (self._pc_start is not None and self._pc_owner is not None
                and self._pc_owner.warmup_count == REQUIRED_NN_PAIR_COUNT
                and self.nn_ready_us is not None and self.storage_ready_us is not None and admission is not None):
            if self.resident_started_us is None:
                self.resident_started_us = now
                self._pc_bootstrap_ready_us = now
                self.event('PC_RESIDENT_SOURCE_BOUND', physical_capture_ready=False,
                    metric_ready=False, world_valid=False)
            if self.duration_s is not None and now-self.resident_started_us >= int(self.duration_s*1000000):
                self.stop('duration_elapsed')

    def runtime_status(self, now):
        # mapper-runtime-1의 physical raw13 projection에 가짜 pipeline/SDK field를 넣지 않는다.
        return None

    def _fence_pc(self, reason):
        for link in self._pc_links.values():
            try:self.selector.unregister(link.endpoint)
            except (KeyError, ValueError):pass
        if self._pc_owner is not None:
            self._pc_owner.fence(reason)
        if self.broker is not None:
            self.broker.set_publication(None)
        if self.jio is not None:
            self.jio.set_world_dispatch_enabled(False)

    def stop(self, reason, error=None):
        self._fence_pc('PC_STOP')
        super().stop(reason, error)

    def run(self, signal_value):
        try:
            result = super().run(signal_value)
        finally:
            for endpoint in tuple(self._pc_children.values()):
                endpoint.close()
            for link in tuple(self._pc_links.values()):
                link.close()
            if self._observation_endpoint is not None:
                self._observation_endpoint.close()
                self._observation_endpoint = None
        result['scope'] = 'PC_SYNTHETIC_COLD_SOURCE_ORDINARY_FOLLOW_NOT_PHYSICAL_READY'
        result['raw_binding'] = None
        result['pc_source'] = dict(descriptor_sha256=None if self.binding is None else self.binding.sha256,
            descriptor=None if self.binding is None else self.binding.fields,
            worker_release_sha256=None if self.artifacts is None else self.artifacts['pc_worker_release_sha256'],
            observation_scope=None if self.binding is None else json.loads(self.observation_scope_bytes),
            parser_pairs=0 if self._pc_owner is None else self._pc_owner.warmup_count,
            source_bound_observed=self._pc_bootstrap_ready_us is not None,
            physical_capture_ready=False, complete_restore_supported=False, scene_supported=False,
            metadata_channels={role: dict(link.counts) for role, link in self._pc_links.items()})
        return result


def run_pc_runtime(profile, state_directory, storage_profile, output, *, observation_endpoint,
                   acquisition_loop_factory, mapper_loop_factory, capture_clock_policy,
                   duration_s=None, service_scope=None, output_root=False):
    """명시 연결 한 개의 실행 entry다. caller는 연결·자산·private JIO profile을 준비한다.

    기존 S와 같은 원 signal/환경/종료 회수를 사용한다. API 호출이나 결과의 source-bound는
    actual PC↔Jetson/HIL·NN 정확도·metric/World·주행 수락을 뜻하지 않는다.
    """
    runtime = PcResidentRuntime(profile, state_directory, storage_profile, output,
        observation_endpoint=observation_endpoint, acquisition_loop_factory=acquisition_loop_factory,
        mapper_loop_factory=mapper_loop_factory, capture_clock_policy=capture_clock_policy,
        duration_s=duration_s, service_scope=service_scope, output_root=output_root)
    names = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')
    previous_environment = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = '1'
    previous_signals, observed = {}, []
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous_signals[number] = signal.signal(number,
                lambda number, _frame: observed.append(number) if not observed else None)
    try:
        result = runtime.run(lambda: bool(observed))
    finally:
        for number, handler in previous_signals.items():
            signal.signal(number, handler)
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if runtime.output_owned:
        with (runtime.output/'runtime-result.json').open('xb') as destination:
            destination.write(canonical(result))
    return result
