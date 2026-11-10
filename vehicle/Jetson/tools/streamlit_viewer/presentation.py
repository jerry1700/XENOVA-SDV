"""관측 snapshot을 표시용 값으로만 정리한다. 제어·통신·권한 판정은 하지 않는다."""

from dataclasses import dataclass


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
    return result_state


def operation_cards(body, *, result_state, source_age_state):
    """기존 current=UNKNOWN/UNVERIFIED와 원 보고값을 분리한 표시 사본이다."""
    operation = body.get('operation') or {}
    eligible = (result_state == 'VALID' and source_age_state == 'MAPPED'
        and body.get('source', {}).get('kind') == 'DIAGNOSTIC')
    fields = (('모드', 'mode'), ('A/B active', 'active_vm'), ('Standby', 'standby_vm'),
        ('주차 허용', 'parking_enabled'), ('차량 상태', 'vehicle_phase'),
        ('READY 보고', 'auto_ready'), ('대기 이유', 'blocked_reason'))
    return tuple(StatusCard(label, 'UNVERIFIED' if eligible and operation.get(key) is not None else UNKNOWN,
        value(operation.get(key))) for label, key in fields)


def input_stages():
    """snapshot-1에는 단계별 typed 입력이 없다. 원 JSON에서 추측해 채우지 않는다."""
    return (
        dict(단계='키 입력 · WASD/브레이크', 관측=UNKNOWN, 근거='typed 입력 관측 미연결'),
        dict(단계='Gateway 수락', 관측=UNKNOWN, 근거='원 요청 키에 결박된 수락 관측 미연결'),
        dict(단계='장치 COMMAND/APPLIED', 관측=UNKNOWN, 근거='가상/실물 구분·원 적용 보고 미연결'),
        dict(단계='실제 이동 · encoder', 관측=UNKNOWN, 근거='원 encoder/이동 표본 미연결'),
    )


def metric_rows(body):
    """정해지지 않은 FPS/단위를 추정하지 않고 원 metric 이름·값만 유한하게 표시한다."""
    metrics = body.get('metrics')
    if type(metrics) is not dict:
        return (), 0
    entries = tuple(metrics.items())
    rows = tuple(dict(원항목=str(key), 원보고=value(item)) for key, item in entries[:24]
        if item is None or type(item) in (str, int, float, bool))
    return rows, len(entries) - len(rows)
