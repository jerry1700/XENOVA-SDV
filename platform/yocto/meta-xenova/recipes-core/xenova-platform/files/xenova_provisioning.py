"""영구 VM 자격의 확보·private 전달. 인증 성공/실행 binding·건강 판정과는 별개다."""
import json
import ipaddress
import os
from pathlib import Path
import stat
import time

import xenova_broker as broker
from xenova_guest_profile import validate
from xenova_runtime import atomic_write
from prepare_rootfs import inside


DISPLAY_GUEST_TOKEN = Path('/etc/xenova/guest-display/guest-data.token')
DISPLAY_GUEST_NETWORK = ipaddress.ip_network('192.168.77.0/24')


def private(path, *, directory=False):
    info=path.lstat()
    if (info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=(0o700 if directory else 0o600)
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode) or info.st_nlink!=1)):
        raise ValueError('private provisioning ownership/mode invalid')


def put(path, content):
    """private adapter 사본만 원자 교체한다. 기존 자격의 새 발급·원본 교체가 아니다."""
    if path.exists() or path.is_symlink():
        private(path)
        if path.read_text()==content:return False
    atomic_write(path,content,mode=0o600)
    return True


def home(data, identity, *, create=False):
    broker.vm_id(identity)
    parent=Path(data)/'provisioning'
    for path in (parent,parent/identity):
        if create and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700,exist_ok=True);broker.sync_directory(path.parent)
        private(path,directory=True)
    return parent/identity


def ensure(data, record, *, gid=None, rules=None, reload=None, deadline=None, clock=time.monotonic, retry_reload=False):
    """검증된 원 대상/프로필의 필요한 계정만 확보한다. 비밀 없는 단계 요약을 반환한다."""
    profile=validate(record['provisioning_profile'],guest_kind=record.get('guest_kind'))
    channels=profile['channels'];identity=broker.vm_id(record['uuid'])
    if not channels:return {'state':'not-requested','declaration':profile['source'],'channels':[]}
    destination=home(data,identity,create=True)
    accounts=([identity] if set(channels)&{'agent','drive'} else [])+(['telemetry-'+identity] if 'telemetry' in channels else [])
    from xenova_preparation import _current, check_creation
    check_creation()
    current=_current()
    if deadline is None and current is not None and current.creation_deadline is not None:
        deadline,clock=current.creation_deadline,current.creation_clock
    remaining=20 if deadline is None else min(20,deadline-clock())
    if remaining<=0:raise TimeoutError('credential preparation budget expired')
    try:
        result,value=broker.ensure_accounts(accounts,data=Path(data),gid=gid,rules=rules,
                     _pass_fds=getattr(current,'fds',()),_hash_timeout=remaining)
    except broker.SetupError as error:
        from xenova_execution import ExecutionError
        raise ExecutionError('CREDENTIAL_REVOKED' if isinstance(error,broker.CredentialRevoked) else 'CREDENTIAL_SOURCE_UNAVAILABLE',
                             str(error),details={'vm_id':identity}) from error
    check_creation()
    if deadline is not None and clock()>=deadline:raise TimeoutError('credential preparation budget expired after storage')
    summary={'schema_version':'1.0','vm_id':identity,'channels':channels,
             'accounts':accounts,'broker_revision':result['revision'],'credential_state':'stored',
             'broker_application':'unconfirmed','guest_application':'not-applied','authentication':'unobserved'}
    old=destination/'status.json'
    if old.exists():
        private(old);prior=json.loads(old.read_text())
        if prior.get('vm_id')!=identity:raise ValueError('provisioning status identity mismatch')
        if result['reused'] and prior.get('broker_revision')==result['revision']:
            summary.update({k:prior[k] for k in ('broker_application','guest_application')})
    for channel in channels:
        username='telemetry-'+identity if channel=='telemetry' else identity
        secret=value['secrets'][username]
        if channel=='agent':
            # 개발 이미지의 MQTT-04 명령을 명시적으로 허용한다. Agent 자체 기본값은 유지한다.
            content=(f'XENOVA_AGENT_VM_ID={identity}\nXENOVA_AGENT_BROKER_HOST=192.168.77.1\n'
                     f'XENOVA_AGENT_BROKER_PORT=1883\nXENOVA_AGENT_DEMO_COMMANDS=1\n'
                     f'XENOVA_AGENT_BROKER_USERNAME={username}\n'
                     f'XENOVA_AGENT_BROKER_PASSWORD={secret}\n')
            put(destination/'agent.env',content)
        else:
            payload={'vm_id':identity,'broker_ipv4':'192.168.77.1','port':1883,
                     'username':username,'password':secret,'trusted_internal_plaintext':True}
            previous=destination/(channel+'.json')
            if channel=='drive' and previous.exists():
                parsed,_=bootstrap_api().credentials(previous,identity,'drive')
                if 'world' in parsed:payload['world']=parsed['world']
            put(destination/(channel+'.json'),json.dumps(payload,sort_keys=True)+'\n')
    if not result['reused'] or retry_reload and summary['broker_application']=='unconfirmed':
        try:summary['broker_application']=(reload or broker.reload_broker)()
        except (broker.SetupError,OSError):summary['broker_application']='unconfirmed'
    put(old,json.dumps(summary,sort_keys=True)+'\n')
    return summary


