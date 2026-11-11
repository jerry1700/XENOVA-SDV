"""route 관리 자료의 구조·원본 hash. 현재 권위·기한·실제 출력은 발급하지 않는다.

원 source/clock/geometry/D receipt와 현재 actor CAS는 소비 owner가 재검증한다.
legacy fingerprint와 generic preflight는 이 모듈에서 재해석하지 않는다.
"""
from enum import IntEnum
from hashlib import sha256
import math
import struct
from uuid import UUID

NIL=bytes(16)
ZERO=bytes(32)
U63=(1<<63)-1
STATE_ROUTE=64
WORLD_ROUTE=16
MAPPER_ROUTE=2
NO_OUTGOING_EDGE=(1<<32)-1

class RoadConnectionKind(IntEnum):
    NEXT=1
    LANE_CHANGE=2
    SLOT_AISLE_CONNECTOR=3

class RoutePurpose(IntEnum):
    EXIT_FOLLOW=1
    RETURN_START=2

class RoutePhase(IntEnum):
    PREPARE_CANDIDATE_ONLY=0
    EXIT=1
    MERGE_STOP=2
    FOLLOW_ROUTE=3
    FINAL_STOP=4
    COMPLETE=5
    HOLD=6

class RouteState(IntEnum):
    UNKNOWN=0
    RESERVED_PREPARED_NOT_ACCEPTABLE=1
    ACTIVE=2
    CLOSED=3

class RouteAction(IntEnum):
    BEGIN=1
    UPDATE=2
    FINISH=3

class RouteResult(IntEnum):
    ACCEPTED=1
    REJECTED=2
    STALE=3
    UNSUPPORTED=4
    EXHAUSTED=5

class RouteCommit(IntEnum):
    UNKNOWN=0
    REGISTERED_PENDING_FULL_STATE=1
    FULL_STATE_ACCEPTED=2
    TARGET_APPLIED=3

class TransitionAction(IntEnum):
    PREPARE=1
    ACTIVATE=2
    ABORT=3
    STATUS=4
    COMMIT_NOTICE=5

class TransitionState(IntEnum):
    UNKNOWN=0
    PREPARED=1
    ACTIVATED=2
    COMMITTED=3
    ABORTED=4
    REJECTED=5

class TerminalResult(IntEnum):
    NONE=0
    COMPLETED=1
    CANCELLED=2
    FAILED=3

class RouteReason(IntEnum):
    READY=0
    UNSUPPORTED_PROFILE=1
    SOURCE_UNAVAILABLE=2
    CLOCK_OR_EXPIRY=3
    STOP_REQUIRED=4
    SPACE_UNKNOWN=5
    TARGET_UNAVAILABLE=6
    TOPOLOGY_INCOMPLETE=7
    GEOMETRY_CONFLICT=8
    WINDOW_INSUFFICIENT=9
    ACTIVE_EPISODE=10
    EPISODE_UNKNOWN=11
    METER_GAP=12
    ROUTE_DEADLINE=13
    PHASE_DEADLINE=14
    NO_PROGRESS=15
    REQUEST_CONTEXT_CHANGED=16
    HASH_OR_BODY_MISMATCH=17
    DURABLE_NOT_READY=18
    POOL_BUSY=19
    TRANSITION_ABORTED=20
    CAPABILITY_LOST=21
    CANCELLED=22
    ACTIVATION_NOT_READY=23
    FRAME_OR_DATASET_CHANGED=24

