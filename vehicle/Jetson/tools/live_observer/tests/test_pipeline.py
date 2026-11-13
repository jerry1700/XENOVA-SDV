"""명시 fixture의 feed·공용 wire→collector→viewer 교차 시험.

기존 C/Python 계약의 World/DriveState fixture와 선언한 PC/가상 장치 leaf를 쓴다.
NN·보드·broker·주행 프로세스를 실행하거나 전체 HIL/물리 수락을 주장하지 않는다.
"""
import copy
import importlib.util
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

ROOT=Path(__file__).resolve().parents[5]
JETSON=ROOT/'vehicle/Jetson'
VIEWER=JETSON/'tools/streamlit_viewer'
sys.path[:0]=[str(JETSON),str(VIEWER),str(ROOT/'platform/apps/drive/world/python')]

from tools.hil_observation.transport import ObservationFrame,SessionScope
from tools.live_observer.collector import ObservationCollector
from tools.live_observer.feed import PcLiveFeed
from tools.live_observer.sources import WORLD_TOPIC,VEHICLE_TOPIC,decode_mqtt,project_operator_status
from xenova_drive_world import encode,schema
from snapshot import SnapshotReader
from presentation import input_stages,live_status,operation_cards
from render import encoded_dimensions,map_panels

_spec=importlib.util.spec_from_file_location('pipeline_shared_world_fixture',ROOT/'platform/apps/drive/tests/test_world.py')
wire=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(wire)
VM_A=wire.fixture.BOOT
DEVICE=str(UUID(int=120))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.now=100.
        self.collector=ObservationCollector(self.root,clock=lambda:self.now)
        self.world=wire.parking_world();self.world.occupancy=bytes([255])+self.world.occupancy[1:]
        self.state=wire.parking_state()
        self.frame=ObservationFrame(SessionScope(str(UUID(int=101)),str(UUID(int=102)),
            'a'*64,'b'*64,'c'*64,str(UUID(int=103))),7,
            rgb_pc_interval=(100,101),depth_pc_interval=(102,103),render_completed_pc_us=110,
            expires_pc_us=140,rgb=bytes([16])*(640*360)+bytes([128])*(640*360//2),
            depth=struct.pack('<H',1000)*(640*480))
        binding=SimpleNamespace(fields=dict(source_id=str(UUID(int=101)),pc_clock_epoch=str(UUID(int=103)),
            jetson_session=str(UUID(int=102)),destination_clock_epoch=self.world.clock_epoch))
        # 호출자가 소유한 명시 leaf로 실제 feed encoder만 사용한다. daemon/운행 owner는 시작하지 않는다.
        with patch('threading.Thread.start'):
            self.feed=PcLiveFeed(self.root,run_id='declared-fixture-run')
        self.feed.offer_state('CAPTURING','RUNNING')
        self.feed.offer_frame(self.frame,binding=binding)
        self.feed._peer=SimpleNamespace(snapshot=lambda:dict(device_session=DEVICE,received=100,
            command_hwm=5,applied=0))

    def event(self,topic,value):
        raw=encode(value);result=decode_mqtt(topic,raw)
        self.assertEqual(encode(value),raw)
        return result

    def operator(self):
        current=dict(host_boot_id=wire.fixture.BOOT,gateway_instance_id=wire.fixture.INSTANCE,
            generation=1,pi_now_ms=1100,mode='AUTO',owner=VM_A,output_permitted=True,
            hold=dict(active=False,reason=None),device=dict(applied={'sequence':'1'}))
        operating=dict(contract='operation-status-1',supported=True,host_boot_id=current['host_boot_id'],
            gateway_instance_id=current['gateway_instance_id'],generation=1,pi_now_us='1100000',
            binding=dict(service_epoch=wire.fixture.INSTANCE,operation_revision='3',
                departure_nonce=str(UUID(int=150)),permission_revision='5',parking_enabled=False),
            vehicle_phase='ACTIVE',ready_revision='7',auto_ready=True,blocked_reason=None,
            valid_until_pi_us='1200000',countdown_until_pi_us='0',desired_mode='AUTO')
        self.collector.ingest_operator(project_operator_status(
            dict(version=2,ok=True,data=current),dict(version=2,ok=True,data=operating)))

    def all_sources(self):
        self.collector.ingest_pc(self.feed._document(1))
        self.collector.ingest_event(self.event(WORLD_TOPIC,self.world))
        sample=schema.VehicleSample(major=1,vehicle=self.world.vehicle,host_boot=wire.fixture.BOOT,
            device_session=DEVICE,gateway_instance=wire.fixture.INSTANCE,pi_session=str(UUID(int=121)),
            odom_epoch=self.world.odom_epoch,sequence=2,source_sample=7,source_us=1000000,
            pi_sample_us=1100000,clock_error_us=10,left_ticks=1,right_ticks=2,
            odom_pose=schema.Pose(x=2.,y=3.,yaw=.4),velocity_mps=.25,position_error_m=.01,
            heading_error_rad=.02,quality=3,calibration=self.world.calibration,estimated_steering_rad=.1)
        self.collector.ingest_event(self.event(VEHICLE_TOPIC,sample))
        self.collector.ingest_event(self.event(f'xenova/v1/vms/{VM_A}/drive/link/out',schema.DriveLink(major=2,snapshot=self.state)))
        self.operator()
        return self.collector.publish()

    def test_original_feed_and_wire_reach_valid_viewer_with_png_owner_path_and_independent_stages(self):
        body=self.all_sources();result=SnapshotReader(self.root).read()
        self.assertEqual(result.state,'VALID')
        self.assertEqual(body['live']['session_id'],self.frame.scope.fields['session_id'])
        media={item.descriptor['panel']:item for item in result.snapshot.media}
        self.assertEqual(media['rgb'].raw,self.frame.rgb);self.assertEqual(media['depth'].raw,self.frame.depth)
        self.assertEqual(media['rgb'].descriptor['original_stamp']['pc_interval_us'],[100,101])
        self.assertEqual(encoded_dimensions(media['grid'].raw,'PNG'),(self.world.width,self.world.height))
        self.assertEqual(media['grid'].descriptor['stamp']['domain'],'JETSON_MONOTONIC')
        self.assertEqual(media['grid'].descriptor['stamp']['us'],self.world.observed_us)
        cards=operation_cards(body,result_state='VALID',source_age_state='UNMAPPED')
        self.assertEqual([x.current for x in cards[:3]],['AUTO',VM_A,'UNKNOWN'])
        self.assertIsNone(body['operation']['standby_vm'])  # owner A는 B의 준비 판정이 아니다.
        frames=dict(map_panels(body));map_frame=f'MAP:{self.world.map_epoch}:{self.world.frame_epoch}'
        self.assertEqual(set(frames),{map_frame,'ODOMETRY:'+self.world.odom_epoch})
        paths=[x for x in body['map']['layers'] if x['kind']=='PATH']
        self.assertEqual(paths[0]['points'],[[p.pose.x,p.pose.y] for p in self.state.path])
        self.assertEqual(body['identities']['world_reference_pose']['vehicle_sequence'],self.world.reference_odom_seq)
        self.assertEqual([x['관측'] for x in input_stages(body,result_state='VALID')],
            ['UNKNOWN','OBSERVED','UNKNOWN','UNKNOWN'])
        self.assertIsNone(body['live']['stages']['command']['source_stamp'])
        # 실제 source/collector 파일을 AppTest가 읽는다. 별도 HTTP/장치 연결은 없다.
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ,{'XENOVA_VIEWER_OBSERVATION_DIR':str(self.root)}):
            app=AppTest.from_file(str(VIEWER/'app.py'),default_timeout=20).run()
        self.assertEqual(list(app.exception),[])
        self.assertEqual([x.value for x in app.metric[:3]],['AUTO',VM_A,'UNKNOWN'])
        tables=[x.value for x in app.table]
        self.assertTrue(any('관측' in t and t['관측'].tolist()==['UNKNOWN','OBSERVED','UNKNOWN','UNKNOWN'] for t in tables))
        self.assertTrue(all(x.disabled for x in app.button))

    def test_foreign_world_or_path_do_not_become_current_geometry(self):
        self.collector.ingest_pc(self.feed._document(1))
        foreign=copy.deepcopy(self.world);foreign.clock_epoch=str(UUID(int=199))
        self.collector.ingest_event(self.event(WORLD_TOPIC,foreign))
        self.assertNotIn('world',self.collector.received)
        self.assertEqual(self.collector.publish()['map']['layers'],[])
        self.collector.ingest_event(self.event(WORLD_TOPIC,self.world))
        foreign_state=copy.deepcopy(self.state);foreign_state.frame_epoch=str(UUID(int=198))
        self.collector.ingest_event(self.event(f'xenova/v1/vms/{VM_A}/drive/link/out',schema.DriveLink(major=2,snapshot=foreign_state)))
        result=SnapshotReader(self.root);self.collector.publish();view=result.read()
        self.assertEqual(view.state,'VALID')
        self.assertFalse(any(x['kind']=='PATH' for x in view.snapshot.body['map']['layers']))
        self.assertEqual(view.snapshot.body['live']['connection']['last_error']['reason'],'PATH_WORLD_FRAME_MISMATCH')
        self.assertIsNone(view.snapshot.body['operation'])

    def test_heartbeat_and_gateway_reports_do_not_refresh_frame_or_infer_applied_and_movement(self):
        self.all_sources();self.now+=4
        self.collector.ingest_pc(self.feed._document(2));body=self.collector.publish()
        result=SnapshotReader(self.root).read();self.assertEqual(result.state,'VALID')
        rows={x['name']:x for x in body['live']['streams']}
        self.assertEqual(rows['pc']['state'],'FRESH');self.assertEqual(rows['rgb']['state'],'STALE')
        self.assertEqual(rows['rgb']['received_count'],1);self.assertEqual(rows['rgb']['source_sequence'],'7')
        self.assertEqual(rows['world']['state'],'STALE');self.assertEqual(rows['operation']['state'],'STALE')
        cards=operation_cards(body,result_state='VALID',source_age_state='UNMAPPED')
        self.assertEqual(cards[0].current,'UNKNOWN');self.assertEqual(cards[0].reported,'AUTO')
        self.assertEqual(body['live']['stages']['applied']['state'],'UNKNOWN')
        self.assertEqual(body['live']['stages']['movement']['state'],'UNKNOWN')
        changed=self.feed._document(3);changed['peer']['applied']=1
        self.collector.ingest_pc(changed);body=self.collector.publish()
        self.assertEqual(body['live']['stages']['applied']['state'],'OBSERVED')
        self.assertEqual(body['live']['stages']['movement']['state'],'UNKNOWN')
        self.assertEqual(live_status(body,result_state='VALID')['streams'][1]['원 순번'],'7')


if __name__=='__main__':unittest.main()
