"""현재성·원 보고·입력 부재와 복사 명령의 표시 경계. 통신이나 제품을 실행하지 않는다."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from presentation import TERMINAL_COMMANDS, input_stages, metric_rows, observation_label, operation_cards
from render import map_svg, unknown_keys_svg


class PresentationTests(unittest.TestCase):
    def body(self):
        return dict(source=dict(kind='DIAGNOSTIC'), operation=dict(mode='AUTO', active_vm='A',
            standby_vm='B', parking_enabled=False, vehicle_phase='WAIT', auto_ready=True,
            blocked_reason='INPUT_EXPIRED'))

    def test_original_seven_reports_survive_while_current_is_never_promoted_to_authority(self):
        body=self.body()
        cards=operation_cards(body,result_state='VALID',source_age_state='MAPPED')
        self.assertEqual([card.reported for card in cards],
            ['AUTO','A','B','False','WAIT','True','INPUT_EXPIRED'])
        self.assertEqual({card.current for card in cards},{'UNVERIFIED'})
        for state,age in (('VALID','UNMAPPED'),('STALE','MAPPED'),('INVALID','MAPPED'),
                ('HISTORICAL','MAPPED'),('VALID','INVALID_FUTURE')):
            with self.subTest(state=state,age=age):
                viewed=operation_cards(body,result_state=state,source_age_state=age)
                self.assertEqual({card.current for card in viewed},{'UNKNOWN'})
                self.assertEqual([card.reported for card in viewed],[card.reported for card in cards])
        self.assertEqual(body['operation']['mode'],'AUTO')

    def test_synthetic_and_missing_values_stay_unknown_even_with_mapped_clock(self):
        body=self.body();body['source']['kind']='OFFLINE_SYNTHETIC'
        self.assertEqual(observation_label(body,'VALID'),'HISTORICAL')
        self.assertEqual({x.current for x in operation_cards(body,result_state='VALID',source_age_state='MAPPED')},{'UNKNOWN'})
        self.assertTrue(all(x.current==x.reported=='UNKNOWN' for x in
            operation_cards({},result_state='UNAVAILABLE',source_age_state='UNMAPPED')))

    def test_missing_input_is_unknown_not_released_accepted_applied_or_stationary(self):
        rows=input_stages()
        self.assertEqual(len(rows),4)
        self.assertEqual({x['관측'] for x in rows},{'UNKNOWN'})
        svg=unknown_keys_svg()
        self.assertIn('UNKNOWN',svg)
        self.assertIn('BRAKE',svg)
        self.assertNotIn('onclick',svg)
        self.assertNotIn('<button',svg)

    def test_single_estimate_is_visible_point_without_invented_body_or_direction(self):
        body=dict(map=dict(layers=[dict(kind='ESTIMATE',frame='odom:original',points=[[1,2]])]))
        svg=map_svg(body)
        self.assertIn('<circle',svg)
        self.assertIn('ESTIMATE observation point',svg)
        self.assertNotIn('marker-end',svg)
        self.assertNotIn('rotate(',svg)
        gt=dict(map=dict(layers=[dict(kind='GT',frame='odom:original',points=[[1,2]])]))
        self.assertIsNone(map_svg(gt))
        self.assertIn('<circle',map_svg(gt,show_gt=True))

    def test_metric_projection_is_bounded_and_does_not_rename_or_infer_fps_units(self):
        body=dict(metrics={f'original_{n}':n for n in range(30)})
        rows,omitted=metric_rows(body)
        self.assertEqual(len(rows),24);self.assertEqual(omitted,6)
        self.assertEqual(rows[0],dict(원항목='original_0',원보고='0'))
        self.assertEqual(len(body['metrics']),30)
        rows,omitted=metric_rows(dict(metrics={'nested':{'fps':15},'original_ms':12}))
        self.assertEqual(rows,(dict(원항목='original_ms',원보고='12'),));self.assertEqual(omitted,1)
        self.assertEqual(metric_rows({}),((),0))

    def test_terminal_commands_are_only_the_verified_copy_list_for_rpi_dom0(self):
        self.assertEqual([x.command for x in TERMINAL_COMMANDS],[
            'xenova-drive status','xenova-drive operation status','xenova-drive mode manual',
            'xenova-drive manual neutral','xenova-drive drive depart auto',
            'xenova-drive parking permission on','xenova-drive parking permission off','xenova-drive stop'])
        self.assertTrue(all(x.device=='RPi dom0 터미널' and x.prerequisite for x in TERMINAL_COMMANDS))


if __name__=='__main__':unittest.main()
