"""조건을 다시 확인하는 일회성 복구 실행. 감시·정책·반복 실행 서비스는 아니다."""
import json
from subprocess import TimeoutExpired

from first_install_contract import uuid4
from xenova_execution import EARLIER_LAUNCH, ExecutionError, fingerprint, refuse, short_lock, read_record
from xenova_runtime import PlatformError
import xenova_catalog as catalog
import xenova_vm as vm
from xenova_trace import bind as trace_bind, traced


def validate_request(spec):
    from xenova_management import normalize_uuid
    fields = {"vm_id", "recovery_attempt_id", "expected_host_boot_id", "expected_lifecycle_generation",
              "expected_intent_revision", "expected_execution", "mode", "deadline_monotonic_ms"}
    if not isinstance(spec, dict) or set(spec) != fields:
        refuse("INVALID_SPEC", "recover_once requires the exact documented specification")
    spec = dict(spec)
    for name in ("vm_id", "recovery_attempt_id", "expected_host_boot_id"):
        spec[name] = normalize_uuid(spec[name])
    for name, low in (("expected_lifecycle_generation", 0), ("expected_intent_revision", 1),
                      ("deadline_monotonic_ms", 1)):
        if type(spec[name]) is not int or spec[name] < low:
            refuse("INVALID_SPEC", name+" must be an integer")
    expected = spec["expected_execution"]
    if not isinstance(expected, dict):
        refuse("INVALID_SPEC", "expected_execution must be a versioned object")
    if set(expected) == {"execution_id"}:
        spec["expected_execution"] = {"execution_id":normalize_uuid(expected["execution_id"])}
    elif set(expected) != {"absent_observation_revision"} or type(expected.get("absent_observation_revision")) is not int or expected["absent_observation_revision"] < 1:
        refuse("INVALID_SPEC", "execution_id or absent_observation_revision is required")
    if spec["mode"] not in ("start-if-absent", "graceful-restart", "force-restart", "restart-crashed"):
        refuse("INVALID_SPEC", "unsupported recovery mode")
    if (spec["mode"] == "start-if-absent") != ("absent_observation_revision" in expected):
        refuse("INVALID_SPEC", "recovery mode and expected presence disagree")
    return spec


def read_attempt(data, identity, *, reader=read_record):
    """원 시도의 무결성만 확인한다. 현재 host·실행·복구 허가는 조회하지 않는다."""
    if not isinstance(identity,str) or not vm.UUID.fullmatch(identity):
        refuse('INVALID_SPEC','canonical recovery attempt required')
    value=reader(data/'recovery-attempts'/(identity+'.json'))
    if value is None:refuse('VM_NOT_FOUND','recovery attempt not found')
    if (value.get('recovery_attempt_id')!=identity or value.get('request_hash')!=fingerprint(value.get('request'))
            or not vm.UUID.fullmatch(value.get('operation_id',''))):
        refuse('RECONCILE_REQUIRED','recovery attempt integrity mismatch')
    return value


