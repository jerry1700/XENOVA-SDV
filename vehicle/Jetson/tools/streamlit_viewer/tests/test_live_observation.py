"""현재 run의 관측 단계를 검산한다. collector나 제품 프로세스를 실행하지 않는다."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from presentation import input_stages,live_status,observation_label,operation_cards
from render import map_panels,map_svg
from snapshot import SnapshotReader,SnapshotError,validate
from live_fixtures import live_document


class LiveObservationTests(unittest.TestCase):
    def test_waiting_run_without_frames_is_valid_and_shows_original_zero_counts(self):
        body=live_document();validate(body)
        state=live_status(body,result_state='VALID')
        self.assertEqual((state['stage'],state['state'],state['stop_reason']),('WAITING_FOR_RUN','WAITING','NO_RUN'))
        self.assertEqual(state['streams'][0]['수신 수'],'0')
        self.assertEqual(body['media'],[])
        self.assertEqual(observation_label(body,'VALID'),'LIVE_HIL · VALID')
        self.assertTrue(all(x.current=='UNKNOWN' for x in operation_cards(body,result_state='VALID',source_age_state='UNMAPPED')))

    def test_command_is_not_applied_or_movement_and_unmapped_original_stamps_survive(self):
        body=live_document();original=dict(domain='PI',epoch='pi-original',us='123')
        body['live']['stages']['command']=dict(state='SENT',source_id='gateway',source_sequence=41,
            source_stamp=original,pc_received_stamp=dict(domain='PC',epoch='pc-boot',us='900000'),detail='actual submitted packet')
        validate(body);rows=input_stages(body,result_state='VALID')
        self.assertEqual([row['관측'] for row in rows],['UNKNOWN','SENT','UNKNOWN','UNKNOWN'])
        self.assertEqual(body['live']['stages']['command']['source_stamp'],original)
        self.assertEqual(input_stages(body,result_state='STALE')[1]['관측'],'STALE · SENT')

    def test_current_operation_is_a_report_and_stale_file_keeps_the_original_report(self):
        body=live_document();body['operation']=dict(mode='AUTO',active_vm='B',standby_vm='A',auto_ready=False)
        body['live']['streams'].append(dict(name='operation',state='FRESH',received_count=1))
        fresh=operation_cards(body,result_state='VALID',source_age_state='UNMAPPED')
        self.assertEqual([x.current for x in fresh[:3]],['AUTO','B','A'])
        stale=operation_cards(body,result_state='STALE',source_age_state='UNMAPPED')
        self.assertEqual(stale[0].current,'UNKNOWN');self.assertEqual(stale[0].reported,'AUTO')
        body['live']['streams'][-1]['state']='STALE'
        upstream_stale=operation_cards(body,result_state='VALID',source_age_state='UNMAPPED')
        self.assertEqual(upstream_stale[0].current,'UNKNOWN');self.assertEqual(upstream_stale[0].reported,'AUTO')

    def test_collector_advancement_does_not_advance_frame_counts_or_refresh_a_stalled_source(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[10.];reader=SnapshotReader(directory,clock=lambda:now[0])
            body=live_document();body['live']['streams'][0].update(state='STALLED',received_count=12,
                source_sequence=99,source_stamp=dict(domain='J',epoch='j-original',us='5'),received_age_s=8.)
            first=reader.accept(json.dumps(body).encode())
            now[0]=11.;later=copy.deepcopy(body);later['source']['sequence']='2';later['live']['collector']['sequence']='2'
            second=reader.accept(json.dumps(later).encode())
            self.assertEqual(second.state,'VALID')
            self.assertEqual(second.snapshot.body['live']['streams'],first.snapshot.body['live']['streams'])
            now[0]=14.;stale=reader.accept(second.snapshot.raw)
            self.assertEqual(stale.state,'STALE')
            self.assertEqual(live_status(stale.snapshot.body,result_state=stale.state)['streams'][0]['연결 보고'],'STALE · STALLED')

    def test_waiting_diagnostic_can_gain_actual_run_with_same_collector_and_preserves_publication_hwm(self):
        with tempfile.TemporaryDirectory() as directory:
            reader=SnapshotReader(directory);body=live_document();body['source']['kind']='DIAGNOSTIC'
            body['live'].update(run_id=None,session_id=None);reader.accept(json.dumps(body).encode())
            self.assertEqual(live_status(body,result_state='VALID')['run_id'],'UNKNOWN')
            changed=copy.deepcopy(body);changed['source'].update(kind='LIVE_HIL',sequence='2');changed['live']['run_id']='actual-run'
            self.assertEqual(reader.accept(json.dumps(changed).encode()).state,'VALID')
            changed['source']['sequence']='1'
            with self.assertRaisesRegex(SnapshotError,'SEQUENCE_REVERSED'):reader.accept(json.dumps(changed).encode())
            changed['source'].update(epoch='another-collector-session',sequence='0')
            self.assertEqual(reader.accept(json.dumps(changed).encode()).state,'VALID')

    def test_bad_live_layout_and_nonfinite_or_negative_receive_age_are_rejected(self):
        for mutate in (lambda b:b['live'].update(schema='other'),
                       lambda b:b['live']['streams'].append(copy.deepcopy(b['live']['streams'][0])),
                       lambda b:b['live']['streams'][0].update(received_age_s=-1),
                       lambda b:b['live']['streams'][0].update(received_age_s=float('inf')),
                       lambda b:b['live']['streams'][0].update(received_count=True),
                       lambda b:b['live'].update(pose=dict(frame='map',x=0,y=0,yaw=float('nan')))):
            body=live_document();mutate(body)
            with self.assertRaises(SnapshotError):validate(body)

    def test_map_model_estimate_path_and_plant_gt_keep_their_original_frames(self):
        body=live_document();body['map']['layers']=[
            dict(kind='MODEL',frame='MAP:one',points=[[0,0],[1,1]]),
            dict(kind='PATH',frame='MAP:one',points=[[0,1],[1,2]]),
            dict(kind='ESTIMATE',frame='ODOM:two',points=[[.5,.5]]),
            dict(kind='GT',frame='PLANT:three',points=[[.7,.7]])]
        body['live']['pose']=dict(frame='ODOM:two',x=.5,y=.5,yaw=1.57);validate(body)
        panels=dict(map_panels(body))
        self.assertEqual(set(panels),{'MAP:one','ODOM:two'})
        self.assertNotIn('reported yaw',panels['MAP:one']);self.assertIn('reported yaw',panels['ODOM:two'])
        self.assertEqual(len(map_panels(body,show_gt=True)),3)
        with self.assertRaisesRegex(SnapshotError,'FRAME_MISMATCH'):map_svg(body)


if __name__=='__main__':unittest.main()
