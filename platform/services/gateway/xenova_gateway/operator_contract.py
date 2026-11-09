"""부록B/15 operator.v2 exact JSON. G1 management 문법과 독립인 로컬 계약."""
from hashlib import sha256
from .ipc_contract import decode as json_decode,uuid,request_key,integer
from .authority_journal import canonical

VERSION=2
ENDPOINT='/run/xenova-gateway/operator.sock'
MAX_BYTES=8192
SAFETY=frozenset(('stop','parking.cancel','scene.revoke','mode.manual'))
VOLATILE=frozenset(('manual.nudge','manual.neutral'))
ACTIONS=frozenset(('mode.manual','auto.resume','auto.cancel','parking.start','parking.cancel','stop',
    'manual.nudge','manual.neutral','scene.survey.begin','scene.survey.commit','scene.revoke',
    'parking.permission.set','drive.depart','home.set','home.return'))


class OperatorError(ValueError):
    def __init__(self,code,outcome='NOT_ACCEPTED'):
        super().__init__(code);self.code=code;self.outcome=outcome


def depth(value,limit=8):
    if limit<0:raise OperatorError('INVALID_FRAME')
    if isinstance(value,dict):
        for child in value.values():depth(child,limit-1)
    elif isinstance(value,list):
        for child in value:depth(child,limit-1)


def decode(raw):
    value=json_decode(raw);depth(value);return request(value)


def request(value):
    if type(value) is not dict or type(value.get('version')) is not int or value['version']!=2:
        raise OperatorError('INVALID_INPUT')
    action=value.get('action')
    if action in ('status','operation.status','home.status'):
        if set(value)!={'version','action'}:raise OperatorError('INVALID_INPUT')
        return value
    if action=='request.get':
        if set(value)!={'version','action','request_id'}:raise OperatorError('INVALID_INPUT')
        if request_key(value['request_id']) is None:raise OperatorError('INVALID_INPUT')
        return value
    fields={'version','action','request_id','host_boot_id','gateway_instance_id',
        'expected_generation','expected_owner','deadline_pi_ms','args'}
    if set(value)!=fields or action not in ACTIONS:raise OperatorError('INVALID_INPUT')
    host,issued=request_key(value['request_id']) or (None,None)
    if host is None:raise OperatorError('INVALID_INPUT')
    uuid(value['host_boot_id']);uuid(value['gateway_instance_id'])
    integer(value['expected_generation'],maximum=2**32-1)
    if value['expected_owner'] is not None:uuid(value['expected_owner'])
    elif action not in ('mode.manual','parking.permission.set','home.set','home.return'):raise OperatorError('INVALID_INPUT')
    integer(value['deadline_pi_ms'],minimum=1,maximum=2**53-1)
    if not issued<value['deadline_pi_ms']<=issued+10000:raise OperatorError('INVALID_INPUT')
    args=value['args']
    if type(args) is not dict:raise OperatorError('INVALID_INPUT')
    if action in ('home.set','home.return'):
        fields={'expected_goal_revision','expected_goal_sha256','expected_operation_revision','expected_permission_revision'}
        if action=='home.return':fields.add('ready_revision')
        if set(args)!=fields:raise OperatorError('INVALID_INPUT')
        for name in fields-{'expected_goal_sha256'}:decimal63(args[name])
        digest=args['expected_goal_sha256']
        if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise OperatorError('INVALID_INPUT')
        if (int(args['expected_goal_revision'])==0)!=(digest=='0'*64):raise OperatorError('INVALID_INPUT')
    elif action=='parking.permission.set':
        if set(args)!={'enabled','expected_permission_revision'} or type(args['enabled']) is not bool:
            raise OperatorError('INVALID_INPUT')
        decimal63(args['expected_permission_revision'])
    elif action=='drive.depart':
        if (set(args)!={'mode','expected_operation_revision','expected_permission_revision','ready_revision','trigger_sha256'}
                or args['mode'] not in ('AUTO','MANUAL')):raise OperatorError('INVALID_INPUT')
        for name in ('expected_operation_revision','expected_permission_revision','ready_revision'):decimal63(args[name])
        digest=args['trigger_sha256']
        if digest is not None and (type(digest) is not str or len(digest)!=64
                or any(c not in '0123456789abcdef' for c in digest)):raise OperatorError('INVALID_INPUT')
    elif action=='parking.start':
        if set(args)!={'strategy'} or args['strategy'] not in ('REVERSE','FORWARD'):raise OperatorError('INVALID_INPUT')
    elif action=='manual.nudge':
        if (set(args)!={'driver_session','expected_action_seq','direction','steer','duration_ms'}
                or args['direction'] not in ('FORWARD','REVERSE') or args['steer'] not in ('LEFT','CENTER','RIGHT')):
            raise OperatorError('INVALID_INPUT')
        uuid(args['driver_session']);decimal(args['expected_action_seq']);integer(args['duration_ms'],minimum=1,maximum=200)
    elif action=='manual.neutral':
        if set(args)!={'driver_session'}:raise OperatorError('INVALID_INPUT')
        uuid(args['driver_session'])
    elif action=='scene.survey.begin':
        if (set(args)!={'policy','allow_mapper_restore'} or args['policy']!='STATIC_FLAT_SURVEY_1'
                or type(args['allow_mapper_restore']) is not bool):raise OperatorError('INVALID_INPUT')
    elif action in ('scene.survey.commit','scene.revoke'):
        field='survey_id' if action.endswith('commit') else 'scene_id'
        if set(args)!={field}:raise OperatorError('INVALID_INPUT')
        uuid(args[field])
    elif args:raise OperatorError('INVALID_INPUT')
    if len(canonical(value))>MAX_BYTES:raise OperatorError('INVALID_INPUT')
    return value