def bootstrap_api():
    """설치된 제공자와 소스 트리의 동일 codec을 쓴다. import에 외부 효과는 없다."""
    import importlib.util
    path=Path(__file__).with_name('provision_xenova_telemetry.py')
    if not path.exists():path=Path(__file__).resolve().parents[5]/'scripts/provision-xenova-telemetry.py'
    spec=importlib.util.spec_from_file_location('xenova_supplied_bootstrap',path)
    api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
    return api


def install_world(data, identity, source):
    """승인받은 별도 broker 자격을 root 전용 파일에서 설치한다. 계정 발급/접속은 하지 않는다.

    source는 vm_id와 world만 담은 승인 입력이다. Pi 자격은 바꾸지 않고
    별도 world 부분만 채택한다. 새 실행 전에 호출하며 기존 receipt를 재작성하지 않는다.
    """
    api=bootstrap_api();broker.vm_id(identity);incoming,_=api.private_json(Path(source))
    if not isinstance(incoming,dict) or set(incoming)!={'vm_id','world'} or incoming['vm_id']!=identity:
        raise ValueError('separate world credential and matching VM required')
    target=credential_file(data,identity,'drive');current,_=api.credentials(target,identity,'drive')
    merged=api.validate_credentials({**current,'world':incoming['world']},identity,'drive')
    put(target,json.dumps(merged,sort_keys=True)+'\n')
    return {'vm_id':identity,'world_profile':incoming['world']['profile'],'state':'private-configured',
            'authentication':'unobserved'}


def credential_file(data, identity, channel):
    """Worker/Bootstrap용 privileged private 경로. 조회만 하며 생성·reload하지 않는다."""
    if channel not in ('agent','telemetry','drive'):raise ValueError('unsupported credential channel')
    directory=home(data,identity)
    private(directory/'status.json');status=json.loads((directory/'status.json').read_text())
    if status.get('vm_id')!=identity or channel not in status.get('channels',[]):raise ValueError('credential channel not supplied')
    path=directory/('agent.env' if channel=='agent' else channel+'.json');private(path)
    return path


def _display_account(root):
    """설치된 전용 계정의 숫자 신원을 guest namespace 안에서만 읽는다."""
    matches = []
    for line in inside(root, 'etc/passwd', create_parents=False).read_text().splitlines():
        fields = line.split(':')
        if fields and fields[0] == 'xenova-display':
            matches.append(fields)
    if len(matches) != 1:
        raise ValueError('display guest account is missing or duplicated')
    fields = matches[0]
    if (len(fields) != 7 or not fields[2].isdigit() or not fields[3].isdigit()
            or int(fields[2]) <= 0 or int(fields[3]) <= 0):
        raise ValueError('display guest account is invalid')
    return int(fields[2]), int(fields[3])


def _display_secret(path, *, owner_uid=0):
    """dom0 원본 토큰은 root 전용 단일 regular file만 허용한다."""
    path = Path(path)
    info = path.lstat()
    if (info.st_uid != owner_uid or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 512):
        raise ValueError('display guest token source must be root-owned mode 0600 regular file')
    token = path.read_text(encoding='utf-8').strip()
    if not token or any(character in token for character in '\r\n\0'):
        raise ValueError('display guest token source is empty or malformed')
    return token


