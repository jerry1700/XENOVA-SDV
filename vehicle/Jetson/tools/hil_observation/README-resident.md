# PC 관측 상주 실행 assembly

`resident.py`는 caller가 준비한 PC 연결과 고정 metadata를 실제 PC 전용 A/S/M/D에
연결한다. 실행 입구 준비이며 실제 PC↔Jetson 교환, Device 시작, NN 정확도,
source 승인, Scene·World·주행 READY 또는 HIL 완료를 증명하지 않는다.
PC 관측은 계속 `SYNTHETIC`이다. physical RawBinding13과 capture30으로 바꾸지 않는다.

## 입력과 고정 모델

assembly1은 다음 원 metadata를 그대로 받는다. 새 수치나 기본 motion 값을 채우지 않는다.

| 입력 | 원 bytes / SHA256 |
|---|---|
| development canonical | 2063 / `32f79148b2ef9982b54eacc48da73f50d369f037fa4fe8aa4b0dec31217b4c0f` |
| motion canonical | 1581 / `d63ee963fe61cf2d6266d6d5b21606e20f1515038ed84386fa66abc6904e1e43` |
| mapper canonical | 2558 / `e80b7cb6d929eeaff0572598d6d57ad16f40fd8f4fac07c33beffc635903a061` |
| first FOLLOW 명시 파생 mapper | 2603 / `a9f83a6de41d45c765baf4d342f1f25d9eeca89e5515fab7be8a5e38bc15c7bd` |
| local calibration 모델 SHA | `c11a8143f657c20db2cb38e822430b822326249dd90a451e67a8b66ef402217a` |

local calibration SHA는 pretty calibration 파일 SHA와 다르다. `load_motion_profile`은
공개 `DevelopmentProfile.from_bytes`, `VirtualSteeringSensorProfile`,
`DevelopmentMotionProfile` constructor로 원 metadata를 재구성한다. 원 canonical
bytes·전체 필드·whole motion SHA·local calibration SHA가 모두 같아야 한다.
private 객체를 `__new__`로 만들거나 qualification/오차/시각을 수정하지 않는다.

mapper SHA는 설정에 반드시 명시한다. e80 원본은 crossing 미설정 음성·진단용이고,
실제 first FOLLOW에는 root가 별도 발급한 a9f83 원문을 지정한다. 파생은
`catalog_policy={crossing_allowed:true,aisle_lane_indices:[]}`만 명시했으며 원 e80이나
나머지 motion/calibration/오차·기한을 고치지 않는다. loader가 null crossing을 기본 true로
채우지 않으며, 이 파생 profile의 runtime 검산도 아직 실행하지 않았다.

YOLOP profile은 명시 파일과 SHA를 받아 기존 `load_yolop_profile`을 사용한다.
실제 asset 검산과 `SimNnManifest.from_current`는 S-log의 준비 단계, Device와
원 start receipt는 실제 A에서 수행한다. JIO는 기존 `JetsonJioProfile.load`만 사용한다.
별도로 설치한 owner-only private 파일이 필요하며 credential은 설정 사본·stdout에
내보내지 않는다. 이 도구는 broker/ACL/계정을 설치하거나 변경하지 않는다.

## 실행 설정 JSON

최대64KiB의 exact object다. 중복·미지·누락 필드와 비유한 수는 거절한다.
상대 경로는 이 설정 파일의 directory 기준이며 경로를 자동 탐색하지 않는다.
아래는 필드 표이며 실행 가능한 기본 설정은 제공하지 않는다.

| 상위 필드 | 필수 내용 |
|---|---|
| `profile` | 문자열 `hil-observation-resident-run-1` |
| `inputs` | `yolop_profile`, `development_profile`, `motion_profile`, `mapper_profile` 각각 exact `{path,sha256}`; 별도 `calibration_profile_sha256` |
| `jio_profile_path` | 기존 owner-only private JIO JSON 경로 |
| `storage` | 기존 `StorageProfile`의 `database_path`, `database_open_policy`, `schema_version`, `selection_policy` 네 필드 모두 |
| `runtime` | `state_directory`, `output`, `output_root`(bool), `duration_s`, `service_scope`(`null` 또는 기존 `systemd`) |
| `pc` | 원 canonical nonzero UUID 다섯 개: `source_id`, `pc_clock_epoch`, `jetson_session`, `rgb_stream`, `depth_stream` |
| `capture_clock` | `max_rtt_us`, `max_offset_width_us`, `drift_ppm`, `lease_us`, `lookback_us` 모두 |
| `probe_interval_us` | 명시 원 probe 간격, 양수이며 capture clock lease보다 작아야 함 |
| `observation_duration_s` | 실제 A observation session의 명시 개발 실행 한도0.1..300초 |
| `slot_limits` | `copy_budget_us`, `offer_budget_us`, `read_budget_us` 모두 |
| `cleanup_limits` | `transport_budget_us` |

