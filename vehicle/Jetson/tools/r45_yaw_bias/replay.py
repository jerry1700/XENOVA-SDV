"""Replay a recorded R45 trajectory through the development feedback-3 path."""

from dataclasses import replace
import json
import math
import socket
from uuid import UUID

import cv2
import numpy as np

from xenova_drive_world import encode, schema
from xenova_drive_world.mapper_clock import ClockPolicy, restore_returned_probe
from xenova_gateway import feedback, feedback3
from xenova_gateway.odometry import OdometryConfig, VehicleOdometry
from xenova_vehicle.mapping.local_map import OCCUPIED, GridSpec
from xenova_vehicle.mapping.odometry import PoseBuffer
from xenova_vehicle.mapping.registration import transform_point, wrap_yaw
from xenova_vehicle.mapping.wheel_motion import WheelMotionProfile
from xenova_vehicle.oak.calibration_snapshot import full_sensor_rgb_geometry
from xenova_vehicle.perception.calibration import CalibrationProfile
from xenova_vehicle.perception.pipeline import PerceptionPipeline
from xenova_vehicle.runtime.mapper_profile import MapperProfile
from xenova_vehicle.simulation.capture import SyntheticCamera
from xenova_vehicle.simulation.feedback import RearWheelFeedback
from xenova_vehicle.simulation.scene import Scene, SceneSnapshot, canonical
from xenova_vehicle.transport.sensor_samples import SensorContext, SensorSamples


DRIVE_STEP_US = 20_000
CAMERA_EVERY_STEPS = 10
CAPTURE_START_US = 97 * 250_000
CAMERA_HEIGHT_M = .14
BODY_LENGTH_M = .38
BODY_WIDTH_M = .21


