"""명시 운영 설정을 원 pixels/SQL/S-M/JIO World 경계까지 잇는 software 회귀다.

Pi Gate/정지/시계는 명시 software fixture이며 실제 Pi/Gateway/장치 수락이 아니다.
원 Processor·Source·Compute·SQLite와 JIO final claim/FakeClient는 실제 구현을 쓴다.
controlled J는 기능용이며 실시간500ms/FPS/물리 정지·즉시 remote World 회수는 수락하지 않는다.
"""

import os
import json
import socket
import unittest

from mapper_processor_flow_rig import ProcessorFlowRig, uid
import test_mapper_processor_full_flow as full_flow_checks
import test_operation_management as management_fixtures
from xenova_vehicle.runtime.operation_management import OperationServer
from xenova_vehicle.storage.contracts import canonical


@unittest.skipUnless(os.name=='posix' and hasattr(socket,'SOCK_SEQPACKET'),
    'actual SQLite and Linux S/M seqpacket required')
class MapperOperationFullFlowTests(unittest.TestCase):
    def rig(self, **kwargs):
        value=ProcessorFlowRig.__new__(ProcessorFlowRig)
        self.addCleanup(value.close)
        value.__init__(**kwargs)
        return value

    def request(self, rig, action):
        identity=uid()
        raw=canonical(dict(profile='jetson-operation-request-1',service_instance=rig.peer.service_instance,
            request_id=identity,action=action,issued_j_us=str(rig.now),expires_j_us=str(rig.now+100_000)))
        # 임시 metadata는 실제 시험 PID/birth만 인증한다. ResourceStore의 admission을 가장하지 않는다.
        metadata=management_fixtures.OperationManagementTests(methodName='runTest')
        metadata.setUp()
        endpoint=None
        try:
            metadata.service=rig.peer.service_instance
            metadata.fields['service_instance']=metadata.service
            metadata._write_manifest()
            server=OperationServer(metadata.directory,metadata.service,status=rig.operation_owner.status,
                submit=rig.operation_owner.submit,query=rig.operation_owner.query,clock=lambda:rig.now)
            metadata.servers.append(server)
            server.start()
            rig.operation_pump=server.poll
            endpoint=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
            endpoint.connect(server.address)
            endpoint.setblocking(False)
            received=[]
            def packet():
                try: content,_,flags,_=endpoint.recvmsg(4097)
                except BlockingIOError:return False
                self.assertTrue(content)
                self.assertFalse(flags & socket.MSG_TRUNC)
                self.assertLessEqual(len(content),4096)
                received.append(json.loads(content))
                return True
            rig.wait(packet,'actual management service HELLO',timeout=2.)
            hello=received.pop()
            self.assertEqual(hello['profile'],'jetson-operation-management-1')
            self.assertEqual(hello['service_instance'],rig.peer.service_instance)
            message=canonical(dict(profile='jetson-operation-client-1',kind='REQUEST',request=raw.decode('ascii')))
            self.assertEqual(endpoint.send(message),len(message))
            rig.wait(packet,'actual management request/S/M/result',timeout=2.)
            response=received.pop()
            self.assertEqual(response['profile'],'jetson-operation-response-1')
            self.assertEqual(response['service_instance'],rig.peer.service_instance)
            self.assertEqual(response['kind'],'REQUEST')
            self.assertEqual(response['result'],rig.operation_owner.query(identity))
            self.assertEqual(rig.operation_owner._records[identity]['request'].raw_bytes,raw)
            return raw,response['result']
        finally:
            rig.operation_pump=None
            if endpoint is not None:endpoint.close()
            metadata.tearDown()

    def world(self, rig):
        world=rig.world()
        full_flow_checks.MapperProcessorFullFlowTests.assert_world(self,rig,world)
        return world

    def test_wait_manual_keep_run_explicit_standby_latches_and_same_s_rejoins_m(self):
        rig=self.rig()
        first=self.world(rig)
        factory=rig.processor
        for mode in ('WAIT','MANUAL'):
            rig.operation_probe_mode=mode
            latest=self.world(rig)
            self.assertGreater(latest.sequence,first.sequence)
            self.assertEqual(rig.operation_owner.status()['confirmed'],'RUN')
            self.assertFalse(rig.compute.snapshot()['operation_standby'])
            self.assertIs(rig.processor,factory)
            first=latest

        original,result=self.request(rig,'STANDBY')
        self.assertEqual(result['status'],'APPLIED',result)
        self.assertEqual(result['action'],'STANDBY')
        self.assertLess(int(result['completed_j_us']),int(result['expires_j_us']))
        self.assertEqual(rig.operation_owner.submit(original),result)
        self.assertTrue(rig.compute.snapshot()['operation_standby'])
        self.assertFalse(rig.mapper.operation_world_enabled)
        self.assertIsNone(rig.publications.peek('WORLD_CANDIDATE',now_us=rig.now))
        original_map=rig.processor._accepted
        original_worlds=len(rig.jio.worlds)
        original_vehicle=rig.vehicle_sequence

        # 원 Gate와 원 요청의100ms보다 이후다. 숫자 J 진행은 과거 Gate/관측 E를 바꾸지 않는다.
        rig.now+=200_000
        rig.operation_probe_mode=None
        rig.renew_clock()
        for at in (rig.now-11_000,rig.now-1000):
            rig._offer_vehicle(at)
            rig.tick()
        self.assertGreater(rig.vehicle_sequence,original_vehicle)
        self.assertEqual(rig.operation_owner.status()['confirmed'],'STANDBY')
        self.assertTrue(rig.compute.snapshot()['operation_standby'])
        self.assertIs(rig.processor._accepted,original_map)
        self.assertFalse(rig.compute.submit(rig.input()))
        self.assertEqual(len(rig.jio.worlds),original_worlds)

        _,result=self.request(rig,'RUN')
        self.assertEqual(result['status'],'APPLIED',result)
        self.assertFalse(rig.compute.snapshot()['operation_standby'])
        self.assertEqual(len(rig.jio.worlds),original_worlds)  # ACK는 새 관측이나 READY가 아니다.
        self.assertIs(rig.processor,factory)
        resumed=self.world(rig)
        self.assertGreater(resumed.sequence,first.sequence)
        self.assertIs(rig.processor,factory)

        rig.operation_probe_mode='WAIT'
        rig.renew_clock()
        _,result=self.request(rig,'STANDBY')
        self.assertEqual(result['status'],'APPLIED',result)
        source_owner=rig.operation_owner
        rig.stop()
        fresh=self.rig(database=rig.db,predecessor=rig)
        self.assertIs(fresh.operation_owner,source_owner)
        self.assertNotEqual(fresh.mapper_worker,rig.mapper_worker)
        self.assertNotEqual(fresh.peer.channel_epoch,rig.peer.channel_epoch)
        self.assertEqual(fresh.mapper_boot,rig.mapper_boot)
        self.assertEqual(fresh.raw.canonical_bytes,rig.raw.canonical_bytes)
        self.assertTrue(fresh.mapper.operation_ready)
        self.assertTrue(fresh.compute.snapshot()['operation_standby'])
        self.assertEqual(fresh.operation_owner.status()['confirmed'],'STANDBY')
        self.assertFalse(fresh.mapper.operation_world_enabled)
        self.assertEqual(fresh.jio.worlds,[])
