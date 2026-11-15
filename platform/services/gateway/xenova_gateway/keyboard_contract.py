"""15장 keyboard-source-1 exact packet. K는 MQTT/Authority/serial 권위를 소유하지 않는다."""
from .ipc_contract import decode as json_decode,uuid,integer
from .operator_contract import depth,decimal
from .authority_journal import canonical

ENDPOINT='/run/xenova-gateway/keyboard.sock'
JETSON_RETURN_PENDING='JETSON_RETURN_PENDING'
FIELDS={'version','kind','host_boot_id','gateway_instance_id','source_instance','channel_epoch',
    'sequence','request_id','issued_pi_us','expires_pi_us','payload'}
PAYLOADS={
    'HELLO':{'device_profile','device_identity_sha256','kernel_abi_sha256'},
    'ATTACH':{'expected_generation','expected_owner','device_instance','link_epoch'},
    'GRANT':{'driver_session','input_epoch','grant_revision','generation','profile_sha256','state','reason'},
    'FRAME':{'driver_session','input_epoch','grant_revision','device_instance','link_epoch','source_sample_seq',
        'key_event_seq','t1_pi_us','t2_pi_us','key_mask','last_event_pi_us'},
    'ARM':{'driver_session','input_epoch','grant_revision','source_sample_seq','arm_event_seq'},
    'FENCE':{'driver_session','input_epoch','grant_revision','fence_revision','key_event_seq','reason_mask','reason'},
    'FENCE_ACK':{'driver_session','input_epoch','fence_revision','accepted_pi_us'},
    'STOP_READY':{'driver_session','input_epoch','fence_revision','stop_command_sequence','stationary_sample_sequence'},
    'STATE':{'driver_session','input_epoch','grant_revision','generation','state','reason','reply_to_kind',
        'reply_to_sequence','accepted','accepted_state_seq'},
    'DETACH':{'driver_session','input_epoch','final_fence_revision','reason'}}
AUTO_FRAME={'device_instance','link_epoch','source_sample_seq','key_event_seq','t1_pi_us','t2_pi_us',
    'key_mask','last_event_pi_us'}
AUTO_PAYLOADS={
    'HELLO_AUTO':PAYLOADS['HELLO']|{'device_instance','link_epoch'},
    'OBSERVE':AUTO_FRAME,
    'AUTO_ENTER':AUTO_FRAME|{'enter_event_seq','operation_revision','permission_revision','ready_revision',
        'generation','expected_owner','operator_request_id'},
    'AUTO_READY':{'device_instance','link_epoch','generation','owner','mode','operation_revision',
        'permission_revision','parking_enabled','ready_revision','auto_ready','vehicle_phase','blocked_reason',
        'valid_until_pi_us','reply_to_kind','reply_to_sequence','supported','active_request_id','desired_mode',
        'manual_attach_available'},
    'AUTO_RESULT':{'operator_request_id','trigger_sha256','accepted','terminal','phase','result','reply_to_sequence'}}
AUTO_KINDS=frozenset(AUTO_PAYLOADS)
REASONS=frozenset(('OK','HELLO_ACCEPTED','ATTACHED','ARMED','DRIVER_BUSY','WRONG_MODE','STOP_REQUIRED',
    'NEUTRAL_REQUIRED','INPUT_EXPIRED','STALE_SAMPLE','SOURCE_MISMATCH','UNSUPPORTED_DEVICE','UNVERIFIED_RADIO',
    'PROTOCOL_ERROR','KEY_CONFLICT','MODIFIER','INPUT_BACKLOG','SYN_DROPPED','DEVICE_LOST','LINK_LOST',
    'GLOBAL_STOP','DETACHED','REVOKED','UNCONFIRMED','UNSUPPORTED_PROFILE','NOT_READY','AUTO_DEPARTING'))
UUID_FIELDS={'driver_session','input_epoch','device_instance','link_epoch','expected_owner'}
INTEGER_FIELDS={'generation','expected_generation','key_mask','reason_mask'}
COUNTERS={'grant_revision','source_sample_seq','key_event_seq','t1_pi_us','t2_pi_us','last_event_pi_us',
    'arm_event_seq','fence_revision','accepted_pi_us','stop_command_sequence','stationary_sample_sequence',
    'reply_to_sequence','accepted_state_seq','final_fence_revision'}


def decode(raw):
    if len(raw)>2048:raise ValueError('keyboard packet cap')
    value=json_decode(raw);depth(value,6);return validate(value)