ROUTE_MESSAGES=frozenset(('RouteBinding', 'StartGoal', 'RoadSectionRef', 'RoadEdge', 'RoadTopology', 'RoadSection', 'RoadConnection', 'ExitSpace', 'RouteOccurrence', 'RoutePlan', 'RouteCore', 'RouteProgress', 'RouteAcceptanceRef', 'RouteSnapshot', 'WorldRouteView', 'RouteStatus', 'RouteQuery', 'RouteCandidate', 'RouteReceipt', 'RoadWindow', 'RoadGeometryScope', 'RoadDurableRef', 'RoadObservationOrigin', 'MapperRouteWitness', 'RouteOwnerContext', 'RouteCurrentWitness', 'RouteExtentProof', 'RouteEligibility', 'RouteEligibilityRef', 'MapperWorldWitness', 'WindowTransitionRequest', 'WindowTransitionReceipt', 'RoadDataRequest', 'RoadRegistrationRequest', 'RoadData', 'RouteExchange'))
ROUTE_CAPS={'RoadData': 16384, 'RouteExchange': 3072, 'RouteBinding': 160, 'RouteSnapshot': 1024, 'WorldRouteView': 384, 'StartGoal': 1024, 'RoadSection': 4096, 'RoadConnection': 1536, 'RoadWindow': 4096, 'RoadTopology': 12288, 'RoutePlan': 4096, 'ExitSpace': 1024, 'RouteCore': 768, 'RouteProgress': 128, 'RouteAcceptanceRef': 256, 'RouteReceipt': 1536, 'RouteStatus': 768, 'RouteEligibility': 2048, 'RouteCandidate': 2816, 'RoadGeometryScope': 768, 'RoadObservationOrigin': 512, 'RoadDurableRef': 256, 'MapperRouteWitness': 512, 'RouteOwnerContext': 320, 'RouteCurrentWitness': 768, 'RouteExtentProof': 320, 'WindowTransitionRequest': 2048, 'WindowTransitionReceipt': 1024, 'RoadRegistrationRequest': 2048, 'MapperWorldWitness': 1024}
ROUTE_BYTE_FIELDS={'RouteBinding.route_id': 16, 'RouteBinding.core_sha256': 32, 'RouteBinding.global_plan_sha256': 32, 'RouteBinding.goal_sha256': 32, 'StartGoal.goal_id': 16, 'StartGoal.geometry_scope_sha256': 32, 'StartGoal.section_id': 16, 'StartGoal.vehicle_raw_sha256': 32, 'StartGoal.alignment_raw_sha256': 32, 'StartGoal.world_raw_sha256': 32, 'StartGoal.terminal_profile_sha256': 32, 'StartGoal.stop_facts_sha256': 32, 'RoadSectionRef.section_id': 16, 'RoadSectionRef.section_sha256': 32, 'RoadEdge.connection_sha256': 32, 'RoadTopology.topology_id': 16, 'RoadTopology.geometry_scope_sha256': 32, 'RoadTopology.origin_roots_sha256': 32, 'RoadSection.section_id': 16, 'RoadSection.geometry_scope_sha256': 32, 'RoadSection.alignment_raw_sha256': 32, 'RoadSection.observation_evidence_sha256': 32, 'RoadConnection.connection_id': 16, 'RoadConnection.geometry_scope_sha256': 32, 'RoadConnection.from_section_sha256': 32, 'RoadConnection.to_section_sha256': 32, 'RoadConnection.evidence_roots_sha256': 32, 'ExitSpace.space_id': 16, 'ExitSpace.geometry_scope_sha256': 32, 'ExitSpace.parking_attempt_id': 16, 'ExitSpace.parking_goal_sha256': 32, 'ExitSpace.road_section_sha256': 32, 'ExitSpace.connection_sha256': 32, 'RoutePlan.route_id': 16, 'RoutePlan.goal_sha256': 32, 'RoutePlan.topology_sha256': 32, 'RoutePlan.geometry_scope_sha256': 32, 'RoutePlan.exit_space_sha256': 32, 'RoutePlan.origin_guest_witness_sha256': 32, 'RoutePlan.merge_section_id': 16, 'RoutePlan.terminal_profile_sha256': 32, 'RouteCore.route_id': 16, 'RouteCore.goal_sha256': 32, 'RouteCore.topology_sha256': 32, 'RouteCore.global_plan_sha256': 32, 'RouteCore.geometry_scope_sha256': 32, 'RouteCore.meter_epoch': 16, 'RouteCore.parking_attempt_id': 16, 'RouteCore.parking_goal_sha256': 32, 'RouteCore.predecessor_episode_sha256': 32, 'RouteCore.budget_profile_sha256': 32, 'RouteCore.local_path_sha256': 32, 'RouteCore.stop_wait_facts_sha256': 32, 'RouteAcceptanceRef.registry_epoch': 16, 'RouteAcceptanceRef.acceptance_nonce': 16, 'RouteAcceptanceRef.request_nonce': 16, 'RouteAcceptanceRef.request_sha256': 32, 'RouteAcceptanceRef.accepted_core_sha256': 32, 'RouteAcceptanceRef.origin_owner_sha256': 32, 'RouteSnapshot.current_window_sha256': 32, 'RouteSnapshot.exit_space_sha256': 32, 'RouteSnapshot.basis_window_sha256': 32, 'RouteSnapshot.current_world_sha256': 32, 'WorldRouteView.geometry_scope_sha256': 32, 'WorldRouteView.topology_sha256': 32, 'WorldRouteView.window_id': 16, 'WorldRouteView.window_sha256': 32, 'WorldRouteView.from_window_id': 16, 'WorldRouteView.from_window_sha256': 32, 'WorldRouteView.transition_id': 16, 'WorldRouteView.first_section_id': 16, 'WorldRouteView.last_section_id': 16, 'WorldRouteView.overlap_prefix_sha256': 32, 'RouteStatus.registry_epoch': 16, 'RouteStatus.route_id': 16, 'RouteStatus.accepted_core_sha256': 32, 'RouteStatus.global_plan_sha256': 32, 'RouteStatus.local_path_sha256': 32, 'RouteStatus.state_message_id': 16, 'RouteStatus.state_sha256': 32, 'RouteStatus.current_permission_sha256': 32, 'RouteStatus.current_window_sha256': 32, 'RouteQuery.query_id': 16, 'RouteQuery.goal_sha256': 32, 'RouteQuery.operator_request_nonce': 16, 'RouteCandidate.request_nonce': 16, 'RouteCandidate.query_id': 16, 'RouteCandidate.query_sha256': 32, 'RouteCandidate.expected_registry_epoch': 16, 'RouteCandidate.current_window_sha256': 32, 'RouteCandidate.basis_window_sha256': 32, 'RouteCandidate.exit_space_sha256': 32, 'RouteCandidate.phase_state_message_id': 16, 'RouteCandidate.control_facts_sha256': 32, 'RouteReceipt.request_nonce': 16, 'RouteReceipt.request_sha256': 32, 'RouteReceipt.query_id': 16, 'RouteReceipt.query_sha256': 32, 'RoadWindow.window_id': 16, 'RoadWindow.geometry_scope_sha256': 32, 'RoadWindow.topology_sha256': 32, 'RoadWindow.section_sha256': 32, 'RoadWindow.from_window_id': 16, 'RoadWindow.from_window_sha256': 32, 'RoadWindow.overlap_prefix_sha256': 32, 'RoadWindow.transition_id': 16, 'RoadGeometryScope.dataset_uuid': 16, 'RoadGeometryScope.map_epoch': 16, 'RoadGeometryScope.frame_epoch': 16, 'RoadGeometryScope.primitive': 32, 'RoadGeometryScope.origin_pi_host_boot': 16, 'RoadGeometryScope.origin_gateway_instance': 16, 'RoadGeometryScope.origin_pi_session': 16, 'RoadGeometryScope.origin_device_session': 16, 'RoadGeometryScope.origin_odom_epoch': 16, 'RoadGeometryScope.alignment_frame_sha256': 32, 'RoadGeometryScope.geometry_policy_sha256': 32, 'RoadGeometryScope.origin_storage_context_sha256': 32, 'RoadDurableRef.database_uuid': 16, 'RoadDurableRef.job_id': 16, 'RoadDurableRef.job_digest': 32, 'RoadDurableRef.origin_context_sha256': 32, 'RoadDurableRef.record_sha256': 32, 'RoadDurableRef.original_receipt_sha256': 32, 'RoadObservationOrigin.mapper_boot': 16, 'RoadObservationOrigin.mapper_instance': 16, 'RoadObservationOrigin.clock_epoch': 16, 'RoadObservationOrigin.raw_binding_sha256': 32, 'RoadObservationOrigin.frame_evidence_sha256': 32, 'RoadObservationOrigin.origin_context_sha256': 32, 'RoadObservationOrigin.alignment_raw_sha256': 32, 'RoadObservationOrigin.mapper_clock_reply_sha256': 32, 'RoadObservationOrigin.independent_world_sha256': 32, 'MapperRouteWitness.mapper_boot': 16, 'MapperRouteWitness.mapper_instance': 16, 'MapperRouteWitness.clock_epoch': 16, 'MapperRouteWitness.pi_host_boot': 16, 'MapperRouteWitness.pi_client_instance': 16, 'MapperRouteWitness.clock_reply_sha256': 32, 'MapperRouteWitness.raw_binding_sha256': 32, 'MapperRouteWitness.frame_evidence_sha256': 32, 'MapperRouteWitness.scene_admission_id': 16, 'RouteOwnerContext.vm': 16, 'RouteOwnerContext.execution': 16, 'RouteOwnerContext.app_instance': 16, 'RouteOwnerContext.mission_id': 16, 'RouteOwnerContext.scene_admission_id': 16, 'RouteOwnerContext.source_key_sha256': 32, 'RouteOwnerContext.geometry_scope_sha256': 32, 'RouteCurrentWitness.clock_reply_sha256': 32, 'RouteCurrentWitness.world_raw_sha256': 32, 'RouteCurrentWitness.vehicle_raw_sha256': 32, 'RouteExtentProof.window_sha256': 32, 'RouteExtentProof.exit_space_sha256': 32, 'RouteExtentProof.cusp_sweep_sha256': 32, 'RouteExtentProof.s19_sweep_sha256': 32, 'RouteEligibility.eligibility_id': 16, 'RouteEligibility.goal_sha256': 32, 'RouteEligibility.exit_space_sha256': 32, 'RouteEligibility.topology_sha256': 32, 'RouteEligibility.window_sha256': 32, 'RouteEligibility.global_plan_sha256': 32, 'RouteEligibilityRef.eligibility_id': 16, 'RouteEligibilityRef.body_sha256': 32, 'MapperWorldWitness.world_raw_sha256': 32, 'WindowTransitionRequest.request_nonce': 16, 'WindowTransitionRequest.query_id': 16, 'WindowTransitionRequest.query_sha256': 32, 'WindowTransitionRequest.route_id': 16, 'WindowTransitionRequest.expected_registry_epoch': 16, 'WindowTransitionRequest.transition_id': 16, 'WindowTransitionRequest.from_window_sha256': 32, 'WindowTransitionRequest.to_window_sha256': 32, 'WindowTransitionRequest.from_world_sha256': 32, 'WindowTransitionRequest.candidate_local_path_sha256': 32, 'WindowTransitionReceipt.request_nonce': 16, 'WindowTransitionReceipt.request_sha256': 32, 'WindowTransitionReceipt.transition_id': 16, 'WindowTransitionReceipt.from_window_sha256': 32, 'WindowTransitionReceipt.to_window_sha256': 32, 'WindowTransitionReceipt.active_window_sha256': 32, 'WindowTransitionReceipt.activated_world_sha256': 32, 'RoadDataRequest.request_nonce': 16, 'RoadDataRequest.record_sha256': 32, 'RoadRegistrationRequest.request_nonce': 16, 'RoadRegistrationRequest.expected_goal_sha256': 32, 'RoadRegistrationRequest.world_raw_sha256': 32, 'RoadData.record_sha256': 32}
ROUTE_UUID_FIELDS={'RouteBinding.route_id': 16, 'StartGoal.goal_id': 16, 'StartGoal.section_id': 16, 'RoadSectionRef.section_id': 16, 'RoadTopology.topology_id': 16, 'RoadSection.section_id': 16, 'RoadConnection.connection_id': 16, 'ExitSpace.space_id': 16, 'ExitSpace.parking_attempt_id': 16, 'RoutePlan.route_id': 16, 'RoutePlan.merge_section_id': 16, 'RouteCore.route_id': 16, 'RouteCore.meter_epoch': 16, 'RouteCore.parking_attempt_id': 16, 'RouteAcceptanceRef.registry_epoch': 16, 'RouteAcceptanceRef.acceptance_nonce': 16, 'RouteAcceptanceRef.request_nonce': 16, 'WorldRouteView.window_id': 16, 'WorldRouteView.from_window_id': 16, 'WorldRouteView.transition_id': 16, 'WorldRouteView.first_section_id': 16, 'WorldRouteView.last_section_id': 16, 'RouteStatus.registry_epoch': 16, 'RouteStatus.route_id': 16, 'RouteStatus.state_message_id': 16, 'RouteQuery.query_id': 16, 'RouteQuery.operator_request_nonce': 16, 'RouteCandidate.request_nonce': 16, 'RouteCandidate.query_id': 16, 'RouteCandidate.expected_registry_epoch': 16, 'RouteCandidate.phase_state_message_id': 16, 'RouteReceipt.request_nonce': 16, 'RouteReceipt.query_id': 16, 'RoadWindow.window_id': 16, 'RoadWindow.from_window_id': 16, 'RoadWindow.transition_id': 16, 'RoadGeometryScope.dataset_uuid': 16, 'RoadGeometryScope.map_epoch': 16, 'RoadGeometryScope.frame_epoch': 16, 'RoadGeometryScope.origin_pi_host_boot': 16, 'RoadGeometryScope.origin_gateway_instance': 16, 'RoadGeometryScope.origin_pi_session': 16, 'RoadGeometryScope.origin_device_session': 16, 'RoadGeometryScope.origin_odom_epoch': 16, 'RoadDurableRef.database_uuid': 16, 'RoadDurableRef.job_id': 16, 'RoadObservationOrigin.mapper_boot': 16, 'RoadObservationOrigin.mapper_instance': 16, 'RoadObservationOrigin.clock_epoch': 16, 'MapperRouteWitness.mapper_boot': 16, 'MapperRouteWitness.mapper_instance': 16, 'MapperRouteWitness.clock_epoch': 16, 'MapperRouteWitness.pi_host_boot': 16, 'MapperRouteWitness.pi_client_instance': 16, 'MapperRouteWitness.scene_admission_id': 16, 'RouteOwnerContext.vm': 16, 'RouteOwnerContext.execution': 16, 'RouteOwnerContext.app_instance': 16, 'RouteOwnerContext.mission_id': 16, 'RouteOwnerContext.scene_admission_id': 16, 'RouteEligibility.eligibility_id': 16, 'RouteEligibilityRef.eligibility_id': 16, 'WindowTransitionRequest.request_nonce': 16, 'WindowTransitionRequest.query_id': 16, 'WindowTransitionRequest.route_id': 16, 'WindowTransitionRequest.expected_registry_epoch': 16, 'WindowTransitionRequest.transition_id': 16, 'WindowTransitionReceipt.request_nonce': 16, 'WindowTransitionReceipt.transition_id': 16, 'RoadDataRequest.request_nonce': 16, 'RoadRegistrationRequest.request_nonce': 16}
ROUTE_REPEATED_CAPS={'RoadTopology.sections': 32, 'RoadTopology.edges': 96, 'RoadSection.reference': 32, 'RoadSection.left_boundary': 32, 'RoadSection.right_boundary': 32, 'RoadSection.point_error_m': 32, 'RoadConnection.overlap_reference': 8, 'RoadConnection.overlap_error_m': 8, 'RoadConnection.connector_polygon': 8, 'RoadConnection.evidence_roots_sha256': 4, 'ExitSpace.selected_slot_polygon': 4, 'ExitSpace.connection_sha256': 2, 'RoutePlan.occurrences': 96, 'RouteStatus.eligibility': 2, 'RoadWindow.section_sha256': 2, 'RoadWindow.reference': 32, 'RoadWindow.left_boundary': 32, 'RoadWindow.right_boundary': 32, 'RoadWindow.point_error_m': 32}
RECORD_TYPES={1: 'StartGoal', 2: 'RoadTopology', 3: 'RoadSection', 4: 'RoadConnection', 5: 'RoadWindow', 6: 'ExitSpace', 7: 'RoutePlan', 8: 'RoadGeometryScope'}