class EncoderReplay:
    def __init__(self, calibration, route, profile, *, wheel_speed_limit_mps, start_at_zero=False):
        encoder = profile['encoder']
        bundle = calibration.vehicle_bundle_sha256
        self.host, self.gateway, self.device = (str(UUID(int=i)) for i in (10, 11, 12))
        self.route, self.at, self.calibration = route, None, calibration
        self.initial_odom = (0.,0.,0.) if start_at_zero else route[0]
        self.sent, self.accepted, self.probes = 0, 0, 0
        odometry = dict(profile['odometry'])
        odometry['maximum_speed_mps'] = wheel_speed_limit_mps
        self.odom = VehicleOdometry(OdometryConfig(
            vehicle='rc-sim-01', calibration=bundle, **odometry,
            **{name: encoder[name] for name in ('metres_per_tick', 'metres_per_tick_upper',
                'track_width_m', 'encoder_bits')}), self.host)
        stationary = feedback3.StationaryPolicy(30000, 100000, 200000,
            encoder['metres_per_tick_upper'], .05, .1, 0., 0.)
        self.protocol = feedback3.Feedback3Profile('a'*64, 'b'*64, 0, 0, 1000,
            1_000_000, 200_000, 0, 0, stationary, 10_000_000, .02, 20_000)
        self.wire_a, self.wire_b = socket.socketpair()
        self.wire_a.setblocking(False)
        self.wire_b.setblocking(False)
        self.sink = feedback3.Feedback3SerialSink('development-fd',
            development_fd=self.wire_a.fileno(), timeout_ms=1000,
            clock=self.clock, odometry=self.odom, profile=self.protocol)
        self.wheels = RearWheelFeedback(**{name: encoder[name] for name in ('metres_per_tick',
            'track_width_m', 'encoder_bits', 'steering_error_rad')},
            pi_session=self.sink.pi_session, device_session=self.device)
        self.wire_reader = feedback.Framer()
        self.installed = self.context = None
        self.buffer = PoseBuffer(**{key: profile['sensor'][value] for key, value in (
            ('max_gap_us','max_gap_us'), ('max_yaw_rate_rps','max_yaw_rate_rps'), ('capacity','pose_capacity'))},
            max_speed_mps=self.odom.config.maximum_speed_mps,
            wheel_profile=WheelMotionProfile.from_odometry(vars(self.odom.config)))

    def clock(self):
        return math.nextafter((self.at or 1) / 1e6, math.inf)

    def close(self):
        self.sink.close()
        self.wire_a.close()
        self.wire_b.close()

    def stats(self):
        return dict(samples_sent=self.sent, samples_accepted=self.accepted, clock_probes=self.probes,
            gateway_rejected=self.sink.rejected, gateway_parser_invalid=self.sink.framer.invalid,
            gateway_fault=self.sink.fault, wheel_speed_limit_mps=self.odom.config.maximum_speed_mps)

    def _peer_exchange(self):
        probed = False
        for _ in range(6):
            try:
                raw = self.wire_b.recv(8192)
            except BlockingIOError:
                return probed
            if not raw:
                raise RuntimeError('Gateway closed feedback unexpectedly')
            for kind, payload in self.wire_reader.feed(raw):
                if kind == feedback.HELLO:
                    ack = feedback3.ACK3.pack(3, UUID(self.sink.pi_session).bytes,
                        UUID(self.device).bytes, feedback3.REQUIRED_CAPS,
                        bytes.fromhex(self.protocol.vehicle_config_sha256),
                        bytes.fromhex(self.protocol.firmware_sha256), self.wheels.encoder_bits)
                    self.wire_b.sendall(feedback.packet(feedback.HELLO_ACK, ack))
                    self.sink.poll(self.clock())
                    # The test defines the route origin once, before the first wheel sample.
                    assert self.odom.previous is None
                    self.odom.pose = self.initial_odom
                elif kind == feedback3.CLOCK_PROBE:
                    probed = True
                    self.probes += 1
                    sample = self.wheels.latch(self.at)
                    self.wire_b.sendall(self.wheels.clock_reply(feedback3, payload, sample,
                        received_device_us=self.at, sent_device_us=self.at))
                    self.sink.poll(self.clock())
                elif kind == feedback3.CLOCK_INSTALL:
                    pi, device, epoch, seq, probe, _, _, _, _, cutoff = feedback3.INSTALL3.unpack(payload)
                    self.installed = str(UUID(bytes=epoch)), seq
                    ack = feedback3.CLOCK_ACK3.pack(pi, device, epoch, seq, probe, 1, 0, self.at, cutoff)
                    self.wire_b.sendall(feedback.packet(feedback3.CLOCK_ACK, ack))
                    self.sink.poll(self.clock())
                else:
                    raise RuntimeError(f'unexpected Gateway feedback kind {kind}')
        raise RuntimeError('unexpected feedback loop')

    def _sample(self, at, distance, yaw, steering):
        self.at = at
        self.wheels.advance_motion(distance, yaw, steering)
        self.sink.poll(self.clock())
        probed = self._peer_exchange()
        if self.installed and not probed:
            sample = self.wheels.latch(at)
            self.wire_b.sendall(self.wheels.sensor_wire(feedback3, sample,
                clock_epoch=self.installed[0], clock_sequence=self.installed[1]))
            self.sent += 1
            self.sink.poll(self.clock())
        row = self.sink.snapshot['vehicle_sample']
        if row is not None and row['source_us'] == at:
            self._receive_vehicle(row)

    def _receive_vehicle(self, row):
        if self.context is None:
            source = dict(vehicle=self.odom.config.vehicle, device='oak-front',
                mapper_boot=str(UUID(int=20)), mapper_instance=str(UUID(int=21)),
                clock_epoch=str(UUID(int=22)), map_epoch=str(UUID(int=23)),
                frame_epoch=str(UUID(int=24)), odom_epoch=row['odom_epoch'],
                configuration='course-sim-1', calibration=self.calibration.vehicle_bundle_sha256,
                primitive='c'*64)
            self.context = SensorContext(self.calibration, source, pi_host_boot=self.host,
                gateway_instance=self.gateway, pi_session=self.sink.pi_session, device_session=self.device)
            self.sensor = SensorSamples(self.context, tof_leaves={}, vehicle_max_age_us=200_000,
                tof_max_age_us=200_000, max_speed_mps=self.odom.config.maximum_speed_mps, max_yaw_rate_rps=2.)
            self.clock_policy = ClockPolicy(
                **{k:source[k] for k in ('vehicle','device','configuration','calibration','primitive')},
                timestamp_error_us=0, drift_ppm=0, max_offset_width_us=100,
                lease_us=1_000_000, vehicle_max_age_us=200_000, lookback_us=500_000)
        source = self.context.source
        probe = schema.MapperClockProbe(major=1, vehicle=source['vehicle'], device=source['device'],
            pi_host_boot=self.host, pi_client_instance=str(UUID(int=25)), probe_seq=self.accepted+1,
            nonce=b'n'*16, pi_send_us=self.at, admission_revision=1,
            expected_mapper_boot=source['mapper_boot'], expected_mapper_instance=source['mapper_instance'],
            expected_clock_epoch=source['clock_epoch'])
        probe.mapper_trust.CopyFrom(schema.WorldTrust(**{k:v for k,v in source.items() if k!='device'},
            lower_offset_us=0, upper_offset_us=0, expires_pi_us=self.at+1_000_000, max_error_us=100))
        lease = restore_returned_probe(encode(probe), self.clock_policy, source, self.host, self.at)
        evidence = self.sensor.accept_vehicle(self.sink._vehicle_wire(row,self.gateway), lease, self.at)
        assert evidence is not None and self.buffer.add(evidence.pose_sample,wheel=evidence.wheel)
        self.accepted += 1

    def warm_up(self, at):
        for stamp in range(at-600_000,at+1,DRIVE_STEP_US):
            self._sample(stamp, 0., 0., 0.)

    def observe(self, obs):
        def clock(frame):
            return replace(frame, stamp=replace(frame.stamp,clock_id=self.context.pose_clock_id))
        obs = replace(obs, rgb=clock(obs.rgb), depth=clock(obs.depth))
        pose = self.buffer.at(obs.rgb.stamp,self.calibration.sha256)
        if pose is None:
            raise RuntimeError(f'no original encoder pose for capture {self.at}, {self.sink.snapshot}')
        return obs,pose


