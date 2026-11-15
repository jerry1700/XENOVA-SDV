# 키보드 AUTO 출발 계약

B09 A3 구현. 기존 `keyboard-source-1` v1의 HELLO/ATTACH/FRAME/ARM/FENCE/STOP_READY/STATE/DETACH 모양과 의미는 유지한다. 설치된 `operation_support` 승인과 기존 장치/RF 수락 profile을 모두 만족한 같은 키보드·evdev reader·SOCK_SEQPACKET 연결에서만 새 v2 kind를 협상한다. 이 문서는 구현 계약이며 동적·실물 시험 결과가 아니다.

## 조작 흐름

`python -m xenova_gateway.keyboard_source --auto-enter`가 명시 opt-in이다. 옵션이 없으면 기존 MANUAL 전용 v1이다. source가 flag를 보내도 중앙 설치 설정을 켤 수는 없다. 새 profile을 지원하지 않으면 HELLO_AUTO를 거절한다.

- 현재 WAIT이면 source는 추진 SID 없는 observer로 시작한다. 실제 AUTO 입력 preflight가 준비되면 AUTO_READY를 안내한다. 모든 조작 키를 놓은 두 원 관측 뒤 Enter를 **새로** 눌러 AUTO 출발을 요청한다.
- 현재 정상 MANUAL이면 기존 ATTACH→실제 STOP/기존 새 GRANT→새 Enter ARM으로 조종한다. arm된 상태에서 WASD 해제·neutral·실제 STOP과 AUTO 준비를 갖춘 뒤의 **다음** 새 Enter는 AUTO 요청이다. 움직이는 중 누른 Enter가 기존 SID를 폐기하거나 재무장하지 않는다.
- 주행 중 양쪽 AUTO의 Jetson World 입력이 만료되어 같은 VM의 MANUAL로 내려간 경우, 키보드에 `JETSON_RETURN_PENDING`이 안내된다. 현재 키보드의 중립 관측과 실제 정지, 새 World·Vehicle·clock 준비가 맞으면 기존 3초 AUTO 카운트다운을 시작한다. 카운트다운 중에도 키보드와 입력 준비가 유지되어야 한다. AUTO 적용 전에 새 Enter를 누르면 기존 `mode.manual` 경로로 수동 유지 의사를 반영한다. 수동 키보드의 추진 재무장은 기존 절차를 따른다.
- Jetson 복귀 절차 중 확인한 키보드 연결이 끊기거나 키 관측 기한이 만료되면 즉시 출력을 차단하고 `SAFE_STOP`으로 전환한다. 재연결 뒤에는 운영자가 MANUAL을 명시적으로 선택하고 키보드를 다시 무장한다.
- 준비 전/만료된 안내에서 누른 Enter는 소비한다. 반복 value2, 중복 down, READY를 기다리며 계속 누르고 있는 Enter는 나중 요청으로 바뀌지 않는다. ARM과 AUTO_ENTER를 한 이벤트로 함께 만들지 않는다.
- 중앙 수락 뒤 COUNTDOWN/APPLYING/ACTIVE의 원 request ID를 확인하면 같은 source는 observer로 남는다. 원 MANUAL FRAME은 AUTO 입력으로 재해석하지 않으며 `AUTO_DEPARTING` 거절로만 전송 credit을 정리한다. HELD 철회·WAIT/countdown·새 grant/fullState/실제 apply/fresh calculation의 출력 gate는 중앙의 책임이다.
- 명시 MANUAL 출발이 실제 MANUAL 제어로 수락되면 같은 source가 기존 ATTACH부터 다시 진행한다. 자동으로 ARM하지 않는다. AUTO→MANUAL의 원 STOP·새 SID/입력 epoch·새 Enter ARM 조건은 유지한다. 정상 MANUAL same-SID CONTINUE는 기존 경로로 원 입력/E/keyup fence를 보존한다.

키보드 준비 안내는 현재 입력·geometry preflight 진술이다. 새 임무/path/APPLIED를 Enter 전에 요구하지 않는다. READY, source packet 접수, SUBMITTED 응답은 모터 출력 허가가 아니다.

## exact local packet

소켓과 봉투 필드 집합은 기존과 같다. `version`은 **새 kind에만2**, 원 kind에는1이다. source/channel/device/link UUID와 Pi host monotonic 원 시각을 쓴다. 새 숫자 문자열은 canonical decimal-u63, generation은 기존 uint32, key mask는 기존8bit다. 최대2048B·JSON 깊이6을 유지한다. `AUTO_FRAME`은 아래8필드의 이름이다.

`device_instance, link_epoch, source_sample_seq, key_event_seq, t1_pi_us, t2_pi_us, key_mask, last_event_pi_us`

| 새 kind | payload의 exact 필드 |
|---|---|
| HELLO_AUTO | 기존 HELLO의 device_profile/device_identity_sha256/kernel_abi_sha256 + device_instance/link_epoch |
| OBSERVE | AUTO_FRAME. SID나 수동 추진 권한이 없는 현재 입력 관측 |
| AUTO_ENTER | AUTO_FRAME + enter_event_seq/operation_revision/permission_revision/ready_revision/generation/expected_owner/operator_request_id |
| AUTO_READY | device_instance/link_epoch/generation/owner/mode/operation_revision/permission_revision/parking_enabled/ready_revision/auto_ready/vehicle_phase/blocked_reason/valid_until_pi_us/active_request_id/desired_mode/manual_attach_available/supported/reply_to_kind/reply_to_sequence |
| AUTO_RESULT | operator_request_id/trigger_sha256/accepted/terminal/phase/result/reply_to_sequence |

