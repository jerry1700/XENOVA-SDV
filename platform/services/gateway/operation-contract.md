# 터미널 운영 계약

`operation_support: true`는 승인된 설치 profile에서만 켠다. 기본값은 false이며
`episode_support: true`와 같은 B09 공통/C/Gateway 쌍이 필요하다. 기능 광고만으로
운영 권위를 발급하지 않는다. 기존 operator.v2 요청·fingerprint와 `status` 응답 모양은 유지한다.
새 서비스는 주차 허용 OFF에서 시작하며 원 STOP과 현재 정지 확인 전에는 STOPPING이다.
같은 서비스의 모드 전환·A/B 승계는 허용 설정과 원 주차 예산을 유지한다.

## 터미널 명령

```text
xenova-drive operation status
xenova-drive parking permission on
xenova-drive parking permission off
xenova-drive drive depart auto
xenova-drive drive depart manual
xenova-drive mode manual
xenova-drive stop
xenova-drive manual keyboard --auto-enter
```

`permission on`은 설정만 바꾸며 WAIT에서 차량을 출발시키지 않는다. 새 profile에서
기존 `auto resume`, `parking start/cancel`은 `OPERATION_ACTION_REQUIRED`로 거절한다.
`mode manual`은 기존 명시 MANUAL 전환·재무장 의미를 유지하며 그 원 key를 수동 operation의
출처로 기록한다. `manual keyboard`의 기본값은 기존 수동 전용이고 `--auto-enter`만
[별도 AUTO Enter 규약](keyboard-auto-enter.md)을 선택한다.

### 차가 꺼져 있던 상태에서 한 번 AUTO 출발하기

1. 기존 설치·Authority 절차로 A 담당/B 대기와 현재 VM/app identity를 준비한다.
   Gateway는 SAFE_STOP의 숨은 preference를 출력 owner로 공개하지 않는다.
   최초 설치의 실제 옵션은 `--vm-a <A_UUID> --vm-b <B_UUID> --initial-active-vm <A_UUID>`이며
   `--initial-active-vm`은 명시 최초 초기화에서만 유효하다. 기존 Authority가 있는 재기동은
   초기화를 반복하지 않는다. 현재 preference가 B라면 기존 G1 handoff를 먼저 완료해야 하며
   아래 AUTO 출발 명령이 owner를 A로 몰래 바꾸지는 않는다.
2. `status`와 `operation status`로 실제 STOPPING→WAIT를 확인한다.
   SAFE_STOP/owner 없음인 상태에서 `drive depart auto`는 거절된다.
3. `mode manual`로 기존 선택 owner의 실제 MANUAL 준비·새 control/app apply·재무장을 완료한다.
   키보드 HELD를 쓸 때는 장치 ATTACH·실제 정지·새 ARM과 WASD 해제를 완료한다.
4. `operation status`에서 현재 generation의 실제 AUTO 입력 준비가 READY인지 확인한다.
   READY는 새 mission/path가 아직 없어도 현재 World/Vehicle/pose/clock/Scene 검사로 만들 수 있다.
5. 새 `drive depart auto` 또는 READY 안내 뒤 새 Enter 한 번으로 출발을 요청한다.
   현재 MANUAL의 실제 정지·중립이 확인되면 추가 WAIT 명령은 필요하지 않다.
6. 같은 actor 회차에서 수동 추진 gate를 철회하고 STOP을 제출한다. 원 요청 기한 안에서
   기존 countdown을 진행하며 조건 상실은 취소한다. 이후 새 grant·완전 State·actual apply·
   더 새로운 계산이 각각 검산되어야 실제 출력이 가능하다.

준비 전 Enter/CLI 요청은 나중 준비를 기다리는 예약이 아니다. 거절된 원 key는 종결 기록이며
같은 key가 READY 뒤에 출발 요청으로 바뀌지 않는다. CLI는 CAS 실패를 새 key로 자동 재시도하지 않는다.
키보드의 원 이벤트 E는 접수 때만 사용하고, 수락 후 countdown은 원 operator 기한과 현재
observer/source/해제 상태를 검사한다. 길게 누른 Enter나 key repeat가 새 출발이 되지 않는다.

## 가산된 operator.v2 action

기존 envelope/UDS/권한·8192B cap·전역 g1 key 규칙을 사용한다.

| action | exact args |
|---|---|
| `parking.permission.set` | `enabled: bool`, `expected_permission_revision: decimal-u63` |
| `drive.depart` | `mode: AUTO\|MANUAL`, `expected_operation_revision`, `expected_permission_revision`, `ready_revision`: decimal-u63, `trigger_sha256`: lowercase SHA256 또는 null |
| `operation.status` | envelope는 `version:2, action` 두 필드만 |

