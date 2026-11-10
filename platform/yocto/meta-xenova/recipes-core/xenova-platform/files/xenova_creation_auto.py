"""허용된 실패 private create의 일회 후속 처리. 기존 LinuxSlot과 원 reconciliation을 쓴다.

시험 프로필의 명시 opt-in만 허용한다. polling·영구 queue·별도 daemon은 만들지 않는다.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

from xenova_preparation import LinuxSlot, request_output
from xenova_runtime import operation_lock, PlatformError, OperationBusy

PROFILE='trial-1-plus-1'


def private_json(path, maximum=8192):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode&0o022:
            raise ValueError('unsafe automatic cleanup metadata')
        raw=stream.read(maximum+1)
    if len(raw)>maximum:raise ValueError('automatic cleanup metadata exceeded')
    return json.loads(raw)


def enrollment(manager):
    path=getattr(manager.p,'automatic_cleanup_policy_path',manager.defaults_path.with_name('creation-reconcile-policy.json'))
    try:
        value=private_json(path,2048)
        if value!={'schema_version':'1.0','profile':PROFILE}:
            raise ValueError('explicit trial-1-plus-1 automatic cleanup policy required')
    except FileNotFoundError:return None
    except (OSError,ValueError) as error:
        from xenova_creation import fail
        fail('INVALID_SPEC','automatic cleanup policy cannot be verified; request not accepted',
             details={'accepted':False,'cause_type':type(error).__name__})
    return {'version':1,'profile':PROFILE,'observations':0,'state':'enrolled',
            'last_condition':None,'first_error':None,'last_error':None}


def validate(op):
    value=op['automatic_cleanup']
    if (not isinstance(value,dict) or set(value)!={'version','profile','observations','state','last_condition','first_error','last_error'}
            or type(value['version']) is not int or value['version']!=1 or value['profile']!=PROFILE or type(value['observations']) is not int
            or not 0<=value['observations']<=2 or value['state'] not in ('enrolled','pending','observing','deferred','complete')
            or op.get('action')!='create' or op.get('template',{}).get('guest_kind')!='linux'):
        raise ValueError('invalid automatic cleanup state')
    token=value['last_condition']
    if token is not None and (not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token)):
        raise ValueError('invalid automatic cleanup condition')
    for key in ('first_error','last_error'):
        error=value[key]
        if error is not None and (not isinstance(error,dict) or len(json.dumps(error))>1024
                or set(error)-{'code','cause_type','errno'}):raise ValueError('invalid automatic cleanup cause')
    if value['observations']==0 and token is not None:raise ValueError('automatic observation was not consumed')
    if value['observations']==0 and (value['first_error'] is not None or value['last_error'] is not None):
        raise ValueError('automatic cleanup lost observation count')
    if (value['state'] in ('enrolled','pending')) != (value['observations']==0):
        raise ValueError('automatic cleanup phase/count differs')
    if value['observations'] and token is None:raise ValueError('automatic cleanup lost condition')
    if value['state']!='enrolled' and (op['status']!='RECOVERY_REQUIRED' or not isinstance(op.get('error'),dict)):
        raise ValueError('automatic cleanup needs the original failed create')
    if value['state']=='complete' and op.get('reconciliation',{}).get('phase')!='complete':
        raise ValueError('automatic cleanup cannot release an unsettled create')


def failed(op):
    value=op.get('automatic_cleanup')
    if value is not None and value['state']=='enrolled' and op.get('phase')=='preparing':value['state']='pending'


def selected(manager, identity):
    from xenova_creation import validate as validate_creation, digest
    from xenova_vm import UUID
    if not isinstance(identity,str) or not UUID.fullmatch(identity):raise ValueError('original operation UUID required')
    op=manager._read(manager.data/'operations'/(identity+'.json'))
    if (op['operation_id']!=identity or op['action']!='create' or op['spec_hash']!=digest(op['initial_spec'])
            or op['owned_workspace']!='instances/.operation-'+identity):raise ValueError('automatic original differs')
    validate_creation(op)
    return op


def cause(error):
    fallback='OBSERVATION_UNAVAILABLE' if isinstance(error,(OSError,ValueError,PlatformError)) else 'INTERNAL_ERROR'
    value={'code':getattr(error,'error',{}).get('code',fallback),'cause_type':type(error).__name__}
    if isinstance(error,OSError) and error.errno is not None:value['errno']=error.errno
    return value


def deferred(op,error):
    value=op['automatic_cleanup'];failure=cause(error)
    value['state']='deferred'
    if value['first_error'] is None:value['first_error']=failure
    value['last_error']=failure


def condition(manager,op):
    """작은 현재 소유/정리 receipt만 비교한다. 변화 없는 호출은 Xen·전체 FD를 재검사하지 않는다."""
    from xenova_creation import digest
    root=manager.data/op['owned_workspace']
    owner=manager._owner_state(op)
    resources=None;ready=True
    if root.exists() or root.is_symlink():
        info=root.lstat()
        if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('unsafe private cleanup workspace')
        resources=private_json(root/'.preparation-resources.json')
        if not isinstance(resources,dict):raise ValueError('invalid preparation resource receipt')
        ready=(type(resources.get('schema')) is int and resources.get('schema')==1 and resources.get('workspace')==str(root) and
               resources.get('clean') is True and resources.get('pending') is False and
               resources.get('mounts')==[] and resources.get('loops')==[])
        inode=[info.st_dev,info.st_ino]
    else:inode=None
    resolution=op.get('reconciliation')
    progress=({k:resolution[k] for k in ('phase','remaining','deleting','root_pending')} if resolution else None)
    return digest({'host':manager.p.boot_id_reader(),'gate':manager.p.execution().barrier(),
                   'owner':owner,'root':inode,'resources':resources,'progress':progress}),ready and owner=='absent'


def eligible(manager,op):
    value=op.get('automatic_cleanup')
    if op['status']!='RECOVERY_REQUIRED' or value is None or value['state'] in ('enrolled','complete') or value['observations']>=2:
        return False
    if op.get('reconciliation',{}).get('phase')=='complete':return False
    if value['observations']:
        try:token,_=condition(manager,op)
        except (OSError,ValueError,PlatformError):return False
        if token==value['last_condition']:return False
    return True


def start(manager, identity):
    """원 응답 후 슬롯 하나를 상속하는 자식만 시작한다. busy에는 대기·기록·재시도하지 않는다."""
    try:
        op=selected(manager,identity)
        if not eligible(manager,op):return
        with request_output(),LinuxSlot(manager.p) as slot:
            environment={k:v for k,v in os.environ.items() if k in (
                'PATH','PYTHONPATH','PYTHONHOME','PYTHONNOUSERSITE','PYTHONDONTWRITEBYTECODE','LD_LIBRARY_PATH','LANG','TZ') or k.startswith('LC_')}
            child=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),str(manager.data),
                str(manager.p.runtime),str(manager.defaults_path),identity,str(slot.fd)],stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(slot.fd,),close_fds=True,
                start_new_session=True,env=environment)
            threading.Thread(target=child.wait,name='creation-cleanup-reaper',daemon=True).start()
    except OperationBusy:
        return
    except Exception as error:
        # 후속 실행 부재는 원 실패/예약을 바꾸지 않는다. 같은 원 요청이 다음 기회를 확인한다.
        return {'code':'AUTOMATIC_CLEANUP_LAUNCH_UNCONFIRMED',**{k:v for k,v in cause(error).items() if k!='code'}}


def run(manager, identity, inherited_fd):
    """최초 1회·조건 변화 시 추가 최대 1회. 횟수는 원장에 먼저 내구 소비한다."""
    from xenova_creation import digest, diagnose, reconcile, save_owned
    from xenova_vm import storage_check
    with request_output() as output:
        slot=LinuxSlot(manager.p).adopt(inherited_fd)
        output.stack.callback(slot.__exit__,None,None,None)
        storage_check(manager.data)
        with operation_lock(manager.p.runtime):
            op=selected(manager,identity)
            if not eligible(manager,op):return
            manager._writable()
            value=op['automatic_cleanup'];previous=digest(op)
            try:token,ready=condition(manager,op);error=None
            except (OSError,ValueError,PlatformError) as failure:
                token=digest(cause(failure));ready=False;error=failure
            value.update(observations=value['observations']+1,last_condition=token,state='observing')
            # 새로운 생성 기한이 아니다. 이미 수락한 cleanup 여유만 일회 후속 예산으로 사용한다.
            deadline=manager.p.clock()+op['creation']['limits']['cleanup_ms']/1000
            manager.p.response_deadline=deadline
        save_owned(manager,op,deadline,previous)
        try:
            if error is not None:raise error
            if not ready:raise PlatformError('owned preparation cleanup has not completed')
            request={'operation_id':identity,'vm_id':op['vm_id'],'expected_spec_sha256':op['spec_hash'],'purpose':'discard-private'}
            resolution=op.get('reconciliation')
            evidence=None
            if resolution:
                request.update(resolution['request'])
            else:
                plan,fingerprint=diagnose(manager,op,deadline)
                evidence=(digest(op),plan,fingerprint,manager.p.boot_id_reader(),manager.p.execution().barrier())
                request.update(expected_context_sha256=fingerprint,reconciliation_id=identity)
            reconcile(manager,request,True,{},deadline,output,_slot=slot,_diagnosis=evidence)
            outcome='complete';failure=None
        except Exception as error:
            outcome='deferred';failure=error
        latest=selected(manager,identity);previous=digest(latest)
        value=latest['automatic_cleanup'];value['state']=outcome
        if failure is not None:deferred(latest,failure)
        if digest(latest)!=previous:save_owned(manager,latest,deadline,previous)


if __name__=='__main__':
    from xenova_management import Manager
    from xenova_vm import Platform
    data,runtime,defaults,identity,fd=sys.argv[1:]
    run(Manager(Platform(Path(data),Path(runtime),defaults_path=Path(defaults))),identity,int(fd))
