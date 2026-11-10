"""명시 binding 만료는 feature 준비를 제한하며 Gateway 예외로 확대하지 않는다."""
from types import SimpleNamespace
from unittest.mock import Mock
from xenova_gateway.episode import EpisodeOwner


def owner(binding):
    value=object.__new__(EpisodeOwner)
    value.r=SimpleNamespace(bindings=SimpleNamespace(current=binding),_clock=lambda:100.,
        episode_support=False,machine=SimpleNamespace(episode_output_evidence_required=False))
    value.peers={('vm-a','execution-a','auto'):0,('vm-a','execution-a','manual'):0}
    value.activated=set();value.registry=None;value.request_hwm={}
    return value


def test_binding_that_expires_on_next_read_is_only_sampled_once_per_vm():
    binding=Mock(side_effect=[{'execution_id':'execution-a'},None]);value=owner(binding)
    value.declare(('vm-b','execution-b','auto'),0)
    binding.assert_called_once_with('vm-a',100.)
    assert ('vm-a','execution-a','auto') in value.peers
    assert ('vm-a','execution-a','manual') in value.peers


def test_expired_binding_removes_old_peer_without_exception_or_grant():
    binding=Mock(return_value=None);value=owner(binding)
    value.declare(('vm-b','execution-b','auto'),0)
    assert not any(key[0]=='vm-a' for key in value.peers)
    assert not value.activated
