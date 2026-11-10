"""실제 PC M compute 사건을 원 lifecycle progress와 parser receipt로 연결한다.

physical nn_input0 stream/SourceStamp를 만들지 않고 원 RGB4/NN8 key를 쓴다.
NN_READY progress는 parser 준비 사실이며 Source/clock/metric/World/주행 권한이 아니다.
무거운 해석·SDK·파일·DB는 호출하지 않는다. 원 PARSER_STARTED full-send 뒤만 decode를 연다.
"""

import json

from .lifecycle_policy import AbsoluteDeadline, REQUIRED_NN_PAIR_COUNT
from .mapper_control import _result_phase
from .pc_compute import PcMapperCompute
from .pc_workers import PcWorkerActor
from .resident_transport import j_us


class PcMapperControl:
    """M-main의 control owner다. actual completion metadata는 최신1로 넘긴다."""

    def __init__(self, actor, compute, parser_deadline, *, clock=j_us, nn_stall_us):
        if (type(actor) is not PcWorkerActor or actor.boot.peer.role != 'M'
                or type(compute) is not PcMapperCompute or type(parser_deadline) is not AbsoluteDeadline
                or type(nn_stall_us) is not int or not 0 < nn_stall_us < 2**63 or not callable(clock)):
            raise ValueError('actual PC M/compute and original parser/stall policy required')
        self.actor, self.compute, self.deadline, self.clock = actor, compute, parser_deadline, clock
        self.nn_stall_us = nn_stall_us
        self.generation = actor.binding.sha256
        self.ready = False
        self.streak = 0
        self.previous = self.last_completion = self._parser = self._metric_job = None
        self._completion = None
        self.latest_summary = self._result_phase = None
        self.result_count = 0
        self.wait_reason = None

    def _progress(self, event, stage, reason=None):
        if len(event.source_key) != 4 or type(event.source_key[3]) is not int:
            raise ValueError('original PC four-field frame key required for progress')
        return self.actor.progress(stage, event.source_key[3], completed=event.completed_us,
            reason=reason, request_id=event.job_id)

    def _nn_complete(self, event, *, fresh):
        if self._parser is None or self._parser[0] != event.job_id:
            raise RuntimeError('PC NN completion has no original actual parser-start send')
        _, key, began = self._parser
        self._parser = None
        now = self.clock()
        if not began <= event.completed_us <= now:
            raise ValueError('PC NN completion changed original start/completion order')
        fresh = fresh and now < event.expires_us
        if not fresh:
            if event.kind == 'NN_COMPLETED': self.compute.take_parser_completion(event)
            if not self.ready: self.streak = 0
            self._progress(event, 'PARSER_COMPLETED', 'STALE_INPUT')
            return
        if self.previous is not None:
            if (key[:3] != self.previous[:3] or key[6] != self.previous[6]
                    or any(key[index] <= self.previous[index] for index in (3, 4, 5))):
                raise ValueError('PC parser did not advance original NN8 key')
        self.previous = key
        self.streak += 1
        self.last_completion = event.completed_us
        self.wait_reason = None
        report = self.compute.take_parser_completion(event)
        if report is None:
            raise RuntimeError('accepted PC NN completion lost its original actual parser metadata')
        if self._completion is not None:
            raise RuntimeError('one actual PC parser completion is waiting for main transport selection')
        self._completion = report
        if not self.ready and self.streak >= REQUIRED_NN_PAIR_COUNT:
            self.ready = True
            self.wait_reason = None
            self.actor.state = 'WAIT_INPUT'
            self._progress(event, 'NN_READY', 'METRIC_NOT_READY')
        else:
            self._progress(event, 'PARSER_COMPLETED', 'METRIC_NOT_READY')

    def tick(self):
        for event in self.compute.poll():
            if event.source_generation != self.generation: continue
            if event.kind == 'JOB_STARTED':
                if self._metric_job is not None: raise RuntimeError('PC compute replaced its active original job')
                self._metric_job = event.job_id
                self._progress(event, 'MAPPER_STARTED', str(event.expires_us))
            elif event.kind == 'PARSER_START_REQUIRED':
                if self.clock() >= min(event.parser_deadline_us, event.expires_us): continue
                key = event.nn_source_key
                if type(key) is not tuple or len(key) != 8 or key[:4] != event.source_key:
                    raise ValueError('PC parser start lacks original full NN8 key')
                if self._parser is not None: raise RuntimeError('one PC compute cannot open two NN parsers')
                original = self.actor.progress('PARSER_STARTED', key[3], completed=event.started_us,
                    request_id=event.job_id)
                actual = self.actor.await_sent(original)
                self._parser = (event.job_id, key, event.started_us)
                self.compute.acknowledge_parser_start(event.job_id, actual)
            elif event.kind == 'NN_COMPLETED':
                self._nn_complete(event, fresh=event.accepted)
            elif event.kind == 'PROGRESS':
                if self._metric_job == event.job_id: self._progress(event, 'MAPPER_PROGRESS', event.reason)
            elif event.kind == 'GEOMETRY_COMPLETED':
                if self._metric_job == event.job_id:
                    self._progress(event, 'MAPPER_COMPLETED', 'COMPLETED' if event.accepted else event.reason)
                    self._metric_job = None
            elif event.kind in ('CANCELLED', 'FAILED'):
                if self._parser is not None and self._parser[0] == event.job_id:
                    self._nn_complete(event, fresh=False)
                if self._metric_job == event.job_id:
                    self._progress(event, 'MAPPER_COMPLETED', event.reason); self._metric_job = None
                if event.kind == 'FAILED':
                    raise RuntimeError('PC mapper compute failed: ' + str(event.reason))
            elif event.kind != 'NN_SKIPPED':
                raise ValueError('unsupported PC compute lifecycle event')
        result = self.compute.take_result()
        if (result is not None and result.accepted and result.payload is not None
                and result.source_generation == self.generation and self.clock() < result.expires_us):
            value = json.loads(result.payload)
            if type(value) is not dict or set(value) != {'processed', 'latest'} or type(value['latest']) is not dict:
                raise ValueError('PC mapper summary fields differ from control contract')
            phase = _result_phase(value['latest'])
            if self.clock() < result.expires_us:
                self.latest_summary = value; self.result_count += 1
                self._result_phase = phase, result.expires_us
        if self.compute.terminal_error is not None:
            raise RuntimeError('PC compute owner failed: ' + self.compute.terminal_error)
        now = self.clock()
        if self.ready:
            current = self._result_phase
            self.actor.state = 'WAIT_INPUT' if current is None or now >= current[1] else current[0]
        if not self.ready and self.deadline.expired(now): self.wait_reason = 'NN_WARMUP_WAIT_INPUT'
        if self.ready and self.last_completion is not None and now - self.last_completion >= self.nn_stall_us:
            self.wait_reason = 'NN_WAIT_INPUT'
            self.actor.state = 'WAIT_INPUT'

    def take_parser_completion(self):
        """원 metadata를 한 번 선택한다. 실제 full-send와 Source 승인으로 세지 않는다."""
        result, self._completion = self._completion, None
        return result
