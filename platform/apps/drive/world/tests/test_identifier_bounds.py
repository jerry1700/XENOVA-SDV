"""공용 wire의 ASCII 식별자 경계와 원 바이트 호환을 확인한다."""
import unittest

from xenova_drive_world import schema,encode,decode


def vehicle(identifier):
    return schema.VehicleSample(major=1,vehicle=identifier,
        host_boot='10000000-0000-4000-8000-000000000001',
        device_session='20000000-0000-4000-8000-000000000002',
        odom_epoch='30000000-0000-4000-8000-000000000003',
        sequence=1,source_sample=1,source_us=100,pi_sample_us=200,clock_error_us=2,
        left_ticks=0,right_ticks=0,odom_pose=schema.Pose(x=0,y=0,yaw=0),velocity_mps=0,
        position_error_m=.01,heading_error_rad=.01,quality=3,calibration='encoder-lab-1')


class IdentifierBoundsTests(unittest.TestCase):
    def test_every_byte_character_preserves_original_ascii_domain_and_wire(self):
        for code in range(256):
            with self.subTest(code=code):
                value=vehicle(chr(code))
                if 33<=code<=126:
                    raw=encode(value)
                    self.assertEqual(raw,value.SerializeToString(deterministic=True))
                    self.assertEqual(decode(raw,schema.VehicleSample),value)
                else:
                    with self.assertRaises(ValueError):encode(value)

    def test_identifier_length_and_unicode_are_not_reinterpreted(self):
        for text in ('','a'*65,'\u2028','\U0001f697','a b','a\x00b'):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):encode(vehicle(text))
        value=vehicle('~'*64)
        self.assertEqual(encode(value),value.SerializeToString(deterministic=True))
