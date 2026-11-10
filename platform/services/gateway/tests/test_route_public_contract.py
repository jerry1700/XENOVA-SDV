"""기존 operator.v2 hash와 home9/원 RETURN revision의 공개 경계. 실제 소켓/주행 시험은 아니다."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from xenova_gateway import operator_client as cli
from xenova_gateway.operator_contract import request,fingerprint,OperatorError


HOST=str(UUID(int=101));INSTANCE=str(UUID(int=102));OWNER=str(UUID(int=103))
KEY='g1:'+HOST+':1000:'+str(UUID(int=104))


def call(action='home.return'):
    args=dict(expected_goal_revision='2',expected_goal_sha256='12'*32,
        expected_operation_revision='4',expected_permission_revision='3')
    if action=='home.return':args['ready_revision']='19'
    return dict(version=2,action=action,request_id=KEY,host_boot_id=HOST,gateway_instance_id=INSTANCE,
        expected_generation=7,expected_owner=OWNER,deadline_pi_ms=11000,args=args)


def test_home_exact_readonly_and_write_contract_preserve_original_fingerprint():
    assert request(dict(version=2,action='home.status'))==dict(version=2,action='home.status')
    original=call();assert request(original) is original
    same=deepcopy(original);same['args']=dict(reversed(tuple(same['args'].items())))
    assert fingerprint(same)==fingerprint(original)
    assert request(call('home.set'))['args']['expected_goal_revision']=='2'
    bad=deepcopy(original);bad['args']['ready_revision']=19
    with pytest.raises(OperatorError):request(bad)
    bad=deepcopy(original);bad['args']['expected_goal_sha256']='0'*64
    with pytest.raises(OperatorError):request(bad)
    with pytest.raises(OperatorError):request(dict(version=2,action='home.status',ready_revision='19'))


@pytest.mark.parametrize('ready',[None,'19'])
def test_cli_uses_current_home_revision_without_reservation_or_manual_revision_option(monkeypatch,capsys,ready):
    sent=[];saved=[]
    class Client:
        def __init__(self,*args):pass
        def call(self,body):
            sent.append(deepcopy(body))
            if body['action']=='status':
                data=dict(host_boot_id=HOST,gateway_instance_id=INSTANCE,generation=7,owner=OWNER)
            elif body['action']=='operation.status':
                data=dict(supported=True,binding=dict(operation_revision='4',permission_revision='3'),ready_revision='999')
            elif body['action']=='home.status':
                data=dict(supported=True,goal_revision='2',goal_sha256='12'*32,
                    return_ready_revision=ready,blocked_reason='STOP_REQUIRED' if ready is None else None)
            else:data=dict(terminal=True,phase='DONE')
            return dict(version=2,ok=True,data=data)
    class Cache:
        def save(self,body):saved.append(deepcopy(body))
        def observe(self,*args):pass
    monkeypatch.setattr(cli,'OperatorClient',Client);monkeypatch.setattr(cli,'Attempts',Cache)
    monkeypatch.setattr(cli,'new_request_id',lambda host:KEY)
    monkeypatch.setattr(Path,'read_text',lambda self,*args,**kwargs:HOST)
    result=cli.main(['--wait-ms','0','home','return']);capsys.readouterr()
    writes=[body for body in sent if body['action']=='home.return']
    assert result==(3 if ready is None else 0)
    if ready is None:assert not writes and not saved
    else:
        assert len(writes)==len(saved)==1 and writes[0]==call()
        assert writes[0]['args']['ready_revision']=='19'  # Public AUTO ready999 is a different namespace.


def test_cli_does_not_offer_an_internal_ready_revision_override():
    with pytest.raises(SystemExit):cli.main(['home','return','--ready-revision','19'])
