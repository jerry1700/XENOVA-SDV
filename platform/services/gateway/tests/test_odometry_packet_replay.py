"""지연·중복 encoder packet 폐기는 원 pose/meter의 수명 갱신·철회와 구분한다."""
from dataclasses import replace
from uuid import uuid4

import pytest
from test_odometry import config
from xenova_gateway.odometry import OdometryError,VehicleOdometry


def seeded():
    original=config()
    owner=VehicleOdometry(replace(original,metres_per_tick_upper=original.metres_per_tick),str(uuid4()))
    owner.device_session(str(uuid4()));pi=str(uuid4())
    for sample in (1,2):
        stamp=1_000_000+sample*20_000
        owner.observe_mapped(pi,owner.device,stamp,sample,sample,sample,5,0,10,stamp,stamp+10)
    assert owner.latest is not None and owner.absolute.snapshot[0].valid
    return owner,pi


@pytest.mark.parametrize('kind',['duplicate','historical'])
def test_replayed_sensor_packet_preserves_only_original_pose_and_budget_lifetime(kind):
    owner,pi=seeded();sample=owner.latest;anchor=owner.absolute.snapshot[0]
    previous,epoch,pose,sequence=owner.previous,owner.epoch,owner.pose,owner.sequence
    number=2 if kind=='duplicate' else 1;stamp=1_000_000+number*20_000
    with pytest.raises(OdometryError,match='ODOM_SAMPLE_ORDER'):
        owner.observe_mapped(pi,owner.device,stamp,number,number,number,5,0,10,stamp+50,stamp+60)
    assert owner.latest is sample and owner.absolute.snapshot[0] is anchor
    assert (owner.previous,owner.epoch,owner.pose,owner.sequence)==(previous,epoch,pose,sequence)
    assert owner.current(sample['pi_sample_us']+owner.config.maximum_sample_age_us+1) is None
    stamp=1_060_000
    owner.observe_mapped(pi,owner.device,stamp,3,3,3,5,0,10,stamp,stamp+10)
    assert owner.absolute.snapshot[0].meter_epoch==anchor.meter_epoch
    assert owner.absolute.snapshot[0].total_ticks==2
    assert owner.latest['sequence']==sequence+1


@pytest.mark.parametrize('changed',['ticks','time','quality','session'])
def test_same_source_id_conflict_and_device_change_still_revoke_trust(changed):
    owner,pi=seeded();stamp=1_040_000;left=right=2;quality=5;device=owner.device
    if changed=='ticks':left=right=3
    elif changed=='time':stamp+=20_000
    elif changed=='quality':quality=0
    else:device=str(uuid4())
    with pytest.raises(OdometryError):
        owner.observe_mapped(pi,device,stamp,2,left,right,quality,0,10,1_060_000,1_060_010)
    assert owner.latest is None
