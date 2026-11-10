"""A1 actual shared C/Python codec. Nano 묶음용이며 운영/실물 수락이 아니다."""
from hashlib import sha256
from pathlib import Path
import subprocess

import pytest
from xenova_drive_world import schema,encode,validate,plan_sha256
from test_input_clock import reply
from test_world import parking_state

ID='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
U63=(1<<63)-1


@pytest.fixture(scope='module')
def operations_probe(tmp_path_factory):
    home=Path(__file__).resolve().parents[1];folder=tmp_path_factory.mktemp('operations-wire');binary=folder/'operations-probe'
    sources=[home/'world.c',home/'generated/world.pb.c',home/'tests/operations_probe.c',
        *(home/'nanopb'/name for name in ('pb_common.c','pb_decode.c','pb_encode.c'))]
    result=subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=undefined',
        '-I'+str(home),'-I'+str(home/'nanopb'),*map(str,sources),'-lm','-o',str(binary)],capture_output=True,text=True,timeout=40)
    assert result.returncode==0,result.stderr
    def run(message,kind='link',*,raw=None):
        path=folder/'input.pb';path.write_bytes(raw if raw is not None else message.SerializeToString(deterministic=True))
        value=subprocess.run([str(binary),kind,str(path)],capture_output=True,text=True,check=True,timeout=5)
        assert not value.stderr
        return value.stdout.strip()
    return run


def binding(epoch=b'e'*16):
    return schema.OperationBinding(service_epoch=epoch,operation_revision=U63,departure_nonce=b'd'*16,
        permission_revision=U63,parking_enabled=True)


def preflight_probe():
    source=schema.EpisodeSourceContext(vehicle='v'*64,mapper_boot=ID,mapper_instance=ID,map_epoch=ID,frame_epoch=ID,
        odom_epoch=ID,clock_epoch=ID,configuration='c'*64,calibration='k'*64,primitive=b'p'*32,
        reference_id='r'*64,reference_revision=U63,world_sequence=U63,observation_sequence=U63)
    report=schema.AutoInputPreflight(major=1,source=source,vehicle_sequence=U63,control_generation=(1<<32)-1,
        checked_guest_us=U63-1000,valid_until_pi_us=U63,clock_probe_sequence=U63-1,clock_reply_sha256=b'h'*32)
    return schema.DriveLink(major=2,probe=schema.ClockProbe(vm=ID,execution=ID,app_instance=ID,sequence=U63,
        guest_send_us=U63,supported_state_features=63,auto_input_preflight=report,
        receipt=schema.GuestClockReceipt(probe_sequence=U63-1,reply_sha256=b'q'*32,guest_receive_us=U63-1)))


def operation_clock():
    value=reply();clock=value.clock;clock.probe.supported_state_features=63
    clock.guest_clock_valid_until_pi_us=clock.pi_send_us+500000
    from uuid import UUID
    clock.operation_status.CopyFrom(schema.OperationStatus(major=1,binding=binding(UUID(clock.gateway_instance).bytes),
        vehicle_phase=3,ready_revision=U63,auto_ready=False,blocked_reason=0,
        valid_until_pi_us=clock.guest_clock_valid_until_pi_us,active_request_nonce=b'a'*16,
        countdown_until_pi_us=U63,desired_mode=1))
    return value


def pi_gate():
    return schema.PiOperationGate(gateway_instance=b'g'*16,control_generation=(1<<32)-1,operation_revision=U63,
        mode=2,wait_confirmed=True,manual_local_admitted=True,vehicle_sequence=U63,
        stop_command_sequence=U63,valid_until_pi_us=U63)


@pytest.mark.parametrize('kind',['preflight','operation','gate','state'])
def test_actual_maximal_new_fields_fit_caps_and_both_codecs(operations_probe,kind):
    if kind=='preflight':value=preflight_probe();cap=1024;probe_kind='link'
    elif kind=='operation':value=operation_clock();cap=32768;probe_kind='link'
    elif kind=='gate':value=pi_gate();cap=96;probe_kind='gate'
    else:
        state=parking_state();state.state_features|=32;state.mission.operation_binding.CopyFrom(binding())
        value=schema.DriveLink(major=2,snapshot=state);cap=32768;probe_kind='link'
    assert len(encode(value))<=cap
    assert operations_probe(value,probe_kind)=='OK'


