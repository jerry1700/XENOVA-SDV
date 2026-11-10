"""PC source의 실제 승인 owner를 기존 S↔M sensor/operation/World 통로에 연결한다.

physical proposal/IdentityOwner guard는 그대로 둔다. 이 명시 variant는 cold EMPTY,
SceneNone만 지원한다. 두 NN 완료 endpoint의 순서 차이는 원 proposal E 안에서 기다리며
실제 parser/D/Vehicle 증거 없이 source ACK를 만들지 않는다. MQTT/thread는 만들지 않는다.
"""

from hashlib import sha256
from uuid import uuid4

from ..oak.sim_nn_manifest import SimNnManifest
from ..simulation.pc_input import PcSourceBinding
from ..storage.contracts import canonical, uuid
from .data_channel import DataLink
from .lifecycle_policy import REQUIRED_NN_PAIR_COUNT
from .pc_channel import PcActorLink
from .pc_job_messages import PcParserComplete
from .pc_source_identity import PcSourceIdentityOwner, PcSourceNotReady
from .source_identity import SourcePolicy, _sample
from .source_runtime import MapperSource, SupervisorSource, SourceChanged


def pc_proposal_metadata(*, binding, policy, mapper_instance, bootstrap, vehicle_wire,
                         nonce, map_epoch, frame_epoch):
    """기존 proposal의 exact 필드를 명시 PC descriptor로 채운다. 현재 승인/시각을 만들지 않는다."""
    if type(binding) is not PcSourceBinding or type(policy) is not SourcePolicy:
        raise ValueError('exact original PC descriptor and source policy required')
    for value in (mapper_instance, nonce, map_epoch, frame_epoch): uuid(value)
    if bootstrap.get('status') != 'EMPTY' or bootstrap.get('selected_checkpoint') is not None:
        raise PcSourceNotReady('PC_COMPLETE_RESTORE_NOT_READY')
    uuid(bootstrap['database_uuid'])
    sample = _sample(vehicle_wire)
    if ((sample.vehicle, sample.calibration) != (policy.vehicle, policy.calibration)
            or binding.fields['calibration_profile_sha256'] != policy.calibration_profile_sha256):
        raise ValueError('original PC source policy/local calibration/whole Vehicle bundle differs')
    return dict(proposal_nonce=nonce, database_uuid=bootstrap['database_uuid'], mapper_instance=mapper_instance,
        raw_binding_sha256=binding.sha256, map_epoch=map_epoch, frame_epoch=frame_epoch,
        odom_epoch=sample.odom_epoch, vehicle=sample.vehicle, configuration=policy.configuration,
        calibration=sample.calibration, primitive=policy.primitive,
        calibration_profile_sha256=policy.calibration_profile_sha256,
        vehicle_sample_sha256=sha256(vehicle_wire).hexdigest(), vehicle_sequence=str(sample.sequence),
        pi_host_boot=sample.host_boot, device_session=sample.device_session, gateway_instance=sample.gateway_instance,
        pi_session=sample.pi_session, checkpoint_manifest_sha256=None)


class _PcIdentityFacade:
    """기존 sensor relay의 작은 owner interface다. 승인은 실제 PC source owner만 수행한다."""

    def __init__(self, source_owner, peer, policy, mapper_instance, epochs, revision_floor):
        self.owner, self.peer, self.policy = source_owner, peer, policy
        self.mapper_instance, self.epochs = mapper_instance, epochs
        self._revision = revision_floor

    @property
    def revision(self): return self._revision

    @property
    def binding(self):
        original = self.owner.admission
        return None if original is None else original.publication

    @property
    def binding_sha256(self):
        original = self.owner.admission
        return None if original is None else original.publication_sha256

    def clear_scene(self): return False

    def accept(self, packet, *, bootstrap, vehicle_wire, vehicle_received_us, current_vehicle_wire,
               now_us, restore_material=None):
        if restore_material is not None:
            raise PcSourceNotReady('PC_COMPLETE_RESTORE_NOT_READY')
        if not packet.issued_us <= now_us < packet.expires_us:
            from .health_policy import FrameExpired
            raise FrameExpired('original PC source proposal expired')
        proposal = packet.header['metadata']
        expected = pc_proposal_metadata(binding=self.owner.binding, policy=self.policy,
            mapper_instance=self.mapper_instance, bootstrap=bootstrap, vehicle_wire=vehicle_wire,
            nonce=proposal.get('proposal_nonce'), map_epoch=self.epochs[0], frame_epoch=self.epochs[1])
        if proposal != expected:
            raise ValueError('PC proposal changed original S epochs/Vehicle/bootstrap/policy')
        admission = self.owner.admit(vehicle_wire, vehicle_received_j_us=vehicle_received_us,
            current_vehicle_wire=current_vehicle_wire, now_us=now_us)
        value = admission.publication
        if (admission.material[1] != bootstrap or value['mapper_worker'] != self.peer.mapper_worker
                or any(value[key] != expected[key] for key in ('mapper_instance', 'map_epoch', 'frame_epoch',
                    'odom_epoch', 'raw_binding_sha256', 'configuration', 'calibration', 'primitive'))):
            raise ValueError('actual PC source admission differs from original M source proposal')
        self._revision = int(value['source_binding_revision'])
        return value


