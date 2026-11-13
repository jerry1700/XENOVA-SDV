"""선택 진단 파일 저장. 실패는 장치·자원·주행 상태 전이의 근거가 아니다."""

import json
from pathlib import Path
from queue import Full, Queue
from threading import Thread


def _raw(value):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('resident diagnostic exceeds its bound')
    return raw


def _write(directory, filename, raw):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with (path / filename).open('xb') as target:
        target.write(raw)


class DiagnosticWriter:
    """프로세스별 선택 I/O 소유자. queue가 차면 버리며 종료를 기다리지 않는다.

    실행·STOP 판정은 이 thread를 기다리지 않는다. 프로세스 종료 시 남은 진단은
    유실될 수 있다. 자원 manifest 원본·DB 저장에는 이 경로를 사용하지 않는다.
    """

    def __init__(self):
        self._queue = Queue(maxsize=8)
        self.last_error = None
        self.dropped = 0
        self._thread = Thread(target=self._run, name='optional-diagnostics', daemon=True)
        self._thread.start()

    def submit(self, operation):
        try:
            self._queue.put_nowait(operation)
        except Full:
            self.dropped += 1
            return False
        return True

    def save(self, directory, filename, value):
        """None 경로는 비활성이다. 수락은 저장 완료를 뜻하지 않으며 기존 파일은 덮지 않는다."""
        if directory is None:
            return True
        # queue에 immutable bounded bytes만 보관한다. I/O는 호출 thread에서 하지 않는다.
        try:
            raw = _raw(value)
        except Exception as error:
            self.last_error = type(error).__name__[:96]
            return False
        return self.submit(lambda: _write(directory, filename, raw))

    def _run(self):
        while True:
            operation = self._queue.get()
            try:
                error = operation()
                if error is not None:
                    self.last_error = error
            except Exception as error:
                self.last_error = type(error).__name__[:96]
            finally:
                self._queue.task_done()