def movement(a, b):
    """Recover the signed travel and yaw of the recorded constant-curvature step."""
    x, y, yaw = a['pose']
    nx, ny, next_yaw = b['pose']
    turn = wrap_yaw(next_yaw - yaw)
    half = turn / 2
    scale = math.sin(half) / half if half else 1.
    return ((nx-x)*math.cos(yaw+half) + (ny-y)*math.sin(yaw+half)) / scale, turn


def wheel_speed_limit(samples, encoder):
    half_width = encoder['track_width_m'] / 2
    max_distance = max(abs(distance) + half_width*abs(turn)
        for distance, turn in (movement(a,b) for a,b in zip(samples,samples[1:])))
    return math.ceil(100*(max_distance + encoder['metres_per_tick'])/(DRIVE_STEP_US/1e6))/100


def _cone_cells(track, spec, world_from_map=None):
    resolution = spec.resolution_m
    ox, oy = spec.origin_xy
    cells = {}
    for cone in track['layout']['obstacle_seeds']:
        half = cone['w'] / 2
        if world_from_map is None:
            cells[cone['id']] = {row*spec.width+col for row in range(spec.height) for col in range(spec.width)
                if ox+(col+1)*resolution > cone['x']-half and ox+col*resolution < cone['x']+half
                and oy+(row+1)*resolution > cone['y']-half and oy+row*resolution < cone['y']+half}
        else:
            cells[cone['id']] = set()
            for row in range(spec.height):
                for col in range(spec.width):
                    center = ox+(col+.5)*resolution,oy+(row+.5)*resolution
                    x,y = transform_point(center,world_from_map)
                    if abs(x-cone['x']) < half and abs(y-cone['y']) < half:
                        cells[cone['id']].add(row*spec.width+col)
    return cells


def _cone_metrics(expected, observations, last):
    return {identity: dict(hit_frames=len(hits), first_s=hits[0][0] if hits else None,
        last_s=hits[-1][0] if hits else None, expected_cells=len(expected[identity]),
        ever_seen_cells=len(set().union(*(cells for _,cells in hits))),
        live_cells_at_end=len(expected[identity] & last))
        for identity,hits in observations.items()}