class PcSupervisorSource(SupervisorSource):
    """S의 기존 실제 JIO relay를 유지하고 PC 승인만 별도 owner에 위임한다."""

    def __init__(self, link, source_owner, jio, *, mapper_instance, mapper_boot, epochs,
                 revision_floor=0, operation_owner=None, clock):
        if type(source_owner) is not PcSourceIdentityOwner or type(epochs) is not tuple or len(epochs) != 2:
            raise ValueError('actual registered PC source owner and original identity epochs required')
        for value in epochs: uuid(value)
        self._pc_owner, self._pc_epochs = source_owner, epochs
        super().__init__(link, source_owner.binding, jio, mapper_instance=mapper_instance,
            mapper_boot=mapper_boot, clock_epoch=source_owner.binding.fields['destination_clock_epoch'],
            revision_floor=revision_floor, operation_owner=operation_owner, clock=clock)

    def _make_identity_owner(self, raw_binding, mapper_instance, revision_floor):
        if raw_binding.canonical_bytes != self._pc_owner.binding.canonical_bytes:
            raise ValueError('PC source relay descriptor differs from actual S admission owner')
        return _PcIdentityFacade(self._pc_owner, self.link.peer, self.policy, mapper_instance,
            self._pc_epochs, revision_floor)

    def _identity(self, packet, *, bind_ready, on_publication):
        try:
            return super()._identity(packet, bind_ready=bind_ready, on_publication=on_publication)
        except PcSourceNotReady:
            # metadata와 source는 다른 FD다. 먼저 도착한 원 proposal만 원 E 안에서 기다린다.
            if self._early_identity is not None and self._early_identity.raw != packet.raw:
                raise ValueError('one original pending PC identity cannot be replaced')
            self._early_identity = packet
            self.counts['pc_admission_wait'] += 1

    def _survey(self, packet, on_publication, now):
        raise ValueError('PC Scene/parking admission is not connected in the ordinary FOLLOW variant')

    def pump(self, material, **kwargs):
        if self.context is not None and self._pc_owner.admission is None:
            self.jio.fence_source()
            raise SourceChanged('actual PC admission owner was fenced')
        super().pump(material, **kwargs)
        if self.context is not None and self.current_vehicle is not None:
            original = self.vehicles.get(sha256(self.current_vehicle).hexdigest())
            if original is None: raise SourceChanged('current PC source lost its original Vehicle callback')
            try:
                self._pc_owner.admit(original[0], vehicle_received_j_us=original[1],
                    current_vehicle_wire=self.current_vehicle, now_us=self.clock())
            except PcSourceNotReady as error:
                self.jio.set_world_dispatch_enabled(False)
                self.counts['wait_current_input']+=1
                return
            self.jio.set_world_dispatch_enabled(self.operation_owner.world_enabled)


class PcMapperSource(MapperSource):
    """M의 실제 binary source/operation 통로다. NN5 actual full-send 전에는 proposal을 만들지 않는다."""

    def __init__(self, link, binding, policy, *, mapper_instance, device, epochs,
                 metadata_link, manifest, clock):
        if (type(link) is not DataLink or link.role != 'M'
                or type(binding) is not PcSourceBinding or type(policy) is not SourcePolicy
                or type(epochs) is not tuple or len(epochs) != 2
                or type(metadata_link) is not PcActorLink or metadata_link.owner_role != 'M'
                or metadata_link.peer.worker_instance != link.peer.mapper_worker
                or metadata_link.peer.service_instance != link.peer.service_instance
                or metadata_link.binding.canonical_bytes != binding.canonical_bytes
                or type(manifest) is not SimNnManifest or manifest.sha256 != binding.fields['nn_manifest_sha256']):
            raise ValueError('actual PC M source/link, original epochs and NN manifest required')
        for value in epochs: uuid(value)
        self._pc_epochs, self._pc_metadata, self._pc_manifest = epochs, metadata_link, manifest
        self._pc_parsed = []
        self._pc_identity_draft = None
        super().__init__(link, binding, policy, mapper_instance=mapper_instance, device=device, clock=clock)

    def _identity_epochs(self): return self._pc_epochs

    def _binding_clock_epoch(self): return self.raw.fields['destination_clock_epoch']

    def _identity_may_propose(self): return len(self._pc_parsed) == REQUIRED_NN_PAIR_COUNT

    def observe_parser_sent(self, sent):
        """실제 M NN 완료 full-send만 세며 source 승인은 S의 독립 A/FILLED 대조에 남긴다."""
        self._pc_metadata.require_sent(sent)
        report = PcParserComplete(sent.packet, self.raw, self._pc_manifest)
        if len(self._pc_parsed) == REQUIRED_NN_PAIR_COUNT: return
        if report.purpose != 'PARSER_WARMUP':
            raise ValueError('initial PC source needs original parser warmup sends')
        if sent.completed_us>=report.expires_us:return
        if self._pc_parsed:
            previous = self._pc_parsed[-1]
            if (report.nn_key[:3] != previous.nn_key[:3]
                    or any(report.nn_key[index] <= previous.nn_key[index] for index in (3, 4, 5))
                    or report.completed_j_us < previous.completed_j_us):
                raise ValueError('PC source parser warmup did not advance its original NN key')
        self._pc_parsed.append(report)

    def _send_identity_proposal(self, bootstrap, now):
        if self._pc_identity_draft is None:
            value = pc_proposal_metadata(binding=self.raw, policy=self.policy, mapper_instance=self.mapper_instance,
                bootstrap=bootstrap, vehicle_wire=self._vehicle_head, nonce=str(uuid4()),
                map_epoch=self.map_epoch, frame_epoch=self.frame_epoch)
            self._pc_identity_draft = (value, str(uuid4()), now, now + 5_000_000)
        proposal, request, issued, expires = self._pc_identity_draft
        if now >= expires:
            self._pc_identity_draft=None
            return
        self._proposal = proposal
        self._proposal_packet = self.link.send('SOURCE_IDENTITY', proposal, request_id=request,
            issued_us=issued, expires_us=expires)
        self._flush()
