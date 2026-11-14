"""VM의 지속 프로세스와 일시 명령의 Linux 수명을 분리한다.

scope는 Xen domain이 아니다. 같은 VM 실행의 libxl monitor/qdisk는 batch 밖에
남기고, xl 명령 자체는 호출자 사망 시 SIGKILL한다. 종료는 신뢰한 Xen 부재 뒤만 한다.
"""
import argparse
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import re
import signal
import stat
import sys


def scope_name(identity, execution_id):
    from xenova_vm import UUID
    if not UUID.fullmatch(identity) or not UUID.fullmatch(execution_id):
        raise ValueError('canonical VM/execution UUID required')
    return 'xenova-vm-'+identity+'-'+execution_id+'.scope'


def owned_scope(identity, name):
    from xenova_vm import UUID
    prefix='xenova-vm-'+identity+'-'
    return (isinstance(name,str) and name.startswith(prefix) and name.endswith('.scope') and
            bool(UUID.fullmatch(name[len(prefix):-6])))


def scope_command(name, argv, *, shutdown_unit='xenova-vm-shutdown.service'):
    """batch 종료와 독립적이며 host 종료 guard보다 나중에 정리되는 scope다."""
    return ['systemd-run','--scope','--quiet','--collect','--unit='+name,
            '--property=Before='+shutdown_unit,*argv]


def command(platform, record, deadline, *, launch=None, target=None, reboot_launch=None):
    """생성은 독립 scope, reboot 요청은 일시 프로세스다. 두 경로 모두 exec 직전 guard를 쓴다."""
    execution=platform.execution()
    context=launch if launch is not None else reboot_launch
    if not context:
        raise ValueError('accepted launch context required')
    args=[sys.executable,'-B',str(Path(__file__).resolve()),
          'create' if launch is not None else 'reboot','--data',str(platform.data),
          '--runtime',str(platform.runtime),'--vm-id',record['uuid'],
          '--host-boot-id',execution.host,'--parent-pid',str(os.getpid()),
          '--deadline-ms',str(int(deadline*1000)),
          '--intent-revision',str(context['intent_revision'] or 0),
          '--execution-id',context['execution_id']]
    if launch is not None:
        scope=launch['process_scope']
        if not owned_scope(record['uuid'],scope):
            raise ValueError('invalid owned launch scope')
        return scope_command(scope,args)
    args += ['--domid',str(target['domid']),'--xen-unique-id',target['xen_unique_id']]
    return args


def cleanup(platform, record, launch, deadline, *, observe_only=False):
    """domain 부재 뒤 소유 backend의 정상 종료를 확인한다. R 예산에서는 신호도 보내지 않는다."""
    from xenova_runtime import PlatformError
    name=(launch or {}).get('process_scope')
    if not name:
        return
    if not owned_scope(record['uuid'],name):
        raise PlatformError('invalid process scope ownership')
    if platform.matching(record,platform.domains(deadline=deadline)):
        raise PlatformError('cannot clean process scope while a domain is present')
    state=platform.timed_command(['systemctl','show',name,'-p','LoadState','--value'],deadline).strip()
    if state=='not-found':
        return
    if state!='loaded':
        raise PlatformError('process scope state unavailable')
    active=platform.timed_command(['systemctl','show',name,'-p','ActiveState','--value'],deadline).strip()
    if active in ('inactive','failed'):
        return
    before=platform.timed_command(['systemctl','show',name,'-p','Before','--value'],deadline).split()
    if 'xenova-vm-shutdown.service' not in before:
        raise PlatformError('process scope shutdown ownership changed')
    if not observe_only:
        # qdisk의 flush/정상 종료 기회를 준다. 무응답을 자동 SIGKILL로 숨기지 않는다.
        # stop job은 Before=shutdown-guard와 역순 대기를 만들 수 있어 신호만 요청한다.
        try:
            platform.timed_command(['systemctl','kill','--signal=TERM','--kill-whom=all',name],deadline)
        except PlatformError:
            # --collect에 의해 조회와 신호 사이에 자연 종료·회수된 scope만 허용한다.
            if platform.timed_command(['systemctl','show',name,'-p','LoadState','--value'],deadline).strip()!='not-found':
                raise
    while True:
        state=platform.timed_command(['systemctl','show',name,'-p','ActiveState','--value'],deadline).strip()
        if state in ('inactive','failed'):
            break
        if not state:
            if platform.timed_command(['systemctl','show',name,'-p','LoadState','--value'],deadline).strip()=='not-found':
                break
            raise PlatformError('process scope final state unavailable')
        if platform.clock()>=deadline:
            raise PlatformError('owned process scope cleanup deadline exceeded')
        platform.sleep(min(.02,max(0,deadline-platform.clock())))
    if platform.matching(record,platform.domains(deadline=deadline)):
        raise PlatformError('domain appeared during scope cleanup; reconciliation required')


