"""엔코더 한 칸의 좌우 반대 변화는 양자화 폭으로 받아들이고, 두 칸부터는 거절하는지 확인한다."""
from uuid import uuid4

import pytest

from xenova_gateway.odometry import OdometryConfig, OdometryError, VehicleOdometry

PERIOD_US = 20000
TICK_M = .001
TRACK_M = .2


def seeded():
    # Explicit kinematic test fixture. No measured vehicle guarantee.
    owner = VehicleOdometry(OdometryConfig('rc-test-1', 'encoder-lab-1', TICK_M, TRACK_M, .25, 2., 20000, 200000,
        20, 32, .005, .01, .02, .02), str(uuid4()))
    owner.clock_profile3 = True
    owner.device_session(str(uuid4()))
    pi = str(uuid4())

    def observe(sample, left, right):
        source = 7000000+sample*PERIOD_US
        return owner.observe_mapped(pi, owner.device, source, sample, left, right, 1, 0, 0,
            source-6000000-500, source-6000000+500)

    assert observe(1, 0, 0) is None
    assert observe(2, 0, 0)['velocity_mps'] == 0
    return owner, observe


@pytest.mark.parametrize('left,right', [(1, -1), (-1, 1)])
def test_one_opposite_tick_keeps_the_sample_and_epoch(left, right):
    owner, observe = seeded()
    epoch = owner.epoch
    row = observe(3, left, right)
    assert row is owner.latest and row['odom_epoch'] == epoch and not owner.errors
    assert (row['left_ticks'], row['right_ticks']) == (left, right)
    assert row['odom_pose']['x'] == row['odom_pose']['y'] == 0 and row['velocity_mps'] == 0
    assert row['odom_pose']['yaw'] == pytest.approx((right-left)*TICK_M/TRACK_M)
    assert owner.current(row['pi_sample_us']) is not None

    # 그 자리에 머무는 표본과 되돌아오는 표본도 같은 epoch 에서 이어진다.
    assert observe(4, left, right)['odom_pose']['yaw'] == row['odom_pose']['yaw']
    back = observe(5, 0, 0)
    assert back['odom_epoch'] == epoch and back['odom_pose']['yaw'] == pytest.approx(0)
    assert back['sequence'] == 5 and not owner.errors


def test_travel_after_one_opposite_tick_integrates_every_tick():
    owner, observe = seeded()
    observe(3, 1, -1)
    row = observe(4, 4, 2)
    assert row['velocity_mps'] == pytest.approx(3*TICK_M/(PERIOD_US/1e6))
    assert row['odom_pose']['x'] == pytest.approx(3*TICK_M, rel=1e-3)
    assert not owner.errors


@pytest.mark.parametrize('left,right', [(2, -2), (-2, 2), (5, -5)])
def test_two_or_more_opposite_ticks_remain_a_trust_failure(left, right):
    owner, observe = seeded()
    with pytest.raises(OdometryError, match='ODOM_ACKERMANN_INCONSISTENT'):
        observe(3, left, right)
    assert owner.latest is None and owner.errors == {'ODOM_ACKERMANN_INCONSISTENT': 1}
