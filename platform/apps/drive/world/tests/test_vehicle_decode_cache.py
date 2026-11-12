"""동일 Vehicle wire 검산 재사용이 호출자 변경·새 입력·불량 입력을 감추지 않는지 검증한다."""
import unittest

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.codec import _validated_vehicle_wire
from test_identifier_bounds import vehicle


class VehicleDecodeCacheTests(unittest.TestCase):
    def setUp(self):_validated_vehicle_wire.cache_clear()

    def test_cached_bytes_return_a_fresh_message_with_original_timestamps(self):
        raw=encode(vehicle('car'))
        first=decode(raw,schema.VehicleSample)
        first.source_us=999;first.odom_pose.x=2
        second=decode(raw,schema.VehicleSample)
        self.assertEqual(second.source_us,100)
        self.assertEqual(second.odom_pose.x,0)
        self.assertEqual(encode(second),raw)
        self.assertEqual(_validated_vehicle_wire.cache_info().hits,1)

    def test_changed_or_malformed_input_is_not_a_previous_success(self):
        message=vehicle('car');raw=encode(message)
        decode(raw,schema.VehicleSample)
        message.sequence=2
        self.assertEqual(decode(encode(message),schema.VehicleSample).sequence,2)
        for bad in (raw+b'\x08\x01',raw[:-1],b''):
            with self.subTest(raw=bad),self.assertRaises(ValueError):decode(bad,schema.VehicleSample)
        message.velocity_mps=float('nan')
        with self.assertRaises(ValueError):decode(message.SerializeToString(),schema.VehicleSample)
        self.assertEqual(_validated_vehicle_wire.cache_info().currsize,2)

    def test_bytes_subclasses_cannot_supply_cache_keys(self):
        class Alias(bytes):
            def __hash__(self):return 0
            def __eq__(self,other):return True
        raw=encode(vehicle('car'))
        decode(raw,schema.VehicleSample)
        self.assertEqual(decode(Alias(raw),schema.VehicleSample),vehicle('car'))
        self.assertEqual(_validated_vehicle_wire.cache_info().currsize,1)
        self.assertEqual(_validated_vehicle_wire.cache_info().hits,0)

    def test_success_cache_is_bounded(self):
        message=vehicle('car')
        for sequence in range(1,258):
            message.sequence=sequence;decode(encode(message),schema.VehicleSample)
        self.assertEqual(_validated_vehicle_wire.cache_info().currsize,128)