null trigger는 명시 CLI 조작이다. 비-null digest는 인증된 KeyboardActor의 원 이벤트와 일치해야 한다.
digest만 operator socket에 적어 넣어 키보드 검사를 대신할 수 없다. 같은 CAS 값의 같은 설정은
`NO_CHANGE`이며 permission revision을 올리지 않는다. 원 key 조회는 현재성 검사보다 먼저다.

새 조회 data의 `contract`는 `operation-status-1`이다. host/Gateway/generation/원 Pi 시각,
binding(service epoch·operation revision·departure nonce·permission revision·enabled), vehicle phase,
READY revision/여부·차단 사유·원 E, active request key/countdown end/desired mode를 제공한다.
STOPPING/WAIT/COUNTDOWN/APPLYING/ACTIVE는 앱 mode·실제 바퀴 이동·주차 결과와 별개다.
READY·Enter 수락·설정 저장은 output permission이나 Device APPLIED가 아니다.

## 입력·출력과 수명

- C의 `ClockProbe.auto_input_preflight`는 비권위 입력 검사다. Gateway는 원 confirmed P4 seq/SHA,
  현재 source/generation/Vehicle·조향 provenance·trust·Scene와 checked lower+100ms의 원 E를 대조한다.
  정상 Vehicle/World 순번만 바뀌면 READY revision을 매 프레임 증가시키지 않는다.
- READY status는 같은 원 Probe의 report에 결박한다. I/O 대기로 끝난 READY는 송신 때 status만
  생략하며 숫자 clock·원 Probe/P4/lease E는 유지한다. 재전송은 원 Reply bytes 그대로다.
- 새 operation binding은 admitted current grant와 State feature32에 함께 있어야 한다. 원 grant가
  아직 publish되지 않은 창에 옛 admitted grant와 새 status를 섞어 보내지 않는다.
- full State 원문은 기존 copy4가 소유한다. 운영 owner는 현재 사본 ID/SHA·plan/phase·계산 번호와
  작은 binding proof만 보관한다. 새 State 뒤의 actual calculation과 최종 출력 시 현재 권위를 재검사한다.
- 서비스 재시작은 새 epoch의 OFF/WAIT다. 옛 operation/예약 reply는 감사·원 key 조회 자료이며
  현재 epoch의 새 권한으로 다시 포장하지 않는다. EXIT/RETURN_START는 후속 구현이다.

## 주차 OFF와 원 예산

SEARCH에서 OFF는 같은 operation의 FOLLOW projection으로 돌아간다. ON 재개는 원 attempt를
현재 grant에 다시 결박한다. 같은 mission ID/revision을 유지하고 grant revision만 전진시켜
Episode의 원 40s/6m를 새 NONE으로 초기화하지 않는다. 새 kind/설정에는 actual 새 plan/State/계산이 필요하다.

첫 SEARCH는 실제 accepted FOLLOW 사본과 그 후의 actual command가 있어야 생성한다.
원 accepted-copy SHA/계산·clock completed lower·정확한 Vehicle meter anchor를 시작 사건으로
남긴다. 수신 시각이나 오래된 ON key·가짜 g1 key로 시작하지 않는다. 120s/SEARCH90s·10m,
execution30s·5m·correction2와 원 exact reply는 OFF 중에도 보존되며 시각·거리 소비가 계속된다.
budget이 소진된 attempt를 토글/rebind로 새로 만들지 않는다.

EVAL_STOP·목표 잠금 이후의 OFF는 원 attempt를 CANCELLED로 철회하고 RAM STOP을 journal보다
먼저 제출한다. 실제 정지가 확인되어야 WAIT다. PARKED와 CANCELLED를 구분한다. 완료·중단 뒤
ON만으로 재출발하지 않는다. 칸 안/실행 중단 pose의 AUTO는 EXIT-ready가 생기기 전까지
ROUTE_NOT_READY로 거절한다. 후속 EXIT/RETURN 임무를 이 OFF 분기가 재취소하지 않는다.

Episode 등록 ACK 뒤 fixed 첫 full State 전의 일반 설정 변경은 BUSY로 거절한다. 수행중 OFF의
즉시 STOP은 유지한다. 같은 fixed State ID에 새 binding bytes를 덮어쓰는 동작은 허용하지 않는다.

current_operation은 Authority 안의 단일 6144B partition이다. attempt·exact reply는 4096B,
전체 Authority는 65536B를 유지한다. cap 초과는 HISTORY_FULL이고 원 nonce·예산을 소비하지 않는다.
고빈도 fsync·별도 DB/daemon·새 의존성은 없다. Mapper의 Pi operation gate도 원 STOP/Vehicle/E를
참조하는 현재 사실이며, absent/expired gate는 Jetson STANDBY 허가가 아니다.