def _need(condition,reason):
    if not condition:raise ValueError('route '+reason)


def _positive(value,*keys):
    _need(all(0<getattr(value,k)<=U63 for k in keys),'positive original key/time')


def _ids(value,*keys):
    _need(all(getattr(value,k)!=NIL for k in keys),'original UUID')


def _hashes(value,*keys):
    _need(all(getattr(value,k)!=ZERO for k in keys),'original SHA')


def _pair(identity,revision):
    _need((identity==NIL)==(revision==0),'identity/revision pair')


def _purpose_goal(value):
    _need(value.purpose in (1,2),'purpose')
    _need((value.goal_sha256==ZERO)==(value.purpose==1),'purpose/start goal')


def _geometry(value):
    from .codec import _open_basis,_simple
    n=len(value.reference)
    _need(2<=n<=32 and len(value.left_boundary)==len(value.right_boundary)==len(value.point_error_m)==n,'shared stations')
    _open_basis([(p.x,p.y) for p in value.reference])
    _simple([(p.x,p.y) for p in value.left_boundary]+[(p.x,p.y) for p in reversed(value.right_boundary)],True)
    _need(len(value.road.divider)==n and all(len(lane.centerline)==n for lane in value.road.lanes),'lane/shared station correspondence')
    _need(all(x>=0 for x in value.point_error_m) and 0<=value.heading_error_rad<=math.pi,'geometry errors')


