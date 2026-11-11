"""실제 A loop/S timer에서 준비 부족과 실제 owner 정체를 구분한다.

SDK·actor·camera frame은 제어된 대역이며 실제 OAK 상주나 OS 복구 증거가 아니다.
원 acquisition_main의 start/poll/finally close와 S의 고장 dispatch를 실행한다.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from slot_message_fixtures import writer_context
from xenova_vehicle.runtime import resident_workers as workers,resident_runtime as runtime,pc_resident_runtime as pc_runtime


class AcquisitionHealthTests(unittest.TestCase):
    def run_loop(self,streams,times):
        context=writer_context();clock=SimpleNamespace(now=1000);stages=[];faults=[]
        boot=SimpleNamespace(peer=context.bind.context.peer,profile=SimpleNamespace(device_mxid='test-oak',sync_tolerance_us=5000),
            raw_scope=context.scope,raw_profile=context.bind.context.profile,raw_tokens=(),
            jpeg_endpoint=None,startup_deadline_us=90_001_000)
        actor=SimpleNamespace(state='STARTING',device_closed=True,failure=None,stop=None,
            await_bind=lambda:context.bind,save=Mock(),heartbeat=Mock(),drain_sent=Mock(),finish=Mock(),
            progress=lambda stage,*args,**kwargs:stages.append(stage),fault=faults.append)
        ticks=iter(times)
        def poll():
            try:clock.now=next(ticks)
            except StopIteration:raise workers._StopWork()
            return ()
        actor.poll=poll
        counts=SimpleNamespace(start=0,close=0,polls=0)
        def start(stage):
            counts.start+=1
            for value in ('ASSET_CHECK','DEVICE_BOOT','PIPELINE_START'):stage(value)
            return dict(model_asset=dict(blob_sha256=boot.raw_profile.model_id),queues_configured_j_us='1000')
        def source_poll():
            counts.polls+=1
            return tuple(SimpleNamespace(stream=name,received_j_us=clock.now) for name in streams)
        def close():counts.close+=1
        source=SimpleNamespace(start=start,poll=source_poll,close=close,sample_clock=lambda:dict(sdk_us=clock.now))
        assembler=SimpleNamespace(bridge=SimpleNamespace(update=Mock()),observe=lambda item:(),latest=lambda now:None)
        writer=SimpleNamespace(close=Mock(),publish=Mock())
        status=0
        with patch.object(workers,'_Actor',return_value=actor),patch.object(workers,'j_us',side_effect=lambda:clock.now),\
                patch.object(workers,'_hold_device_lease',return_value=42),patch.object(workers,'CaptureAssembler',return_value=assembler),\
                patch.object(workers.time,'sleep'),patch('xenova_vehicle.runtime.native_resources.RawArenaWriter',return_value=writer),\
                patch('xenova_vehicle.oak.yolop_device.YolopOakSource',return_value=source):
            try:workers.acquisition_main(object(),boot)
            except SystemExit as error:status=error.code
        writer.publish.assert_not_called()
        return status,counts,faults,stages,actor

    def test_one_silent_raw_stream_before_capture30_is_an_actual_stream_fault(self):
        status,counts,faults,stages,actor=self.run_loop(('rgb_video',),(1_001_000,16_001_000))
        self.assertEqual(status,1);self.assertEqual(len(faults),1)
        self.assertIsInstance(faults[0],TimeoutError)
        self.assertIn('active RGB/depth stream stalled',str(faults[0]))
        self.assertNotIn('CAPTURE_READY',stages)
        self.assertEqual((counts.start,counts.close),(1,1));self.assertTrue(actor.device_closed)

    def test_missing_both_raw_streams_is_bounded_from_actual_queue_start(self):
        status,counts,faults,stages,_=self.run_loop((),(16_001_000,))
        self.assertEqual(status,1);self.assertIsInstance(faults[0],TimeoutError)
        self.assertEqual((counts.start,counts.close),(1,1))

    def test_live_streams_without_fresh_pairs_keep_one_model_past_cold90_until_explicit_stop(self):
        status,counts,faults,stages,actor=self.run_loop(('rgb_video','depth_right'),
            tuple(range(1_001_000,112_000_000,10_000_000)))
        self.assertEqual(status,0);self.assertEqual(faults,[])
        self.assertGreater(counts.polls,9)
        self.assertNotIn('CAPTURE_READY',stages)
        self.assertEqual((counts.start,counts.close),(1,1));self.assertTrue(actor.device_closed)


class RuntimeControlHealthTests(unittest.TestCase):
    def runtime(self,role,*,heartbeat,graph=True):
        value=runtime._Runtime.__new__(runtime._Runtime)
        failures=[]
        value.log_pending=None;value.stopping=False;value.resident_started_us=None
        value.startup_deadline=90_001_000;value.a_phase='WARMUP';value.db_broker=None
        value.capture_ready_us=value.nn_ready_us=value.storage_ready_us=None
        value.progress={('A','GRAPH_STARTED'):dict(completed_us=1000)} if graph else {}
        value.recovery=SimpleNamespace(active=None,suppressed=lambda role,host:False,
            fail=lambda role,reason:failures.append((role,reason)))
        value.workers={role:SimpleNamespace(local_watch=None,bind_draft=None,bind_ack=None,
            discovery=None,bootstrap=None,compute_watch=None,last_heartbeat_us=heartbeat,hello=True,admitted=True)}
        return value,failures

    def test_a_control_stall_after_graph_is_detected_before_first_capture_pair(self):
        value,failures=self.runtime('A',heartbeat=1000)
        with patch.object(runtime,'j_us',return_value=6_001_000):value._timers()
        self.assertEqual(len(failures),1);self.assertEqual(failures[0][0],'A')
        self.assertIsNone(value.capture_ready_us)

    def test_m_control_stall_after_hello_is_detected_while_waiting_for_raw_bind(self):
        value,failures=self.runtime('M',heartbeat=1000)
        with patch.object(runtime,'j_us',return_value=6_001_000):value._timers()
        self.assertEqual(len(failures),1);self.assertEqual(failures[0][0],'M')
        self.assertIsNone(value.workers['M'].bind_ack)

    def test_healthy_control_does_not_require_capture_or_nn_readiness_after_cold90(self):
        for role in ('A','M'):
            with self.subTest(role=role):
                value,failures=self.runtime(role,heartbeat=100_001_000)
                with patch.object(runtime,'j_us',return_value=100_001_000):value._timers()
                self.assertEqual(failures,[])
                self.assertIsNone(value.capture_ready_us);self.assertIsNone(value.nn_ready_us)

    def test_a_cold_device_boot_keeps_the_single_startup_budget(self):
        value,failures=self.runtime('A',heartbeat=1000,graph=False)
        value.a_phase='DEVICE_BOOT'
        with patch.object(runtime,'j_us',return_value=value.startup_deadline),self.assertRaises(TimeoutError):value._timers()
        self.assertEqual(failures,[])


class PcLocalStartupBoundaryTests(unittest.TestCase):
    def runtime(self):
        value=pc_runtime.PcResidentRuntime.__new__(pc_runtime.PcResidentRuntime)
        value.began=1000;value.startup_deadline=90_001_000
        value.stopping=False;value.resident_started_us=None;value._pc_start=None
        value.progress={};value.log_pending=None;value.log=SimpleNamespace(submit=Mock(return_value=True))
        value.recovery=pc_runtime._PcRecovery(value)
        return value

    def test_verified_pc_model_start_releases_only_future_operation_cold_cap(self):
        value=self.runtime()
        # _pc_message sets this slot only after owner.observe_nn_start validated the actual receipt.
        original_start=SimpleNamespace(packet=SimpleNamespace(issued_us=2000,expires_us=3000))
        value._pc_start=original_start
        self.assertEqual(value.recovery.operation_cap,2**64-1)
        now=100_001_000
        with patch.object(runtime,'j_us',return_value=now):
            self.assertTrue(value._request('BIRTHS','BIRTHS'))
        self.assertEqual(value.log.submit.call_args.kwargs['issued_us'],now)
        self.assertEqual(value.log.submit.call_args.kwargs['expires_us'],now+5_000_000)
        self.assertIs(value._pc_start,original_start)
        self.assertEqual((original_start.packet.issued_us,original_start.packet.expires_us),(2000,3000))
        self.assertIsNone(value.resident_started_us)

    def test_missing_pc_model_start_keeps_original_cold_operation_cap(self):
        value=self.runtime()
        self.assertEqual(value.recovery.operation_cap,value.startup_deadline)
        with patch.object(runtime,'j_us',return_value=value.startup_deadline),self.assertRaises(TimeoutError):
            value._request('BIRTHS','BIRTHS')
        value.log.submit.assert_not_called()


if __name__=='__main__':unittest.main()