def validate(value):
    if type(value) is not dict or set(value)!=FIELDS:raise ValueError('keyboard exact envelope')
    auto=value['kind'] in AUTO_KINDS
    payloads=AUTO_PAYLOADS if auto else PAYLOADS
    if (type(value['version']) is not int or value['version']!=(2 if auto else 1)
            or value['kind'] not in payloads or type(value['payload']) is not dict
            or set(value['payload'])!=payloads[value['kind']]):raise ValueError('keyboard exact envelope')
    for name in ('source_instance','channel_epoch','request_id'):uuid(value[name])
    for name in ('host_boot_id','gateway_instance_id'):
        if value[name] is not None:uuid(value[name])
        elif value['kind']!='HELLO':raise ValueError('keyboard identity required')
    for name in ('sequence','issued_pi_us','expires_pi_us'):
        if value[name] is None and value['kind']=='HELLO' and name!='sequence':continue
        number=decimal(value[name]);integer(number,minimum=1 if name=='sequence' else 0)
        if auto and number>=2**63:raise ValueError('keyboard AUTO u63 envelope')
    if value['issued_pi_us'] is not None and not int(value['issued_pi_us'])<int(value['expires_pi_us']):
        raise ValueError('keyboard original expiry')
    if auto:
        _validate_auto(value)
        if len(canonical(value))>2048:raise ValueError('keyboard packet cap')
        return value
    for name,item in value['payload'].items():
        if name in UUID_FIELDS:
            if item is not None:uuid(item)
            elif value['kind'] not in ('STATE',):raise ValueError('keyboard source UUID required')
        elif name in INTEGER_FIELDS:
            integer(item,maximum=255 if name=='key_mask' else 15 if name=='reason_mask' else 2**32-1)
        elif name in COUNTERS:
            if item is None and name in ('last_event_pi_us','reply_to_sequence','accepted_state_seq'):continue
            integer(decimal(item))
        elif name.endswith('sha256'):
            if type(item) is not str or len(item)!=64 or any(c not in '0123456789abcdef' for c in item):raise ValueError('keyboard SHA256')
        elif name=='reason':
            if item not in REASONS:raise ValueError('keyboard registered reason')
        elif name=='accepted':
            if item is not None and type(item) is not bool:raise ValueError('keyboard accepted flag')
        elif item is not None and (type(item) is not str or not 1<=len(item)<=64 or not item.isascii()):
            raise ValueError('keyboard bounded identifier')
    if len(canonical(value))>2048:raise ValueError('keyboard packet cap')
    return value


def _validate_auto(value):
    """협상된 새 kind만 검사한다. 기존 v1 FRAME/ARM의 의미는 확장하지 않는다."""
    from .ipc_contract import request_key
    counters={'source_sample_seq','key_event_seq','enter_event_seq','t1_pi_us','t2_pi_us','last_event_pi_us',
        'operation_revision','permission_revision','ready_revision','valid_until_pi_us','reply_to_sequence'}
    for name,item in value['payload'].items():
        if name in ('device_instance','link_epoch','owner','expected_owner'):
            if item is None and name in ('owner','expected_owner'):continue
            uuid(item)
        elif name in counters:
            if item is None and name in ('last_event_pi_us','reply_to_sequence'):continue
            integer(decimal(item),maximum=2**63-1)
        elif name=='generation':integer(item,maximum=2**32-1)
        elif name=='key_mask':integer(item,maximum=255)
        elif name in ('parking_enabled','auto_ready','supported','accepted','terminal','manual_attach_available'):
            if type(item) is not bool:raise ValueError('keyboard AUTO boolean')
        elif name=='operator_request_id':
            if request_key(item) is None:raise ValueError('host-bound AUTO request required')
        elif name=='active_request_id':
            if item is not None and request_key(item) is None:raise ValueError('host-bound active request required')
        elif name.endswith('sha256'):
            if type(item) is not str or len(item)!=64 or any(c not in '0123456789abcdef' for c in item):
                raise ValueError('keyboard AUTO SHA256')
        elif item is not None and (type(item) is not str or not 1<=len(item)<=64 or not item.isascii()):
            raise ValueError('keyboard AUTO bounded identifier')
    p=value['payload']
    if value['kind']=='AUTO_READY':
        if (p['vehicle_phase'] not in ('STOPPING','WAIT','COUNTDOWN','APPLYING','ACTIVE')
                or p['mode'] not in ('SAFE_STOP','MANUAL','AUTO_READY','AUTO')
                or p['desired_mode'] not in ('NONE','AUTO','MANUAL')
                or p['reply_to_kind'] not in (None,'HELLO_AUTO','OBSERVE')
                or (p['reply_to_kind'] is None)!=(p['reply_to_sequence'] is None)):
            raise ValueError('keyboard AUTO readiness domain')
    if value['kind']=='AUTO_ENTER':
        host,issued=request_key(p['operator_request_id'])
        if (host!=value['host_boot_id'] or p['last_event_pi_us'] is None
                or issued!=int(p['last_event_pi_us'])//1000
                or p['enter_event_seq']!=p['key_event_seq']):raise ValueError('keyboard original AUTO event key')