def _display_address(value):
    if not isinstance(value, str):
        raise ValueError('display guest requires its assigned private IPv4')
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError('display guest requires its assigned private IPv4') from error
    gateway = DISPLAY_GUEST_NETWORK.network_address + 1
    if (not isinstance(address, ipaddress.IPv4Address) or address not in DISPLAY_GUEST_NETWORK
            or address in (DISPLAY_GUEST_NETWORK.network_address,
                           DISPLAY_GUEST_NETWORK.broadcast_address, gateway)):
        raise ValueError('display guest requires its assigned private IPv4')
    return str(address)


def apply_guest_display(record, root, *, token_path=DISPLAY_GUEST_TOKEN, token_owner_uid=0):
    """display 템플릿 사본에 실제 할당 IP와 dom0 Relay 자격을 오프라인 공급한다.

    일반 웹 생성 VM은 factory 예약 주소(.20)를 소유하지 않는다. 템플릿의 .20은
    factory 기본값일 뿐이므로, 인스턴스 사본에는 registry가 확정한 주소를 쓴다.
    토큰은 공용 템플릿에 저장하지 않고 이 private root mount에만 복사한다.
    """
    root = Path(root)
    config_path = inside(root, 'etc/xenova/guest-display-domu.json',
                         follow_final=False, create_parents=False)
    if not config_path.exists() and not config_path.is_symlink():
        return {'state': 'not-requested'}
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError('display guest config must be a regular file')
    address = _display_address(record.get('ipv4'))
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if (not isinstance(config, dict) or config.get('data_token_file') !=
            '/etc/xenova/guest-display/guest-data.token'):
        raise ValueError('display guest token destination differs from the provisioning contract')
    uid, gid = _display_account(root)
    token = _display_secret(token_path, owner_uid=token_owner_uid)
    config['bind'] = address
    atomic_write(config_path, json.dumps(config, indent=2) + '\n', mode=0o644)

    directory = inside(root, 'etc/xenova/guest-display', follow_final=False)
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('display guest credential directory is unsafe')
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    os.chown(directory, uid, gid, follow_symlinks=False)
    destination = inside(root, 'etc/xenova/guest-display/guest-data.token', follow_final=False)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError('display guest token destination is unsafe')
    atomic_write(destination, token + '\n', mode=0o600)
    os.chown(destination, uid, gid, follow_symlinks=False)
    return {'state': 'private-root-configured', 'bind': address,
            'token_path': '/etc/xenova/guest-display/guest-data.token'}


def receipt_directory(data,identity,execution,*,create=False):
    """기존 ProductionBootstrap의 receipt_dir/VM/execution 배치를 공유한다. 새 신원 원본은 없다."""
    broker.vm_id(identity);broker.vm_id(execution)
    parent=Path(data)/'drive-rejoin'
    for path in (parent,parent/identity,parent/identity/execution):
        if create and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700,exist_ok=True);broker.sync_directory(path.parent)
        private(path,directory=True)
    return parent/identity/execution


def apply_linux(data, record, root, *, factory=False, display_token_path=DISPLAY_GUEST_TOKEN, **kwargs):
    """기존 private root mount에서만 호출. 공용 template/seed 빌드에는 호출하지 않는다."""
    from prepare_rootfs import write
    summary=ensure(data,record,**kwargs)
    root=Path(root)
    display=apply_guest_display(record,root,token_path=display_token_path)
    if 'agent' not in summary['channels']:
        return {**summary,'display_application':display['state']}
    if not inside(root,'usr/lib/systemd/system/xenova-agent.service',create_parents=False).is_file():
        raise ValueError('declared Agent adapter is not installed')
    if factory:
        write(root,'etc/xenova/platform-identity.json',json.dumps({'schema':1,'vm_id':record['uuid']})+'\n',0o600)
    elif inside(root,'etc/xenova/instance-id').read_text().strip()!=record['uuid']:
        raise ValueError('guest identity differs from credential target')
    target=inside(root,'etc/xenova/agent.env',follow_final=False)
    put(target,credential_file(data,record['uuid'],'agent').read_text())
    write(root,'etc/systemd/system/xenova-agent.service.d/20-xenova-provisioning.conf',
          '[Unit]\nConditionPathExists=/etc/xenova/agent.env\n',0o644)
    summary['guest_application']='private-root-configured'
    summary['display_application']=display['state']
    stored={key:value for key,value in summary.items() if key!='display_application'}
    put(home(data,record['uuid'])/'status.json',json.dumps(stored,sort_keys=True)+'\n')
    return summary