def _core(value):
    _ids(value,'route_id');_positive(value,'revision','phase_revision');_purpose_goal(value)
    _hashes(value,'topology_sha256','global_plan_sha256','geometry_scope_sha256','budget_profile_sha256','local_path_sha256')
    _need(value.phase in range(1,7) and value.max_laps==1 and value.target_lane_id>0,'phase/lap/target')
    _need(value.max_travel_m>0 and value.progress_mode in (1,2),'travel/progress policy')
    _need(value.terminal_result<=3 and value.terminal_reason<=24,'terminal enum')
    if value.terminal_result:
        _need(value.phase==(5 if value.terminal_result==1 else 6),'terminal phase')
        _need((value.terminal_reason==0)==(value.terminal_result==1),'terminal result/reason')
    else:_need(value.phase!=5 and not value.terminal_reason,'active core terminal sentinel')
    _pair(value.parking_attempt_id,0 if value.parking_attempt_id==NIL else 1)
    _need((value.parking_attempt_id==NIL)==(value.parking_goal_sha256==ZERO),'parking origin pair')
    _need((value.started_pi_us==0)==(value.deadline_pi_us==0),'birth clock pair')
    _need((value.phase_started_pi_us==0)==(value.phase_expires_pi_us==0),'phase clock pair')
    if value.started_pi_us:
        _need(value.started_pi_us<value.deadline_pi_us,'original route deadline')
        _ids(value,'meter_epoch');_need(value.metres_per_tick_upper>0,'original meter scale')
    else:
        _need(value.meter_epoch==NIL and not value.meter_baseline_total_ticks and not value.meter_gap_revision
              and not value.metres_per_tick_upper,'unaccepted BEGIN Pi fields')
    if value.phase_started_pi_us:
        _need(value.started_pi_us<=value.phase_started_pi_us<value.phase_expires_pi_us<=value.deadline_pi_us,'original phase deadline')
    _need((value.stop_wait_command_sequence==0)==(value.stop_wait_facts_sha256==ZERO),'original STOP facts pair')
    _need(bool(value.stop_wait_started_pi_us)==bool(value.stop_wait_command_sequence),'original STOP wait start')