def decimal(value):
    if type(value) is not str or not value.isascii() or not value.isdigit() or str(int(value))!=value:
        raise OperatorError('INVALID_INPUT')
    integer(int(value),maximum=2**64-1);return int(value)


def decimal63(value):
    number=decimal(value)
    if number>2**63-1:raise OperatorError('INVALID_INPUT')
    return number


def fingerprint(call):return sha256(canonical({'domain':'operator.v2','request':call})).hexdigest()


def error(code,key=None,outcome='NOT_ACCEPTED'):
    return dict(version=2,ok=False,error=dict(code=code,request_id=key,outcome=outcome,
        retryable=code in ('UNAVAILABLE','BUSY_NOT_ACCEPTED','NOT_READY')))


def validate_store(value):
    rows=value.get('operator_ledger')
    if rows is None:return
    if type(rows) is not list or len(rows)>16:raise ValueError('operator ledger capacity')
    uuid(value['operator_host_boot_id']);integer(value['operator_floor_ms']);integer(value['operator_startup_ms'])
    seen={r['request_id'] for name in ('transitions','protections') for r in value.get(name,[])}
    for row in rows:
        fields={'key','domain','fingerprint','request','target','phase','result','terminal','original','retain_until_ms'}
        if (set(row)!=fields or row['domain']!='operator.v2' or row['key'] in seen
                or row['key']!=row['request']['request_id'] or fingerprint(row['request'])!=row['fingerprint']
                or len(canonical(row))>1536 or type(row['terminal']) is not bool):raise ValueError('operator record')
        request(row['request']);seen.add(row['key']);integer(row['retain_until_ms'])
    if len(canonical(value.get('current_mission')))>4096:raise ValueError('mission record capacity')
    from .mission_authority import validate_record
    validate_record(value.get('current_mission'))
    from .operation_owner import validate_operation
    validate_operation(value.get('current_operation'))


def conflict(store,key):
    return key in getattr(store,'operator_reservations',{}) or any(row['key']==key for row in store.value.get('operator_ledger',[]))


def reserved_bytes(value):
    if 'operator_ledger' not in value:return 0
    return ((16-len(value['operator_ledger']))*1536
        +sum(4096-len(canonical(value.get(k))) for k in ('current_mission','current_scene'))
        +(6144-len(canonical(value['current_operation'])) if 'current_operation' in value else 0))


def capacity(value):
    """B5 reserved partitions, including currently unused terminal slots."""
    if 'operator_ledger' not in value:return len(canonical(value))<=65536
    rows=value['operator_ledger']
    if len(rows)>16 or any(len(canonical(row))>1536 for row in rows):return False
    reserved=reserved_bytes(value)
    for key in ('current_mission','current_scene'):
        size=len(canonical(value.get(key)))
        if size>4096:return False
    if 'current_operation' in value and len(canonical(value['current_operation']))>6144:return False
    return len(canonical(value))+reserved<=65536