def confirm_created_domain(platform, record, launch, row, deadline, *,
                           proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup'),
                           log_root=Path('/var/log/xen')):
    """소유 scope의 현재 xl monitor가 생성 완료한 도메인인지 읽기 근거를 대조한다."""
    from xenova_execution import fingerprint, refuse
    scope = scope_name(record['uuid'], launch['execution_id'])
    if launch.get('process_scope') != scope or launch.get('reboot_from'):
        refuse('RECONCILE_REQUIRED', 'original create scope cannot be confirmed')
    output = platform.timed_command(
        ['systemctl', 'show', scope, '-p', 'LoadState', '-p', 'ActiveState',
         '-p', 'ControlGroup', '-p', 'Before'], deadline)
    properties = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    group = properties.get('ControlGroup', '')
    if (properties.get('LoadState') != 'loaded' or properties.get('ActiveState') != 'active'
            or 'xenova-vm-shutdown.service' not in properties.get('Before', '').split()
            or not group.startswith('/') or '..' in Path(group).parts
            or not group.endswith('/'+scope)):
        refuse('RECONCILE_REQUIRED', 'original create scope is unavailable')
    config = platform.runtime/(record['id']+'.cfg')
    if config.is_symlink() or fingerprint(config.read_text()) != launch['configuration_sha256']:
        refuse('TARGET_CHANGED', 'original create configuration changed')
    # libxl records this at build time, before xl starts the death monitor.
    # A newer incarnation cannot use an older monitor's log as proof.
    started = platform.timed_command(
        ['xenstore-read', '/vm/'+record['uuid']+'/start_time'], deadline).strip()
    if not re.fullmatch(r'[0-9]{1,12}\.[0-9]{2}', started):
        refuse('RECONCILE_REQUIRED', 'Xen domain build time cannot be confirmed')
    start_ns = int(started.replace('.', '')) * 10_000_000
    members = (cgroup_root/group.lstrip('/')/'cgroup.procs').read_text().split()
    log = log_root/('xl-'+record['name']+'.log')
    expected_group = '0::'+group
    proofs = []
    for pid in members:
        if not pid.isdecimal():
            refuse('RECONCILE_REQUIRED', 'invalid create scope process identity')
        process = proc_root/pid
        try:
            before = (process/'stat').read_text().rsplit(')', 1)[1].split()[19]
            argv = (process/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            if len(argv) != 3 or Path(os.fsdecode(argv[0])).name != 'xl' or argv[1:] != [b'create', os.fsencode(config)]:
                continue
            if expected_group not in (process/'cgroup').read_text().splitlines():
                continue
            expected = f"Waiting for domain {record['name']} (domid {row['domid']}) to die [pid {pid}]\n".encode()
            with os.fdopen(os.open(process/'fd/1', os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_nlink != 1 or metadata.st_mtime_ns < start_ns
                        or not os.path.samestat(metadata, log.stat()) or stream.read(len(expected)+1) != expected):
                    continue
            if ((process/'stat').read_text().rsplit(')', 1)[1].split()[19] != before
                    or (process/'cmdline').read_bytes().rstrip(b'\0').split(b'\0') != argv
                    or expected_group not in (process/'cgroup').read_text().splitlines()):
                continue
            proofs.append({'process_scope':scope, 'monitor_pid':int(pid), 'monitor_start_ticks':int(before),
                           'domid':row['domid'], 'xen_unique_id':row['xen_unique_id'],
                           'monitor_log_mtime_ns':metadata.st_mtime_ns, 'xenstore_start_time':started})
        except (FileNotFoundError, ProcessLookupError, IndexError):
            continue
    if platform.clock() >= deadline:
        refuse('DEADLINE_EXCEEDED', 'create monitor confirmation deadline expired')
    if len(proofs) != 1:
        refuse('RECONCILE_REQUIRED', 'current owned monitor does not confirm the original create')
    return proofs[0]


def check_reboot_quiescent(platform, record, launch, deadline):
    """구형 reboot 명령은 재사용 scope 밖에서 실행될 수 있다."""
    from xenova_execution import refuse
    domid = str(launch['reboot_from']['domid']).encode()
    script = os.fsencode(Path(__file__).name)
    vm_id = record['uuid'].encode()
    for path in Path('/proc').iterdir():
        if platform.clock() >= deadline:
            refuse('DEADLINE_EXCEEDED', 'reboot process observation budget expired')
        if not path.name.isdigit():
            continue
        try:
            argv = (path/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        xl_reboot = os.path.basename(argv[0]) == b'xl' and argv[1:3] == [b'reboot', domid]
        helper_reboot = (any(os.path.basename(arg) == script for arg in argv[:4])
                         and b'reboot' in argv and b'--vm-id' in argv and vm_id in argv)
        if xl_reboot or helper_reboot:
            refuse('RECONCILE_REQUIRED', 'original reboot helper may still be active')


def arm_parent_death(expected_parent):
    """fork된 daemon에는 상속되지 않는 Linux PDEATHSIG를 명령 프로세스에만 설정한다."""
    if type(expected_parent) is not int or expected_parent <= 1:
        raise ValueError('invalid command parent')
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGKILL,0,0,0) != 0:
        raise OSError(ctypes.get_errno(),'PR_SET_PDEATHSIG failed')
    if os.getppid()!=expected_parent:
        raise RuntimeError('command caller exited before submission')


def exec_owned(argv, expected_parent, guard):
    """guard의 non-inheritable lock FD는 exec에서 닫힌다. 대기 중에는 잡지 않는다."""
    arm_parent_death(expected_parent)
    with guard():
        if os.getppid()!=expected_parent:
            raise RuntimeError('command caller exited before exec')
        os.execvp(argv[0],argv)


def main(argv=None):
    from xenova_vm import Platform, UUID
    from xenova_execution import short_lock, refuse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=('create','reboot'))
    for name in ('data','runtime','vm-id','host-boot-id'):
        parser.add_argument('--'+name,required=True)
    for name in ('parent-pid','deadline-ms','intent-revision'):
        parser.add_argument('--'+name,required=True,type=int)
    parser.add_argument('--execution-id')
    parser.add_argument('--domid',type=int)
    parser.add_argument('--xen-unique-id')
    args=parser.parse_args(argv)
    if os.geteuid()!=0 or not UUID.fullmatch(args.vm_id) or not UUID.fullmatch(args.host_boot_id):
        parser.error('root and canonical identities required')
    p=Platform(Path(args.data),Path(args.runtime));e=p.execution();deadline=args.deadline_ms/1000
    records=p.records(validate_assets=False)
    record=next((r for r in records.values() if r['uuid']==args.vm_id),None)
    if record is None or e.host!=args.host_boot_id:
        refuse('TARGET_CHANGED','submission identity or host changed')
    launch=e.run_record(record)
    if not launch or launch['execution_id']!=args.execution_id or launch['phase']!='launching':
        refuse('TARGET_CHANGED','launch owner changed before submission')
    if args.kind=='create':
        scope=launch.get('process_scope')
        cgroup=Path('/proc/self/cgroup').read_text()
        if not owned_scope(record['uuid'],scope) or not any(line.endswith('/'+scope) for line in cgroup.splitlines()):
            refuse('RECONCILE_REQUIRED','create is not in the owned process scope')
        config=p.runtime/(record['id']+'.cfg')
        if config.is_symlink() or config.read_text()!=p.config(record):
            refuse('TARGET_CHANGED','launch config changed before exec')
        physical=['xl','create',str(config)]
    else:
        if args.domid is None or args.domid<=0 or not re.fullmatch(r'[1-9][0-9]{0,19}',args.xen_unique_id or ''):
            parser.error('reboot requires observed domain incarnation')
        # Xen 조회는 짧은 의도 잠금 밖이다. 잠금 안에는 저장된 문맥과 최종 exec만 둔다.
        p.resolve_domain(record,expected={'domid':args.domid,'xen_unique_id':args.xen_unique_id},deadline=deadline)
        physical=['xl','reboot',str(args.domid)]
    @contextmanager
    def guard():
        with short_lock(p.runtime,'admission',deadline=deadline,clock=p.clock,sleep=p.sleep):
            intent=e.intent(record)
            if (p.boot_id_reader()!=args.host_boot_id or
                    (intent['intent_revision'] if intent else 0)!=args.intent_revision):
                refuse('INTENT_MISMATCH','running intent changed before command exec')
            e.guard_launch(record,launch,deadline)
            yield
    try:
        exec_owned(physical,args.parent_pid,guard)
    except (RuntimeError,OSError,ValueError):
        # exec가 성공하면 Python은 돌아오지 않는다. helper의 최종 guard 거절도
        # 제출 전 부재가 확인된 경우만 종결하며, 이미 실행한 xl 오류와 혼동하지 않는다.
        if args.kind=='create':
            try:e.abort_unsubmitted_launch(record,launch,deadline)
            except (RuntimeError,OSError,ValueError):pass
        raise


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError) as error:
        print('guest command refused: '+str(error),file=sys.stderr)
        raise SystemExit(1)
