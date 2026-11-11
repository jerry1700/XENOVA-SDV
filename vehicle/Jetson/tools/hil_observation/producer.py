"""같은 실제 가상 plant의 locked pose reader를 PC 관측 송신에 연결한다.

물리/feedback owner를 만들거나 poll하지 않는다. caller는 start_session을 명시 호출한
뒤 기존 run_peer_session의 on_session에 attach를 연결한다. GT는 renderer 안에서만
사용하고 원 PC capture pair/completion/E와 pixel bytes만 전송한다. SDK·Vehicle·지도·
READY를 생성하지 않으며 종료 진단도 실제 차량 정지나 폐루프 수락을 뜻하지 않는다.
"""

import errno
import math
import socket
import threading
import time
from uuid import UUID

from .session import monotonic_us, run_pc_session
from .transport import SessionScope, ObservationFrame


_U63=(1<<63)-1


def _uuid(value):
    if type(value) is not str or str(UUID(value))!=value or not UUID(value).int:
        raise ValueError('explicit canonical nonzero capture UUID required')
    return value


def _tick(value):
    if type(value) is not int or not 0<=value<_U63:
        raise ValueError('actual floor monotonic microseconds must be a native u63 integer')
    return value


def _seconds(value,maximum=None):
    if (type(value) not in (int,float) or not math.isfinite(value) or value<=0
            or maximum is not None and value>maximum):
        raise ValueError('explicit finite positive bounded duration required')
    return float(value)