def validate_one(value):
    """구조·원 proof 결박만 검사한다. now/current authority는 caller가 검산한다."""
    name=value.DESCRIPTOR.name
    clone=type(value)();clone.CopyFrom(value);clone.DiscardUnknownFields()
    _need(clone.SerializeToString(deterministic=True)==value.SerializeToString(deterministic=True),'unknown management fields')
    if name=='RouteBinding':
        _ids(value,'route_id');_positive(value,'revision');_purpose_goal(value)
        _hashes(value,'core_sha256','global_plan_sha256')
    elif name=='RoadGeometryScope':
        _ids(value,'dataset_uuid','map_epoch','frame_epoch','origin_pi_host_boot','origin_gateway_instance',
             'origin_pi_session','origin_device_session','origin_odom_epoch')
        _hashes(value,'primitive','alignment_frame_sha256','geometry_policy_sha256','origin_storage_context_sha256')
        _need(value.coordinate_policy==1,'coordinate policy')
    elif name=='RoadDurableRef':
        _ids(value,'database_uuid','job_id');_positive(value,'durable_revision')
        _hashes(value,'job_digest','origin_context_sha256','record_sha256','original_receipt_sha256')
        _need(value.status==1,'original COMMITTED receipt')
    elif name=='RoadObservationOrigin':
        _ids(value,'mapper_boot','mapper_instance','clock_epoch')
        _positive(value,'observation_sequence','frame_source_sequence','vehicle_sequence')
        _need(value.capture_mapper_lower_us<=value.capture_mapper_upper_us<=value.completed_mapper_us,'original capture interval')
        _hashes(value,'raw_binding_sha256','frame_evidence_sha256','origin_context_sha256','alignment_raw_sha256','mapper_clock_reply_sha256')
        if value.HasField('independent_world_sha256'):_hashes(value,'independent_world_sha256')
    elif name=='MapperRouteWitness':
        _ids(value,'mapper_boot','mapper_instance','clock_epoch','pi_host_boot','pi_client_instance')
        _positive(value,'clock_probe_sequence','checked_mapper_us','valid_until_pi_us','source_binding_revision','vehicle_sequence','observation_sequence')
        _hashes(value,'clock_reply_sha256','raw_binding_sha256','frame_evidence_sha256')
        _pair(value.scene_admission_id,value.scene_revision)
    elif name=='RouteOwnerContext':
        _ids(value,'vm','execution','app_instance');_positive(value,'control_generation')
        _pair(value.mission_id,value.mission_revision)
        _need(bool(value.grant_revision)==bool(value.mission_revision),'mission/grant pair')
        _pair(value.scene_admission_id,value.scene_revision)
        _hashes(value,'source_key_sha256','geometry_scope_sha256')
    elif name=='RouteCurrentWitness':
        _positive(value,'vehicle_sequence','checked_guest_us','valid_until_pi_us','clock_probe_sequence')
        _hashes(value,'clock_reply_sha256','world_raw_sha256','vehicle_raw_sha256')
    elif name=='StartGoal':
        _ids(value,'goal_id','section_id');_positive(value,'revision','lane_id','origin_vehicle_sequence','stop_command_sequence','stop_report_sequence')
        _hashes(value,'geometry_scope_sha256','vehicle_raw_sha256','alignment_raw_sha256','world_raw_sha256','terminal_profile_sha256','stop_facts_sha256')
        _need(value.station_m>=0 and value.position_error_m>=0 and 0<=value.heading_error_rad<=math.pi,'start goal bounds')
        _need(value.origin_vehicle_sequence==value.origin.vehicle_sequence,'original start Vehicle')
        if value.origin.HasField('independent_world_sha256'):
            _need(value.origin.independent_world_sha256==value.world_raw_sha256,'independent registration World')
    elif name=='RoadSectionRef':
        _ids(value,'section_id');_hashes(value,'section_sha256')
        _need(value.left_lane_id>0 and value.right_lane_id>0 and value.left_lane_id!=value.right_lane_id
              and value.length_m>0 and value.order<32,'section lanes/length/order')
    elif name=='RoadEdge':
        _need(value.from_section_index<32 and value.to_section_index<32 and value.from_lane_id>0 and value.to_lane_id>0
              and value.kind in (1,2) and value.from_station_m>=0 and value.to_station_m>=0,'directed edge')
        _hashes(value,'connection_sha256')
        if value.kind==2:
            _need(value.from_section_index==value.to_section_index and value.from_lane_id!=value.to_lane_id
                  and value.from_station_m<value.to_station_m and not value.closes_circuit,'observed lane crossover interval')
    elif name=='RoadTopology':
        _ids(value,'topology_id');_positive(value,'revision');_hashes(value,'geometry_scope_sha256','origin_roots_sha256')
        _need(1<=len(value.sections)<=32 and len(value.edges)<=96,'topology count')
        _need(len({x.section_id for x in value.sections})==len(value.sections),'unique sections')
        for edge in value.edges:
            _need(max(edge.from_section_index,edge.to_section_index)<len(value.sections),'edge section index')
            a,b=value.sections[edge.from_section_index],value.sections[edge.to_section_index]
            _need(edge.from_lane_id in (a.left_lane_id,a.right_lane_id) and edge.to_lane_id in (b.left_lane_id,b.right_lane_id),'edge original lanes')
        _need(value.closed_observed==any(edge.closes_circuit for edge in value.edges),'observed circuit evidence')
    elif name in ('RoadSection','RoadWindow'):
        _positive(value,'revision');_hashes(value,'geometry_scope_sha256');_geometry(value)
        if name=='RoadSection':
            _ids(value,'section_id');_hashes(value,'alignment_raw_sha256','observation_evidence_sha256')
        else:
            _ids(value,'window_id');_hashes(value,'topology_sha256')
            _need(1<=len(value.section_sha256)<=2 and all(x!=ZERO for x in value.section_sha256),'window original sections')
            if value.from_window_id==NIL:
                _need(value.from_window_sha256==ZERO and value.transition_id==NIL and value.overlap_prefix_sha256==ZERO
                      and not value.overlap_point_count,'initial window transition sentinel')
            else:
                _ids(value,'transition_id');_hashes(value,'from_window_sha256','overlap_prefix_sha256')
                _need(value.from_window_id!=value.window_id and 2<=value.overlap_point_count<=len(value.reference),'exact suffix-prefix transition')
    elif name=='RoadConnection':
        from .codec import _simple
        _ids(value,'connection_id');_positive(value,'revision','from_lane_id','to_lane_id')
        _hashes(value,'geometry_scope_sha256','from_section_sha256','to_section_sha256')
        n=len(value.overlap_reference)
        _need(value.kind in (1,2,3) and value.from_point_index<32 and value.to_point_index<32
              and (n==0 or 2<=n<=8) and len(value.overlap_error_m)==n and all(x>=0 for x in value.overlap_error_m)
              and 3<=len(value.connector_polygon)<=8 and 1<=len(value.evidence_roots_sha256)<=4
              and all(x!=ZERO for x in value.evidence_roots_sha256) and 0<=value.heading_error_rad<=math.pi,'observed connector evidence')
        _simple([(p.x,p.y) for p in value.connector_polygon],True)
        _need(_convex_polygon(value.connector_polygon),'convex connector')
        if value.kind==3:
            _need(value.from_section_sha256==value.to_section_sha256 and value.from_lane_id==value.to_lane_id
                  and value.from_point_index==value.to_point_index,'slot connector actual road anchor')
    elif name=='ExitSpace':
        _ids(value,'space_id','parking_attempt_id');_positive(value,'revision','slot_id','slot_revision','aisle_lane_id')
        _hashes(value,'geometry_scope_sha256','parking_goal_sha256','road_section_sha256')
        _need(len(value.selected_slot_polygon)==4 and _convex_polygon(value.selected_slot_polygon)
              and 1<=len(value.connection_sha256)<=2 and all(x!=ZERO for x in value.connection_sha256)
              and value.position_error_m>=0 and 0<=value.heading_error_rad<=math.pi,'selected-slot exit space')
    elif name=='RouteOccurrence':
        _need(value.section_index<32 and (value.edge_index<96 or value.edge_index==NO_OUTGOING_EDGE) and value.lane_id>0
              and 0<=value.entry_station_m<=value.exit_station_m,'route occurrence')
    elif name=='RoutePlan':
        _ids(value,'route_id');_positive(value,'revision');_purpose_goal(value)
        _hashes(value,'topology_sha256','geometry_scope_sha256','origin_guest_witness_sha256','terminal_profile_sha256')
        _need(value.max_laps==1 and len(value.occurrences)<=96,'bounded route lap/occurrences')
        if value.purpose==2:_need(len(value.occurrences)>0,'RETURN route occurrence')
        for index,item in enumerate(value.occurrences):
            _need((item.edge_index==NO_OUTGOING_EDGE)==(index==len(value.occurrences)-1),'outgoing edge final sentinel')
        if value.exit_space_sha256!=ZERO:
            _ids(value,'merge_section_id');_positive(value,'merge_lane_id')
            _need(0<=value.merge_station_lower_m<=value.merge_station_upper_m,'merge terminal interval')
        else:
            _need(value.purpose==2 and value.merge_section_id==NIL and not value.merge_lane_id
                  and not value.merge_station_lower_m and not value.merge_station_upper_m
                  and not any((value.merge_seed_pose.x,value.merge_seed_pose.y,value.merge_seed_pose.yaw)),'road-only merge sentinel')
    elif name=='RouteCore':_core(value)
    elif name=='RouteProgress':
        _positive(value,'vehicle_sequence','calculation_sequence','revision')
        _need(0<=value.station_lower_m<=value.station_upper_m and value.remaining_upper_m>=0
              and value.completed_laps<=1,'progress interval/lap')
    elif name=='RouteAcceptanceRef':
        _ids(value,'registry_epoch','acceptance_nonce','request_nonce');_positive(value,'registry_revision','request_sequence')
        _hashes(value,'request_sha256','accepted_core_sha256','origin_owner_sha256')
    elif name=='RouteSnapshot':
        _need(value.state in (2,3) and value.task_space_kind in (1,2),'accepted snapshot state/space')
        c=value.core
        _positive(c,'started_pi_us','deadline_pi_us','phase_started_pi_us','phase_expires_pi_us')
        _hashes(value,'current_window_sha256','basis_window_sha256','current_world_sha256');_positive(value,'basis_window_revision')
        _need(route_core_sha256(c)==value.acceptance.accepted_core_sha256,'accepted immutable core SHA')
        _need((value.state==3)==bool(c.terminal_result),'closure/result pair')
        if value.task_space_kind==2:_hashes(value,'exit_space_sha256')
        else:_need(value.exit_space_sha256==ZERO,'road task space sentinel')
        if value.progress.last_progress_pi_us:
            _need(c.started_pi_us<=value.progress.last_progress_pi_us<=c.deadline_pi_us,'original progress time')
    elif name=='WorldRouteView':
        _ids(value,'window_id','first_section_id','last_section_id');_positive(value,'window_revision','geometry_revision')
        _hashes(value,'geometry_scope_sha256','topology_sha256','window_sha256')
        if value.from_window_id==NIL:
            _need(value.from_window_sha256==ZERO and value.transition_id==NIL and value.overlap_prefix_sha256==ZERO,'initial World window')
        else:_ids(value,'transition_id');_hashes(value,'from_window_sha256','overlap_prefix_sha256')
    elif name=='RouteExtentProof':
        _need(value.mode in (1,2,3),'extent mode');_hashes(value,'window_sha256');_positive(value,'vehicle_sequence')
        _need(all(getattr(value,k)>=0 for k in ('available_forward_m','available_backward_m','required_forward_m',
              'required_backward_m','braking_distance_m','body_radius_m','position_bound_m','heading_bound_rad')),'extent finite bounds')
        if value.mode==1:_need(value.cusp_sweep_sha256==value.s19_sweep_sha256==ZERO,'input extent has no phase sweep authority')
        if value.mode==2:_hashes(value,'exit_space_sha256','cusp_sweep_sha256')
        if value.mode==3:_hashes(value,'s19_sweep_sha256')
    elif name=='RouteEligibility':
        _ids(value,'eligibility_id');_purpose_goal(value);_need(value.task_space_kind in (1,2) and value.reason<=24,'eligibility domain')
        _need(value.eligible==(value.reason==0),'eligibility/reason')
        _hashes(value,'topology_sha256','window_sha256')
        _need(route_source_key_sha256(value.witness.source)==value.owner.source_key_sha256,'eligibility original source key')
        _need(value.extent.window_sha256==value.window_sha256 and value.extent.vehicle_sequence==value.witness.vehicle_sequence,'eligibility current extent')
        if value.task_space_kind==2:_hashes(value,'exit_space_sha256')
        else:_need(value.exit_space_sha256==ZERO,'road eligibility space')
    elif name=='RouteEligibilityRef':
        _ids(value,'eligibility_id');_hashes(value,'body_sha256');_positive(value,'ready_revision','valid_until_pi_us')
        _need(value.purpose in (1,2) and value.reason<=24 and value.task_space_kind in (1,2)
              and value.eligible==(value.reason==0),'eligibility reference')
    elif name=='RouteQuery':
        _ids(value,'query_id','operator_request_nonce');_purpose_goal(value);_positive(value,'issued_pi_us','valid_until_pi_us')
        _need(value.issued_pi_us<value.valid_until_pi_us and value.owner.operation_binding.departure_nonce!=NIL,'admitted query intent')
    elif name=='RouteCandidate':_candidate(value)
    elif name=='RouteReceipt':
        _ids(value,'request_nonce','query_id');_hashes(value,'request_sha256','query_sha256');_positive(value,'request_sequence')
        _need(value.result in range(1,6) and value.reason<=24,'receipt result')
        if value.result==1:
            _need(value.HasField('snapshot') and value.HasField('initial_target_context'),'accepted complete snapshot/target')
            a=value.snapshot.acceptance
            _need(a.request_nonce==value.request_nonce and a.request_sha256==value.request_sha256
                  and a.request_sequence==value.request_sequence,'receipt original request')
        else:_need(not value.HasField('initial_target_context'),'rejection has no target permission')
    elif name=='RouteStatus':_status(value)
    elif name=='MapperWorldWitness':
        _hashes(value,'world_raw_sha256')
        m,s=value.mapper,value.source
        _need(UUID(bytes=m.mapper_boot).hex==UUID(s.mapper_boot).hex and UUID(bytes=m.mapper_instance).hex==UUID(s.mapper_instance).hex
              and UUID(bytes=m.clock_epoch).hex==UUID(s.clock_epoch).hex and m.observation_sequence==s.observation_sequence,'actual published World source')
    elif name=='WindowTransitionRequest':
        _ids(value,'request_nonce','query_id','route_id','expected_registry_epoch','transition_id')
        _hashes(value,'query_sha256','from_window_sha256','to_window_sha256','from_world_sha256')
        _positive(value,'expected_registry_revision','expected_source_binding_revision','phase_revision')
        _need(value.action in range(1,6) and value.from_window_sha256!=value.to_window_sha256,'transition action/window')
        _need(route_source_key_sha256(value.witness.source)==value.owner.source_key_sha256,'transition current source')
        if value.action==2:_hashes(value,'candidate_local_path_sha256')
    elif name=='WindowTransitionReceipt':
        _ids(value,'request_nonce','transition_id');_hashes(value,'request_sha256','from_window_sha256','to_window_sha256')
        _positive(value,'source_binding_revision','transition_generation','valid_until_pi_us')
        _need(value.state in range(6) and value.reason<=24,'transition receipt domain')
        if value.state in (2,3):_hashes(value,'activated_world_sha256');_positive(value,'activated_world_sequence')
        else:_need(value.activated_world_sha256==ZERO and not value.activated_world_sequence,'unactivated World sentinel')
    elif name=='RoadDataRequest':
        _ids(value,'request_nonce');_hashes(value,'record_sha256');_positive(value,'valid_until_pi_us')
        _need(value.record_kind in RECORD_TYPES,'record lookup kind')
    elif name=='RoadRegistrationRequest':
        _ids(value,'request_nonce');_hashes(value,'world_raw_sha256');_positive(value,'issued_pi_us','valid_until_pi_us')
        _need(value.issued_pi_us<value.valid_until_pi_us and (value.expected_goal_revision==0)==(value.expected_goal_sha256==ZERO),'goal registration CAS')
        _need(value.facts.meter.vehicle_sequence==value.vehicle.sequence and value.facts.HasField('stop')
              and value.facts.stop.state==3 and value.valid_until_pi_us<=value.facts.valid_until_pi_us,'actual registration stationary facts')
        _need(value.vehicle.HasField('pi_session') and value.facts.stop.pi_session==value.vehicle.pi_session
              and value.facts.stop.device_session==value.vehicle.device_session
              and value.facts.stop.stationary_last_sequence<=value.vehicle.sequence
              and value.facts.stop.stationary_observed_pi_us<=value.vehicle.pi_sample_us,'registration original Vehicle/session')
    elif name=='RoadData':_road_data(value)
    elif name=='RouteExchange':_need(value.WhichOneof('payload') is not None,'management payload required')


