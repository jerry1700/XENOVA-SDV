"""PC 전용 관측의 비차단·원 frame 수명·기동 실패 표시를 실제 daemon/파일로 검사한다.

보드/SDK/주행 권한을 만들지 않는다. 운행 중 비대기와 CLI terminal bounded join을 구분한다.
"""

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import UUID

from tools.live_observer import feed as module
from tools.hil_observation.transport import ObservationFrame,SessionScope
from tools.hil_observation import producer as producer_module
from tools.hil_observation import pc as pc_module


def uid(value):return str(UUID(int=value))


def original_frame(sequence=7):
    scope=SessionScope(uid(1),uid(2),'a'*64,'b'*64,'c'*64,uid(3))
    return ObservationFrame(scope,sequence,rgb_pc_interval=(100,101),depth_pc_interval=(102,103),
        render_completed_pc_us=110,expires_pc_us=140,rgb=bytes(640*360*3//2),depth=bytes(640*480*2))


def failure_cli_arguments(directory):
    # 실제 parser를 거치되 device UUID 검사에서 끝나므로 board/socket을 사용하지 않는다.
    result=['--host','127.0.0.1','--port','17600','--accept-timeout-s','1','--duration-s','1',
        '--poll-interval-s','.001','--brake-drain-s','.1','--device-session','invalid',
        '--plant-epoch',uid(1),'--initial-pose','0','0','0','--scene','unused','--scene-sha256','a'*64,
        '--capture-epoch',uid(2),'--capture-lifetime-us','100000','--render-interval-us','50000',
        '--observation-host','127.0.0.1','--observation-port','17601','--observation-connect-timeout-s','.1',
        '--observation-duration-s','1','--bootstrap-receipt','unused','--bootstrap-timeout-s','.1','--join-timeout-s','.1',
        '--observer-dir',str(directory),'--observer-run-id','process-failure']
    for name in ('development','motion','feedback'):result+=['--'+name+'-sha256','a'*64]
    return result+['--development-profile','unused','--motion-arguments','unused','--feedback-profile','unused']


class FeedTests(unittest.TestCase):
    def setup_feed(self,**kwargs):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        feed=module.PcLiveFeed(temporary.name,**kwargs)
        self.addCleanup(lambda:(feed.close(),feed._thread.join(2)))
        return feed,Path(temporary.name)/'pc.json'

    def await_document(self,path,condition=lambda row:True):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            try:
                result=json.loads(path.read_bytes())
                if condition(result):return result
            except (FileNotFoundError,json.JSONDecodeError):pass
            time.sleep(.01)
        self.fail('observer did not publish requested diagnostic')

    def test_disabled_feed_starts_no_worker(self):
        with patch.object(module.threading.Thread,'start') as start:
            self.assertIsNone(module.start_feed(None))
        start.assert_not_called()

    def test_boot_has_zero_frames_and_unknown_actual_session(self):
        feed,path=self.setup_feed()
        row=self.await_document(path)
        self.assertEqual(row['stage'],'CONNECTING_JETSON');self.assertIsNone(row['session_id'])
        self.assertIsNone(row['binding']);self.assertIsNone(row['source_sequence'])
        self.assertEqual(row['media'],[]);self.assertTrue(all(value==0 for value in row['counters'].values()))
        self.assertEqual(row['run_id_origin'],'DIAGNOSTIC_UUID');self.assertFalse(row['authority'])

    def test_same_frame_survives_heartbeat_without_refreshing_original_capture_or_expiry(self):
        feed,path=self.setup_feed(run_id='explicit-run')
        original=original_frame();self.assertTrue(feed.offer_frame(original))
        first=self.await_document(path,lambda row:row['source_sequence']==7)
        later=self.await_document(path,lambda row:row['sequence']>first['sequence'])
        self.assertEqual(later['media'],first['media']);self.assertEqual(later['source_stamp'],first['source_stamp'])
        self.assertEqual(later['captured_interval_us'],[100,101]);self.assertEqual(later['session_id'],uid(2))
        self.assertEqual([row['captured_interval_us'] for row in later['media']],[[100,101],[102,103]])
        self.assertEqual([row['original_stamp']['pc_interval_us'] for row in later['media']],[[100,101],[102,103]])
        self.assertEqual([row['expires_pc_us'] for row in later['media']],[140,140])
        self.assertEqual(base64.b64decode(later['media'][0]['data_base64']),original.rgb)
        self.assertEqual(base64.b64decode(later['media'][1]['data_base64']),original.depth)
        self.assertLess(path.stat().st_size,module.MAX_BYTES)

    def test_trylock_drop_does_not_wait_or_take_a_frame(self):
        feed,_=self.setup_feed();original=original_frame()
        with feed._lock:
            before=time.monotonic();accepted=feed.offer_frame(original)
            elapsed=time.monotonic()-before
        self.assertFalse(accepted);self.assertLess(elapsed,.05);self.assertIsNone(feed._frame)

    def test_frame_offer_recovers_parent_binding_without_a_separate_boot_offer(self):
        feed,path=self.setup_feed()
        fields=dict(source_id=uid(1),pc_clock_epoch=uid(3),jetson_session=uid(2),destination_clock_epoch=uid(4))
        self.assertTrue(feed.offer_frame(original_frame(),binding=SimpleNamespace(fields=fields)))
        row=self.await_document(path,lambda row:row['source_sequence']==7)
        self.assertEqual(row['binding'],fields);self.assertEqual(row['session_id'],uid(2))

    def test_blocked_observer_disk_does_not_hold_offer_lock_or_close_caller(self):
        entered=threading.Event();release=threading.Event()
        def blocked_write(owner,row):entered.set();release.wait(2)
        with patch.object(module.PcLiveFeed,'_write',blocked_write):
            feed,_=self.setup_feed();self.assertTrue(entered.wait(1))
            before=time.monotonic()
            self.assertTrue(feed.offer_frame(original_frame()))
            feed.close();elapsed=time.monotonic()-before
            self.assertLess(elapsed,.05)
            release.set();feed._thread.join(2)
        self.assertFalse(feed._thread.is_alive())

    def test_terminal_completion_abandons_stalled_disk_within_its_own_budget(self):
        entered=threading.Event();release=threading.Event()
        def blocked_write(owner,row):entered.set();release.wait(2)
        with patch.object(module.PcLiveFeed,'_write',blocked_write):
            feed,_=self.setup_feed();self.assertTrue(entered.wait(1))
            before=time.monotonic();completed=feed.finish_terminal(.05)
            elapsed=time.monotonic()-before
            self.assertFalse(completed);self.assertLess(elapsed,.2)
            release.set();feed._thread.join(2)

    def test_actual_cli_process_publishes_final_failure_before_daemon_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            code=('import time\nfrom tools.live_observer.feed import PcLiveFeed\n'
                'original=PcLiveFeed._write\n'
                'PcLiveFeed._write=lambda self,row:(time.sleep(.05),original(self,row))[1]\n'
                'from tools.hil_observation.pc import main\nraise SystemExit(main())\n')
            result=subprocess.run([sys.executable,'-c',code,*failure_cli_arguments(directory)],
                env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,timeout=10)
            self.assertEqual(result.returncode,1,result.stderr.decode(errors='replace'))
            row=json.loads((Path(directory)/'pc.json').read_bytes())
            self.assertEqual(row['stage'],'FAILED');self.assertEqual(row['error'],'ValueError')
            self.assertIsNone(row['session_id']);self.assertEqual(row['media'],[])

    def test_actual_cli_process_exits_when_terminal_diagnostic_write_is_stalled(self):
        with tempfile.TemporaryDirectory() as directory:
            code=('import time\nfrom tools.live_observer.feed import PcLiveFeed\n'
                'PcLiveFeed._write=lambda self,row:time.sleep(5)\n'
                'from tools.hil_observation.pc import main\nraise SystemExit(main())\n')
            began=time.monotonic()
            result=subprocess.run([sys.executable,'-c',code,*failure_cli_arguments(directory)],
                env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,timeout=10)
            self.assertEqual(result.returncode,1,result.stderr.decode(errors='replace'))
            self.assertLess(time.monotonic()-began,3)

    def test_failed_disk_keeps_drive_offer_and_records_only_observation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'regular-file';target.write_text('existing')
            feed=module.PcLiveFeed(target);self.addCleanup(lambda:(feed.close(),feed._thread.join(2)))
            deadline=time.monotonic()+2
            while not feed._write_failures and time.monotonic()<deadline:time.sleep(.01)
            self.assertGreaterEqual(feed._write_failures,1);self.assertIsNone(feed.failure)
            self.assertTrue(feed.offer_frame(original_frame()));self.assertEqual(target.read_text(),'existing')
            self.assertFalse(feed.finish_terminal())

    def test_transient_write_error_is_reported_in_next_successful_heartbeat(self):
        original_write=module.PcLiveFeed._write;calls=[]
        def write(owner,row):
            calls.append(row['sequence'])
            if len(calls)==1:raise PermissionError(13,'temporary reader sharing conflict')
            return original_write(owner,row)
        with patch.object(module.PcLiveFeed,'_write',write):
            feed,path=self.setup_feed()
            row=self.await_document(path,lambda row:row.get('write_failures')==1)
            self.assertEqual(row['io_error']['type'],'PermissionError');self.assertEqual(row['io_error']['errno'],13)
            self.assertIsNone(feed.failure);self.assertTrue(feed._thread.is_alive())
            self.assertTrue(feed.offer_frame(original_frame()))
            recovered=self.await_document(path,lambda row:row['source_sequence']==7)
            self.assertEqual(recovered['write_failures'],1);self.assertEqual(recovered['io_error'],row['io_error'])
            self.assertGreater(recovered['sequence'],row['sequence']);self.assertEqual(len(recovered['media']),2)

    def test_snapshots_are_worker_only_and_preserve_original_terminal_reason(self):
        feed,path=self.setup_feed()
        caller=threading.get_ident();threads=[]
        def snapshot():
            threads.append(threading.get_ident())
            return dict(counts=dict(rendered=1,taken=1),failed=False,session_done=True,
                session_result=dict(reason='DURATION'),failure=None)
        fields=dict(source_id=uid(1),pc_clock_epoch=uid(3),jetson_session=uid(2))
        feed.bind(SimpleNamespace(fields=fields))
        feed.attach(producer=SimpleNamespace(snapshot=snapshot),
            peer=SimpleNamespace(snapshot=lambda:dict(received=12,command_hwm=0,applied=0)))
        row=self.await_document(path,lambda row:row['producer'] is not None)
        self.assertTrue(threads);self.assertNotIn(caller,threads)
        self.assertEqual(row['binding'],fields);self.assertEqual(row['stop_reason'],'DURATION')
        self.assertEqual(row['stage'],'ENDED');self.assertEqual(row['peer']['command_hwm'],0)

    def test_failed_boot_is_observable_before_any_frame_or_terminal_receipt(self):
        feed,path=self.setup_feed()
        feed.offer_state('FAILED','FAILED',error='ConnectionRefusedError');feed.close()
        row=self.await_document(path,lambda row:row['stage']=='FAILED')
        self.assertEqual(row['error'],'ConnectionRefusedError');self.assertEqual(row['media'],[])
        self.assertIsNone(row['source_stamp']);self.assertIsNone(row['session_id'])

    def test_renderer_observer_failure_does_not_fence_capture_or_change_latest1(self):
        value=producer_module.PcObservationProducer.__new__(producer_module.PcObservationProducer)
        value._observer=SimpleNamespace(offer_frame=Mock(side_effect=OSError('diagnostic only')))
        value._guard=threading.Lock();value._stop=threading.Event();value._latest=None
        value._counts=dict.fromkeys(('attempted','rendered','expired','quantization_future','replaced','taken'),0)
        value._failure=None;value._lifetime=40;value._deadline_us=200;value._interval=10
        value._epoch=uid(4);value._clock_epoch=uid(3);value._motion=value._renderer_profile=value._scene=None
        value._clock=Mock(side_effect=(100,102,200));value._scope=original_frame().scope
        value._binding=SimpleNamespace(fields=dict(calibration_profile_sha256='b'*64))
        capture=SimpleNamespace(observation=SimpleNamespace(rgb=SimpleNamespace(stamp=SimpleNamespace(sequence=7),payload=bytes(345600)),
            depth=SimpleNamespace(payload=bytes(614400))),captured_j_us=(100,101),completed_j_us=110,expires_j_us=140)
        camera=SimpleNamespace(calibration=SimpleNamespace(sha256='b'*64),capture=Mock(return_value=capture),close=Mock())
        with patch('xenova_vehicle.simulation.wall_sensor.WallSyntheticCamera',return_value=camera),\
                patch('xenova_vehicle.simulation.wall_sensor.WallPoseSample.sample',return_value=SimpleNamespace(captured_j_us=(100,101))):
            value._render_loop(SimpleNamespace(renderer_pose=lambda:None))
        self.assertEqual(value._counts['rendered'],1);self.assertIsNone(value._failure)
        self.assertFalse(value._stop.is_set());self.assertIsNone(value._observer)
        self.assertEqual(value._latest.sequence,7);camera.close.assert_called_once()

    def test_pc_entry_reports_pre_capture_failure_without_a_board_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(observer_dir=directory,observer_run_id='entry-run',device_session='invalid')
            created=[];factory=module.start_feed
            def create(*args,**kwargs):
                result=factory(*args,**kwargs);created.append(result);return result
            with patch.object(pc_module,'_arguments',return_value=args),patch.object(module,'start_feed',side_effect=create),\
                    patch.object(pc_module.socket,'create_connection') as connect,redirect_stdout(io.StringIO()):
                self.assertEqual(pc_module.main(),1)
            connect.assert_not_called()
            self.assertEqual(len(created),1);created[0]._thread.join(2)
            row=self.await_document(Path(directory)/'pc.json',lambda row:row['stage']=='FAILED')
            self.assertEqual(row['run_id'],'entry-run');self.assertIsNone(row['session_id']);self.assertEqual(row['media'],[])

    def test_pc_entry_without_opt_in_does_not_create_observer(self):
        args=SimpleNamespace(observer_dir=None,observer_run_id=None,device_session='invalid')
        with patch.object(pc_module,'_arguments',return_value=args),patch.object(module,'start_feed') as create,\
                redirect_stdout(io.StringIO()):
            self.assertEqual(pc_module.main(),1)
        create.assert_not_called()


if __name__=='__main__':unittest.main()