class Recovery:
    def __init__(self, manager):
        self.m = manager
        self.p = manager.p
        self.e = self.p.execution()
        self.diagnostics = []

    def path(self, identity):
        return self.e._path(self.p.data, "recovery-attempts", identity)

    def read_attempt(self, identity):
        return read_attempt(self.p.data,identity,reader=self.e.read)

    def attempts(self):
        root = self.p.data/"recovery-attempts"
        if not root.exists() and not root.is_symlink():
            return []
        catalog.plain(root, directory=True)
        result = []
        for path in root.glob("*.json"):
            if not vm.UUID.fullmatch(path.stem):
                refuse("RECONCILE_REQUIRED", "invalid recovery journal filename")
            result.append(self.read_attempt(path.stem))
        return result

    def save(self, op, phase):
        trace_bind(operation_id=op['operation_id'], recovery_attempt_id=op['recovery_attempt_id'],
                   host_boot_id=self.e.host, vm_id=op['request']['vm_id'], phase=phase,
                   deadline_monotonic_ms=op['request']['deadline_monotonic_ms'],
                   intent_revision=op['request']['expected_intent_revision'],
                   lifecycle_generation=op['request']['expected_lifecycle_generation'],
                   policy_revision=(op.get('cpu_policy') or {}).get('policy_revision'))
        op["phase"] = phase
        op.setdefault("events", []).append({"phase":phase, "host_boot_id":self.e.host,
                                          "monotonic_ms":int(self.p.clock()*1000)})
        self.e.write(self.path(op["recovery_attempt_id"]), op)
        self.e.checkpoint("recovery-"+phase, op)

    def conditions(self, spec, deadline, *, target=True, gate=True, identity_only=False):
        from xenova_preparation import guard_asset
        guard_asset(self.p, spec['vm_id'])
        if spec["expected_host_boot_id"] != self.e.host:
            refuse("TARGET_CHANGED", "recovery request belongs to another host boot", details={"reason":"HOST_BOOT_CHANGED"})
        if gate:
            self.e.gate()
        records = self.p.records(validate_assets=False)
        tomb = self.m._state(spec["vm_id"], records)
        if tomb["lifecycle"] != "REGISTERED" or tomb["generation"] != spec["expected_lifecycle_generation"]:
            refuse("TARGET_CHANGED", "recovery lifecycle changed", details={"reason":"LIFECYCLE_CHANGED"})
        record = tomb["record"]
        intent = self.e.intent(record)
        if (not intent or intent["desired_state"] != "running" or intent["intent_revision"] != spec["expected_intent_revision"]
                or intent["lifecycle_generation"] != tomb["generation"]):
            refuse("INTENT_MISMATCH", "current applicable intent is not the expected running revision")
        admission = self.e.admission(record)
        if (not admission or not admission["allowed"] or admission["intent_revision"] != intent["intent_revision"]
                or admission["lifecycle_generation"] != tomb["generation"]):
            refuse("BOOT_ADMISSION_REQUIRED", "this host boot has not admitted this intent")
        from xenova_creation import holds
        if any(q["vm_id"] == record["uuid"] and holds(self.m,q) for q in self.m._operations()):
            refuse("OPERATION_IN_PROGRESS", "an incomplete asset operation owns this VM", retryable=True)
        self.m._record_at(record, self.p.data/"instances"/record["id"])
        if self.p.clock() >= deadline:
            refuse("DEADLINE_EXCEEDED", "recovery deadline expired")
        observation = self.e.observe(record, deadline, identity_only=identity_only)
        if target:
            expected = spec["expected_execution"]
            if "absent_observation_revision" in expected:
                if (observation["presence"] != "absent" or
                        observation["observation_revision"] != expected["absent_observation_revision"]):
                    refuse("TARGET_CHANGED", "expected absent observation was superseded")
            elif observation["execution_status"] != "managed" or observation["execution_id"] != expected["execution_id"]:
                refuse("TARGET_CHANGED", "expected managed execution changed", details={"reason":"EXECUTION_CHANGED"})
            elif spec["mode"] == "restart-crashed" and observation["xen_state"] != "crashed":
                refuse("TARGET_CHANGED", "expected Xen execution is no longer crashed")
        return record, records, observation

    def factory_drive(self, record):
        if vm.guest_kind(record) != "zephyr" or record["id"] not in ("zephyr-drive-a", "zephyr-drive-b"):
            return False
        path = self.p.data/".first-install-state.json"
        if not path.exists() and not path.is_symlink():
            return False
        state = json.loads(catalog.plain(path).read_text())
        return (state.get("schema") == 1 and state.get("phase") == "complete"
                and state["identities"].get(record["id"], {}).get("uuid") == record["uuid"])

    def mode_allowed(self, spec, record):
        if spec["mode"] == "graceful-restart" and not vm.cooperative(record):
            refuse("RECOVERY_MODE_UNSUPPORTED", "this guest has no cooperative shutdown")
        if spec["mode"] == "force-restart" and record["uuid"] not in self.e.policy()["force_restart_vm_ids"]:
            refuse("RECOVERY_MODE_UNSUPPORTED", "force recovery has no per-VM administrator approval")
        if (spec["mode"] == "restart-crashed" and not self.factory_drive(record)
                and record["uuid"] not in self.e.policy()["force_restart_vm_ids"]):
            refuse("RECOVERY_MODE_UNSUPPORTED", "crash recovery requires a factory drive VM or per-VM approval")

    @traced('recovery.complete')
    def complete(self, op, observation):
        record = self.m._state(op['request']['vm_id'], self.p.records(validate_assets=False))['record']
        run = self.e.run_record({"uuid":op["request"]["vm_id"]})
        proof = observation.get('_description', {}).get('execution_context')
        if (observation["execution_status"] != "managed" or not run or
                run.get("owner") != {"kind":"recovery", "recovery_attempt_id":op["recovery_attempt_id"]} or
                proof != run or run.get('cpu_verification') != 'complete' or
                observation.get('configuration_revision') != fingerprint(record) or
                self.p.cpu().context(record) != op.get('cpu_policy', {'configured':False})):
            refuse("RECONCILE_REQUIRED", "new launch cannot be correlated to this attempt")
        intent = self.e.intent(record)
        if (self.e.lifecycle(record) != ('REGISTERED',op['request']['expected_lifecycle_generation']) or
                not intent or intent['desired_state'] != 'running' or
                intent['intent_revision'] != op['request']['expected_intent_revision']):
            refuse('INTENT_MISMATCH', 'recovery completion intent or lifecycle changed')
        op["result"] = {"vm_id":op["request"]["vm_id"], "host_boot_id":self.e.host,
                        "lifecycle_generation":op["request"]["expected_lifecycle_generation"],
                        "intent_revision":op["request"]["expected_intent_revision"],
                        "execution_id":observation["execution_id"], "observation":observation,
                        "platform_elapsed_ms":int(self.p.clock()*1000)-op["accepted_monotonic_ms"],
                        "app_readiness":"unknown"}
        op["error"] = None
        try:
            self.save(op, "completed")
        except OSError as error:
            op["phase"] = "observed"
            raise ExecutionError("RECONCILE_REQUIRED", "required completion journal was not durably confirmed") from error
        self.event("recovery-completed", {"uuid":op["request"]["vm_id"]}, op["operation_id"],
                     {"recovery_attempt_id":op["recovery_attempt_id"], "execution_id":observation["execution_id"]})
        return op["result"]

    def event(self, *args, **kwargs):
        warning = self.e.event(*args, **kwargs)
        if warning:
            self.diagnostics.append(warning)

    def once(self, spec, *, execute, context):
        self.diagnostics = []
        try:
            result = self._once(spec, execute=execute, context=context)
        except ExecutionError as error:
            if self.diagnostics:
                error.error["details"].setdefault("diagnostic_warnings", []).extend(self.diagnostics)
            raise
        if self.diagnostics:
            result["diagnostic_warnings"] = self.diagnostics
        return result

    def unresolved(self, op, message):
        refuse("RECONCILE_REQUIRED", message, details={
            "blocking_attempt_id":op["recovery_attempt_id"], "blocking_operation_id":op["operation_id"],
            "phase":op["phase"], "next_action":"inspect_original_attempt",
            "allowed_actions":["query_original_attempt", "replay_original_to_finalize_observed_launch",
                               "review_planned_host_restart"], "automatic_retry":False})

    def settle_expired(self, op, deadline):
        """만료한 launch와 입증된 구형 미제출 기록만 자동 종결한다."""
        spec = validate_request(op['request'])
        if spec['expected_host_boot_id'] != self.e.host or self.p.clock()*1000 < spec['deadline_monotonic_ms']:
            return False
        run = self.e.run_record({'uuid':spec['vm_id']})
        owner = {'kind':'recovery', 'recovery_attempt_id':op['recovery_attempt_id']}
        if ((not run or run.get('owner') != owner) and not self.launch_refused_before_submit(op)):
            return False
        result = self.reconcile(op['recovery_attempt_id'], execute=True, deadline=deadline,
                                context={}, _expired_only=True)
        return bool(result and result['attempt']['phase'] == 'failed')

    @staticmethod
    def launch_refused_before_submit(op):
        details = (op.get('error') or {}).get('details') or {}
        cause = details.get('cause') or {}
        return (op['phase'] == 'uncertain' and details.get('failed_phase') == 'launching'
                and cause.get('code') == 'RECONCILE_REQUIRED' and cause.get('message') == EARLIER_LAUNCH)

    def reconcile(self, identity, *, execute, deadline, context, _expired_only=False):
        """원 attempt만 진단/종결한다. 이 경로는 VM 제어·새 수락·기한 연장을 하지 않는다.

        execution flock은 Manager가 소유한다. 외부 관측 뒤 짧은 admission lock 안에서
        원 journal/intent/run을 다시 대조하며, 과거 PID나 부재 하나를 종료 증명으로 쓰지 않는다.
        """
        op = self.read_attempt(identity)
        context.update(operation_id=op['operation_id'], replayed=True)
        spec = validate_request(op['request'])
        if op.get('schema_version') != '1.0' or op.get('host_boot_id') != spec['expected_host_boot_id']:
            refuse('RECONCILE_REQUIRED', 'recovery journal context invalid')
        if spec['expected_host_boot_id'] != self.e.host:
            refuse('TARGET_CHANGED', 'original attempt belongs to another host; no cross-host deadline comparison',
                   details={'operation_id':op['operation_id'], 'reason':'HOST_BOOT_CHANGED'})
        if self.p.clock() >= deadline:
            refuse('DEADLINE_EXCEEDED', 'recovery reconciliation budget expired')
        marker = {'operation_id':op['operation_id'], 'recovery_attempt_id':identity,
                  'deadline_monotonic_ms':spec['deadline_monotonic_ms']}
        pending_run = self.e.run_record({'uuid':spec['vm_id']})
        resume_disposal = bool(op['phase']=='failed' and pending_run
            and pending_run.get('owner')=={'kind':'recovery','recovery_attempt_id':identity}
            and pending_run.get('recovery_reconciliation')==marker
            and pending_run.get('phase') in ('uncertain','stopped')
            and (op.get('reconciliation') or {}).get('outcome')=='stopped_without_replacement')
        if op['phase'] in ('completed', 'failed') and not resume_disposal:
            if _expired_only:
                return None
            if execute and op.get('reconciliation'):
                # 이전 replace 뒤 parent fsync 응답을 잃었을 수도 있어 같은 결과를 재확정한다.
                self.e.write(self.path(identity), op)
            return {'attempt':op, 'reconciliation':{'status':'recorded', 'executed':bool(execute and op.get('reconciliation')),
                    'physical_commands':0}, 'original_result':op.get('result')}
        record, _, observed = self.conditions(spec, deadline, target=False, gate=False)
        run = self.e.run_record(record)
        intent = self.e.intent(record)
        snapshot = {'record':fingerprint(record), 'run':run, 'intent':intent}
        phase = 'uncertain' if resume_disposal else op['phase']
        failed_phase = (op.get('error') or {}).get('details', {}).get('failed_phase')
        stopping = phase == 'stopping' or phase == 'uncertain' and failed_phase == 'stopping'
        owner = {'kind':'recovery', 'recovery_attempt_id':identity}
        checks = {'cpu_policy':self.p.cpu().context(record) == op.get('cpu_policy', {'configured':False})}
        outcome = None
        dispose_launch = False
        if phase in ('accepted', 'removed'):
            # 이 phase 이후의 create는 먼저 launching을 내구 저장해야 한다.
            checks['no_unresolved_launch'] = not run or run.get('phase') not in ('launching', 'uncertain')
            checks['no_owned_launch'] = not run or run.get('owner') != owner
            checks['removed_is_absent'] = phase != 'removed' or observed['presence'] == 'absent'
            outcome = 'not_submitted' if phase == 'accepted' else 'stopped_without_replacement'
        elif stopping:
            proof = op.get('stop_submission') or {}
            target = proof.get('target') or {}
            checks['stop_target'] = (target.get('execution_id') == spec['expected_execution'].get('execution_id')
                and target.get('host_boot_id') == self.e.host and type(target.get('domid')) is int
                and isinstance(target.get('xen_unique_id'), str))
            checks['no_replacement_launch'] = not run or (run.get('execution_id') == target.get('execution_id')
                and run.get('phase') not in ('launching', 'uncertain'))
            state = proof.get('state')
            checks['original_call_finished'] = state in ('not-submitted', 'returned')
            checks['target_state'] = (observed['presence'] == 'absent' or state == 'not-submitted'
                and observed['execution_status'] == 'managed' and observed['execution_id'] == target.get('execution_id')
                and observed['domid'] == target.get('domid') and observed['xen_unique_id'] == target.get('xen_unique_id'))
            outcome = 'not_submitted' if state == 'not-submitted' else 'stopped_without_replacement'
        elif (self.launch_refused_before_submit(op) and observed['presence'] == 'absent'
                and run and run.get('owner') != owner and run.get('phase') == 'stopped'):
            # Older recovery wrote launching before the prior-owner guard.
            # Its recorded rejection proves this attempt never submitted.
            checks['expired_launch'] = self.p.clock()*1000 >= spec['deadline_monotonic_ms']
            outcome = 'not_submitted'
        elif phase in ('launching', 'observed', 'uncertain'):
            checks['correlated_execution'] = bool(run and run.get('owner') == owner
                and run.get('phase') == 'observed' and run.get('cpu_verification') == 'complete'
                and observed['execution_status'] == 'managed' and observed['execution_id'] == run.get('execution_id')
                and observed.get('_description', {}).get('execution_context') == run)
            if checks['correlated_execution']:
                self.p.cpu().require(record, deadline, expected=op.get('cpu_policy', {'configured':False}),
                                     present=True, expected_identity=run)
                outcome = 'launch_observed'
            elif run and run.get('owner') == owner and run.get('launch_aborted_before_submit') is True:
                checks['correlated_execution'] = observed['presence'] == 'absent' and run.get('phase') == 'stopped'
                outcome = 'not_submitted'
            elif (run and run.get('owner')==owner and self.p.process_isolation
                    and observed['presence']=='absent'
                    and (run.get('phase') in ('launching','uncertain') or resume_disposal)):
                from xenova_guest_process import scope_name
                checks['correlated_execution'] = True
                checks['expired_launch'] = self.p.clock()*1000 >= spec['deadline_monotonic_ms']
                checks['owned_launch_scope'] = run.get('process_scope') == scope_name(spec['vm_id'],run['execution_id'])
                if _expired_only:
                    checks['original_launch_context'] = (
                        run.get('lifecycle_generation') == spec['expected_lifecycle_generation']
                        and run.get('intent_revision') == spec['expected_intent_revision']
                        and run.get('intent_sha256') == fingerprint(intent)
                        and run.get('record_sha256') == snapshot['record']
                        and run.get('configuration_sha256') == fingerprint(self.p.config(record))
                        and run.get('cpu_policy') == op.get('cpu_policy', {'configured':False})
                        and run.get('domid') is None and run.get('xen_unique_id') is None)
                # Absence alone cannot release an interrupted create. The existing
                # observe-only cleanup below must also prove its exact scope empty.
                outcome = 'stopped_without_replacement'
                dispose_launch = True
        else:
            checks['supported_phase'] = False
        if _expired_only and not (dispose_launch or (outcome == 'not_submitted'
                and (failed_phase == 'launching' or run and run.get('owner') == owner))):
            return None
        if outcome != 'launch_observed' and observed['presence'] == 'absent' and self.p.process_isolation:
            # observe_only는 신호나 scope stop을 보내지 않는다. 살아 있는 backend/helper는 차단한다.
            from xenova_guest_process import cleanup
            if not run or not run.get('process_scope'):
                if stopping or phase not in ('accepted', 'removed'):
                    checks['process_ownership'] = False
            else:
                try:
                    cleanup(self.p, record, run, deadline, observe_only=True)
                except (PlatformError, OSError, ValueError):
                    checks['process_quiescent'] = False
        reasons = [key for key, passed in checks.items() if not passed]
        if outcome is None:
            reasons.append('original_effect_unconfirmed')
        result = {'status':'blocked' if reasons else 'confirmable', 'outcome':outcome,
                  'checks':checks, 'reasons':reasons, 'executed':False, 'physical_commands':0,
                  'host_boot_id':self.e.host, 'observed_monotonic_ms':int(self.p.clock()*1000)}
        if self.p.clock() >= deadline:
            refuse('DEADLINE_EXCEEDED', 'recovery reconciliation observation budget expired')
        if execute and reasons:
            refuse('RECONCILE_REQUIRED', 'original effects or ownership not fully confirmed; no record closed',
                   details={'operation_id':op['operation_id'], 'recovery_attempt_id':identity,
                            'reconciliation':result, 'automatic_retry':False})
        if not execute:
            return {'attempt':op, 'reconciliation':result, 'original_result':op.get('result')}
        self.e.checkpoint('recovery-reconcile-before-guard', op)
        # 실제 identity는 다시 관측하고, 그 뒤의 저장된 상태 guard는 admission 안에서 수행한다.
        final = self.e.observe(record, deadline)
        fields = ('presence','execution_status','execution_id','domid','xen_unique_id','configuration_revision')
        if any(final.get(key) != observed.get(key) for key in fields):
            refuse('TARGET_CHANGED', 'execution changed after reconciliation diagnostic')
        with short_lock(self.p.runtime, 'admission', deadline=deadline, clock=self.p.clock, sleep=self.p.sleep):
            current = self.m._state(spec['vm_id'], self.p.records(validate_assets=False))
            if (self.read_attempt(identity) != op or self.p.boot_id_reader() != self.e.host
                    or current['lifecycle'] != 'REGISTERED' or current['generation'] != spec['expected_lifecycle_generation']
                    or fingerprint(current['record']) != snapshot['record'] or self.e.intent(record) != snapshot['intent']
                    or self.e.run_record(record) != snapshot['run']
                    or (_expired_only and dispose_launch
                        and fingerprint(self.p.config(record)) != run['configuration_sha256'])
                    or self.p.cpu().context(record) != op.get('cpu_policy', {'configured':False})):
                refuse('TARGET_CHANGED', 'original attempt, intent or execution changed before durable confirmation')
            if self.p.clock() >= deadline:
                refuse('DEADLINE_EXCEEDED', 'recovery reconciliation guard budget expired')
            prior = op.get('reconciliation') or {}
            op['reconciliation'] = {**result, 'status':'confirmed', 'executed':True,
                                    'original_phase':prior.get('original_phase',phase),
                                    'original_error':prior.get('original_error',op.get('error'))}
            try:
                if dispose_launch:
                    # Keep the runtime launch gate closed until the original
                    # terminal record is durable. A cut between writes resumes
                    # settlement of the same attempt before any new create.
                    pending_run = {**run, 'phase':'uncertain', 'recovery_reconciliation':marker}
                    self.e.write(self.e._path(self.p.runtime,'executions',record['uuid']),pending_run)
                if outcome == 'launch_observed':
                    self.complete(op, final)
                else:
                    expired = self.p.clock()*1000 >= spec['deadline_monotonic_ms']
                    code = 'DEADLINE_EXCEEDED' if expired else 'XEN_EXECUTION_FAILED'
                    op['error'] = {'code':code, 'message':'original recovery ended without a replacement launch',
                        'retryable':False, 'details':{'operation_id':op['operation_id'],
                        'recovery_attempt_id':identity, 'reconciliation_outcome':outcome,
                        'next_action':'observe_current_intent_before_any_new_request'}}
                    if _expired_only and outcome == 'not_submitted':
                        op['error']['message'] = 'original recovery was blocked before launch submission'
                    op['result'] = None
                    self.save(op, 'failed')
                if dispose_launch:
                    self.e.write(self.e._path(self.p.runtime,'executions',record['uuid']),
                                 {**pending_run,'phase':'stopped','stopped_monotonic_ms':int(self.p.clock()*1000)})
            except OSError as error:
                rollback = None
                if dispose_launch:
                    try:
                        # replace may have succeeded before its directory fsync
                        # failed. Retain the pending gate while still owning the
                        # admission lock; never restore a different execution.
                        current = self.e.run_record(record)
                        if (current and current.get('execution_id')==run['execution_id']
                                and current.get('owner')==owner
                                and current.get('recovery_reconciliation')==marker):
                            self.e.write(self.e._path(self.p.runtime,'executions',record['uuid']),pending_run)
                    except (PlatformError,OSError,ValueError) as restore_error:
                        rollback = {'cause_type':type(restore_error).__name__,
                                    'errno':getattr(restore_error,'errno',None)}
                refuse('RECONCILE_REQUIRED', 'reconciliation durability not confirmed; inspect same original attempt',
                       details={'operation_id':op['operation_id'], 'recovery_attempt_id':identity,
                                'storage_cause_type':type(error).__name__, 'storage_errno':getattr(error,'errno',None),
                                **({'rollback_error':rollback} if rollback else {})})
        return {'attempt':op, 'reconciliation':op['reconciliation'], 'original_result':op.get('result')}

    def expire_prelaunch(self, op):
        """기존 소유 잠금 안에서, launch 전임을 재확인한 만료 시도만 닫는다."""
        if op["phase"] not in ("accepted", "removed"):
            self.unresolved(op, "physical action may be unresolved; expiration cannot release this owner")
        try:
            record, _, current = self.conditions(op["request"], self.p.clock()+15, target=False, gate=False)
        except (PlatformError, OSError, ValueError):
            self.unresolved(op, "expired attempt could not be reconciled against current assets and Xen")
        run = self.e.run_record(record)
        if run and run.get("phase") in ("launching", "uncertain"):
            self.unresolved(op, "an unresolved launch prevents expiration cleanup")
        if op["phase"] == "removed" and current["presence"] != "absent":
            self.unresolved(op, "domain appeared after recorded removal; expiration needs reconciliation")
        op["error"] = {"code":"DEADLINE_EXCEEDED", "message":"recovery attempt expired before launch",
                       "retryable":False, "details":{"operation_id":op["operation_id"],
                       "recovery_attempt_id":op["recovery_attempt_id"], "expired_before_launch":True,
                       "next_action":"use_new_attempt_after_fresh_observation"}}
        previous_phase = op["phase"]
        try:
            self.save(op, "failed")
        except OSError:
            op["phase"] = previous_phase
            self.unresolved(op, "expiration journal could not be durably closed")

    def confirm_launch(self, op, record, observation, deadline):
        """원 소유 실행을 관측해 종결한다. Xen 제어 명령이나 신원 공급은 하지 않는다."""
        spec = op['request']
        run = self.e.run_record(record)
        if not run or run.get('owner') != {'kind':'recovery', 'recovery_attempt_id':op['recovery_attempt_id']}:
            return None
        if observation['xen_state'] == 'unknown' and (run['phase'] == 'launching'
                or observation['execution_status'] == 'managed'):
            refuse('OBSERVATION_UNAVAILABLE', 'Xen state of the original launch is not yet known')
        expected_cpu = op.get('cpu_policy', {'configured':False})
        proof = None
        if observation['execution_status'] == 'managed':
            self.p.cpu().require(record, deadline, expected=expected_cpu, present=True, expected_identity=run)
            if run.get('cpu_verification') != 'complete':
                # An interrupted bind already has the incarnation but still needs CPU verification.
                run = {**run, 'cpu_verification':'complete'}
                run.pop('cpu_error', None)
                self.e.write(self.e._path(self.p.runtime, 'executions', record['uuid']), run)
                row = {k:observation[k] for k in ('domid', 'xen_state', 'xen_state_flags')}
                observation = self.e.publish_observation(record, row, observation['xen_unique_id'], deadline)
        elif (self.p.process_isolation and observation['presence'] == 'present'
                and observation['xen_state'] in ('running', 'blocked')
                and run.get('phase') == 'launching' and run.get('domid') is None and run.get('xen_unique_id') is None
                and run.get('lifecycle_generation') == spec['expected_lifecycle_generation']
                and run.get('intent_revision') == spec['expected_intent_revision']
                and run.get('cpu_policy') == expected_cpu):
            from xenova_guest_process import confirm_created_domain
            self.e.guard_launch(record, run, deadline)
            try:
                proof = confirm_created_domain(self.p, record, run, observation, deadline)
            except (OSError, ValueError) as error:
                refuse('RECONCILE_REQUIRED', 'original create monitor evidence is unavailable',
                       details={'cause_type':type(error).__name__})
            # bind_launch rechecks the exact Xen incarnation and all accepted context before publication.
            observation = self.e.bind_launch(record, run, observation, deadline, reconciling=True)['observation']
            if observation['xen_state'] == 'unknown':
                refuse('OBSERVATION_UNAVAILABLE', 'Xen state changed to unknown while confirming the original launch')
        else:
            return None
        if self.p.clock() >= deadline:
            refuse('DEADLINE_EXCEEDED', 'recovery confirmation budget expired')
        if proof or op.get('error'):
            op['reconciliation'] = {'status':'confirmed', 'executed':True, 'outcome':'launch_observed',
                'physical_commands':0, 'checks':{'correlated_execution':True, 'cpu_policy':True}, 'reasons':[],
                'host_boot_id':self.e.host, 'observed_monotonic_ms':observation['observed_monotonic_ms'],
                'original_phase':op['phase'], 'original_error':op.get('error'),
                **({'launch_proof':proof} if proof else {})}
        result = self.complete(op, observation)
        return {'attempt':op, 'original_result':result, 'current_observation':observation}

    def observe_original_launch(self, op, deadline):
        """확인 기한 초과는 원 시도의 실패가 아니며, 같은 기록만 다시 확인할 수 있다."""
        try:
            record, _, observation = self.conditions(op['request'], deadline, target=False,
                                                     gate=False, identity_only=True)
            return observation, self.confirm_launch(op, record, observation, deadline)
        except ExecutionError as error:
            cause = error.error
            if cause['code'] == 'CPU_APPLY_INCOMPLETE':
                cause = cause['details'].get('cause', cause)
            if cause['code'] not in ('OBSERVATION_UNAVAILABLE', 'DEADLINE_EXCEEDED'):
                raise
            reason = cause['message']
        except (PlatformError, OSError, ValueError) as error:
            if self.p.clock() < deadline and not isinstance(error.__cause__, TimeoutExpired):
                raise
            reason = str(error)
        refuse('OBSERVATION_UNAVAILABLE', reason, retryable=True,
               details={'operation_id':op['operation_id'], 'recovery_attempt_id':op['recovery_attempt_id'],
                        'accepted':True})

    @traced('recovery.once')
    def _once(self, spec, *, execute, context):
        spec = validate_request(spec)
        identity = spec["recovery_attempt_id"]
        deadline = spec["deadline_monotonic_ms"]/1000
        if spec["expected_host_boot_id"] != self.e.host:
            refuse("TARGET_CHANGED", "previous host boot recovery is not executable")
        op = self.read_attempt(identity) if self.path(identity).exists() or self.path(identity).is_symlink() else None
        if op:
            context.update(operation_id=op["operation_id"], replayed=True)
            if op["request"] != spec:
                refuse("IDEMPOTENCY_CONFLICT", "recovery attempt ID has a different request")
            if op["phase"] in ("completed", "failed"):
                if (execute and op['phase'] == 'failed'
                        and (op.get('reconciliation') or {}).get('outcome') == 'stopped_without_replacement'):
                    run = self.e.run_record({'uuid':spec['vm_id']})
                    if run and run.get('phase') == 'uncertain' and self.settle_expired(
                            op, min(self.p.clock()+15, getattr(self.p, 'response_deadline', float('inf')))):
                        op = self.read_attempt(identity)
                current = None
                try:
                    record = self.m._state(spec["vm_id"], self.p.records(validate_assets=False))["record"]
                    current = self.e.observe(record, self.p.clock()+15)
                except (PlatformError, OSError, ValueError, KeyError, TypeError) as error:
                    self.diagnostics.append({'code':'DIAGNOSTIC_UNAVAILABLE',
                        'sink':'current-observation','cause_type':type(error).__name__})
                self.event("recovery-replayed", {'uuid':spec['vm_id']}, op["operation_id"], {"recovery_attempt_id":identity})
                if op.get("error"):
                    refuse(op["error"]["code"], op["error"]["message"],
                           details={**op["error"]["details"], "operation_id":op["operation_id"], "replayed":True,
                                    "current_observation":current})
                return {"attempt":op, "original_result":op["result"], "current_observation":current}
            if self.p.clock() >= deadline and op["phase"] in ("accepted", "removed"):
                if execute:
                    self.expire_prelaunch(op)
                refuse("DEADLINE_EXCEEDED", "recovery attempt deadline expired",
                       details={"operation_id":op["operation_id"], "recovery_attempt_id":identity,
                                "replayed":True, "next_action":"use_new_attempt_after_fresh_observation" if execute else "execute_original_attempt_to_reconcile_expiration"})
            if not execute:
                return {"plan":True, "attempt":op}
            # A recorded launch observed under this exact owner can be finalized without issuing Xen again.
            if op["phase"] in ("launching", "observed", "uncertain"):
                # 만료 후의 15초는 기존 실행을 확인·종결하는 읽기 예산이다.
                # 이 분기에는 start/stop/create/repin 경로가 없다.
                reconcile_end = min(self.p.clock()+15, getattr(self.p, 'response_deadline', float('inf')))
                observation, result = self.observe_original_launch(op, reconcile_end)
                if result is not None:
                    return result
                if observation['presence'] == 'absent' and self.settle_expired(op, reconcile_end):
                    settled = self.read_attempt(identity)
                    error = settled['error']
                    refuse(error['code'], error['message'],
                           details={**error['details'], 'replayed':True, 'current_observation':observation})
                refuse("RECONCILE_REQUIRED", "a previous launch may still exist; no stop/create is repeated",
                       details={"operation_id":op["operation_id"], "replayed":True})
        if self.p.clock() >= deadline or deadline-self.p.clock() > 120:
            refuse("DEADLINE_EXCEEDED", "same-host recovery deadline must be within the next 120 seconds")
        resume_removed = op is not None and op["phase"] in ("stopping", "removed")
        record, records, observation = self.conditions(spec, deadline, target=not resume_removed)
        self.mode_allowed(spec, record)
        cpu_context = self.p.cpu().require(record, deadline, expected=op.get('cpu_policy', {'configured':False}) if op else None)
        if resume_removed and observation["presence"] != "absent":
            refuse("RECONCILE_REQUIRED", "old shutdown result is uncertain; request is not repeated",
                   details={"operation_id":op["operation_id"], "replayed":True})
        if execute and observation["presence"] == "absent":
            self.e.settle_expired_launch(record, deadline)
        for other in self.attempts():
            request = other["request"]
            if (other["recovery_attempt_id"] != identity and request["vm_id"] == spec["vm_id"] and
                    other["phase"] not in ("completed", "failed") and
                    request["expected_host_boot_id"] == self.e.host and
                    request["expected_lifecycle_generation"] == spec["expected_lifecycle_generation"] and
                    request["expected_intent_revision"] == spec["expected_intent_revision"]):
                if other["phase"] in ("launching", "observed", "uncertain", "stopping"):
                    if execute and self.settle_expired(other, deadline):
                        continue
                    self.unresolved(other, "an earlier physical action needs reconciliation")
                if self.p.clock()*1000 >= request["deadline_monotonic_ms"]:
                    if execute:
                        self.expire_prelaunch(other)
                        continue
                    self.unresolved(other, "expired pre-launch owner must be reconciled by an explicit execution request")
                refuse("OPERATION_IN_PROGRESS", "another unresolved recovery owns this intent", retryable=True,
                       details={"blocking_attempt_id":other["recovery_attempt_id"]})
        if not execute:
            return {"plan":True, "request":spec, "current_observation":observation}
        if op is None:
            op = {"cpu_policy":cpu_context, "schema_version":"1.0", "operation_id":uuid4(), "recovery_attempt_id":identity,
                  "request":spec, "request_hash":fingerprint(spec), "host_boot_id":self.e.host,
                  "accepted_monotonic_ms":int(self.p.clock()*1000), "result":None, "error":None, "events":[]}
            context["operation_id"] = op["operation_id"]
            # Admission publication and host barrier are serialized by the same short lock.
            from xenova_execution import short_lock
            with short_lock(self.p.runtime, "admission", deadline=deadline,
                            clock=self.p.clock, sleep=self.p.sleep):
                self.e.gate()
                self.save(op, "accepted")
        owner = {"kind":"recovery", "recovery_attempt_id":identity}
        confirmed = {}
        crash_only = spec["mode"] == "restart-crashed"
        stop_result = {}
        try:
            if not resume_removed and observation["presence"] == "present":
                op['stop_submission'] = {'state':'not-submitted', 'target':{
                    'host_boot_id':self.e.host, **{name:observation[name] for name in
                    ('execution_id','domid','xen_unique_id')}}}
                self.save(op, "stopping")
                op['stop_submission']['state'] = 'submitted'
                self.save(op, 'stopping')
                try:
                    self.p.stop_many([record], force=crash_only or spec["mode"] == "force-restart", deadline=deadline,
                                     _expected_runs={record["uuid"]:observation["xen_unique_id"]},
                                     _required_state="crashed" if crash_only else None, _result=stop_result)
                except (PlatformError, OSError, ValueError):
                    op['stop_submission']['state'] = (
                        'not-submitted' if crash_only and not stop_result.get('physical_submitted') else 'returned')
                    raise
                op['stop_submission']['state'] = 'returned'
                self.save(op, 'stopping')
                self.e.checkpoint('recovery-stop-returned', op)
            self.save(op, "removed")
            record, records, absent = self.conditions(spec, deadline, target=False)
            if absent["presence"] != "absent":
                refuse("TARGET_CHANGED", "a domain appeared before recovery launch")
            if observation["presence"] == "present":
                self.e.settle_expired_launch(record, deadline)
            self.e.require_launch_available(record)
            if vm.has_network(record):
                self.p.prepare_dhcp(records, deadline=deadline)
            if self.p.cpu().context(record) != op.get('cpu_policy', {'configured':False}):
                refuse('TARGET_CHANGED', 'accepted recovery CPU policy changed')
            self.save(op, "launching")
            self.p.start(record, _owner=owner, deadline=deadline, _result=confirmed)
            observed = confirmed['observation']
            self.save(op, "observed")
            result = self.complete(op, observed)
            return {"attempt":op, "original_result":result, "current_observation":observed}
        except (PlatformError, OSError, ValueError) as error:
            code = error.error["code"] if isinstance(error, ExecutionError) else "XEN_EXECUTION_FAILED"
            # A submitted shutdown can remove the old domain before its response
            # is lost. Keep that owner unresolved just like a submitted launch.
            uncertain = op["phase"] in ("stopping", "observed")
            if crash_only and op["phase"] == "stopping" and op["stop_submission"]["state"] == "not-submitted":
                uncertain = False
                if isinstance(error, vm.DomainIdentityError):
                    code = "TARGET_CHANGED"
            if op["phase"] == "launching":
                # A failed write/read of launch ownership must leave the durable
                # launching phase in place. A prior attempt's run is not ours.
                run = self.e.run_record(record)
                owned = bool(run and run.get('owner') == owner)
                aborted = owned and run.get('phase') == 'stopped' and run.get('launch_aborted_before_submit') is True
                uncertain = not aborted and (confirmed.get('physical_submitted') or owned)
            cause = (error.error if isinstance(error, ExecutionError) else
                     {'code':code, 'message':str(error), 'retryable':False, 'details':{}})
            op["error"] = {"code":"RECONCILE_REQUIRED" if uncertain else code, "message":str(error),
                           "retryable":False, "details":{"operation_id":op["operation_id"],
                           "recovery_attempt_id":identity, "accepted":True,
                           "failed_phase":op["phase"],
                           **({'cause':cause} if uncertain or isinstance(error, ExecutionError) else {})}}
            try:
                self.save(op, "uncertain" if uncertain else "failed")
            except OSError:
                refuse("RECONCILE_REQUIRED", "accepted recovery journal could not be finalized",
                       details={"operation_id":op["operation_id"], "recovery_attempt_id":identity, "accepted":True})
            original_error = op['error']
            if (confirmed.get('physical_submitted') and self.p.process_isolation
                    and original_error['details']['failed_phase'] == 'launching'
                    and run and run.get('phase') == 'launching'):
                # The worker can receive a completed result on the original call even if
                # the physical submission's response was lost. This is observation only.
                reconcile_end = min(self.p.clock()+15, getattr(self.p, 'response_deadline', float('inf')))
                try:
                    _, result = self.observe_original_launch(op, reconcile_end)
                    if result is not None:
                        return result
                except ExecutionError as confirmation_error:
                    if confirmation_error.error['code'] == 'OBSERVATION_UNAVAILABLE':
                        raise
                except (PlatformError, OSError, ValueError):
                    pass
            raise ExecutionError(**original_error) from error