def run(route, scene_path, profiles, rgb_eeprom, make_encoder, *, estimate_yaw_bias=False, map_local_origin=False):
    """Feed only RGB, depth and encoder samples into the unchanged mapping entry point."""
    cv2.setNumThreads(1)
    mapper = MapperProfile.from_bytes((profiles/'mapper-profile.json').read_bytes())
    calibration = CalibrationProfile.from_dict(json.loads((profiles/'perception-calibration.json').read_bytes()))
    if mapper.calibration_profile_sha256 != calibration.sha256:
        raise ValueError('mapper and calibration disagree')
    eeprom = dict(json.loads(rgb_eeprom.read_bytes())['cameraData'])[0]
    geometry = full_sensor_rgb_geometry((eeprom['intrinsicMatrix'],eeprom['width'],eeprom['height']))
    fields = calibration.to_dict()
    height_delta = CAMERA_HEIGHT_M - calibration.rgb.translation[2]
    for sensor in ('rgb','right'):
        translation = list(fields[sensor]['translation'])
        translation[2] += height_delta
        fields[sensor]['translation'] = translation
    body = fields['body']
    length_delta = BODY_LENGTH_M - body['length']
    body.update(length=BODY_LENGTH_M,width=BODY_WIDTH_M,
        front=body['front']+length_delta/2,rear=body['rear']+length_delta/2)
    fields['rgb'].update(k=geometry['k'],roi=geometry['roi'])
    calibration = CalibrationProfile.from_dict(fields)
    scene_fields = Scene(scene_path.read_bytes()).fields
    layout = scene_fields['track']['layout']
    layout['camera']['height_m'] = CAMERA_HEIGHT_M
    layout['vehicle'].update(length_m=BODY_LENGTH_M,width_m=BODY_WIDTH_M)
    scene = Scene(canonical(scene_fields))
    camera = SyntheticCamera(calibration)
    # Use the existing operational localization gate on development-generated samples.
    live_calibration = CalibrationProfile.from_dict(dict(calibration.to_dict(),kind='LIVE',
        vehicle_bundle_sha256='b'*64))
    track = scene.fields['track']
    x0,y0,x1,y1 = track['layout']['bounds']
    cell = mapper.pipeline.grid.resolution_m
    if map_local_origin:
        # Fixed test canvas only. Camera rendering and post-run scoring own the scene coordinates.
        x0,y0,x1,y1 = -3.,-3.,3.,3.
    spec = GridSpec(math.ceil((x1-x0)/cell),math.ceil((y1-y0)/cell),cell,(x0,y0))
    road = mapper.pipeline.road
    half_width = track['lane_width_m']/2
    if not math.isclose(half_width,.285):
        raise ValueError('this comparison requires the measured 28.5 cm R45 course')
    width_error = road.missing_outer_width_error_m
    road = replace(road,missing_outer_width_bounds_m=(half_width-width_error,half_width+width_error))
    profile = replace(mapper.pipeline,grid=spec,road=road,tape=replace(mapper.pipeline.tape,x_max_m=1.5))
    core = PerceptionPipeline(live_calibration,profile,targets=mapper.targets,
                              estimate_yaw_bias=estimate_yaw_bias)
    encoder = make_encoder(live_calibration,[pose for _,pose in route])
    policy = mapper.localization
    origin = route[0][1] if map_local_origin else None
    expected = _cone_cells(track,spec,origin)
    occupied_by_cone = {identity:[] for identity in expected}
    frames = []
    try:
        for index,(elapsed_us,truth) in enumerate(route):
            at = CAPTURE_START_US + elapsed_us
            encoder.advance(index,at)
            observation = camera.capture(SceneSnapshot(str(UUID(int=1)),96+index,at,truth,scene))
            observation,pose = encoder.observe(observation)
            result = core.process(replace(observation,kind='LIVE',calibration_id=live_calibration.sha256),
                pose=pose,motion_qualified=True,completed_clock=lambda:at,
                localization_valid_for_us=policy.registration_valid_for_us,
                localization_position_drift_mps=policy.position_drift_mps,
                localization_yaw_drift_rps=policy.yaw_drift_rps)
            summary = result.summary
            seconds = elapsed_us/1e6
            if origin is not None:
                x,y = transform_point((truth[0]-origin[0],truth[1]-origin[1]),(0.,0.,-origin[2]))
                truth = x,y,wrap_yaw(truth[2]-origin[2])
            frames.append(dict(elapsed_s=seconds,truth=truth,mapped=summary['mapped_pose'],
                status=summary['localization_status'],position_error_m=summary['mapped_position_error_m'],
                yaw_error_rad=summary['mapped_yaw_error_rad'],qualified=summary['localization_qualified'],
                yaw_bias=result.yaw_bias))
            snapshot = core.grid.snapshot(at,clock_id=core.grid.clock_id)
            occupied = set(np.flatnonzero(snapshot['arrays']['state']==OCCUPIED).tolist())
            for identity,cells in expected.items():
                hits = cells & occupied
                if hits:
                    occupied_by_cone[identity].append((seconds,hits))
    finally:
        encoder.close()
    return frames,encoder,_cone_metrics(expected,occupied_by_cone,occupied)