def _convex_polygon(points):
    from .codec import _simple
    xy=[(p.x,p.y) for p in points];_simple(xy,True)
    signs=[]
    for i in range(len(xy)):
        a,b,c=xy[i-2],xy[i-1],xy[i]
        cross=(b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])
        if abs(cross)>1e-9:signs.append(cross>0)
    return bool(signs) and all(x==signs[0] for x in signs)


def _candidate(value):
    _ids(value,'request_nonce','query_id','phase_state_message_id');_hashes(value,'query_sha256')
    _positive(value,'request_sequence','plan_revision','calculation_sequence','basis_window_revision')
    _need(value.action in (1,2,3) and value.task_space_kind in (1,2),'candidate action/space')
    _hashes(value,'current_window_sha256','basis_window_sha256')
    _pair(value.expected_registry_epoch,value.expected_registry_revision)
    _need(value.progress.vehicle_sequence==value.witness.vehicle_sequence
          and value.progress.calculation_sequence==value.calculation_sequence,'candidate original completion/Vehicle')
    _need(route_source_key_sha256(value.witness.source)==value.owner.source_key_sha256,'candidate current source key')
    _need(value.extent.vehicle_sequence==value.witness.vehicle_sequence and value.extent.window_sha256==value.current_window_sha256,'candidate actual extent')
    if value.action==1:
        _positive(value,'began_guest_us','phase_started_guest_us')
        _need(value.began_guest_us<=value.phase_started_guest_us<=value.witness.checked_guest_us
              and not value.core.started_pi_us and not value.core.phase_started_pi_us
              and not value.progress.used_travel_ticks and not value.progress.last_progress_pi_us,'BEGIN original Guest/Pi split')
    else:_need(not value.began_guest_us and value.core.started_pi_us>0,'cross-owner original birth')
    if value.action==3:_need(value.core.terminal_result>0 and not value.phase_started_guest_us,'FINISH original phase')
    else:_need(not value.core.terminal_result,'live candidate terminal sentinel')
    if value.task_space_kind==2:
        _hashes(value,'exit_space_sha256');_need(value.extent.mode==2,'EXIT actual full sweep')
    else:_need(value.exit_space_sha256==ZERO,'road candidate space sentinel')