class PcObservationProducer:
    """caller가 소유/종료하는 non-daemon renderer1/session1이다.

    constructor는 원 설정만 검산하며 파일·socket I/O·thread 시작을 하지 않는다.
    producer는 render active1/latest1, 기존 session은 별도 Sender active1/pending FRAME1을
    가진다. 이 합계를 전역 두 frame뿐이라고 주장하지 않는다. native render 중단과
    실제 OS process reap은 바깥 supervisor 책임이며 원 plant gap을 늘리지 않는다.
    """

    def __init__(self,observation_endpoint,*,binding,scope,motion_profile,scene,capture_epoch,
                 capture_clock_epoch,duration_s,capture_lifetime_us,render_interval_us,join_timeout_s,
                 clock=monotonic_us):
        from xenova_vehicle.simulation.pc_input import PcSourceBinding
        from xenova_vehicle.simulation.motion_profile import DevelopmentMotionProfile
        from xenova_vehicle.simulation.scene import Scene
        if (type(observation_endpoint) is not socket.socket or type(binding) is not PcSourceBinding
                or type(scope) is not SessionScope or type(motion_profile) is not DevelopmentMotionProfile
                or type(scene) is not Scene or not callable(clock)):
            raise ValueError('actual caller socket and exact original PC scope/profile/scene required')
        binding.check_profile(motion_profile)
        fields=binding.fields
        expected=dict(source_id=fields['source_id'],session_id=fields['jetson_session'],
            source_descriptor_sha256=binding.sha256,calibration_sha256=fields['calibration_profile_sha256'],
            motion_bundle_sha256=fields['motion_profile_sha256'],pc_clock_epoch=fields['pc_clock_epoch'])
        if scope.fields!=expected:raise ValueError('original PC session differs from bound descriptor/model/clock')
        _uuid(capture_epoch);_uuid(capture_clock_epoch)
        if capture_clock_epoch!=fields['pc_clock_epoch']:
            raise ValueError('renderer clock epoch differs from the actual original PC clock')
        duration=_seconds(duration_s);join=_seconds(join_timeout_s,5.)
        if (duration>=_U63/1_000_000 or not 1<int(duration*1_000_000)<_U63 or type(capture_lifetime_us) is not int
                or not 1<capture_lifetime_us<=500_000
                or type(render_interval_us) is not int or not 0<render_interval_us<_U63):
            raise ValueError('original duration/capture500ms/render interval bounds required')
        renderer_profile=motion_profile.development_profile.for_scene(scene)
        motion_profile.check_development_profile(renderer_profile)
        renderer_profile.check_scene(scene)
        self._endpoint,self._binding,self._scope=observation_endpoint,binding,scope
        self._motion,self._renderer_profile,self._scene=motion_profile,renderer_profile,scene
        self._motion_bytes=motion_profile.canonical_bytes
        self._epoch,self._clock_epoch=capture_epoch,capture_clock_epoch
        self._duration,self._lifetime,self._interval,self._join=duration,capture_lifetime_us,render_interval_us,join
        self._clock=clock;self._owner=threading.get_ident()
        self._guard=self._stop=None
        self._session=self._renderer=None
        self._session_started=self._attached=self._session_done=self._closed=False
        self._close_started=False;self._close_deadline=None
        self._failure=None;self._latest=None;self._result=None
        self._started_us=self._deadline_us=None
        self._counts=dict(attempted=0,rendered=0,expired=0,quantization_future=0,replaced=0,taken=0)

    def _owned(self):
        if threading.get_ident()!=self._owner:
            raise RuntimeError('producer start/attach/close belongs to its original caller owner')

    def start_session(self):
        """원 연결에서 clock 교환을 먼저 시작한다. attach/accept로 실행 기한을 연장하지 않는다."""
        self._owned()
        if self._session_started or self._close_started or self._session is not None:
            raise RuntimeError('one original PC session can be started once')
        if self._endpoint.getblocking() or self._endpoint.type!=socket.SOCK_STREAM:
            raise ValueError('caller must hand over one connected nonblocking stream')
        self._endpoint.getpeername()
        now=_tick(self._clock());duration=int(self._duration*1_000_000)
        if duration>=_U63-now:raise ValueError('original PC session deadline overflows')
        self._started_us,self._deadline_us=now,now+duration
        self._guard=threading.Lock();self._stop=threading.Event()
        self._session=threading.Thread(target=self._session_loop,name='pc-observation-session',daemon=False)
        try:
            self._session.start()
            with self._guard:self._session_started=True
        except BaseException as error:
            self._failed('session_start',error)
            with self._guard:self._session_done=True
            self._endpoint.close()
            raise

    def attach(self,plant,peer):
        """기존 feedback 서버의 on_session callback이다. plant/peer를 새로 만들지 않는다."""
        self._owned()
        # coherent feedback package는 실제 PC 역할에서만 읽는다. peer 진단으로 권한을 추론하지 않는다.
        from xenova_vehicle.simulation.plant import VirtualPlant
        from xenova_vehicle.simulation.feedback_peer import VirtualFeedbackPeer
        from xenova_vehicle.simulation.motion_profile import DevelopmentMotionProfile
        if (type(plant) is not VirtualPlant or type(peer) is not VirtualFeedbackPeer or peer.plant is not plant
                or type(plant.profile) is not DevelopmentMotionProfile or plant.profile.canonical_bytes!=self._motion_bytes):
            raise ValueError('same actual plant/feedback peer and original canonical motion profile required')
        if self._attached or self._close_started:raise RuntimeError('one original renderer can be attached once')
        if not self._session_started:self.start_session()
        with self._guard:
            if self._session_done or self._failure is not None or self._stop.is_set():
                raise RuntimeError('original PC session already ended before plant attachment')
            self._renderer=threading.Thread(target=self._render_loop,args=(plant,),
                name='pc-observation-renderer',daemon=False)
            self._attached=True
        try:self._renderer.start()
        except BaseException as error:
            self._failed('renderer_start',error)
            raise

    def _count(self,name):
        self._counts[name]=min(_U63,self._counts[name]+1)

    def _failed(self,role,error):
        # renderer 예외의 임의 repr에 pose/scene 값이 들어가도 진단 밖으로 내보내지 않는다.
        text=(role+': '+type(error).__name__)[:256]
        with self._guard:
            if self._failure is None:self._failure=text
        self._stop.set()

    def _cancelled(self):
        return self._stop.is_set() or _tick(self._clock())>=self._deadline_us

    def _session_loop(self):
        try:
            remaining=self._deadline_us-_tick(self._clock())
            if remaining<=1:raise RuntimeError('original PC session duration expired before thread start')
            result=run_pc_session(self._endpoint,scope=self._scope,duration_s=remaining/1_000_000,
                next_frame=self.next_frame,cancelled=self._cancelled,clock=self._clock,wait_for_probe=True)
            # 원 종료 사실만 고정한다. PC clock 수치나 lease를 Jetson 승인으로 바꾸지 않는다.
            summary=dict(role=result.role,reason=result.reason,started_local_us=result.started_local_us,
                finished_local_us=result.finished_local_us,counters=result.counts,
                last_frame_sequence=result.last_frame_sequence,failure=result.failure)
            with self._guard:
                self._result=summary
                if result.failure is not None and self._failure is None:self._failure=('session: '+result.failure)[:256]
        except BaseException as error:self._failed('session',error)
        finally:
            self._stop.set()
            self._endpoint.close()
            with self._guard:self._session_done=True

    def _render_loop(self,plant):
        camera=None;sequence=0;due=0;last_tick=None
        try:
            from xenova_vehicle.simulation.wall_sensor import WallSyntheticCamera, WallPoseSample, MAX_CAPTURE_LIFETIME_US
            if self._lifetime>MAX_CAPTURE_LIFETIME_US:raise ValueError('original wall capture policy changed')
            camera=WallSyntheticCamera(self._renderer_profile,self._scene,epoch=self._epoch,
                clock_epoch=self._clock_epoch,motion_profile=self._motion,clock=self._clock)
            if camera.calibration.sha256!=self._binding.fields['calibration_profile_sha256']:
                raise ValueError('renderer changed original bound camera calibration')
            while not self._stop.is_set():
                now=_tick(self._clock())
                if last_tick is not None and now<last_tick:raise ValueError('actual PC renderer clock regressed')
                last_tick=now
                if now>=self._deadline_us:break
                if now<due:
                    self._stop.wait(min(due-now,self._deadline_us-now)/1_000_000);continue
                sample=WallPoseSample.sample(self._epoch,sequence,plant.renderer_pose,clock=self._clock)
                sequence+=1
                if sequence>=_U63:raise OverflowError('original PC capture sequence exhausted')
                due=sample.captured_j_us[0]+self._interval
                with self._guard:self._count('attempted')
                # Wall의 기존 *_j_us/label은 내부 renderer carrier다. 여기서는 실제 PC clock
                # callback의 원 pair만 사용하며 해당 label/pose/source를 wire에 보내지 않는다.
                expires=sample.captured_j_us[0]+self._lifetime
                available=_tick(self._clock())
                if available<sample.captured_j_us[1]:
                    with self._guard:self._count('quantization_future')
                    continue
                capture=camera.capture(sample,expires_j_us=expires)
                sample=None
                if capture is None:
                    with self._guard:self._count('expired')
                    continue
                frame=ObservationFrame(self._scope,capture.observation.rgb.stamp.sequence,
                    rgb_pc_interval=capture.captured_j_us,depth_pc_interval=capture.captured_j_us,
                    render_completed_pc_us=capture.completed_j_us,expires_pc_us=capture.expires_j_us,
                    rgb=capture.observation.rgb.payload,depth=capture.observation.depth.payload)
                capture=None
                old=None
                with self._guard:
                    self._count('rendered')
                    if not self._stop.is_set():
                        old=self._latest;self._latest=frame
                        if old is not None:self._count('replaced')
                # 큰 pixel 참조의 마지막 해제는 작은 mailbox lock 밖에서 일어난다.
                old=frame=None
        except BaseException as error:self._failed('renderer',error)
        finally:
            if camera is not None:
                try:camera.close()
                except BaseException as error:self._failed('renderer_close',error)

    def next_frame(self):
        """session owner의 nonblocking latest1 take다. render/hash/JSON/파일 I/O는 없다."""
        if threading.current_thread() is not self._session:
            raise RuntimeError('only the actual PC session thread takes original frames')
        if not self._guard.acquire(blocking=False):return None
        try:
            frame=self._latest;self._latest=None
            if frame is None:return None
            if self._stop.is_set() or _tick(self._clock())>=frame.expires_pc_us:
                self._count('expired');return None
            self._count('taken');return frame
        finally:self._guard.release()

    def snapshot(self):
        """작은 비권한 진단이다. thread_alive=False는 process reap이나 실제 정지가 아니다."""
        if self._guard is None:return self._snapshot()
        with self._guard:return self._snapshot()

    def _snapshot(self):
        result=None if self._result is None else dict(self._result,counters=dict(self._result['counters']))
        return dict(session_started=self._session_started,attached=self._attached,session_done=self._session_done,
            failed=self._failure is not None,closed=self._closed,failure=self._failure,
            threads_alive=dict(renderer=self._renderer is not None and self._renderer.is_alive(),
                session=self._session is not None and self._session.is_alive()),
            counts=dict(self._counts),session_result=result,started_pc_us=self._started_us,
            deadline_pc_us=self._deadline_us,binding_sha256=self._binding.sha256,
            authority=False,physical_qualified=False,closed_loop_accepted=False)

    def close(self):
        """원 close cap 안에서만 join한다. 살아 있는 thread를 성공/실제 reap으로 바꾸지 않는다."""
        self._owned()
        if self._close_deadline is None:self._close_deadline=time.monotonic()+self._join
        self._close_started=True
        if self._stop is not None:self._stop.set()
        if self._endpoint.fileno()>=0:
            try:self._endpoint.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                if error.errno not in (errno.ENOTCONN,errno.EBADF):
                    if self._guard is None:self._failure=('shutdown: '+type(error).__name__)[:256]
                    else:self._failed('shutdown',error)
        for worker in (self._session,self._renderer):
            if worker is not None and worker.ident is not None:
                worker.join(max(0.,self._close_deadline-time.monotonic()))
        self._endpoint.close()
        old=None
        if self._guard is not None:
            with self._guard:
                old=self._latest;self._latest=None
                alive=any(t is not None and t.is_alive() for t in (self._session,self._renderer))
                self._closed=not alive
                if alive and self._failure is None:self._failure='close: original thread join remains unconfirmed'
        else:self._closed=True
        old=None
        return self.snapshot()