@pytest.mark.parametrize('fault',['support','self-clock','future-check','empty-sha','zero-generation','zero-source','sha-size'])
def test_preflight_is_prior_original_clock_and_source_not_future_confirmation(operations_probe,fault):
    value=preflight_probe();p=value.probe;r=p.auto_input_preflight
    if fault=='support':p.supported_state_features=31
    elif fault=='self-clock':r.clock_probe_sequence=p.sequence
    elif fault=='future-check':r.checked_guest_us=p.guest_send_us+1
    elif fault=='empty-sha':r.clock_reply_sha256=bytes(32)
    elif fault=='zero-generation':r.control_generation=0
    elif fault=='zero-source':r.source.world_sequence=0
    else:r.clock_reply_sha256=b'x'*31
    with pytest.raises(ValueError):validate(value)
    assert operations_probe(value)=='REJECT'


@pytest.mark.parametrize('fault',['support','service','clock-end','countdown','ready-blocked'])
def test_current_operation_cannot_be_self_advertised_or_extend_parent_clock(operations_probe,fault):
    value=operation_clock();c=value.clock;s=c.operation_status
    if fault=='support':c.probe.supported_state_features=31
    elif fault=='service':s.binding.service_epoch=b'x'*16
    elif fault=='clock-end':s.valid_until_pi_us=c.guest_clock_valid_until_pi_us+1
    elif fault=='countdown':s.active_request_nonce=bytes(16)
    else:s.auto_ready=True;s.blocked_reason=1
    with pytest.raises(ValueError):validate(value)
    assert operations_probe(value)=='REJECT'


@pytest.mark.parametrize('fault',['wait-proof','manual-mode','epoch'])
def test_pi_gate_is_original_stationary_or_local_manual_fact(operations_probe,fault):
    value=pi_gate()
    if fault=='wait-proof':value.stop_command_sequence=0
    elif fault=='manual-mode':value.mode=1
    else:value.gateway_instance=bytes(16)
    with pytest.raises(ValueError):validate(value)
    assert operations_probe(value,'gate')=='REJECT'


def test_new_operation_has_exact_state_presence_and_raw_identity_without_rekeying_geometry(operations_probe):
    state=parking_state();original=plan_sha256(state);wire=encode(state)
    state.mission.operation_binding.CopyFrom(binding())
    invalid=schema.DriveLink(major=2,snapshot=state)
    with pytest.raises(ValueError):validate(invalid)
    assert operations_probe(invalid)=='REJECT'
    state.state_features|=32;changed=encode(state)
    assert sha256(changed).digest()!=sha256(wire).digest() and plan_sha256(state)==original
    assert operations_probe(schema.DriveLink(major=2,snapshot=state))=='OK'
    state.ClearField('mission');assert operations_probe(schema.DriveLink(major=2,snapshot=state))=='REJECT'


def test_preflight_absence_preserves_original_clock_bootstrap(operations_probe):
    value=preflight_probe();value.probe.ClearField('auto_input_preflight');value.probe.ClearField('receipt')
    value.probe.sequence=1;value.probe.guest_send_us=1;value.probe.supported_state_features=31
    assert encode(value) and operations_probe(value)=='OK'


def mapper_probe():
    return schema.MapperClockProbe(major=1,vehicle='v'*64,device='d'*64,pi_host_boot=ID,pi_client_instance=ID,
        probe_seq=U63,nonce=b'n'*16,pi_send_us=U63-1,expected_mapper_boot=ID,expected_mapper_instance=ID,
        expected_clock_epoch=ID,admission_revision=U63,operation_gate=pi_gate(),
        mapper_trust=schema.WorldTrust(vehicle='v'*64,mapper_boot=ID,mapper_instance=ID,map_epoch=ID,frame_epoch=ID,
            odom_epoch=ID,configuration='c'*64,calibration='k'*64,primitive='a'*64,clock_epoch=ID,
            lower_offset_us=-(1<<62),upper_offset_us=-(1<<62)+1,expires_pi_us=U63,max_error_us=(1<<32)-1))


