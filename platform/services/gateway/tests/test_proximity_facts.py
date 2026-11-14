import struct
from uuid import UUID

from xenova_gateway.feedback3 import PROXIMITY_CAPABILITY, REQUIRED_CAPS, SENSOR, SENSOR3
from tof_feedback_fixture import DEVICE, WireBench


def test_proximity_is_bound_to_accepted_sensor_and_original_tof_expiry():
    bench = WireBench(capabilities=REQUIRED_CAPS | PROXIMITY_CAPABILITY | 0x800)
    try:
        bench.install_clock()
        runner, _ = bench.actor()
        pi = UUID(bench.sink.pi_session).bytes
        device = UUID(DEVICE).bytes
        stamp = int(bench.now * 1e6) - 98000
        bench.send(SENSOR, SENSOR3.pack(pi, device, 4, stamp, 103, 103, 0x09, 0, 0,
                                        bench.epoch, bench.clock_sequence), advance=.005)
        view = bench.sink.snapshot
        assert view['proximity_flags'] == 8 and view['vehicle_sample']['quality'] == 3
        for sensor, measurement in ((0, (145, 1, 1)), (1, (65535, 2, 0))):
            frames, _ = bench.frame(sensor=sensor, measurement=struct.pack('<HBB', *measurement))
            bench.feed_frames(frames)
        runner._mapper_tof_once()
        value = runner.control_facts(proximity_supported=True)
        p = value.proximity
        assert p.front_blocked and not p.rear_blocked
        assert p.front_vehicle_sequence == value.meter.vehicle_sequence and p.front_range_mm == 145
        assert p.rear_range_mm == 65535 and p.rear_valid_until_pi_us <= value.valid_until_pi_us
        assert not runner.control_facts().HasField('proximity')

        # Old clear samples cannot release the blocked direction.
        bench.send(SENSOR, SENSOR3.pack(pi, device, 3, stamp - 1000, 103, 103, 1, 0, 0,
                                        bench.epoch, bench.clock_sequence))
        assert bench.sink.snapshot['proximity_flags'] == 8
        assert not runner._tof_receiver.current_ranges(view['vehicle_sample'], int(bench.now * 1e6) + 510000)
    finally:
        bench.close()
