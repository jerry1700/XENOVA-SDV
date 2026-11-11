"""실제 FD/feedback3/encoder의 moving 읽기 회귀다. C 폐루프 시험은 아니다.

명시 software caller의 DriveFrame만 사용한다. 원 Gateway clock/ACK/SENSOR/odometry와
가상 plant는 실제 경로이며 GT pose/가짜 READY/새 관측 시각을 주입하지 않는다.
"""

import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_sim_fd_motion import declared_rig


@unittest.skipUnless(os.environ.get('XENOVA_TEST_PAIRED_GATEWAY') == '1',
    'NOT_SELECTED: explicit coherent Gateway/common requires XENOVA_TEST_PAIRED_GATEWAY=1')
class SimFdMotionActiveTests(unittest.TestCase):
    def setUp(self):
        if os.name!='posix':self.skipTest('actual POSIX development-FD contract')

    def _active(self,rig,predicate=lambda value:True,timeout_s=.5):
        deadline=time.monotonic()+timeout_s
        while time.monotonic()<deadline:
            value=rig.current_motion_vehicle()
            if value is not None and predicate(value):return value
            if rig.owner.first_failure is not None or rig.peer.failure is not None:
                self.fail(str(rig.diagnostics()))
            time.sleep(rig.poll_interval_s)
        self.fail('original active sensor proof missing: '+str(rig.diagnostics()))

    def test_original_drive_moves_encoders_and_active_vehicle_without_claiming_a_stationary_window(self):
        from xenova_drive_world import decode,schema
        from xenova_gateway.frame import DriveFrame
        from sim_fd_motion_rig import FdActiveMotionObservation,FdMotionObservation
        rig=declared_rig()
        try:
            initial=rig.wait_ready()
            self.assertIs(type(initial),FdMotionObservation)
            before=decode(initial.vehicle_bytes,schema.VehicleSample)
            now=rig.clock();issued=int(now*1e6)
            witness=dict(rig.stop_frame.witness)
            witness['command']=dict(witness['command'],command_kind='DRIVE',calculation_sequence='2')
            # 원 output lease100ms 안의 명시 software 명령이다. native C 계산 수락을 뜻하지 않는다.
            expires=min(issued+90_000,initial.clock_valid_until_pi_us)
            frame=DriveFrame(rig.stop_frame.generation,1,.08,0.,issued,expires,witness)
            rig.owner.write(frame,now)
            def moved(value):
                current=decode(value.vehicle_bytes,schema.VehicleSample)
                return current.left_ticks>before.left_ticks and current.right_ticks>before.right_ticks
            active=self._active(rig,moved)
            vehicle=decode(active.vehicle_bytes,schema.VehicleSample)
            self.assertIs(type(active),FdActiveMotionObservation)
            self.assertIsNone(rig.current_vehicle())
            self.assertEqual(active.vehicle_window,(active.vehicle_bytes,))
            self.assertGreater(vehicle.source_sample,before.source_sample)
            self.assertGreater(vehicle.source_us,before.source_us)
            self.assertGreater(vehicle.velocity_mps,0.)
            self.assertEqual((vehicle.host_boot,vehicle.gateway_instance,vehicle.pi_session,vehicle.device_session,
                vehicle.odom_epoch,vehicle.calibration),(before.host_boot,before.gateway_instance,before.pi_session,
                before.device_session,before.odom_epoch,before.calibration))
            kind,raw=rig.fb.unpack(active.sensor_wire);sensor=rig.f3.SENSOR3.unpack(raw)
            self.assertEqual(kind,rig.f3.SENSOR)
            self.assertEqual(sensor[2:7],(vehicle.source_sample,vehicle.source_us,vehicle.left_ticks,vehicle.right_ticks,3))
            self.assertGreater(vehicle.steering_error_rad,0.)
            self.assertTrue(active.clock_ack_verified_by_gateway)
            self.assertLess(active.observed_pi_us,active.clock_valid_until_pi_us)
            self.assertFalse(active.fields['stationary_witness'])
            self.assertFalse(active.fields['closed_loop']);self.assertFalse(active.fields['authority'])
            self.assertFalse(hasattr(active,'stop_command_payload'))
            self.assertEqual(decode(initial.vehicle_bytes,schema.VehicleSample).left_ticks,before.left_ticks)
        finally:rig.close()
        self.assertFalse(rig.diagnostics()['owner_thread_alive'])
        self.assertFalse(rig.peer.snapshot()['thread_alive'])

    def test_initial_install_and_original_ack_source_are_required_without_rewriting_their_bytes(self):
        rig=declared_rig()
        try:
            self.assertIsNone(rig.current_motion_vehicle())
            initial=rig.wait_ready();active=self._active(rig)
            original=(active.install_payload,active.peer_produced_ack_wire,active.vehicle_bytes)
            with patch.object(rig.peer,'clock_evidence_snapshot',return_value=()):
                self.assertIsNone(rig.current_motion_vehicle())
                self.assertEqual(rig.last_reason,'WAIT_ORIGINAL_ACCEPTED_INSTALL')
            read=rig.owner.device_view
            def foreign():
                view=read()
                if view.get('vehicle_sample') is not None:
                    view['vehicle_sample']=dict(view['vehicle_sample'],odom_epoch=str(uuid4()))
                return view
            with patch.object(rig.owner,'device_view',side_effect=foreign):
                self.assertIsNone(rig.current_motion_vehicle())
                self.assertEqual(rig.last_reason,'ACTIVE_SOURCE_CHANGED')
            self.assertEqual((active.install_payload,active.peer_produced_ack_wire,active.vehicle_bytes),original)
            self.assertEqual(active.pi_session,initial.pi_session)
            self.assertIsNotNone(self._active(rig))
        finally:rig.close()

    def test_actual_peer_eof_rejects_active_query_and_preserves_original_source_and_clock(self):
        rig=declared_rig()
        try:
            initial=rig.wait_ready();active=self._active(rig)
            original=(active.vehicle_bytes,active.install_payload,active.peer_produced_ack_wire)
            rig.force_peer_eof(timeout_s=1.)
            self.assertIsNone(rig.current_motion_vehicle())
            self.assertIsNone(rig.current_vehicle())
            view=rig.owner.device_view()
            self.assertEqual((view['pi_session'],view['device_session'],view['clock_epoch']),
                (initial.pi_session,initial.device_session,initial.clock_epoch))
            self.assertIsNone(view['vehicle_sample']);self.assertFalse(view['ready'])
            self.assertEqual((active.vehicle_bytes,active.install_payload,active.peer_produced_ack_wire),original)
        finally:rig.close()
        self.assertFalse(rig.diagnostics()['owner_thread_alive'])
        self.assertFalse(rig.peer.snapshot()['thread_alive'])