def mapper_reply():
    return schema.MapperClockReply(major=1,pi_host_boot=ID,pi_client_instance=ID,probe_seq=U63,nonce=b'n'*16,
        pi_send_us=U63,device='d'*64,mapper_boot=ID,mapper_instance=ID,clock_epoch=ID,map_epoch=ID,frame_epoch=ID,
        odom_epoch=ID,configuration='c'*64,calibration='k'*64,primitive='a'*64,j2=U63-1,j3=U63,
        supported_mapper_features=1,survey_summary=schema.SurveySummary(major=1,request_nonce=b'r'*16,
            survey_id=b's'*16,survey_revision=U63,status=4,world_sequence=U63,evidence_sequence=U63,
            started_peer_us=U63-2,last_observed_peer_us=U63-1,completed_peer_us=U63,
            positive_cell_count=(1<<32)-1,observed_slot_count=64,required_sensor_mask=31,valid_sensor_mask=31,
            evidence_sha256=b'e'*32,coverage_profile_sha256=b'c'*32,observation_station_count=24,
            hazard_revision=U63,alignment_revision=U63,anchor_position_bound_m=1e6,anchor_heading_bound_rad=1e6,
            evidence_flags=63,failure_flags=0,timestamp_error_us=(1<<32)-1))


@pytest.mark.parametrize('kind',['mapper-probe','mapper-reply'])
def test_actual_new_mapper_extensions_fit_original_caps(operations_probe,kind):
    value=mapper_probe() if kind=='mapper-probe' else mapper_reply()
    assert len(encode(value))<=1024 and operations_probe(value,kind)=='OK'
    if kind=='mapper-probe':value.operation_gate.valid_until_pi_us=value.pi_send_us
    else:value.supported_mapper_features=4  # 2 is the supported route capability.
    with pytest.raises(ValueError):validate(value)
    assert operations_probe(value,kind)=='REJECT'


def test_operation_status_and_current_grant_use_the_same_setting_revision(operations_probe):
    from test_engine import lane_grant
    from uuid import UUID
    value=operation_clock();grant=lane_grant().mission_grant
    grant.gateway_instance=UUID(value.clock.gateway_instance).bytes
    grant.pi_host_boot=UUID(value.clock.host_boot).bytes
    grant.operation_binding.CopyFrom(value.clock.operation_status.binding)
    value.clock.current_mission.CopyFrom(grant)
    assert encode(value) and operations_probe(value)=='OK'
    value.clock.current_mission.operation_binding.permission_revision-=1
    with pytest.raises(ValueError):validate(value)
    assert operations_probe(value)=='REJECT'
    # Closed provenance need not be rewritten to the new current WAIT/setting.
    value.clock.current_mission.admitted=False
    assert encode(value) and operations_probe(value)=='OK'


@pytest.mark.parametrize('fault',['absent','expired','extended'])
def test_auto_ready_cannot_outlive_or_replace_the_original_preflight(operations_probe,fault):
    value=operation_clock();clock=value.clock;clock.probe.sequence=2
    report=preflight_probe().probe.auto_input_preflight
    report.clock_probe_sequence=1;report.checked_guest_us=clock.probe.guest_send_us
    report.valid_until_pi_us=clock.pi_send_us+100000
    clock.probe.auto_input_preflight.CopyFrom(report)
    clock.operation_status.auto_ready=True;clock.operation_status.valid_until_pi_us=report.valid_until_pi_us
    assert encode(value) and operations_probe(value)=='OK'
    if fault=='absent':clock.probe.ClearField('auto_input_preflight')
    elif fault=='expired':clock.probe.auto_input_preflight.valid_until_pi_us=clock.pi_send_us
    else:clock.operation_status.valid_until_pi_us+=1
    with pytest.raises(ValueError):validate(value)
    assert operations_probe(value)=='REJECT'
