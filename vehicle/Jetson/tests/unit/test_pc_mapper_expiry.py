"""M 예약 상태의 원 관측 E를 검사한다. native lease 반환 자체의 증거는 아니다."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from tools.hil_observation.mapper import _MapperMain


def mapper():
    owner=object.__new__(_MapperMain)
    owner.actor=SimpleNamespace(cancel_request=Mock())
    owner.acquire=SimpleNamespace(expires_us=200)
    owner.meta=(object(),SimpleNamespace(expires_j_us=100),'PARSER_WARMUP')
    owner.offer=object();owner.offer_typed=object();owner.acquire_draft=None
    owner.early=owner.outgoing=None
    return owner


class PcMapperExpiryTests(unittest.TestCase):
    def test_expired_frame_cancels_original_pending_read_even_before_reservation_expiry(self):
        for now in (99,100,101):
            with self.subTest(now=now):
                owner=mapper();original=owner.acquire
                with patch('tools.hil_observation.mapper.j_us',return_value=now):owner._expire_heads()
                owner.actor.cancel_request.assert_called_once_with(original)
                self.assertIsNone(owner.meta);self.assertIsNone(owner.acquire);self.assertIsNone(owner.offer)

    def test_current_frame_keeps_original_pending_read(self):
        owner=mapper();original=owner.acquire
        with patch('tools.hil_observation.mapper.j_us',return_value=98):owner._expire_heads()
        owner.actor.cancel_request.assert_not_called();self.assertIs(owner.acquire,original)

    def test_cancellation_failure_is_not_masked_as_a_successful_frame_drop(self):
        owner=mapper();owner.actor.cancel_request.side_effect=RuntimeError('original cancel failure')
        with patch('tools.hil_observation.mapper.j_us',return_value=100),self.assertRaises(RuntimeError):
            owner._expire_heads()
        self.assertIsNotNone(owner.acquire)
