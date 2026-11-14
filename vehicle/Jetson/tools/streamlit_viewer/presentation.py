"""관측 snapshot을 표시용 값으로만 정리한다. 제어·통신·권한 판정은 하지 않는다."""

from dataclasses import dataclass
import json


UNKNOWN = 'UNKNOWN'


@dataclass(frozen=True)
class StatusCard:
    label: str
    current: str
    reported: str


@dataclass(frozen=True)
class TerminalCommand:
    label: str
    command: str
    prerequisite: str
    device: str = 'RPi dom0 터미널'


# 실설치 CLI에서 확인한 복사 전용 목록이다. 명령 실행 기능은 제공하지 않는다.
TERMINAL_COMMANDS = (
    TerminalCommand('주행 상태', 'xenova-drive status', '설치된 Gateway의 현재 보고를 확인합니다.'),
    TerminalCommand('운영 상태', 'xenova-drive operation status', '출발·주차 설정과 현재 준비 상태를 확인합니다.'),
    TerminalCommand('수동 모드', 'xenova-drive mode manual', '모드 접수와 실제 STOP/수동 입력 자격은 각각 확인합니다.'),
    TerminalCommand('수동 중립', 'xenova-drive manual neutral', '현재 수동 세션의 중립 요청이며 실제 정지 피드백을 확인합니다.'),
    TerminalCommand('AUTO 출발', 'xenova-drive drive depart auto', '현재 READY·실제 정지 등 기존 출발 조건이 필요합니다. 거절 시 자동 재시도하지 않습니다.'),
    TerminalCommand('주차 허용 ON', 'xenova-drive parking permission on', '주차 허용 설정만 바꾸며 설정만으로 출발하지 않습니다.'),
    TerminalCommand('주차 허용 OFF', 'xenova-drive parking permission off', '주차 중 해제의 STOP/WAIT 적용 상태는 별도로 확인합니다.'),
    TerminalCommand('STOP', 'xenova-drive stop', '요청 접수와 장치의 실제 정지·APPLIED는 구분합니다.'),
)


def value(item):
    return UNKNOWN if item is None else str(item)


def observation_label(body, result_state):
    """합성/과거 자료를 현재 연결 상태로 승격하지 않는다."""
    kind = body.get('source', {}).get('kind')
    if result_state == 'HISTORICAL' or kind in ('OFFLINE_SYNTHETIC', 'HISTORICAL_OAK_REPRESENTATIVE'):
        return 'HISTORICAL'
    if kind=='LIVE_HIL':return 'LIVE_HIL · '+result_state
    return result_state


def operation_cards(body, *, result_state, source_age_state):
    """기존 current=UNKNOWN/UNVERIFIED와 원 보고값을 분리한 표시 사본이다."""
    operation = body.get('operation') or {}
    eligible = (result_state == 'VALID' and source_age_state == 'MAPPED'
        and body.get('source', {}).get('kind') == 'DIAGNOSTIC')
    live=body.get('live')
    operation_stream=next((item for item in live['streams'] if item['name']=='operation'),None) if live else None
    live_fresh=(result_state=='VALID' and operation_stream is not None and operation_stream.get('state')=='FRESH')
    fields = (('모드', 'mode'), ('A/B active', 'active_vm'), ('Standby', 'standby_vm'),
        ('주차 허용', 'parking_enabled'), ('차량 상태', 'vehicle_phase'),
        ('READY 보고', 'auto_ready'), ('대기 이유', 'blocked_reason'))
    return tuple(StatusCard(label, value(operation.get(key)) if live_fresh else 'UNVERIFIED' if eligible and not live and operation.get(key) is not None else UNKNOWN,
        value(operation.get(key))) for label, key in fields)


def _detail(item):
    if item is None:return UNKNOWN
    if type(item) in (dict,list):return json.dumps(item,ensure_ascii=False,sort_keys=True)[:2048]
    return str(item)[:2048]


def input_stages(body=None, *, result_state='UNAVAILABLE'):
    """관측 단계는 독립이다. 송신/APPLIED에서 이동이나 키 입력을 만들어내지 않는다."""
    body=body or {}
    if body.get('live') is not None:
        stages=(body.get('live') or {}).get('stages') or {}
        result=[dict(단계='키 입력 · WASD/브레이크',관측=UNKNOWN,근거='typed 키 관측 미제공')]
        for key,label in (('command','명령 도착 · 가상 장치 수신'),('applied','가상 장치 APPLIED'),('movement','가상 차량 이동')):
            item=stages.get(key) or {};state=value(item.get('state'))
            if result_state!='VALID' and state!=UNKNOWN:state='STALE · '+state
            result.append(dict(단계=label,관측=state,근거=_detail(item.get('detail'))))
        return tuple(result)
    return (
        dict(단계='키 입력 · WASD/브레이크', 관측=UNKNOWN, 근거='typed 입력 관측 미연결'),
        dict(단계='Gateway 수락', 관측=UNKNOWN, 근거='원 요청 키에 결박된 수락 관측 미연결'),
        dict(단계='장치 COMMAND/APPLIED', 관측=UNKNOWN, 근거='가상/실물 구분·원 적용 보고 미연결'),
        dict(단계='실제 이동 · encoder', 관측=UNKNOWN, 근거='원 encoder/이동 표본 미연결'),
    )


def live_status(body, *, result_state):
    """collector heartbeat와 각 입력의 원 관측을 별도 표시 사본으로 만든다."""
    live=body.get('live')
    if live is None:return None
    connection=live['connection'];rows=[]
    for item in live['streams']:
        state=value(item.get('state'))
        if result_state!='VALID':state='STALE · '+state
        rows.append({'입력':item['name'],'연결 보고':state,'수신 수':value(item.get('received_count')),
            '수집 당시 수신 age(s)':value(item.get('received_age_s')),
            '원 순번':value(item.get('source_sequence')),'최근 오류':_detail(item.get('error'))})
    state=value(connection.get('state'))
    if result_state!='VALID':state='STALE · '+state
    return dict(run_id=value(live.get('run_id')),session_id=value(live.get('session_id')),collector=live['collector'],
        stage=connection['stage'],state=state,stop_reason=value(connection.get('stop_reason')),
        last_error=_detail(connection.get('last_error')),streams=tuple(rows))


def metric_rows(body):
    """정해지지 않은 FPS/단위를 추정하지 않고 원 metric 이름·값만 유한하게 표시한다."""
    metrics = body.get('metrics')
    if type(metrics) is not dict:
        return (), 0
    entries = tuple(metrics.items())
    rows = tuple(dict(원항목=str(key), 원보고=value(item)) for key, item in entries[:24]
        if item is None or type(item) in (str, int, float, bool))
    return rows, len(entries) - len(rows)
