"""동일 재합류 진단의 중복 저장만 제거한다. 새 lease·준비·복구 권한은 만들지 않는다."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import dom0_manager.recovery.rejoin as rejoin

HOST='11111111-1111-4111-8111-111111111111'
A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
EXECUTION='cccccccc-cccc-4ccc-8ccc-cccccccccccc'


@pytest.fixture
def coordinator(tmp_path):
    config=dict(vm_ids=[A,B],receipt_dir=str(tmp_path/'receipts'),
                binding_file=str(tmp_path/'binding.json'),authority_file=str(tmp_path/'authority.json'))
    value=rejoin.RejoinCoordinator(config,lambda vm:dict(status='stopped'),
        lambda row:None,HOST,clock=lambda:10.,uid=os.geteuid())
    try:yield value
    finally:assert value.stop()


def test_repeated_actual_nonobserved_work_records_once_without_publishing_binding(coordinator):
    q=coordinator;write=rejoin.atomic_json
    with patch.object(rejoin,'atomic_json',wraps=write) as stored, \
         patch.object(rejoin,'private_json',wraps=rejoin.private_json) as read:
        for _ in range(12):assert q._work(A,initial_row=dict(status='stopped'))==dict(status='stopped')
        assert stored.call_count==1 and read.call_count==11
    assert q.records[A]==dict(stage='STOPPED',execution_id=None)
    assert not Path(q.config['binding_file']).exists()
    assert q.cache.current(A,10.) is None


@pytest.mark.parametrize('field',['applied_generation','observed_monotonic_ms','deadline_monotonic_ms'])
def test_entire_original_record_including_time_fields_is_compared(coordinator,field):
    # 원래부터 generic journal에 보존 가능한 값의 차이다. 이 자료는 admission/clock proof가 아니다.
    q=coordinator;old=dict(stage='PREPARING',execution_id=EXECUTION,**{field:500})
    q._record(A,old);changed={**old,field:501}
    with patch.object(rejoin,'atomic_json',wraps=rejoin.atomic_json) as stored:
        q._record(A,changed)
        assert stored.call_count==1
    assert q.records[A][field]==501
    assert json.loads(q.path.read_text())['vms'][A]==changed


def test_new_stage_save_failure_keeps_previous_record_and_exact_retry(coordinator):
    q=coordinator;old=dict(stage='PREPARING',execution_id=EXECUTION);q._record(A,old)
    changed=dict(stage='READY_OBSERVED',execution_id=EXECUTION,applied_generation=42,reason=None)
    before=q.path.read_bytes()
    with patch.object(rejoin,'atomic_json',side_effect=OSError('declared write failure')) as write:
        with pytest.raises(OSError):q._record(A,changed)
        with pytest.raises(OSError):q._record(A,changed)
        assert write.call_count==2
    assert q.records[A]==old and q.path.read_bytes()==before
    q._record(A,changed)
    assert q.records[A]==changed


@pytest.mark.parametrize('change',['missing','corrupt','public','foreign_content'])
def test_unchanged_memory_cannot_hide_missing_changed_or_unsafe_original_file(coordinator,change):
    q=coordinator;row=dict(stage='UNAVAILABLE',execution_id=None);q._record(A,row)
    if change=='missing':q.path.unlink()
    elif change=='corrupt':q.path.write_bytes(b'{invalid')
    elif change=='public':q.path.chmod(0o644)
    else:q.path.write_text(json.dumps(dict(schema_version='1.0-drive-rejoin',vms={})))
    with patch.object(rejoin,'atomic_json',wraps=rejoin.atomic_json) as write:
        q._record(A,row)
        assert write.call_count==1
    assert rejoin.private_json(q.path,os.geteuid())==dict(schema_version='1.0-drive-rejoin',vms={A:row})
    assert q.path.stat().st_mode&0o077==0


def test_failed_fallback_is_propagated_instead_of_returning_cached_success(coordinator):
    q=coordinator;row=dict(stage='UNAVAILABLE',execution_id=None);q._record(A,row)
    with patch.object(rejoin,'private_json',side_effect=OSError('original read failure')), \
         patch.object(rejoin,'atomic_json',side_effect=OSError('original write failure')):
        with pytest.raises(OSError,match='original write failure'):q._record(A,row)
    assert q.records[A]==row


def test_other_vm_records_are_preserved_in_exact_noop_and_changed_commit(coordinator):
    q=coordinator;first=dict(stage='UNAVAILABLE',execution_id=None)
    other=dict(stage='PROVISIONED',execution_id=EXECUTION)
    q._record(A,first);q._record(B,other)
    with patch.object(rejoin,'atomic_json',wraps=rejoin.atomic_json) as write:
        q._record(A,dict(first));assert write.call_count==0
        q._record(A,dict(stage='STOPPED',execution_id=None));assert write.call_count==1
    assert q.records[B]==other and json.loads(q.path.read_text())['vms'][B]==other
