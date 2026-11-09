"""원본 재읽기를 유지하면서 동일 Authority의 중복 해석만 줄이는 회귀.

호출 수는 중복 제거 근거다. 실보드 CPU·주행 지연 개선을 대신하지 않는다.
"""
import errno
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import xenova_gateway.authority as authority
from test_authority_cost import store_at


def test_unchanged_bytes_reuse_one_validation_but_read_file_and_marker_every_time(tmp_path):
    store=store_at(tmp_path/'authority')
    store.update(generation=7,phase='PENDING',pending=dict(host_boot_id=store.host,
        generation=7,deadline_ms=9000,arm_after_apply=True))
    original=store.path.read_bytes()
    with patch.object(store,'_read_current_bytes',wraps=store._read_current_bytes) as read, \
         patch.object(store,'_decode_current',wraps=store._decode_current) as decode, \
         patch.object(store,'validate',wraps=store.validate) as validate, \
         patch.object(store,'check_marker',wraps=store.check_marker) as marker:
        assert all(store.verify() for _ in range(16))
        assert read.call_count==marker.call_count==16
        assert decode.call_count==validate.call_count==1
    assert store.value['pending']['deadline_ms']==9000 and store.path.read_bytes()==original


@pytest.mark.parametrize('change',['mode','uid','marker','symlink','fifo'])
def test_warm_byte_cache_does_not_hide_current_metadata_or_marker_fault(tmp_path,change):
    store=store_at(tmp_path/'authority');assert store.verify()
    original=store.path.read_bytes()
    if change=='mode':store.path.chmod(0o644)
    elif change=='marker':store.marker.write_bytes(b'0\n')
    elif change in ('symlink','fifo'):
        store.path.unlink()
        if change=='fifo':os.mkfifo(store.path,0o600)
        else:
            other=tmp_path/'other';other.write_bytes(original);other.chmod(0o600);store.path.symlink_to(other)
    if change=='uid':
        fstat=os.fstat
        def wrong_uid(fd):
            info=fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode,st_uid=store.uid+1,st_size=info.st_size)
        with patch.object(authority.os,'fstat',wrong_uid):assert not store.verify()
    else:assert not store.verify()
    assert store.error=='AUTHORITY_STORAGE_UNCERTAIN'


def test_same_bytes_new_inode_is_checked_without_renewing_original_authority(tmp_path):
    store=store_at(tmp_path/'authority');assert store.verify()
    before=store.value.copy();original=store.path.read_bytes();old_inode=store.path.stat().st_ino
    temporary=store.path.with_name('replacement.json')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(original)
    temporary.replace(store.path)
    assert store.path.stat().st_ino!=old_inode
    with patch.object(store,'_decode_current',wraps=store._decode_current) as decode:
        assert store.verify()
        assert decode.call_count==0
    assert store.value==before


def test_changed_raw_format_is_revalidated_and_foreign_record_never_adopted(tmp_path):
    store=store_at(tmp_path/'authority');assert store.verify()
    original=store.path.read_bytes()
    with patch.object(store,'_decode_current',wraps=store._decode_current) as decode:
        store.path.write_bytes(b' \n'+original)
        assert store.verify() and decode.call_count==1
        assert store.verify() and decode.call_count==1
        foreign={**store.value,'generation':2}
        store.path.write_text(json.dumps(foreign),encoding='utf-8')
        assert not store.verify() and decode.call_count==2
    assert store.value['generation']==1
    store.path.write_bytes(original)
    assert not store.verify()  # 원 파일이 돌아와도 기존 storage 오류는 자동 해제하지 않는다.


def test_cached_nested_record_is_independent_of_public_reads_and_current_ram(tmp_path):
    store=store_at(tmp_path/'authority');assert store.verify()
    public=store.read_current();public['requests'].append('aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa')
    assert store.value['requests']==[] and store.verify()
    store.value['requests'].append('bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb')
    assert not store.verify() and store.error=='AUTHORITY_STORAGE_UNCERTAIN'


def test_successful_own_save_and_invalid_new_body_both_leave_cache_bounded(tmp_path):
    store=store_at(tmp_path/'authority');assert store.verify()
    store.update(generation=2,reason='current explicit durable change')
    assert store.verify() and store.value['generation']==2
    with patch.object(store,'validate',wraps=store.validate) as validate:
        store.path.write_bytes(b'{invalid')
        assert not store.verify() and validate.call_count==0
    assert store.value['generation']==2


def test_custom_read_hook_is_still_called_and_cannot_be_hidden_by_warm_cache(tmp_path):
    store=store_at(tmp_path/'authority');assert store.verify()
    read=store.read_current
    with patch.object(store,'read_current',side_effect=[read(),OSError(errno.EIO,'read fault')]) as custom:
        assert store.verify()
        assert not store.verify()
        assert custom.call_count==2


def test_changed_validator_is_not_satisfied_by_previous_validation(tmp_path):
    store=store_at(tmp_path/'authority');assert store.verify()
    with patch.object(store,'validate',side_effect=ValueError('changed validator')) as validate:
        assert not store.verify() and validate.call_count==1