def _status(value):
    _positive(value,'valid_until_pi_us');_need(value.route_state in (0,2,3) and value.commit_state in range(4)
        and value.reason<=24 and value.terminal_result<=3,'status domain')
    _need(len(value.eligibility)<=2 and len({x.purpose for x in value.eligibility})==len(value.eligibility),'bounded current eligibility purposes')
    if value.route_state==0:
        _need(value.registry_epoch==NIL and value.route_id==NIL and not value.registry_revision
              and value.accepted_core_sha256==value.global_plan_sha256==value.local_path_sha256==ZERO
              and not value.commit_state and not value.terminal_result,'unknown registry sentinel')
    else:
        _ids(value,'registry_epoch','route_id');_positive(value,'registry_revision')
        _hashes(value,'accepted_core_sha256','global_plan_sha256','local_path_sha256','current_window_sha256')
        _need((value.route_state==3)==bool(value.terminal_result),'status closure result')
    if value.commit_state>=2:
        _ids(value,'state_message_id');_hashes(value,'state_sha256','current_permission_sha256')
        _positive(value,'state_revision','plan_revision','phase_revision','calculation_hwm','vehicle_hwm')


def _road_data(value):
    field=value.WhichOneof('payload');_need(field is not None,'record payload required')
    record=getattr(value,field);kind=next((k for k,t in RECORD_TYPES.items() if t==record.DESCRIPTOR.name),None)
    _need(kind is not None and value.record_sha256==route_record_sha256(kind,record),'record original SHA')
    if value.publisher_kind==1:
        _need(value.HasField('mapper') and not value.HasField('guest_owner') and not value.HasField('guest_witness'),'current mapper publisher')
        _need(value.HasField('durable'),'mapper record original D receipt')
    elif value.publisher_kind==2:
        _need(not value.HasField('mapper') and value.HasField('guest_owner') and value.HasField('guest_witness')
              and record.DESCRIPTOR.name=='RoutePlan','actual Guest global planner only')
        _need(route_source_key_sha256(value.guest_witness.source)==value.guest_owner.source_key_sha256,'Guest plan source key')
        _need(record.origin_guest_witness_sha256==route_witness_sha256(value.guest_witness),'global plan original Guest witness')
    else:raise ValueError('route publisher kind')
    if value.HasField('durable'):_need(value.durable.record_sha256==value.record_sha256,'D original record membership key')


def validate_parent(value):
    """부모와 route 구조 결박. active peer/profile/current source 수락은 runtime 책임이다."""
    name=value.DESCRIPTOR.name
    if name=='WorldSnapshot':
        _need(bool(value.required_features&WORLD_ROUTE)==value.HasField('route_view'),'World route presence')
        if value.HasField('route_view'):
            _need(value.required_features in (27,31),'route modern World profile')
    elif name in ('MissionExecution','MissionGrant'):
        _need(value.HasField('route_binding')==(value.kind==3),'kind3 route binding')
        if value.kind==3:
            _need(value.HasField('operation_binding'),'route requires actual operation binding')
            if name=='MissionExecution':_need(value.phase in range(1,7),'route execution phase')
    elif name=='DriveState':
        _need(bool(value.state_features&STATE_ROUTE)==value.HasField('route'),'State route presence')
        if not value.HasField('route'):
            _need(value.mission.kind!=3,'kind3 complete route snapshot');return
        r=value.route
        if r.state==3 and value.mission.kind!=3:return  # Historical closure is not a current motion lease.
        _need(value.mission.kind==3 and value.world_features&WORLD_ROUTE,'active route kind/World')
        c,bind=r.core,value.mission.route_binding
        _need(bind.route_id==c.route_id and bind.revision==c.revision and bind.purpose==c.purpose
              and bind.core_sha256==r.acceptance.accepted_core_sha256
              and bind.global_plan_sha256==c.global_plan_sha256 and bind.goal_sha256==c.goal_sha256,'State current route binding')
        _need(value.mission.phase==c.phase,'route actual phase')
        _need(value.calculation_sequence==r.progress.calculation_sequence and value.vehicle_sequence==r.progress.vehicle_sequence,'route actual source completion')
        if r.task_space_kind==1:_need(value.HasField('task_reference'),'road route actual Task33')
        else:_need(not value.HasField('task_reference') and not value.HasField('episode') or
                   not value.HasField('task_reference') and value.episode.core.state==1 and value.episode.core.phase==5,'EXIT explicit task space, no fake NONE')
    elif name=='ClockReply' and value.HasField('route_status'):
        r=value.route_status
        _need(value.probe.HasField('supported_state_features') and value.probe.supported_state_features&STATE_ROUTE,'route status capability')
        _need(value.HasField('guest_clock_valid_until_pi_us') and r.valid_until_pi_us<=value.guest_clock_valid_until_pi_us,'route status original clock E')
        if r.current_permission_sha256!=ZERO:
            _need(value.HasField('current_mission') and r.current_permission_sha256==route_permission_sha256(value.current_mission),'route current permission statement')


# Hashes preserve each original canonical body. They never mint current permission.
def _canonical(value,name):
    from .codec import schema,decode,encode
    if type(value) is bytes:
        decoded=decode(value,getattr(schema,name));raw=encode(decoded)
        _need(raw==value,'noncanonical original body')
        return value
    _need(getattr(getattr(value,'DESCRIPTOR',None),'name',None)==name,'hash message type')
    return encode(value)


def _h(domain,raw):
    return sha256(domain.encode('ascii')+b'\0'+struct.pack('<I',len(raw))+raw).digest()


def route_record_sha256(kind,value):
    _need(type(kind) is int and kind in RECORD_TYPES,'record hash kind')
    return _h('xenova.drive.route.record.v1/'+str(kind),_canonical(value,RECORD_TYPES[kind]))


def _message_hash(value,name,domain):return _h('xenova.drive.route.'+domain+'.v1',_canonical(value,name))