AUTO_READY의 mode는 실제 `SAFE_STOP|MANUAL|AUTO_READY|AUTO`, phase는 `STOPPING|WAIT|COUNTDOWN|APPLYING|ACTIVE`, desired_mode는 `NONE|AUTO|MANUAL`이다. owner는 SAFE_STOP에서 null일 수 있고 blocked_reason/active_request_id/result도 해당 상태에서 null일 수 있다. `manual_attach_available`은 현재 실제 mode MANUAL이며 operator hold가 없다는 UI 문맥일 뿐 기존 ATTACH의 STOP·현재 owner/generation/apply 검사를 대신하지 않는다. 정기 안내는 reply_to_kind/sequence=null, HELLO_AUTO/OBSERVE의 원 응답은 두 원 값을 갖는다.

필드 집합과 타입별 최대 길이로 구성한 stdlib JSON shape 상한은 HELLO_AUTO804B, OBSERVE740B, AUTO_ENTER1114B, AUTO_READY1222B, AUTO_RESULT869B다. g1 key는 실제 허용 길이보다 작게 계산하지 않도록100문자로 보수 계산했다. 이는 앱/codec 실행 결과가 아닌 정적 상한이다. 실제 최대 벡터 codec 회귀는 작성했으며 Nano 통합 묶음에서 실행해야 한다.

AUTO_ENTER의 enter_event_seq는 key_event_seq와 같고 원 operator key는 `g1:<host>:<keydown_pi_us//1000>:<원 nonce>`다. actor는 정확한 원 frame/sample/event/device/link와 envelope t1/E를 검사한다. 두 neutral 표본 뒤 원 event HWM이 정확히1 증가한 Enter down만 후보다. actor의 최초 READY 안내보다 먼저 발생한 이벤트도 거절한다. 준비 revision은 원 상태의 revision이며 새 정상 Vehicle/World 표본마다 임의로 증가시키지 않는다.

## 중앙 연결·원 수명

내부 호출은 다음 하나다.

```python
OperatorService.submit_auto_depart(call, *, trigger_sha256, valid_until_pi_us, current)
```

반환은 기존 operator 응답 모양의 `queue.Queue(maxsize=1)`다. 현재 상태는 `OperatorService.operation_status()`의 별도 `operation-status-1` view를 읽는다. 기존 status v2 exact 형태와 operator.v2 fingerprint/domain은 바꾸지 않는다.

`trigger_sha256 = SHA256(canonical({"domain":"keyboard.auto-enter.v2","packet":<원 v2 packet 전체>}))`다. 중앙 call은 `drive.depart`와 원 g1 key, 원 mode/owner/generation, 원 operation/permission/ready revisions와 이 digest를 사용한다. `deadline_pi_ms=원 keydown 발급ms+10000`으로 고정한다. 일반 operator socket에서 digest 문자열만 자칭하여 typed K 입력이 되는 것은 중앙이 거절한다.

원 입력 접수 기한은 frame t1+150ms이며 재전송은 원 bytes/E다. actor는 즉시 `AUTO_RESULT accepted=true, terminal=false, phase=SUBMITTED, result=PENDING`을 보낼 수 있다. 이는 중앙 호출 인계 접수이며 내구 수락이나 이동 허가가 아니다. 그 뒤에도 source가 원 FRAME/OBSERVE를 계속 보내므로 journal 대기가 입력을 멈추지 않는다. 중앙 Queue1 결과를 같은 actor가 가져와 원 key/digest에 결박한 비동기 결과로 보낸다. 거절·종료 결과를 새 READY에서 다시 출발시키지 않는다.

접수 뒤3초 countdown에150ms event E를 새로 붙이거나 계속 요구하지 않는다. 중앙의 원 operator deadline과 현재 preflight/의미 문맥, 같은 source/channel/device/link의 새 원 관측을 검사한다. callback의 관측 freshness는150ms다. 원 Enter HWM/64 mask 유지 또는 그 Enter의 단 한 번 release(HWM+1/0 mask)만 허용한다. 다른 조작 키의 짧은 down+up도 HWM을2 올려 취소하므로 bitmap만0이라는 이유로 숨기지 않는다. source/연결 손실이나 기존 release/fault fence도 callback을 무효화한다. SID 없는 observer의 입력 오류는 채널을 닫아 전송 대기 중인 옛 AUTO_ENTER가 오류 뒤에 나가지 않게 한다. READY·owner·mode·permission·STOP 변화의 중앙 검사와 별개이며, 원 event/SHA/request/E를 갱신하지 않는다.

## 자원·시험 경계

기존 foreground K1/reader1, 기존 Gateway actor와 socket thread를 재사용한다. observer scope1·원 frame slot1·후보1·pending trigger1·결과 cache1만 추가하며 source의 ordinary/latest/fence와 server의 connection2/TX4 상한을 유지한다. 별도 Authority writer·fsync owner·daemon·poll thread·런타임 의존성은 없다. AUTO status 안내는50ms 간격이며 기존 bounded outgoing 경로를 쓴다. 정보성 갱신은 queue가 차면 생략하며 이것만으로 정상 MANUAL SID를 fence하지 않는다. 원 결과·STOP/fence 전송의 기존 장애 처리는 유지한다.

`tests/test_keyboard_auto.py`는 실제 parser/source/KeyboardActor와 명시 중앙 API leaf를 연결한다. 중앙 journal/countdown·C preflight/완전 State·장치 STOP은 별도 통합 묶음에서 검증한다. `tests/test_keyboard_server.py`는 같은 seqpacket leaf의 새 kind 전달을 검사한다. 기존 keyboard source/actor/server의 MANUAL·sameSID·release·RF/링크 fence 회귀도 통합 선택에 포함한다. 작성 당시 PC에서는 AST만 확인하고 앱/import/pytest/collect/SSH는 실행하지 않았다.
