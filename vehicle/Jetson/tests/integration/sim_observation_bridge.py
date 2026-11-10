"""완료된 개발 관측의 latest1을 독립 diagnostic owner에 넘기는 선택형 bridge다.

생성자/offer는 한 producer, start/drain/close는 그와 다른 한 caller owner가 쓴다.
자동 thread/process를 만들지 않는다. offer는 원 DTO 참조만 교환하며 JSON/hash/파일은
진단 owner의 drain에서만 처리한다. 직렬화 bytes는 새 wire가 아닌 원 완료 DTO의 투영이다.
시각/순번/권한을 발급하지 않으며 표시 GT는 넘기지 않는다. 종료/join은 caller 책임이다.
"""

import json
from pathlib import Path
import threading

from sim_mapper_motion_rig import MapperMotionObservation
from sim_observation_export import SimObservationExporter


_COUNTER_MAX = (1 << 64) - 1
_OFFER_KEYS = ('offered', 'replaced', 'dropped_busy', 'dropped_closed')
_DRAIN_KEYS = ('drained', 'written', 'unchanged', 'failures')


def _increment(values, index):
    return tuple(min(_COUNTER_MAX, value + 1) if offset == index else value
        for offset, value in enumerate(values))


class SimObservationBridge:
    """pending latest1 + active1이다. 진단 실패는 sticky이며 원 관측/운영 결과와 별개다.

    directory는 기존 exporter의 전용 폴더 규칙을 따른다. source/epoch 변경에는 새
    bridge와 새 전용 폴더를 명시한다. 파일 작업의 시간 상한이나 thread 종료를 보증하지 않는다.
    """

    def __init__(self, directory):
        self._directory = Path(directory)
        self._producer = threading.get_ident()
        self._owner = None
        self._lock = threading.Lock()
        self._pending = self._active = self._exporter = None
        self._offer_counts = (0,) * len(_OFFER_KEYS)
        self._drain_counts = (0,) * len(_DRAIN_KEYS)
        self._started = self._closed = self._draining = False
        self._accepting = True
        self._failure = self._last_result = None

    def validate_producer(self):
        """Rig 기동 전에 원 producer 역할을 검사한다. 관측/파일/등록 권위는 검사하지 않는다."""
        if threading.get_ident() != self._producer:
            raise RuntimeError('observation offer belongs to original producer')
        return self

    def offer(self, original):
        """원 exact DTO만 latest1에 둔다. 경합/종료 때 False이며 원 결과는 바꾸지 않는다."""
        self.validate_producer()
        if type(original) is not MapperMotionObservation:
            raise ValueError('exact original MapperMotionObservation required')
        if not self._lock.acquire(blocking=False):
            self._offer_counts = _increment(self._offer_counts, 2)
            return False
        retired = None
        try:
            if not self._accepting:
                self._offer_counts = _increment(self._offer_counts, 3)
                return False
            retired, self._pending = self._pending, original
            self._offer_counts = _increment(self._offer_counts, 0)
            if retired is not None:
                self._offer_counts = _increment(self._offer_counts, 1)
        finally:
            self._lock.release()
            # 큰 원 관측의 마지막 참조 해제도 producer lock 밖에 둔다.
            retired = None
        return True

    def _owned(self, *, establish=False):
        current = threading.get_ident()
        if current == self._producer:
            raise RuntimeError('diagnostic owner must differ from observation producer')
        if self._owner is None and establish:
            with self._lock:
                if self._owner is None:
                    self._owner = current
        if self._owner != current:
            raise RuntimeError('observation export belongs to original diagnostic owner')

    def _fail(self, stage, error):
        failure = (stage, type(error).__name__, str(error)[:256])
        with self._lock:
            if self._failure is None:
                self._failure = failure
            self._accepting = False
            retired, self._pending = self._pending, None
            self._drain_counts = _increment(self._drain_counts, 3)
        retired = None

    def start(self):
        """caller diagnostic owner에서 exporter를 한 번 연다. 실패는 snapshot에만 남긴다."""
        self._owned(establish=True)
        if self._started or self._closed:
            raise RuntimeError('observation bridge is single-start')
        self._started = True
        try:
            self._exporter = SimObservationExporter(self._directory)
        except Exception as error:
            self._fail('START', error)
            return False
        return True

    def drain(self):
        """최신 원 관측 하나를 역사 snapshot으로 내보낸다. 재수락/기한 갱신은 없다."""
        self._owned()
        if not self._started:
            raise RuntimeError('diagnostic owner must start before draining')
        if self._draining:
            raise RuntimeError('observation drain cannot be reentered')
        with self._lock:
            if self._closed or self._failure is not None or self._pending is None:
                return None
            original, self._pending = self._pending, None
            self._active = original
            self._draining = True
            self._drain_counts = _increment(self._drain_counts, 0)
        try:
            # fields는 이 delivery에서 한 번만 읽고 그 bytes를 exporter에 그대로 준다.
            raw = json.dumps(original.fields, sort_keys=True, ensure_ascii=True,
                separators=(',', ':'), allow_nan=False).encode('ascii')
            result = self._exporter.export(raw, original.observation, display_gt_layers=None)
            with self._lock:
                self._last_result = (result.status, result.source_sequence,
                    result.original_json_sha256, result.snapshot_sha256)
                self._drain_counts = _increment(self._drain_counts, 1 if result.status == 'WRITTEN' else 2)
            return result
        except Exception as error:
            self._fail('EXPORT', error)
            return None
        finally:
            with self._lock:
                self._active = None
                self._draining = False
            original = None

    def close(self):
        """caller owner가 더 이상 drain하지 않게 한다. 제품 종료를 지연·재발급하지 않는다."""
        self._owned(establish=True)
        if self._draining:
            raise RuntimeError('diagnostic owner cannot close during drain')
        with self._lock:
            self._accepting = False
            self._closed = True
            retired, self._pending = self._pending, None
        retired = None
        exporter, self._exporter = self._exporter, None
        if exporter is not None:
            try:
                exporter.close()
            except Exception as error:
                self._fail('CLOSE', error)

    def snapshot(self):
        """원 DTO/큰 JSON 없이 작은 독립 상태 사본을 반환한다. READY 자료가 아니다."""
        with self._lock:
            state = ('FAILED' if self._failure is not None else 'CLOSED' if self._closed else
                'STARTED' if self._started else 'NOT_STARTED')
            pending, active = self._pending is not None, self._active is not None
            offer, drain = self._offer_counts, self._drain_counts
            failure, last = self._failure, self._last_result
        return dict(state=state, pending=pending, active=active,
            counts=dict(zip(_OFFER_KEYS + _DRAIN_KEYS, offer + drain)),
            failure=None if failure is None else dict(stage=failure[0], error=failure[1], detail=failure[2]),
            last_result=None if last is None else dict(status=last[0], source_sequence=last[1],
                original_json_sha256=last[2], snapshot_sha256=last[3]),
            authority=False, physical_acceptance=False)
