"""표시 source와 실제 공용 protobuf/MQTT envelope fixture의 교차 시험.

공용 C/Python 회귀가 사용하는 합성 wire fixture이며 보드·APPLIED·현재 경로의
수락 증거가 아니다. source adapter에는 Jetson runtime/SDK import가 없어야 한다.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from xenova_drive_world import encode,schema
from xenova_mqtt.envelope import Identity,build
from live_observer.sources import (WORLD_TOPIC,VEHICLE_TOPIC,SourceDecodeError,
                                  decode_mqtt,project_operator_status,project_gateway_context)

ROOT=Path(__file__).resolve().parents[5]
spec=importlib.util.spec_from_file_location('live_source_shared_wire_fixture',ROOT/'platform/apps/drive/tests/test_world.py')
wire=importlib.util.module_from_spec(spec);spec.loader.exec_module(wire)
VM=wire.fixture.BOOT
TOPIC=f'xenova/v1/vms/{VM}/drive/link/out'


def vehicle():
    f=wire.fixture
    return schema.VehicleSample(major=1,vehicle='rc-test-1',host_boot=f.BOOT,device_session=f.INSTANCE,
        gateway_instance=f.MAP,pi_session=f.FRAME,odom_epoch=f.ODOM,sequence=2,source_sample=7,
        source_us=1000000,pi_sample_us=1100000,clock_error_us=10,left_ticks=1,right_ticks=2,
        odom_pose=schema.Pose(x=2,y=3,yaw=.4),velocity_mps=.25,position_error_m=.01,
        heading_error_rad=.02,quality=3,calibration='encoder-lab-1',estimated_steering_rad=.1)


def runtime():
    f=wire.fixture
    value=dict(profile='mapper-runtime-1',vehicle='rc-test-1',device='oak-1',service_instance=f.INSTANCE,
        mapper_boot=f.BOOT,mapper_instance=f.MAP,pipeline_instance=f.FRAME,clock_epoch=f.CLOCK,
        release_sha256='a'*64,runtime_state='RUNNING',source_binding_revision='4',snapshot_j_us='1100000',
        capture_ready=True,metric_ready=False,reason='METRIC_UNASSESSED',
        model=dict(requirement='REQUIRED',blob_sha256='b'*64,residency_evidence='PAIRED_OUTPUT',
                   inference_state='VALID',parser_state='VALID',last_input_j_us='1000000',last_result_j_us='1050000'),
        workers=[dict(role='M',instance=f.MAP,state='WAIT_CLOCK',last_progress_sequence='3',last_progress_j_us='1050000')])
    return build(Identity(source_id='jetson',boot_id=f.BOOT,agent_instance_id=f.INSTANCE),'mapper.runtime',value,9)


def status():
    return dict(host_boot_id=wire.fixture.BOOT,gateway_instance_id=wire.fixture.INSTANCE,generation=42,
                pi_now_ms=1000,mode='AUTO',owner=VM,output_permitted=True,hold=dict(active=False,reason=None),
                device=dict(applied={'sequence':'1'}))


def operating():
    return dict(contract='operation-status-1',supported=True,host_boot_id=wire.fixture.BOOT,
        gateway_instance_id=wire.fixture.INSTANCE,generation=42,pi_now_us='1000000',
        binding=dict(service_epoch=wire.fixture.INSTANCE,operation_revision='3',departure_nonce=str(uuid4()),
                     permission_revision='5',parking_enabled=False),vehicle_phase='ACTIVE',ready_revision='7',
        auto_ready=True,blocked_reason=None,valid_until_pi_us='1100000',active_request_id=None,
        countdown_until_pi_us='0',desired_mode='AUTO')


def test_world_keeps_original_map_grid_unknown_cells_and_mapper_time_without_mutation():
    value=wire.fixture.world();before=encode(value)
    value.occupancy=bytes([255])+value.occupancy[1:]
    original=encode(value);event=decode_mqtt(WORLD_TOPIC,original)
    assert event['stream']=='world' and event['source_sequence']==value.sequence
    assert event['source_stamp']==dict(domain='JETSON_MONOTONIC',epoch=value.clock_epoch,us=value.observed_us)
    body=event['payload'];frame=f'MAP:{value.map_epoch}:{value.frame_epoch}'
    assert all(layer['frame']==frame and layer['kind']=='MODEL' for layer in body['layers'])
    assert body['grid']['occupancy'][0]==255 and body['grid']['cell_flags']==list(value.cell_flags)
    assert body['grid']['origin']==dict(frame=frame,x=0.,y=0.,yaw=0.)
    assert body['layers'][0]['points'][0]==[value.reference[0].x,value.reference[0].y]
    assert body['reference_anchor']['vehicle_sequence']==value.reference_odom_seq
    assert encode(value)==original and original!=before
    body['grid']['occupancy'][0]=0
    assert decode_mqtt(WORLD_TOPIC,original)['payload']['grid']['occupancy'][0]==255


def test_original_lane_and_parking_geometry_are_bounded_for_display_with_omissions_reported():
    value=wire.parking_world()
    second=value.parking_slots.add();second.CopyFrom(value.parking_slots[0]);second.slot_id=2
    for point in second.corners:point.x+=1
    second.reverse_goal.x+=1
    value.parking_set.track_count_in_roi=2
    body=decode_mqtt(WORLD_TOPIC,encode(value))['payload']
    assert len(body['layers'])==7 and len(body['model_geometry'])==8
    assert body['omitted_layers']==['parking:2'] and len(body['parking_slots'])==2
    assert body['model_geometry'][-1]['points']==[[point.x,point.y] for point in second.corners]
    assert body['model_geometry'][-1]['closed'] and body['identities']['vehicle']=='rc-test-1'


def test_vehicle_pose_is_odom_and_both_original_clock_domains_are_preserved():
    value=vehicle();event=decode_mqtt(VEHICLE_TOPIC,encode(value));body=event['payload']
    assert body['pose']['frame']=='ODOMETRY:'+value.odom_epoch
    assert (body['pose']['x'],body['pose']['y'])==(2.,3.)
    assert body['velocity_mps']==.25 and event['source_sequence']==2
    assert event['source_stamp']==dict(domain='PI_MONOTONIC',epoch=value.host_boot,us=1100000)
    assert body['sensor_stamp']==dict(domain='MCU_MONOTONIC',epoch=value.device_session,us=1000000)
    assert body['identities']['gateway_instance']==value.gateway_instance
    assert 'map_pose' not in body


def test_state_snapshot_has_its_original_map_frame_and_no_invented_timestamp_or_applied_status():
    state=wire.parking_state();link=schema.DriveLink(major=2,snapshot=state);raw=encode(link)
    event=decode_mqtt(TOPIC,raw);body=event['payload']
    assert event['stream']=='path' and event['source_sequence']==state.state_revision and event['source_stamp'] is None
    assert body['identities']['vm_id']==VM and body['identities']['source_generation']==1
    layer=body['layers'][0]
    assert layer['kind']=='PATH' and layer['frame']==f'MAP:{state.map_epoch}:{state.frame_epoch}'
    assert layer['points']==[[point.pose.x,point.pose.y] for point in state.path]
    assert body['protocol_stage']=='VM_SNAPSHOT_OBSERVED' and 'applied' not in body
    state.frame_epoch=str(uuid4())
    different=decode_mqtt(TOPIC,encode(schema.DriveLink(major=2,snapshot=state)))
    assert different['payload']['layers'][0]['frame']!=layer['frame']
    assert different['payload']['layers'][0]['points']==layer['points']


def test_snapshot_identity_mismatch_and_other_link_phases_never_become_active_path():
    state=wire.parking_state();state.source_vm=str(uuid4())
    with pytest.raises(SourceDecodeError,match='VM'):decode_mqtt(TOPIC,encode(schema.DriveLink(major=2,snapshot=state)))
    probe=schema.ClockProbe(vm=VM,execution=wire.fixture.INSTANCE,app_instance=wire.fixture.MAP,sequence=1,guest_send_us=1000)
    assert decode_mqtt(TOPIC,encode(schema.DriveLink(major=2,probe=probe))) is None
    assert decode_mqtt('xenova/v1/vehicle/control',b'not a command consumer') is None


@pytest.mark.parametrize('topic,raw',[(WORLD_TOPIC,b'\x08'),(VEHICLE_TOPIC,b''),(TOPIC,b'bad')])
def test_invalid_binary_input_raises_display_error_without_default_geometry(topic,raw):
    with pytest.raises(SourceDecodeError):decode_mqtt(topic,raw)


def test_runtime_envelope_crosses_shared_mqtt_contract_with_original_ids_and_j_time():
    value=runtime();raw=json.dumps(value).encode();event=decode_mqtt('xenova/v1/mapper/oak-1/runtime',raw)
    assert event['stream']=='runtime' and event['source_sequence']==9
    assert event['source_stamp']==dict(domain='JETSON_MONOTONIC',epoch=wire.fixture.CLOCK,us=1100000)
    assert event['payload']['snapshot_j_us']=='1100000' and event['payload']['metric_ready'] is False
    assert event['payload']['envelope']['message_id']==value['message_id']
    assert event['payload']['model']['residency_evidence']=='PAIRED_OUTPUT'
    assert 'envelope' not in value['payload']


def test_runtime_decoder_does_not_import_network_client_sdk_or_product_runtime(monkeypatch):
    import builtins
    original=builtins.__import__;attempts=[]
    def guarded(name,*args,**kwargs):
        if name.startswith(('paho','depthai','xenova_vehicle','xenova_mqtt')):
            attempts.append(name);raise AssertionError('decoder imported product/network runtime')
        return original(name,*args,**kwargs)
    raw=json.dumps(runtime()).encode()
    monkeypatch.setattr(builtins,'__import__',guarded)
    assert decode_mqtt('xenova/v1/mapper/oak-1/runtime',raw)['source_sequence']==9
    assert attempts==[]


@pytest.mark.parametrize('fault',['device','type','boot','service','sequence','duplicate','oversize','nonfinite'])
def test_runtime_malformed_or_mixed_source_never_creates_a_default_ready_report(fault):
    value=runtime()
    if fault=='device':value['payload']['device']='other-device'
    elif fault=='type':value['type']='agent.command'
    elif fault=='boot':value['payload']['mapper_boot']=str(uuid4())
    elif fault=='service':value['payload']['service_instance']=str(uuid4())
    elif fault=='sequence':value['seq']=True
    raw=json.dumps(value).encode()
    if fault=='duplicate':raw=raw.replace(b'"seq": 9',b'"seq": 9, "seq": 10')
    elif fault=='oversize':raw+=b' '*4096
    elif fault=='nonfinite':raw=raw.replace(b'"seq": 9',b'"seq": NaN')
    with pytest.raises(SourceDecodeError):decode_mqtt('xenova/v1/mapper/oak-1/runtime',raw)


def test_operator_projection_preserves_actual_reports_and_does_not_infer_standby_or_movement():
    current,operation=status(),operating();original=deepcopy((current,operation))
    result=project_operator_status(dict(version=2,ok=True,data=current),dict(version=2,ok=True,data=operation))
    assert result['operation']==dict(mode='AUTO',vehicle_phase='ACTIVE',parking_enabled=False,
        active_vm=VM,standby_vm=None,auto_ready=True,blocked_reason=None)
    assert result['source']['host_boot']==wire.fixture.BOOT and result['source']['generation']==42
    assert result['source']['valid_until_pi_us']=='1100000'
    assert result['source']['protocol_stage']=='OPERATOR_STATUS_OBSERVED' and 'moving' not in result['operation']
    assert (current,operation)==original


@pytest.mark.parametrize('change',['host_boot_id','gateway_instance_id','generation'])
def test_operator_context_mismatch_does_not_mix_old_parking_and_ready_with_current_owner(change):
    operation=operating();operation[change]=43 if change=='generation' else str(uuid4())
    result=project_operator_status(status(),operation)
    assert result['source']['context_match'] is False and result['operation']['mode']=='AUTO'
    assert result['operation']['parking_enabled'] is None and result['operation']['auto_ready'] is None


def test_missing_failed_or_unsupported_operator_report_stays_unknown():
    for current,operation in ((None,None),(dict(version=2,ok=False,error={'code':'UNAVAILABLE'}),None),
                              ({},dict(contract='operation-status-1',supported=False))):
        result=project_operator_status(current,operation)['operation']
        assert result['mode']=='UNKNOWN' and result['vehicle_phase']=='UNKNOWN'
        assert result['parking_enabled'] is None and result['auto_ready'] is None and result['standby_vm'] is None


def test_only_actual_active_hold_reason_is_reported_as_stop_reason():
    current=status();current['hold']=dict(active=True,reason='PARKING_PERMISSION_OFF')
    result=project_operator_status(current)
    assert result['source']['stop_reason']=='PARKING_PERMISSION_OFF'
    assert result['source']['pi_now_ms']==1000
    current['hold']['active']=False
    assert project_operator_status(current)['source']['stop_reason'] is None
    del current['hold']
    current['mission']=dict(phase='HOLD',result='UNREPORTED')
    assert project_operator_status(current)['source']['stop_reason'] is None


def gateway_record(mode='SAFE_STOP'):
    return dict(kind='gateway_context',context=dict(host_boot_id=wire.fixture.BOOT,owner=VM,
        generation=7,mode=mode,phase='FAILED',output_permitted=False),operator_interface='ABSENT',
        observation=dict(observer_epoch=wire.fixture.MAP,sequence=3,response_pi_us=99000))


def test_provider_context_preserves_original_mode_and_does_not_invent_operator_fields():
    record=gateway_record();before=deepcopy(record);result=project_gateway_context(record)
    assert result['operation']['mode']=='SAFE_STOP' and result['operation']['active_vm'] is None
    assert result['source']['selected_owner']==VM and result['source']['channel']=='G1_CONTEXT'
    assert result['source']['operator_interface']=='ABSENT'
    assert result['source']['response_stamp']==dict(domain='PI_OBSERVER_RESPONSE',epoch=wire.fixture.MAP,us='99000')
    assert result['operation']['auto_ready'] is None and result['operation']['parking_enabled'] is None
    assert result['operation']['vehicle_phase']=='UNKNOWN' and 'gateway_instance' not in result['source']
    assert record==before
    assert project_gateway_context(gateway_record('MANUAL'))['operation']['active_vm']==VM


@pytest.mark.parametrize('field,value',[('mode','invented'),('generation',True),('output_permitted',None)])
def test_invalid_basic_context_is_not_promoted_to_operator_status(field,value):
    record=gateway_record();record['context'][field]=value
    with pytest.raises(SourceDecodeError):project_gateway_context(record)