def route_query_sha256(value):return _message_hash(value,'RouteQuery','query')
def route_candidate_sha256(value):return _message_hash(value,'RouteCandidate','candidate')
def route_core_sha256(value):return _message_hash(value,'RouteCore','core')
def route_owner_sha256(value):return _message_hash(value,'RouteOwnerContext','owner')
def route_witness_sha256(value):return _message_hash(value,'RouteCurrentWitness','current-witness')
def route_receipt_sha256(value):return _message_hash(value,'RouteReceipt','receipt')
def route_eligibility_sha256(value):return _message_hash(value,'RouteEligibility','eligibility')
def route_permission_sha256(value):return _message_hash(value,'MissionGrant','current-permission')
def route_stop_facts_sha256(value):return _message_hash(value,'DriveControlFacts','stop-facts')
def route_transition_request_sha256(value):return _message_hash(value,'WindowTransitionRequest','transition-request')
def route_transition_receipt_sha256(value):return _message_hash(value,'WindowTransitionReceipt','transition-receipt')
def route_data_request_sha256(value):return _message_hash(value,'RoadDataRequest','data-request')
def route_registration_request_sha256(value):return _message_hash(value,'RoadRegistrationRequest','registration-request')
def route_world_witness_sha256(value):return _message_hash(value,'MapperWorldWitness','world-witness')


def route_source_key_sha256(source):
    """실제 validated source의1~10필드. 원 wire를0으로 바꾸지 않는다."""
    _canonical(source,'EpisodeSourceContext')
    chunks=[]
    for name in ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch',
                 'clock_epoch','configuration','calibration','primitive'):
        value=getattr(source,name);raw=value.encode('ascii') if type(value) is str else bytes(value)
        chunks.append(struct.pack('<I',len(raw))+raw)
    return _h('xenova.drive.route.source-key.v1',b''.join(chunks))


def _nested_body(raw,name,number,expected=None):
    from .codec import decode,schema
    value=decode(raw,getattr(schema,name))
    if expected is not None:_need(value.WhichOneof('payload')==expected,'raw nested direction')
    at=0
    def varint():
        nonlocal at
        out=0
        for shift in range(0,70,7):
            _need(at<len(raw),'truncated original varint')
            item=raw[at];at+=1;out|=(item&127)<<shift
            if not item&128:return out
        raise ValueError('route overlong original varint')
    while at<len(raw):
        tag=varint();kind=tag&7
        if kind==0:varint();continue
        length=varint() if kind==2 else 8 if kind==1 else 4 if kind==5 else -1
        _need(length>=0 and at+length<=len(raw),'original nested length')
        if tag>>3==number:
            _need(kind==2,'original submessage wire type');return raw[at:at+length]
        at+=length
    raise ValueError('route original nested body absent')


def road_data_body(link_wire):return _nested_body(link_wire,'DriveLink',12,'road_data')
def route_exchange_body(link_wire):return _nested_body(link_wire,'DriveLink',13,'route_exchange')


def route_candidate_body(link_wire):
    return _nested_body(route_exchange_body(link_wire),'RouteExchange',4,'route_candidate')


def road_record_body(raw_road_data):
    """검산된 envelope의 원 oneof body를 보존한다. reencode 결과를 원문이라 하지 않는다."""
    from .codec import decode,schema
    value=decode(raw_road_data,schema.RoadData);field=value.WhichOneof('payload')
    _need(field is not None,'record payload')
    number=value.DESCRIPTOR.fields_by_name[field].number
    return _nested_body(raw_road_data,'RoadData',number,field)


def route_local_path_sha256(state,*,global_plan_sha256,geometry_scope_sha256,basis_window_sha256,
                           task_space_kind,exit_space_sha256,basis_revision,basis_prefix):
    """등록 전 geometry commitment. fullState/accepted ref 검사를 앞세워 순환시키지 않는다."""
    for raw in (global_plan_sha256,geometry_scope_sha256,basis_window_sha256,exit_space_sha256):
        _need(type(raw) is bytes and len(raw)==32,'local path reference SHA')
    _need(all(x!=ZERO for x in (global_plan_sha256,geometry_scope_sha256,basis_window_sha256)),'local path original references')
    _need(task_space_kind in (1,2) and (exit_space_sha256==ZERO)==(task_space_kind==1),'local path task-space')
    _need(0<basis_revision<=U63 and 2<=len(basis_prefix)<=32 and 2<=len(state.path)<=512
          and 1<=len(state.segments)<=3,'local path bounded geometry')
    _need(state.direction in (-1,0,1) and state.maneuver_phase<=2 and 0<state.maneuver_revision<=U63,'local maneuver')
    _need(state.segments[0].first_index==0 and state.segments[-1].last_index==len(state.path)-1,'complete segment partition')
    for index,segment in enumerate(state.segments):
        _need(segment.signed_direction in (-1,1) and segment.first_index<segment.last_index<len(state.path),'local gear segment')
        if index:
            old=state.segments[index-1]
            _need(segment.first_index==old.last_index+1 and segment.signed_direction!=old.signed_direction,'actual cusp partition')
            before,after=state.path[old.last_index],state.path[segment.first_index]
            _need(before.pose==after.pose and not before.velocity_mps and not after.velocity_mps,'actual stationary cusp pair')
    def text(value):
        raw=value.encode('ascii')
        _need(0<len(raw)<=64 and all(33<=x<=126 for x in raw),'local identifier')
        return struct.pack('<I',len(raw))+raw
    def floats(*values):
        _need(all(math.isfinite(x) for x in values),'local finite geometry')
        return struct.pack('<'+'f'*len(values),*values)
    _need(len(state.primitive)==64 and all(x in '0123456789abcdef' for x in state.primitive),'local primitive SHA')
    def chunks():
        yield struct.pack('<I',1)
        yield global_plan_sha256;yield geometry_scope_sha256;yield basis_window_sha256
        yield struct.pack('<I',task_space_kind);yield exit_space_sha256
        yield text(state.reference_id);yield struct.pack('<Q',basis_revision)
        yield struct.pack('<I',len(basis_prefix))
        for point in basis_prefix:yield floats(point.x,point.y)
        yield struct.pack('<I',len(state.path))
        for point in state.path:yield floats(point.pose.x,point.pose.y,point.pose.yaw,point.steering_rad,point.velocity_mps)
        yield struct.pack('<I',len(state.segments))
        for segment in state.segments:yield struct.pack('<IIi',segment.first_index,segment.last_index,segment.signed_direction)
        yield floats(state.merge_goal.x,state.merge_goal.y,state.merge_goal.yaw)
        yield struct.pack('<iIQ',state.direction,state.maneuver_phase,state.maneuver_revision)
        yield text(state.configuration);yield text(state.calibration);yield bytes.fromhex(state.primitive)
        for name in ('map_epoch','frame_epoch','odom_epoch'):yield UUID(getattr(state,name)).bytes
    length=sum(len(raw) for raw in chunks())
    _need(length<=16384,'local hash preimage cap')
    result=sha256(b'xenova.drive.route.local-path.v1\0'+struct.pack('<I',length))
    for raw in chunks():result.update(raw)
    return result.digest()
