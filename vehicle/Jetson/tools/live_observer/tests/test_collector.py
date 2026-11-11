"""독립 PC 수집기의 현재 run·단계·나이·부분 관측을 검산한다. 실보드 시험은 아니다."""

import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve()
JETSON = HERE.parents[3]
sys.path.insert(0,str(JETSON))
sys.path.insert(0,str(JETSON/'tools/streamlit_viewer'))

from tools.live_observer.collector import ObservationCollector,packed
from tools.live_observer.feed import PcLiveFeed
from tools.live_observer.relay import relay
from tools.hil_observation.transport import ObservationFrame,SessionScope
from snapshot import SnapshotReader,validate


def pc(sequence=1, frame_sequence=None, *, run='test-run', **changes):
    body = dict(schema='pc-live-source-1',run_id=run,session_id='actual-test-session',
        sequence=sequence,source_id='actual-test-pc',source_epoch='original-test-pc-epoch',
        source_sequence=frame_sequence,source_stamp=None,stage='WAITING_FEEDBACK',state='WAITING',
        error=None,stop_reason=None,counters=dict(rendered=0,taken=0),producer={},peer={},plant=None,media=[])
    if frame_sequence is not None:
        body['media'] = [dict(panel='rgb',encoding='NV12',width=2,height=2,
            source_sequence=frame_sequence,source_stamp=dict(domain='PC',epoch='original-test-pc-epoch',us='100'),
            data_base64=base64.b64encode(bytes((16,16,16,16,128,128))).decode('ascii'))]
    body.update(changes)
    return body


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 100.
        self.owner = ObservationCollector(self.directory.name,clock=lambda:self.now)

    def test_preflight_without_run_or_frames_is_a_valid_progress_snapshot(self):
        body = self.owner.publish()
        validate(body)
        self.assertEqual(body['source']['kind'],'DIAGNOSTIC')
        self.assertIsNone(body['live']['run_id'])
        self.assertEqual(body['live']['connection']['stage'],'WAITING_FOR_RUN')
        self.assertEqual(body['media'],[])
        self.assertTrue(all(row['received_count']==0 for row in body['live']['streams']))
        result = SnapshotReader(self.directory.name).read()
        self.assertEqual(result.state,'VALID')

    def test_failed_boot_without_frames_still_shows_original_error_and_reason(self):
        self.owner.ingest_pc(pc(state='FAILED',stage='CONNECTING_JETSON',error='ConnectionRefusedError',stop_reason='CONNECT_FAILED'))
        body = self.owner.publish()
        self.assertEqual(body['live']['connection']['stage'],'CONNECTING_JETSON')
        self.assertEqual(body['live']['connection']['stop_reason'],'CONNECT_FAILED')
        self.assertEqual(body['live']['connection']['last_error']['reason'],'ConnectionRefusedError')
        self.assertEqual(body['media'],[])
        validate(body)

    def test_heartbeat_does_not_invent_new_frame_or_refresh_source_age(self):
        self.owner.ingest_pc(pc(frame_sequence=7))
        self.now += 4.
        self.owner.ingest_pc(pc(2,frame_sequence=7))
        body = self.owner.publish()
        rows = {r['name']:r for r in body['live']['streams']}
        self.assertEqual(rows['pc']['state'],'FRESH')
        self.assertEqual((rows['rgb']['state'],rows['rgb']['received_count']),('STALE',1))
        self.assertEqual(rows['rgb']['received_age_s'],4.)
        self.assertEqual(body['media'][0]['source_sequence'],'7')
        self.assertEqual(body['media'][0]['stamp']['us'],'100')

    def test_transmitted_command_is_neither_applied_nor_actual_movement(self):
        self.owner.ingest_pc(pc(peer=dict(device_session='device',received=100,command_hwm=5,applied=0)))
        stages = self.owner.publish()['live']['stages']
        self.assertEqual(stages['command']['state'],'OBSERVED')
        self.assertEqual(stages['applied']['state'],'UNKNOWN')
        self.assertEqual(stages['movement']['state'],'UNKNOWN')
        self.owner.ingest_pc(pc(2,peer=dict(device_session='device',command_hwm=5,applied=1)))
        stages = self.owner.publish()['live']['stages']
        self.assertEqual(stages['applied']['state'],'OBSERVED')
        self.assertEqual(stages['movement']['state'],'UNKNOWN')

    def test_clock_packets_are_not_command_receipts_and_zero_speed_is_not_motion(self):
        self.owner.ingest_pc(pc(peer=dict(received=1000,command_hwm=0,applied=0),plant=dict(
            epoch='plant',observed_us=500,pose=[1.,2.,.2],velocity_mps=0.,invalid_reason=None)))
        body = self.owner.publish()
        self.assertEqual(body['live']['stages']['command']['state'],'UNKNOWN')
        self.assertFalse(body['live']['stages']['movement']['detail']['moved'])
        self.assertIsNone(body['live']['pose'])
        self.assertEqual(body['map']['layers'][0]['frame'],'PLANT:plant')

    def test_actual_simulated_motion_is_a_separate_observation_with_original_tick(self):
        self.owner.ingest_pc(pc(plant=dict(epoch='plant',observed_us=501,pose=[1.1,2.,.2],velocity_mps=.1)))
        row = self.owner.publish()['live']['stages']['movement']
        self.assertTrue(row['detail']['moved'])
        self.assertEqual(row['source_stamp']['us'],'501')

    def test_new_run_clears_previous_media_map_path_operation_and_counters(self):
        self.owner.ingest_pc(pc(frame_sequence=1))
        self.owner.operation = dict(mode='AUTO',active_vm='old-vm')
        self.owner.layers['world'] = [dict(kind='MODEL',frame='MAP:old',points=[[0,0],[1,0]],closed=False)]
        old_epoch = self.owner.publish()['source']['epoch']
        self.owner.ingest_pc(pc(run='new-run'))
        body = self.owner.publish()
        self.assertNotEqual(body['source']['epoch'],old_epoch)
        self.assertEqual(body['media'],[])
        self.assertEqual(body['map']['layers'],[])
        self.assertIsNone(body['operation'])
        self.assertEqual(next(r for r in body['live']['streams'] if r['name']=='rgb')['received_count'],0)

    def test_same_old_pc_source_is_not_counted_again(self):
        self.owner.ingest_pc(pc(frame_sequence=3))
        self.now += 10
        self.owner.ingest_pc(pc(frame_sequence=3))
        row = next(r for r in self.owner.publish()['live']['streams'] if r['name']=='pc')
        self.assertEqual((row['received_count'],row['state']),(1,'STALE'))

    def test_bad_media_does_not_hide_stage_or_stop_other_streams(self):
        body = pc(frame_sequence=3)
        body['media'][0]['data_base64'] = 'not-base64'
        self.owner.ingest_pc(body)
        result = self.owner.publish()
        self.assertEqual(result['live']['connection']['stage'],'WAITING_FEEDBACK')
        self.assertEqual(result['media'],[])
        self.assertEqual(next(r for r in result['live']['streams'] if r['name']=='rgb')['state'],'ERROR')
        validate(result)

    def test_old_unmodified_feed_file_is_not_imported_as_an_active_run(self):
        path = Path(self.directory.name)/'pc.json'
        path.write_bytes(packed(pc(frame_sequence=1)))
        owner = ObservationCollector(Path(self.directory.name)/'out',clock=lambda:self.now)
        owner.poll_pc(self.directory.name)
        self.assertIsNone(owner.run_id)
        path.write_bytes(packed(pc(2,frame_sequence=2)))
        os.utime(path,ns=(owner.started_wall_ns+1_000_000,owner.started_wall_ns+1_000_000))
        owner.poll_pc(self.directory.name)
        self.assertEqual(owner.run_id,'test-run')

    def test_corrupt_feed_keeps_collector_running_and_publishes_progress_error(self):
        path = Path(self.directory.name)/'pc.json'
        path.write_bytes(b'{')
        os.utime(path,ns=(self.owner.started_wall_ns+1_000_000,self.owner.started_wall_ns+1_000_000))
        self.owner.poll_pc(self.directory.name)
        body = self.owner.publish()
        self.assertEqual(body['live']['connection']['last_error']['reason'],'PC_SOURCE_READ_OR_FORMAT_ERROR')
        self.assertEqual(body['live']['connection']['stage'],'WAITING_FOR_RUN')

    def test_world_without_run_or_different_clock_is_not_current_map(self):
        event = dict(stream='world',source_id='mapper',source_epoch='map',source_sequence=1,
            source_stamp=None,payload=dict(identities=dict(clock_epoch='clock'),layers=[]))
        self.owner.ingest_event(event)
        self.assertNotIn('world',self.owner.received)
        self.owner.ingest_pc(pc(binding=dict(destination_clock_epoch='other-clock')))
        self.owner.ingest_event(event)
        self.assertNotIn('world',self.owner.received)
        self.assertEqual(self.owner.last_error['reason'],'SOURCE_RUN_CLOCK_MISMATCH')

    def test_active_vm_path_is_selected_separately_from_the_standby(self):
        self.owner.ingest_pc(pc(binding=dict(destination_clock_epoch='clock')))
        self.owner.identities['world'] = dict(map_epoch='map')
        for vm in ('A','B'):
            self.owner.ingest_event(dict(stream='path',source_id=vm,source_epoch='execution-'+vm,
                source_sequence=1,source_stamp=None,payload=dict(identities=dict(vm_id=vm,map_epoch='map'),layers=[
                    dict(kind='PATH',frame='MAP:map',points=[[0,0],[1 if vm=='A' else 2,0]],closed=False)])))
        self.assertEqual(self.owner.publish()['map']['layers'],[])
        self.owner.ingest_operator(dict(source=dict(gateway_instance='gw',host_boot='boot',pi_now_ms=1),
            operation=dict(mode='AUTO',active_vm='A')))
        path = self.owner.publish()['map']['layers'][0]
        self.assertEqual(path['points'],[[0,0],[1,0]])

    def test_stopping_viewer_reader_does_not_close_or_change_collector_sources(self):
        self.owner.ingest_pc(pc(frame_sequence=1))
        self.owner.publish()
        reader = SnapshotReader(self.directory.name,clock=lambda:self.now)
        self.assertEqual(reader.read().state,'VALID')
        reader.close()
        self.owner.ingest_pc(pc(2,frame_sequence=2))
        self.owner.publish()
        self.assertEqual(SnapshotReader(self.directory.name).read().snapshot.body['media'][0]['source_sequence'],'2')

    def test_actual_frame_feed_to_collector_to_viewer_preserves_original_capture(self):
        from uuid import UUID
        uid=lambda n:str(UUID(int=n))
        frame=ObservationFrame(SessionScope(uid(1),uid(2),'a'*64,'b'*64,'c'*64,uid(3)),7,
            rgb_pc_interval=(100,101),depth_pc_interval=(102,103),render_completed_pc_us=110,
            expires_pc_us=140,rgb=bytes(640*360*3//2),depth=bytes(640*480*2))
        binding=SimpleNamespace(fields=dict(source_id=uid(1),pc_clock_epoch=uid(3),jetson_session=uid(2),
            destination_clock_epoch=uid(4)))
        # 실제 feed encoder/문서를 사용한다. 카메라·보드 실행을 가장한 fixture가 아니다.
        with patch('threading.Thread.start'):
            feed=PcLiveFeed(self.directory.name,run_id='synthetic-contract-test')
        feed.offer_state('CAPTURING','RUNNING')
        feed.offer_frame(frame,binding=binding)
        self.owner.ingest_pc(feed._document(1));self.owner.publish()
        result=SnapshotReader(self.directory.name).read()
        self.assertEqual(result.state,'VALID')
        media={item.descriptor['panel']:item for item in result.snapshot.media}
        self.assertEqual(media['rgb'].raw,frame.rgb)
        self.assertEqual(media['depth'].raw,frame.depth)
        stamp=media['rgb'].descriptor['original_stamp']
        self.assertEqual(stamp['sequence'],7)
        self.assertEqual(stamp['expires_pc_us'],140)
        self.assertEqual(stamp['pc_interval_us'],[100,101])

    def test_missing_binding_does_not_merge_unrelated_live_world(self):
        self.owner.ingest_pc(pc())
        self.owner.ingest_event(dict(stream='world',source_id='mapper',source_epoch='epoch',source_sequence=1,
            payload=dict(identities=dict(clock_epoch='unbound-clock'),layers=[])))
        self.assertNotIn('world',self.owner.received)

    def test_separate_relay_keeps_latest_per_topic_and_can_end_without_control(self):
        directory=Path(self.directory.name)/'relay'
        rows=[dict(kind='error',reason='GATEWAY_STATUS_UNAVAILABLE'),
              dict(kind='mqtt',topic='unrelated-topic',payload_base64='AA==')]
        relay(io.BytesIO(b'\n'.join(packed(row) for row in rows)+b'\n'),directory)
        for path in directory.glob('*.json'):
            os.utime(path,ns=(self.owner.started_wall_ns+1_000_000,self.owner.started_wall_ns+1_000_000))
        self.owner.poll_remote(directory)
        body=self.owner.publish();validate(body)
        self.assertEqual(body['live']['connection']['last_error']['reason'],'GATEWAY_STATUS_UNAVAILABLE')
        self.assertIsNone(body['live']['run_id'])

    def test_relay_drops_one_busy_file_and_keeps_receiving_without_restart(self):
        from tools.live_observer.collector import atomic
        directory=Path(self.directory.name)/'busy-relay'
        rows=[dict(kind='error',reason='FIRST'),dict(kind='error',reason='SECOND')]
        attempts=[]
        def once(path,raw):
            attempts.append(raw)
            if len(attempts)==1:raise PermissionError('simulated Windows read sharing')
            atomic(path,raw)
        with patch('tools.live_observer.relay.atomic',side_effect=once):
            relay(io.BytesIO(b'\n'.join(packed(row) for row in rows)+b'\n'),directory)
        body=json.loads(next(directory.glob('*.json')).read_bytes())
        self.assertEqual(body['payload']['reason'],'SECOND')
        self.assertEqual(body['write_errors'],1)

    def test_basic_gateway_context_is_fresh_before_hil_but_cannot_arm_or_drive(self):
        from tools.live_observer.sources import project_gateway_context
        from uuid import UUID
        uid=lambda n:str(UUID(int=n))
        record=dict(kind='gateway_context',context=dict(host_boot_id=uid(1),owner=uid(2),generation=1,
            mode='SAFE_STOP',phase='FAILED',output_permitted=False),operator_interface='ABSENT',
            observation=dict(observer_epoch=uid(3),sequence=1,response_pi_us=3000))
        directory=Path(self.directory.name)/'g1'
        relay(io.BytesIO(packed(record)+b'\n'),directory)
        for path in directory.glob('*.json'):
            os.utime(path,ns=(self.owner.started_wall_ns+1_000_000,self.owner.started_wall_ns+1_000_000))
        self.owner.poll_remote(directory);body=self.owner.publish();validate(body)
        self.assertEqual(body['operation']['mode'],'SAFE_STOP')
        self.assertIsNone(body['live']['run_id']);self.assertIsNone(body['operation']['active_vm'])
        self.assertEqual(body['identities']['gateway']['selected_owner'],uid(2))
        self.assertEqual(next(row for row in body['live']['streams'] if row['name']=='operation')['state'],'FRESH')
        self.assertTrue(all(row['state']=='UNKNOWN' for row in body['live']['stages'].values()))
        self.now+=4;self.owner.poll_remote(directory)
        self.assertEqual(next(row for row in self.owner.publish()['live']['streams'] if row['name']=='operation')['state'],'STALE')


if __name__ == '__main__':
    unittest.main()