`runtime.duration_s`도0.1..300초의 명시 개발 실행 값이다. 이 시간은 local source
준비 뒤 S의 실행 종료 조건이며 A의 원 session 기한과 별개다. caller가 기동·warmup을
포함할 충분한 observation session 한도를 선언해야 한다. 먼저 끝난 session이나 원
frame·clock·cleanup 기한을 새 now로 갱신하지 않는다. 이300초 상한은 개발용 bounded
run의 한도이며 실시간성·장시간 운용 수락값이 아니다.

`state_directory`는 기존 `xenova_vehicle runtime-init --state-dir <새 경로>`로
명시 초기화한 protected state여야 한다. 이 도구는 runtime-init을 대신하지 않으며
기존 state/DB를 지우거나 복구하려고 새 자원으로 우회하지 않는다. `systemd`를 고르면
기존 고정 service/slice guard가 실제 실행 문맥을 검산한다. `null`도 그 guard를
위조하거나 기존 실행 이력을 버리는 허가가 아니다. DB 생성 여부는 caller가 지정한
기존 `database_open_policy`에 따른다. 현재 PC source 경로는 실제 EMPTY bootstrap만
지원하며 COMPLETE·Scene·parking·worker 교체는 미지원으로 닫는다.

## 연결과 소유 순서

1. 같은 frozen source와 필요한 공통 package가 올바른 import 경로에 설치돼 있어야 한다.
   이 도구는 `sys.path`를 바꾸거나 다른 source로 fallback하지 않는다.
2. 라이브러리 caller는 연결된 stream socket을 준비하고
   `installation = load_installation(path)` 뒤 `run_connected(installation, endpoint)`를
   호출한다. 검사된 installation을 넘긴 시점부터 함수가 endpoint를 소유하며 import나
   기동 실패에도 닫는다. caller는 그 endpoint를 다시 사용하거나 별도 owner에게 주지 않는다.
3. CLI가 필요하면 아래 명시 값으로 한 연결만 받는다. listener는 원 accept 기한이 끝나면
   닫히며 자동 포트·재연결·추가 client·협상 thread는 없다.

```text
python -m tools.hil_observation.resident --installation <명시 JSON> --listen-host <명시 IPv4> --listen-port <명시 포트> --accept-timeout-s <명시 초>
```

4. PC는 연결만 먼저 하고 영상은 보내지 않는다. S가 원 service/J clock과 descriptor를
   정한 뒤 S-log가 새 output의 `artifacts/pc-source-binding.json`과
   `artifacts/pc-observation-scope.json`을 배타 출판한다. `output_root=true`면 실제 service
   UUID directory 안에 생긴다. 이 원 bytes의 회수·PC 전달은 caller의 별도 orchestration
   책임이다. 소켓 안에서 가짜 descriptor·SDK field나 새 협상 wire를 만들지 않는다.
5. PC는 이 원 scope를 `SessionScope`로 대조하고
   `run_pc_session(..., wait_for_probe=True)`를 사용한다. 실제 A pump의 첫 CLOCK_PROBE를
   받기 전에는 `next_frame`을 take하지 않아 OAK cold start 동안200ms 전송 기한을
   소모하지 않는다. renderer는 별도 owner에서 latest1을 생산하며 session callback 안에서
   blocking render를 하지 않는다. 원 PC capture interval/epoch/frame E를 그대로 보낸다.
6. S는 실제 A NN_START 뒤에만 M BIND를 보내며 원 M ACK/NN5 parser/D bootstrap과 현재
   Vehicle을 대조해 source를 승인한다. 원 clock4시각, R1/현재Vehicle, capture pose,
   frame500ms, SHM FILLED/RELEASE와 final source/operation fence는 각각 실제 owner가
   검산한다. scope 파일·연결·NN 시작·enqueue ACK만으로 READY를 만들지 않는다.

정상 종료와 실패 모두 기존 S의 STOP→실제 child reap→native/log 정리를 거친다.
CLI는 private 오류 문자열을 복제하지 않고 오류 타입만 stderr에 남긴다. 실제 runtime
결과는 새 output의 `runtime-result.json`이며 stdout 요약은 physical admission을 주장하지
않는다. 상태 directory·DB·MQTT 파일의 기존 내용을 이 도구가 자동 초기화하지 않는다.

이 두 파일 작성 단계에서는 앱·제품 import·시험·Device·socket 실행을 하지 않았다.
실제 실행·연관 회귀는 전체 A/S/M/D 묶음에서 별도로 검증해야 한다.