def status(data, identity):
    """공개 가능한 단계 요약. 비밀 파일·broker 원본·실행 관측을 읽지 않는다."""
    path=Path(data)/'provisioning'/broker.vm_id(identity)/'status.json'
    if not path.exists() and not path.is_symlink():return {'vm_id':identity,'state':'not-supplied'}
    home(data,identity);private(path);value=json.loads(path.read_text())
    if value.get('vm_id')!=identity:raise ValueError('provisioning status identity mismatch')
    allowed={'schema_version','vm_id','channels','accounts','broker_revision','credential_state',
             'broker_application','guest_application','authentication'}
    if set(value)!=allowed:raise ValueError('invalid provisioning status')
    return value


def apply_stopped_linux(platform, record, *, factory=False, **kwargs):
    """호출자는 기존 대상 lease·정지/미사용 guard를 유지해야 한다. 자기 mount만 정리한다."""
    from prepare_instance import Workspace
    work=Workspace(platform.data)
    try:
        root=work.path/'root'
        work.mount(work.loop(platform.data/'instances'/record['id']/'root.raw'),root)
        return apply_linux(platform.data,record,root,factory=factory,**kwargs)
    finally:work.close()


def bootstrap_execution(platform, record, *, deadline, store_factory=None, observe=None):
    """현재 실행에 기존 Bootstrap/receipt를 적용한다. Store 게시와 guest 소비/건강은 별개다."""
    from xenova_drive_binding import observe as binding
    api=bootstrap_api()
    fields=('vm_id','host_boot_id','execution_id','domid','xen_unique_id')
    applied=[]
    previous_live=None

    def current():
        if platform.clock()>=deadline:raise TimeoutError('execution supply budget expired')
        row=(observe or (lambda:binding(platform,record['uuid'],timeout_s=min(2,deadline-platform.clock()),
                                        wait_for_live=True,previous_live=previous_live)))()
        if row.get('status')!='observed':raise ValueError('current execution is not eligible for provisioning')
        if row.get('signal_profile') is None:raise ValueError('loaded execution profile unavailable; no latest-profile inference')
        profile=validate(row['signal_profile'])
        if applied and applied[0]!=profile:raise ValueError('loaded execution profile changed')
        if not applied:applied.append(profile)
        return {key:row[key] for key in fields}
    context=current();profile=applied[0]
    previous_live=context
    directory=receipt_directory(platform.data,record['uuid'],context['execution_id'],create=True)
    channels=[c for c in ('telemetry','drive') if c in profile['channels']]
    for channel in channels:
        credential,source=api.credentials(credential_file(platform.data,record['uuid'],channel),record['uuid'],channel)
        path=directory/(channel+'.json')
        with api.receipt_lock(path):
            store=(store_factory or api.XenStore)(channel)
            try:
                api.provision(context,credential,source,path,store=store,observe=current,
                              channel=channel,metadata=None,
                              guest_boot_receipt=directory/'telemetry.json' if channel=='drive' else None,
                              drive_profile=('warm-drive-1' if profile['apps'].get('autonomous',{}).get('standby_progress')=='idle'
                                             else 'verified-drive-1') if channel=='drive' else None)
            finally:store.close()
    result=status(platform.data,record['uuid'])
    result.update(guest_application='store-published',execution_id=context['execution_id'])
    # 현재 실행 근거는 원 receipt가 소유한다. 영구 상태 요약에 실행 신원을 중복 저장하지 않는다.
    saved={k:v for k,v in result.items() if k!='execution_id'}
    put(home(platform.data,record['uuid'])/'status.json',json.dumps(saved,sort_keys=True)+'\n')
    return result
