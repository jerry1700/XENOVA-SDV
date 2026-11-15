# VM 관리 API 계약 v1

**최종 갱신** 2026-11-13
**대상 시스템** Raspberry Pi 4 + Xen 멀티 OS 관리 플랫폼
**구성 계층** 프론트 ↔ Spring (공개 API) ↔ dom0-manager (FastAPI) ↔ Xen `xl`

이 문서는 계약 확정안과 플랫폼·Spring 구현 검토 결과를 합친 구현 기준 문서입니다. 실측이나 생명주기 정책이 바뀌면 이 문서를 함께 갱신합니다.

---

## 1. 계층 책임

### 1.1 영구 식별자와 플랫폼 registry

Spring 기원 생성에서는 Spring이 발급한 UUID `vm_id`를 registry와 Xen에 그대로 사용한다.
로컬 CLI 생성과 factory 초기 설치는 기존 플랫폼 발급/UUID 계획이 영구 VM 신원을 확정한다.
이미 등록된 VM의 Spring 등록 통지는 이 UUID를 바꾸거나 create/start를 역호출하지 않는다.
[등록 조회·통지 계약](vm-provisioning-v1.md)의 하위 `registrations`와 FastAPI GET `/api/v1/vm-registrations`,
POST `/api/v1/vm-registrations/{vm_id}/notify`를 제공한다. GET은 실행과 무관한 현재 등록 목록의 여덟 필드 배열이다.
기존 `/vms`는 domain 조회로 유지한다. Spring `800d584` 구현 소스를 확인했으며 DB/운영 공동 수락은 별도다.

플랫폼 registry는 등록·보관·복구 생명주기의 원본입니다. dom0-manager는 플랫폼 관리 계층을 통해 registry와 Xen 실행 상태를 함께 조회합니다. `domid`는 재실행마다 바뀔 수 있으므로 영구 식별자로 사용하지 않습니다.

### 1.2 책임 분담

| 책임 | 담당 |
|---|---|
| VM 명세 보유 (`name`, `template_id`, `template_version`) | Spring |
| `vm_id` 발급 및 외부 생성 재요청 | Spring (같은 요청에 같은 `vm_id` 재사용) |
| 전달된 UUID의 최초 명세 검증·생성 작업 기록·IP 예약 | 플랫폼 공통 Manager |
| 종료된 VM을 포함한 전체 목록 구성 | Spring |
| 표시 이름 중복 검사 | Spring |
| Xen 런타임 상태 조회·변경 | dom0-manager |
| 디스크 수명주기 집행 | dom0-manager |
| 자원 가용 판정 | dom0-manager |
| 자동 복구: heartbeat·Xen 상태·desired_state 검사, 재시도·backoff, 플랫폼 `recover_once` 호출 | dom0 Recovery Worker (제어 계층, 2026-10-20 확정, `docs/build-guides/xenova-recovery-foundation.md`). Spring은 관측 상태와 복구 이벤트를 받을 뿐 재시작 명령을 내리지 않음 |
| IP 자동 할당과 중복 방지 | 플랫폼 전역 잠금 내부 |

### 1.3 계층별 계약 분리

이 문서의 모든 응답 규약은 **어느 계층의 계약인지 명시**합니다. 두 계층의 상태 코드와 오류 명명이 다르므로 한 표에 섞지 않습니다.

### 1.4 플랫폼 공통 Manager와 HTTP 어댑터의 경계

플랫폼 함수·JSON CLI의 원본 계약은 [CLI·Manager 정식 계약](../platform/cli-manager-spec.md)입니다.
이 문서의 내부 HTTP 계약과 공통 Manager의 입출력은 서로 다른 인터페이스입니다.

| 항목 | 공통 Manager / CLI | dom0-manager HTTP |
|---|---|---|
| 생성 입력 | `vm_id`, `name`, 정확한 템플릿 ID·버전, vCPU·메모리. Linux IP는 플랫폼이 예약 | 4.2절의 6개 필드. Spring의 표시 이름을 플랫폼 create에 전달 |
| 생성과 시작 | `create`는 정지 인스턴스 등록, `start`는 별도 동작 | POST가 자산 준비·재사용과 도메인 시작을 연결하는 계약 |
| 목록·상세 | 정지한 등록 VM도 조회. 템플릿·현재 설정·수명주기와 Xen 관측을 구분 | Xen 도메인만 반환. Spring이 자기 레코드와 조합 |
| `ipv4` | 예약한 내부 주소. 부팅·DHCP 획득 증거가 아님 | HTTP VM 객체에는 없음. Spring 공개 `ipv4`는 에이전트 보고 값 |
| 생성 재시도 | 같은 UUID·최초 명세는 같은 작업. 다른 최초 명세는 충돌. RETIRED/PURGED를 create로 되살리지 않음 | 같은 명세의 실행 도메인은 `409 VMExists`. 다른 명세는 `409 VMSpecConflict` |
| 보관·복원·삭제 | `retire` / `restore` / `purge`와 작업 키·세대 보호 | DELETE는 보관. restore/purge의 HTTP 전달 규약은 별도 연결 필요 |

현재 HTTP 서비스의 Xen 조회 드라이버와 공통 Manager를 호출하는 명령 어댑터는 구분합니다.
`XenDriver.create/shutdown/destroy`와 `UnavailableProvisioner`의 변경 연산은
`NotImplementedError`로 남아 있습니다. HTTP 경로·mock 시험을 실제 플랫폼 명령 연동으로
해석하지 않습니다.

platform 모드는 Spring `name`을 `VmLaunchSpec` → `Dom0LaunchRequest` → FastAPI 요청 → Provisioner → 플랫폼 create의 기존 `spec["name"]`으로 전달합니다. 등록 후 Driver의 기존 `start`를 호출합니다.
수명주기 연동에서는 작업의 원래 키·operation_id·대상 세대를 명시해야 합니다.
플랫폼의 필수 입력을 생략하거나 재시도마다 다른 키·신원·주소를 만드는 방식으로 연결하지 않습니다.
생성 응답·Xen 도메인 존재는 앱 준비 완료를 뜻하지 않으며 플랫폼의 `app_readiness=unknown`을 유지합니다.

영구 UUID는 신원 대조에 사용하고 실제 Xen 제어에는 재확인한 현재 domid를 사용합니다.
기존 factory 기본 VM 3개의 보드별 UUID 발급·고유화는 일반 Spring create와 구분한 예외로 유지합니다.
템플릿 불변성, 독립 디스크·펌웨어, restore/purge 세대 보호와 factory 예약·배포 버전의
검증 규칙은 공통 Manager 계약을 따릅니다.

---

## 2. 리소스 모델

### 2.1 VM 객체 — dom0-manager

Xen에서 직접 읽히는 필드만 포함합니다.

```json
{
  "vm_id": "550e8400-e29b-41d4-a716-446655440000",
  "state": "running",
  "vcpus": 1,
  "memory_mib": 256
}
```

### 2.2 VM 객체 — 프론트 공개

Spring이 자기 레코드와 dom0-manager 응답을 조합해 구성합니다. 목록·단건 조회 응답에는 에이전트 상태(`agent_status`)가 함께 들어갑니다.

```json
{
  "vm_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "web-01",
  "guest_kind": "linux",
  "state": "running",
  "agent_status": "ONLINE",
  "ipv4": "192.168.77.20",
  "assigned_ipv4": "192.168.77.20",
  "template_id": "debian-13-arm64",
  "template_version": "1.0.0",
  "vcpus": 1,
  "memory_mib": 256,
  "role": "interaction"
}
```

| 필드 | 의미 |
|---|---|
| `vm_id` | VM 고유 ID. Spring이 생성 |
| `name` | 표시 이름. Spring 레코드 |
| `guest_kind` | VM 종류 `linux` / `zephyr`. 생성 때 템플릿에서 복사해 Spring 레코드에 저장. **Spring만 쓰는 값이며 dom0-manager 계약에는 없음.** `zephyr`는 Linux Agent 생존 보고가 없어 `agent_status`가 `UNKNOWN`이며, 네트워크 지원은 펌웨어에 따라 다름 |
| `state` | Xen 기준 실행 상태 (3.3절). 도메인이 없으면 `stopped` |
| `agent_status` | heartbeat 기준 에이전트 상태 `UNKNOWN`/`ONLINE`/`STALE`/`OFFLINE`. **목록·단건 조회에만** 있고 생성·시작·정지 응답에는 없음 |
| `ipv4` | 에이전트가 마지막으로 보고한 IPv4 (eth0 우선, loopback 제외). 꺼져 있어도 마지막 값을 유지. 보고가 없으면 `null`, `zephyr`는 항상 `null`. **목록·단건 조회에만** 있음. dom0-manager VM 객체에는 IP가 없어 에이전트 보고를 씀 |
| `assigned_ipv4` | 플랫폼 등록부에서 Spring에 저장한 예약 IPv4. Linux와 Zephyr 모두 표시하며 저장값이 없으면 `null`. 목록·단건 조회와 역할 변경 응답에서 제공. VM이 정지해도 보이며, 실제 게스트 주소 획득이나 접속 여부는 별도 관측값임 |
| `template_id`, `template_version` | 생성에 쓴 템플릿. Spring 레코드 |
| `vcpus`, `memory_mib` | 실행 중이면 dom0-manager가 관측한 값, 꺼져 있으면 Spring 레코드의 값 |
| `role` | RC카 시나리오에서 이 VM의 배치 역할. `primary_drive`(주 구동) / `backup_drive`(백업 구동) / `interaction`(인터랙션), 없으면 `null`. MQTT `app_id`가 아니다 — 구동 VM 하나가 `autonomous`·`manual` 앱을 함께 돌릴 수 있으므로 앱 ID로 역할을 정하지 않는다(표시 계약 `display-pipeline-v1.md`의 `role`과 같은 값). 생성 때 지정하거나 `PATCH`로 바꿈. **Spring 레코드에만 있고 dom0-manager 계약에는 없음.** 생성 명세가 아니라서 같은 이름·명세 재시도(200)는 역할이 달라도 기존 VM을 그대로 돌려줌. 같은 역할을 여러 VM이 가져도 막지 않음 |

### 2.3 템플릿 객체

`GET /api/v1/templates`는 아래 객체의 배열을 반환합니다.

| 필드 | 의미 |
|---|---|
| `template_id` | 플랫폼 공개 카탈로그 ID (예: `debian-13-arm64`) |
| `version` | 보드에 공개된 이 템플릿의 최신 버전. VM 생성 요청에서는 이 값을 `template_version`으로 보냄 |
| `display_name` | 드롭다운 표시 이름 (예: `Debian 13`) |
| `min_memory_mib` | 이 템플릿의 최소 메모리 |
| `guest_kind` | 이 템플릿으로 만든 VM의 종류 `linux` / `zephyr` (보드 카탈로그 값) |

```json
[
  { "template_id": "ubuntu-24.04-arm64", "version": "1.0.5", "display_name": "Ubuntu 24.04", "min_memory_mib": 512, "guest_kind": "linux" },
  { "template_id": "debian-13-arm64", "version": "1.0.8", "display_name": "Debian 13", "min_memory_mib": 256, "guest_kind": "linux" },
  { "template_id": "zephyr-drive-lattice-lab-arm64", "version": "1.0.8", "display_name": "Zephyr Drive Lattice Lab", "min_memory_mib": 16, "guest_kind": "zephyr" }
]
```

플랫폼 기본 Ubuntu·Debian·Zephyr 공개 카탈로그의 ID와 버전을 그대로 사용합니다. dom0-manager는 템플릿 값을 변환하지 않습니다. OS 검증에서는 카탈로그 ID를 게스트의 `os-release` ID(`ubuntu`, `debian`)와 별도로 대응시킵니다.

**버전은 보드에서 읽습니다.** Spring은 dom0-manager `GET /api/v1/templates`(보드 `/srv/xenova/catalog`의 모든 공개 버전)를 조회해 `xenova.template-catalog.refresh-interval`(기본 30초) 동안 캐시합니다. 그래서 이미지를 보드에 올리기만 하면 위저드에 반영되고, 백엔드 커밋은 필요 없습니다.

- **목록(`GET /api/v1/templates`)**: `template_id`마다 가장 높은 버전 하나만 보입니다. 버전은 점으로 나눈 숫자로 비교합니다(`1.0.10` > `1.0.9`).
- **검증(생성·시작·자원 변경)**: 보드에 남아 있는 모든 버전을 허용합니다. 옛 버전으로 만든 VM도 그 버전이 보드에 남아 있으면 계속 쓸 수 있습니다.
- **표시 이름·최소 메모리**: 보드 카탈로그에 없으므로 `application.yml`의 `xenova.template-catalog.templates`에 `template-id`별로 둡니다. 여기에 없는 템플릿은 보드에 있어도 보이지 않고, 여기에 있어도 보드에 없으면 보이지 않습니다. 목록 순서가 위저드 표시 순서입니다.
- **보드 장애**: 조회에 실패하면 마지막으로 읽은 목록을 계속 씁니다. 한 번도 읽지 못했으면 dom0-manager 오류(`503 VM_CONTROL_UNAVAILABLE` 등)를 그대로 돌려줍니다.

---

### 2.4 통계 객체 — dom0-manager (xentop 원시값)

dom0-manager가 `xentop -b -i 1 -n -x -f` 한 번의 출력에서 만드는 VM별 원시값입니다. 누적 카운터와 현재값만 싣고 **사용률(CPU %, bytes/s)은 싣지 않습니다.** 사용률은 Spring이 연속 표본의 차이로 계산합니다(5장). 에이전트가 MQTT-03으로 보고하는 게스트 내부 메트릭(`source = guest_agent`)과는 `source = xen`으로 구분합니다.

```json
{
  "vm_id": "550e8400-e29b-41d4-a716-446655440000",
  "domid": 3,
  "source": "xen",
  "sampled_at": "2026-10-21T01:23:45.120Z",
  "state": "running",
  "vcpus": 1,
  "cpu_time_s": 123,
  "memory_mib": 512,
  "max_memory_mib": 512,
  "net_rx_bytes": 1048576,
  "net_tx_bytes": 262144,
  "disk_rd_bytes": 8388608,
  "disk_wr_bytes": 4194304,
  "unavailable": {}
}
```

| 필드 | xentop 원천 | 의미 |
|---|---|---|
| `vm_id` | `NAME` ↔ `xl list -v`의 `UUID` 열 | 같은 요청 안에서 이름으로 대조. `-f`가 없으면 이름이 10자로 잘려 대조가 깨지므로 항상 `-f` |
| `domid` | `NAME` ↔ `xl list` | 재실행마다 바뀐다. Spring이 누적값 초기화(재시작) 판정에 쓴다 |
| `source` | 상수 `xen` | 에이전트 보고(`guest_agent`)와 구분. Spring 메트릭 조회(REST-12·WS-01)의 `source` 필터 허용값(`xen`, `guest_agent`)과 같은 문자열 |
| `sampled_at` | dom0 시각, xentop 종료 시점 | 표본 시각. 단조 시계 간격은 Spring이 수신 시각으로 잰다 |
| `state` | `STATE` 열 | 3.1절 상태로 변환. xentop의 글자 순서는 `d s b c p r`(dying, shutdown, blocked, crashed, paused, running)로 **`xl list`(`r b p s <reason> d`)와 다르다**. `c`가 있으면 `crashed`, `s`·`d`는 `stopping`, `p`는 `paused`, 그 외 `running` |
| `vcpus` | `VCPUS` | |
| `cpu_time_s` | `CPU(sec)` | 도메인 시작 후 누적 CPU 시간(정수 초) |
| `memory_mib` | `MEM(k)` ÷ 1024 | 현재 할당 |
| `max_memory_mib` | `MAXMEM(k)` ÷ 1024 | `no limit`이면 `null`, `unavailable.max_memory_mib = NOT_REPORTED` |
| `net_rx_bytes`, `net_tx_bytes` | `-n` 장치 줄의 `RX/TX bytes` 합 | 도메인 시작 후 누적. 장치 줄이 없으면 `NETRX(k)`·`NETTX(k)` × 1024 |
| `disk_rd_bytes`, `disk_wr_bytes` | `VBD_RSECT`·`VBD_WSECT` × 512 | 도메인 시작 후 누적 |
| `unavailable` | — | 측정하지 못한 필드 → 사유. 그 필드 값은 `null`. 비어 있으면 `{}` |

`unavailable` 사유: `NO_DEVICE`(`NETS`·`VBDS`가 0, Zephyr가 여기 해당), `DEVICE_ERROR`(xentop이 `-`로 표시), `NOT_REPORTED`(`n/a`, `no limit`), `PARSE_ERROR`. **측정 실패를 0으로 적지 않습니다.** Domain-0은 목록에 없고, 꺼진 VM은 xentop에 없으므로 표본이 없습니다.

### 2.5 복구 이벤트 객체 — dom0 Recovery Worker

자동 복구를 수행하는 dom0 Recovery Worker(1.2절)가 시도마다 남기는 이벤트입니다. 플랫폼 `recover_once` journal(`docs/build-guides/xenova-recovery-foundation.md`)의 `recovery_attempt_id`를 그대로 씁니다.

```json
{
  "event_id": "9b2e3b7c-6b1f-4f1e-9d33-0b1c2a3d4e5f",
  "type": "recovery.completed",
  "vm_id": "550e8400-e29b-41d4-a716-446655440000",
  "recovery_id": "4a0e8400-e29b-41d4-a716-446655440000",
  "recovery_attempt_id": "650e8400-e29b-41d4-a716-446655440000",
  "attempt": 1,
  "reason": "HEARTBEAT_LOST",
  "mode": "graceful-restart",
  "detected_at": "2026-10-21T01:23:40.000Z",
  "occurred_at": "2026-10-21T01:23:48.420Z",
  "attempt_duration_ms": 6120,
  "recovery_duration_ms": 8420,
  "execution_id": "850e8400-e29b-41d4-a716-446655440000",
  "final": false,
  "next_retry_at": null,
  "error": null
}
```

| 필드 | 의미 |
|---|---|
| `event_id` | UUID. Spring이 중복 수신을 거르는 키 |
| `type` | `recovery.started` / `recovery.completed` / `recovery.recovered` / `recovery.cancelled` / `recovery.failed`. Spring `EventType`의 문자열과 같다. `completed`는 Worker의 시도로 새 Xen 실행이 관측됨, `recovered`는 시도가 완료되기 전에 다른 경로(사용자 조작·플랫폼 쪽 재시작·생존 신호 복귀)로 건강해짐, `cancelled`는 사용자 stop·삭제·의도 변경으로 대상이 아니게 됨, `failed`는 실제 복구 실패 |
| `recovery_id` | 장애 한 건의 ID. 최초 판정(`detected_at`)부터 최종 성공 또는 포기(`final`)까지 같은 값. Worker가 재시작해도 dom0에 저장된 미완료 `recovery_id`를 이어 간다 |
| `recovery_attempt_id` | 논리적 복구 시도마다 발급하는 UUID이자 플랫폼 `recover_once` journal 키. **같은 시도의 재전달(응답 유실·Worker 재시작 뒤 재확인)과 결과 조회(`recovery-operation`)에는 반드시 같은 값을 재사용한다.** 이전 시도가 `failed`로 확정되고 backoff 뒤 다음 시도를 시작할 때만 새 UUID를 발급한다. Spring 이벤트의 `operation_id`로 기록 |
| `attempt` | 같은 `recovery_id` 안의 시도 번호(1부터). 새 `recovery_attempt_id`를 발급할 때만 1씩 증가하고 재전달·재확인에서는 그대로다. 새 `recovery_id`에서 1로 돌아간다. 재시도 한도·대기는 복구 정책(S15P31E110-125) |
| `reason` | 판정 근거. `HEARTBEAT_LOST`(Linux VM, `agent.heartbeat` 30초 무수신) / `APP_HEARTBEAT_LOST`(Zephyr VM, `app.heartbeat` 15초 무수신, 시나리오 D8. 대상은 템플릿 capability `app_heartbeat` 가 `true` 이거나 신호를 한 번이라도 받은 VM) / `VM_UNTRUSTED`(binding이 검증된 현재 Zephyr 주행 execution에서 `vm.trust`가 발행되어 Gateway가 출력 신뢰를 철회함) / `DOMAIN_ABSENT`(의도 running인데 도메인 없음, 60초 유예 뒤) / `DOMAIN_CRASHED`(`crashed`) |
| `mode` | 플랫폼 `recover_once`의 `mode`: `start-if-absent` / `graceful-restart` / `force-restart` / `restart-crashed` |
| `detected_at` | 장애 판정 시각 (dom0) |
| `occurred_at` | 이 이벤트의 시각 (dom0) |
| `attempt_duration_ms` | `started` 외에만. 마지막 시도의 `started`부터 이 이벤트까지 |
| `recovery_duration_ms` | 장애 한 건을 끝내는 이벤트(`completed`·`recovered`·`cancelled`, `final`인 `failed`)에만. `detected_at`부터 그 끝까지. Spring 성능 지표 `vm_recovery_ms`의 원천은 `completed`의 값뿐 |
| `execution_id` | `completed`에만. 플랫폼이 결합한 새 실행 ID |
| `final` | `failed`에만. 재시도 한도 도달·정책상 중단이면 `true`(더 시도하지 않음) |
| `next_retry_at` | `failed`이고 `final`이 아닐 때 다음 시도 예정 시각 |
| `error` | `failed`(실패 사유)와 `cancelled`(취소 사유)에만. `{code, platform_code, message}`. `platform_code`는 플랫폼 원본 코드(8.1절)이고 Worker가 스스로 끝낸 경우 `null`. `code`는 Worker의 결과 코드로, 플랫폼 응답이면 `platform_code`와 같고(`cancelled`의 `VM_NOT_FOUND` 등) 그 밖에 `TARGET_NOT_ELIGIBLE`(`cancelled`: 사용자 stop·삭제·의도 변경) / `REOBSERVE_TIMEOUT`(플랫폼이 대상 변경을 알린 뒤 감시기 판단이 없음) / `RECONCILE_REQUIRED`(관측만으로 spec을 만들 수 없거나 새 실행 ID 없이 completed) / `INTERNAL_ERROR` |

한 시도는 `started` 뒤 `completed`·`recovered`·`cancelled`·`failed` 중 하나로 끝나고, 장애 한 건(`recovery_id`)은 `completed`·`recovered`·`cancelled` 하나 또는 `final: true`인 `failed` 하나로 끝납니다. `completed`는 **이 시도로** 플랫폼이 새 Xen 실행을 관측하고 `execution_id`를 돌려준 경우에만 냅니다. 시도가 완료되기 전에 VM이 건강해지면 Worker는 더 시도하지 않고 `recovered`로, 사용자 stop·삭제·의도 변경이면 `cancelled`로 닫습니다. 둘 다 복구 실패가 아닙니다(5장). 시도 없이 끝난 장애(판정 직후 회복·취소, 정책 `none`)는 이벤트를 내지 않습니다. `recovered`·`cancelled`가 이미 `failed`(`final: false`)로 끝난 시도 뒤(backoff 중)에 오면 그 시도가 아니라 장애 한 건의 종료이므로 `recovery_attempt_id`·`attempt_duration_ms`가 `null`입니다(한 시도에 종료 이벤트는 하나). 같은 `recovery_id`의 이벤트를 묶으면 시도 횟수·최종 성공 여부·장애당 복구 시간이 나옵니다. Worker는 이벤트를 상태 전이와 함께 dom0에 저장한 뒤 발행하므로, Worker가 그 사이 재시작하면 같은 이벤트가 **같은 `event_id`로** 다시 발행될 수 있습니다(Spring은 `event_id`로 거릅니다). 재시작 성공은 Xen 실행이 다시 관측됐다는 뜻이며 앱 준비·주행 권한 회복이 아닙니다(플랫폼 `app_readiness: unknown`). 차량 제어권 전환(활성→대기 승격)은 주행 게이트웨이가 `drive.mode.changed`(MQTT 계약 6장, MQTT-06)와 주행 명세의 `failover_event`로 알리며 **VM 복구 이벤트가 아닙니다.**

## 3. 상태 모델

### 3.1 공개 state

네 가지이며 전부 `xl list`에서 그대로 읽히는 실제 Xen 상태입니다. API가 생성하는 상태는 `running`뿐이고 나머지 셋은 조회로만 관찰됩니다.

| state | 의미 |
|---|---|
| `running` | Xen runtime 실행 중 |
| `stopping` | 종료 진행 중 |
| `paused` | 일시정지 (dom0에서 `xl pause`를 직접 실행한 경우에만 발생) |
| `crashed` | 비정상 종료 |

"꺼짐"은 state가 아니라 **도메인의 부재**로 표현됩니다.

### 3.2 `xl list` 상태 문자 매핑

이 매핑은 계약의 일부입니다. **`b`(blocked)는 정상 동작 중인 VM의 일반적인 상태이며 장애가 아닙니다.** 유휴 상태로 I/O를 대기하는 도메인이 `b`로 표시됩니다.

| `xl list` 상태 문자 | 공개 state |
|---|---|
| `r` (running), `b` (blocked) | `running` |
| `------` (플래그 없음) | `running` |
| `s` (shutdown), `d` (dying) | `stopping` |
| `p` (paused) | `paused` |
| `c` (crashed) | `crashed` |
| 도메인 없음 | 404 / 목록에서 제외 |
| 그 밖의 문자 | `running`으로 넘기지 않고 `500 DriverError` |

`------`는 스케줄링 순간에 아무 플래그도 서지 않은 정상 도메인입니다. dom0-manager 골격이 이 규칙으로 구현돼 있어 계약에 추가했습니다.

실보드 확인(RPi4, Xen 4.21.1-pre, 2026-10-20, S15P31E110-36): 유휴 게스트는 `-b----`, `xl pause`한 유휴 게스트는 `-bp---`로 `b`와 `p`가 함께 섭니다. 문자는 열 위치로 읽고 `p`가 `b`보다 우선하므로 `paused`입니다. `------`는 유휴 보드 30회 표본에서 나오지 않았으며, libxl 소스상 실행 가능하지만 그 순간 pCPU에 올라가 있지 않은 도메인의 표시라 `running` 매핑을 유지합니다. `xl list -l`에서 Domain-0은 `c_info.uuid`가 없고 게스트만 uuid를 가지며, 이 값은 `xl list -v`의 UUID 열과 같습니다. 출력 원본은 `platform/services/dom0-manager/tests/fixtures/rpi4-xen-4.21/`에 있습니다.

`crashed`가 남아 있는 이유는 플랫폼 도메인 설정이 `on_crash = 'preserve'`이기 때문입니다(`xenova_vm.py`). 정리(stop 또는 DELETE)하기 전까지 이 상태로 남습니다.

### 3.3 Spring 공개 state

Spring은 3.1의 네 상태에 `stopped`를 더해 다섯 상태를 공개합니다.

| state | 의미 |
|---|---|
| `running` / `stopping` / `paused` / `crashed` | dom0-manager가 알려 준 Xen 상태 그대로 |
| `stopped` | Spring 레코드는 있지만 Xen에 도메인이 없음. **Spring만 만드는 값이며 dom0-manager는 반환하지 않음** |

---

## 4. dom0-manager 엔드포인트 (내부 계약 / OpenAPI 원본)

| 메서드 | 경로 | 성공 응답 | 오류 |
|---|---|---|---|
| `GET` | `/api/v1/vms` | `200` VM 배열 | `401`, `500`, `503`, `504` |
| `POST` | `/api/v1/vms` | `201` VM | `400`, `401`, `409`, `507`, `500`, `501`, `503`, `504` |
| `GET` | `/api/v1/vms/{vm_id}` | `200` VM | `400`, `401`, `404`, `500`, `503`, `504` |
| `POST` | `/api/v1/vms/{vm_id}/stop` | `204`. `Idempotency-Key` 가 있으면 `200`·`202` 작업 상태 (4.3.1) | `400`, `401`, `409`, `500`, `501`, `503`, `504` |
| `POST` | `/api/v1/vms/{vm_id}/reboot` | `204`. `Idempotency-Key` 가 있으면 `200`·`202` 작업 상태 (4.10) | `400`, `401`, `404`, `409`, `500`, `501`, `503`, `504` |
| `GET` | `/api/v1/vms/{vm_id}/control-operations` | `200` 작업 상태 (4.8) | `400`, `401`, `404`, `500`, `501`, `503`, `504` |
| `DELETE` | `/api/v1/vms/{vm_id}` | `204` | `400`, `401`, `404`, `409`, `500`, `501`, `503`, `504` |
| `GET` | `/api/v1/cpu-layout` | `200` CPU 배치 (4.11) | `401`, `500`, `501`, `503`, `504` |
| `GET` | `/api/v1/vms/{vm_id}/cpu-layout` | `200` CPU 배치 (4.11) | `400`, `401`, `404`, `500`, `501`, `503`, `504` |
| `PUT` | `/api/v1/vms/{vm_id}/resources` | `200` 자원 변경 결과 (4.12) | `400`, `401`, `404`, `409`, `500`, `501`, `503`, `504` |
| `GET` | `/api/v1/display/phone-link` | `200` 휴대폰 중계 상태 (4.13) | `401`, `503` |
| `POST` | `/api/v1/display/phone-link/pairing-code` | `200` 새 연결번호 (4.13) | `401`, `409`, `503` |
| `GET` | `/api/v1/display/mode` | `200` 표시 모드 (4.14) | `401`, `503`, `504` |
| `POST` | `/api/v1/display/mode` | `200` 전환 뒤 표시 모드 (4.14) | `400`, `401`, `409`, `500`, `503`, `504` |
| `GET` | `/healthz` | `200` | `401`, `503` |

`/healthz` 를 뺀 모든 엔드포인트는 선택 헤더 `X-Request-Budget-Ms`(요청 예산, 4.9)를 받는다.

드라이버·플랫폼 실패는 모든 엔드포인트에서 날 수 있으며 code와 HTTP 상태가 1:1입니다(8.1절). `500 DriverError`는 분류되지 않은 실행 실패, `503 PlatformUnavailable`은 일시 장애(재시도 가능), `504 DriverTimeout`은 호출 시한 초과, `501 NotSupported`는 드라이버·플랫폼이 제공하지 않는 동작입니다. 서비스 토큰이 설정된 배포에서는 토큰이 없거나 틀리면 `401 Unauthorized`입니다. `XENOVA_SERVICE_TOKEN`이 설정된 배포에서는 `/healthz`를 포함한 모든 엔드포인트가 토큰이 없거나 틀리면 `401 Unauthorized`를 냅니다. OpenAPI는 `ServiceToken` bearer 보안 스킴을 전역 security로 선언하고 각 엔드포인트에 `401`을 명시합니다. 토큰 검사 예외는 자체 소스 토큰으로 인증하는 로그 수신(`/api/v1/logs/ingest`) 하나뿐입니다.

API 문서 경로(`/docs`·`/redoc`·`/openapi.json`)는 **기본으로 열지 않습니다**(404). 개발용 `XENOVA_DOM0_API_DOCS=1`로만 켜며, 켜도 서비스 토큰이 설정된 배포에서는 위와 같이 토큰을 검사합니다(2026-11-06). 계약 대조는 서비스가 만드는 스키마(`app.openapi()`)와 기계 판독 원본 yaml 로 하며 문서 경로에 의존하지 않습니다.

### 4.1 `GET /api/v1/vms`

**Xen에 존재하는 도메인만** 반환합니다. 종료된 VM을 포함한 전체 목록은 Spring이 자기 레코드와 조합해 구성합니다.

목록(`GET /api/v1/vms`)과 단건(`GET /api/v1/vms/{vm_id}`)의 조회 한도는 **5초**입니다(Spring 조회 응답 대기 6초 안, 10장). platform 모드는 하위 `list`·`get` 에 `timeout_ms`(기본 5000, `XENOVA_PLATFORM_READ_TIMEOUT` 초 단위 설정, 0 초과 15 이하)를 명시하며, 요청 예산(4.9)이 더 짧으면 그 값으로 줄입니다. 한도를 넘기면 `504 DriverTimeout`(`platform_code=DEADLINE_EXCEEDED`)입니다. 별도로 Manager 직렬화 대기(`XENOVA_PLATFORM_BUSY_TIMEOUT`, 기본 5초)가 앞에 올 수 있습니다(8.1 `503`).

### 4.2 `POST /api/v1/vms` — 생성 및 재시작

요청 본문: `vm_id`, `name`, `template_id`, `template_version`, `vcpus`, `memory_mib`

- 해당 `vm_id`의 디스크가 존재하면 **재사용**하고, 존재하지 않으면 `template_id`·`template_version`으로 새로 생성한다.
- 디스크 경로는 `vm_id`에서 파생한다.
- Xen에 같은 `vm_id`의 도메인이 이미 있으면 dom0-manager가 등록 명세를 확인한다. 일치하는 재요청은 `409 VMExists`이고 Spring이 기존 실행 결과를 회수한다(10.1절). 명세가 다르면 `409 VMSpecConflict`다.
- Linux IP는 Spring이 전달하지 않는다. 플랫폼이 전역 잠금 안에서 IP 풀과 registry를 확인해 빈 주소를 자동 할당한다.
- 플랫폼 `create`에는 Spring 발급 `vm_id`, 요청의 표시 이름 `name`, 템플릿 ID·버전, vcpus·memory_mib를 보낸다. 내부 실행 이름(`vm-<hex>`)·MAC·IP는 플랫폼이 발급하고, IP가 바닥나면 플랫폼 `IP_POOL_EXHAUSTED`를 `507 InsufficientResources`(`resource.name=ipv4`)로 낸다. 플랫폼 `INVALID_SPEC`은 `400 InvalidArgument`다.
- 플랫폼 `VM_NAME_CONFLICT`(발급한 내부 실행 이름·UUID 예약·MAC 등이 **다른 VM** 과 겹침)는 `409 VMNameConflict`다. 같은 VM 이 이미 있다는 `VMExists` 와 다르며 **생성 성공이 아니다**(2026-11-06). Spring 은 `409 VM_NAME_CONFLICT` 로 전달한다(8.2).

**Spring 불변조건** — 동일한 `vm_id`에는 항상 동일한 생성 명세를 전송한다. 플랫폼은 registry의 기존 생성 명세와 비교해 위반을 거부한다. 단 `vcpus`·`memory_mib` 는 최초 명세가 아니라 **지금 등록 설정**과 비교한다. 자원 변경(4.12) 뒤 재시작은 바뀐 값(`200` 응답의 `after`)으로 보낸다.

<a id="lower-create-compatibility"></a>
2026-11-01 하위 호환 보완: 위 **기존 완전 명시 HTTP 요청은 그대로 유효**하며 endpoint·header·
응답 모델을 추가로 요구하지 않는다. 하위 Manager의 선택적 자원 생략은 별도 기능이다.
같은 원 create key에서 vcpus/memory_mib를 생략하면 원 생성 확정값으로 보충하고, 그 값의
명시와 양방향으로 같은 요청으로 본다(최초 비기본 명시값 포함). 현재 설정/default로 원 값을
다시 고르지 않는다. 실제 차이·무효 입력·기존 IP/대상 비교는 유지한다. 저장 형식은 하위가
처리하고 호출자에게 journal 버전/파일 해석/변환을 요구하지 않는다. HTTP/화면의 선택적 생략
지원은 아직 별도 담당 확장이다. 실행 중 VM의 일치 요청은 기존 409이고, 등록 완료 뒤에 start를 수행한다.

- 도메인이 없고 등록된 디스크만 있을 때(재시작)는 요청 명세가 다르면 `409 VMSpecConflict`다. 템플릿 ID·버전은 최초 명세와 비교하며, 최초 명세가 없는 factory VM은 등록에 기록된 템플릿을 사용한다. 구형 등록에 없는 필드는 비교할 수 없다. `vcpus`·`memory_mib`는 현재 등록 설정(4.12 변경 반영)과 비교한다. 표시 이름은 등록을 재사용할 때 변경하지 않는다. 같은 명세면 `201`로 재시작한다.
- 아직 등록되지 않았더라도 원 생성 작업과 충돌하면 `409 VMSpecConflict`로 반환하고 `platform_code=IDEMPOTENCY_CONFLICT`를 보존한다.

`start` 엔드포인트는 두지 않습니다. 꺼진 VM의 재시작은 같은 `vm_id`로 이 엔드포인트를 재호출하는 방식이며, 디스크가 보존되어 있으므로 데이터는 유지됩니다.

### 4.3 `POST /api/v1/vms/{vm_id}/stop`

`xl shutdown`을 사용하는 graceful shutdown이며 도메인은 소멸합니다. 디스크는 **보존**됩니다.

- 성공 응답은 `204 No Content`. 종료 후에는 Xen에 도메인이 없으므로 반환할 VM 객체가 존재하지 않는다.
- **없는 VM에 stop을 호출하면 `204`.** 멱등성은 목표 상태 기준이므로 이미 실행 중이 아닌 VM의 stop은 목표가 달성된 상태다. 다만 **등록된 VM이면 도메인이 없어도 플랫폼 stop을 호출**해 `desired_state: stopped` 의도를 기록한다(플랫폼은 이미 꺼진 유효한 stop도 기록한다). 등록 자체가 없을 때만 플랫폼을 부르지 않는다. 그래서 Spring도 runtime 유무와 관계없이 stop을 전달한다(5장).
- 호출은 동기 방식이며 플랫폼은 기본 최대 90초까지 도메인 소멸을 기다린다. 일반 stop은 시간 초과 시 자동으로 강제 종료하지 않는다.
- 플랫폼의 Zephyr 개별 stop/reboot는 명시적 force가 필요하다. `board_shutdown_policy=force-allowed`는 **보드 전체 종료용 승인**이며 개별 stop의 강제 종료 승인으로 사용하지 않는다.
- 현재 HTTP stop에는 force 입력이 없다. Zephyr 개별 종료를 자동 강제로 연결하지 않으며 전달 규약은 13장의 연동 항목으로 둔다.

#### 4.3.1 요청 추적 모드 (`Idempotency-Key`)

호출자가 **최초 전송 전에** 정한 키를 `Idempotency-Key` 헤더(ASCII 가시 문자 1..200자)로 보내면 요청 추적 모드다. 키가 없으면 위 동작(동기 `204`)은 바뀌지 않는다.

- dom0-manager 는 VM 잠금·Manager 직렬화를 기다리지 않고 플랫폼에 정지 의도를 **먼저 내구 수락**시킨다. 긴 start·복구가 실행 잠금을 쥐고 있으면 플랫폼은 기다리지 않고 접수 상태를 돌려준다.
- 응답 본문은 작업 상태 객체(아래)다. **2xx 를 정지 완료로 보지 않는다. `status` 를 본다.**

| 응답 | `status` | 의미·호출자가 할 일 |
|---|---|---|
| `200` | `completed` | 원 작업 완료. 같은 키 재전달이어도 같은 `operation_id` 이고 물리 동작을 반복하지 않는다 |
| `202` | `pending` | 정지 의도를 내구 수락했고 실행이 남음. 같은 키·같은 요청으로 다시 보내면 원 기한 안에서 이행할 수 있다. 조회에서 `execution_deadline_expired: true`가 확인되면 원 요청의 물리 실행 권한은 만료됐다 |
| `204` | — | 등록이 없어 정지 목표 달성(작업 기록 없음) |
| `409 InvalidState` + `operation.status=superseded` | `superseded` | 이후 사용자 의도(새 start 등)가 수락돼 이 요청이 대체됨. 원 결과로 현재 VM 상태를 덮지 않는다 |
| `503` + `operation.status=uncertain` | `uncertain` | 플랫폼이 결과를 확정하지 못함(`RECONCILE_REQUIRED`·`phase=uncertain`). **자동 재시도·새 키·force 로 우회하지 않는다.** 조사 대상 |
| `504 DriverTimeout` + `execution_deadline_expired: true` | 원 접수 기록 유지 | 실행 전에 원 기한을 넘김. 이행을 다시 시작하지 않으며 원 ID와 기한을 보존한다 |
| `5xx` + `operation.status=failed` | `failed` | 수락 뒤 실행 실패 확정. 수락된 의도(`stopped`)는 되돌리지 않는다 |
| 오류 + `operation.status=rejected` | `rejected` | 수락 전 거절이 확인됨. 수락 잠금 혼잡으로 원 키까지 확인하지 못했다면 `unknown`으로 반환한다 |
| 오류 + `operation.status=unknown` | `unknown` | 수락 저장을 확인하지 못함. 미수락으로 추측하지 않고 같은 키로 조회(4.8)한다 |

- 같은 키를 다른 VM·다른 요청(force 등)에 쓰면 충돌(`409`, `platform_code=IDEMPOTENCY_CONFLICT`)이다. 키는 VM 마다 독립인 이름표가 아니라 플랫폼 제어 기록 전체에서 비교된다.
- 응답을 잃으면(시간 초과·연결 끊김) **같은 키로 조회(4.8)** 하거나 같은 키로 다시 보낸다. 이후 보관됐어도 확인된 과거 완료의 원 ID는 회수할 수 있다. 이 응답은 현재 VM 상태와 별개다.
- 종료 명령 뒤 결과가 `uncertain`이면 조회는 원 기록을 그대로 반환한다. 명시적으로 같은 키로 다시 제출하면 기존 host·수명 세대·정지 의도에서 Xen 부재와 소유 프로세스 종료를 확인해 원 작업을 마감할 수 있다. 같은 실행이 남거나 새 의도가 들어오면 기존 종료 명령을 반복하지 않는다. 원 기한과 최초 오류는 보존한다.
- mock·xen 드라이버는 작업 기록이 없어 동기 정지 뒤 `200 completed`(`operation_id: null`)로 답하고 조회는 `501` 이다.

작업 상태 객체 (`ControlOperation`)

| 필드 | 의미 |
|---|---|
| `vm_id`, `action` | 대상과 동작(`stop` 또는 `reboot`) |
| `request_key` | 최초 키 |
| `operation_id` | 하위 원 제어 작업. 수락 전 거절이면 `null` |
| `status` | 위 표 |
| `accepted` | 의도 수락 여부. 확인하지 못했으면 `null` |
| `phase` | 원 작업 단계 `prepared`·`accepted`·`executing`·`completed`·`failed`·`uncertain` |
| `intent_revision`, `host_boot_id`, `lifecycle_generation` | 수락한 의도의 순서와 host·VM 수명 문맥. 새 start 뒤에 도착한 옛 stop 응답은 이 값으로 가려 현재 화면을 덮지 않는다 |
| `deadline_monotonic_ms`, `execution_deadline_expired` | 원 실행 기한(그 host 의 단조 시각)과 만료 여부. 다른 시계와 빼지 않는다 |
| `next_action` | 플랫폼이 권하는 다음 동작(진단 문자열, 분기용 열거값 아님) |
| `error_code` | 원 작업의 플랫폼 오류 코드 |
| `warnings` | 진단 경고 코드. 결과를 바꾸지 않는다 |

raw journal·파일 경로·인증정보는 내지 않는다.

### 4.4 `DELETE /api/v1/vms/{vm_id}`

실행 중인 VM은 `409`로 거부합니다. 정지된 VM은 디스크를 즉시 지우지 않고 플랫폼의 retire 생명주기로 숨김 보관하며 tombstone과 generation을 유지합니다. 동일 디스크를 restore할 때는 기존 `vm_id`를 유지하고, purge 또는 복제로 새 VM을 만들 때는 Spring이 새 `vm_id`를 발급합니다.

dom0-manager는 기존 DELETE 요청 형식을 사용합니다. 플랫폼은 현재 등록 세대에 연결된 보관 작업을 기존 대상 잠금 안에서 선택합니다. 이동 후 응답에 실패해도 같은 세대의 작업을 이어 가며, 현재 보관 기록이 그 작업의 완료를 확인하면 다시 `204`를 반환합니다. 복원 후 DELETE는 새 등록 세대의 보관입니다. 이 경로는 요청 키를 받지 않으므로 복원 뒤 도착한 지연 재전달도 그 시점의 등록 세대를 대상으로 합니다. 명시적으로 지정한 과거 작업 결과는 기존 플랫폼 `operation` 조회로 확인합니다.

복원과 경합할 수 있는 호출자는 `GET /api/v1/vms/{vm_id}/lifecycle`의 `REGISTERED` 상태와 `generation`을 확인하고, `DELETE /api/v1/vms/{vm_id}?expected_lifecycle_generation=<generation>`에 `Idempotency-Key`를 함께 보냅니다. 둘 중 하나만 보내면 `400`입니다. 같은 키·세대는 같은 작업을 회수합니다. 복원 뒤에는 도착한 적 없는 옛 요청도 `409 TARGET_CHANGED`로 거절합니다. 같은 키의 대상이나 세대를 바꾸면 `409 IDEMPOTENCY_CONFLICT`입니다. 복원한 VM을 새로 보관하려면 새 키와 현재 세대를 사용합니다. 기존 보호에 따라 실행 중이면 거절합니다.

DELETE 응답 전체를 잃었다면 `GET /api/v1/vms/{vm_id}/operations?request_key=<key>`로 보관 원장의 상태와 ID를 조회합니다. 복원 뒤에도 과거 완료 기록을 읽을 수 있습니다. 현재 세대는 `/lifecycle`에서 별도로 확인합니다. 조회의 `404 OperationNotFound`는 그 시점의 검색 부재입니다.

### 4.5 `GET /healthz`

dom0-manager는 정상이지만 Xen이 응답하지 않는 상황을 구분하기 위한 엔드포인트입니다.

| 응답 | 본문 | 의미 |
|---|---|---|
| `200` | `{"status": "ok"}` | dom0-manager와 Xen 모두 응답 |
| `503` | `{"status": "xen_unavailable"}` | dom0-manager는 살아 있지만 Xen(`xl`)이 응답하지 않음 |

Spring의 시스템 상태 점검(REST-28)에서 Xen 상태를 판단할 때 사용합니다. 자동 복구는 dom0의 Recovery Worker가 담당하므로(1.2절) Spring은 이 응답으로 복구를 시작하지 않습니다.

### 4.6 `GET /api/v1/stats`

실행 중인 모든 VM의 통계 객체(2.4절)를 한 번의 `xentop` 실행으로 돌려줍니다. VM별 엔드포인트는 두지 않습니다(한 번에 모든 도메인을 읽는 것이 xentop의 단위이고, VM별 시계열은 Spring이 보관합니다).

| 응답 | 본문 |
|---|---|
| `200` | `{"sampled_at": "...", "vms": [통계 객체]}`. 실행 중 도메인만. Domain-0 제외. 도메인이 없으면 빈 배열 |
| `503 PlatformUnavailable` | xentop을 실행할 수 없음(없음·권한) |
| `504 DriverTimeout` | 조회 한도(5초) 초과 |
| `500 DriverError` | 출력을 해석하지 못함 |

조회 분류라 10장의 조회 재시도·예산(응답 대기 6초)을 그대로 따릅니다. Spring은 상태 조회와 같은 주기(기본 5초, `METRICS_XENTOP_POLL_INTERVAL_MS`)로 호출하고 실패한 회차는 표본을 만들지 않습니다. 에이전트 메트릭(MQTT-03)은 이 엔드포인트를 거치지 않습니다.

### 4.7 `GET /api/v1/recovery/events`

Recovery Worker가 dom0에 남긴 복구 이벤트(2.5절)를 오래된 순으로 돌려줍니다. 실시간 전달은 MQTT(MQTT-12)이고, 이 엔드포인트는 Spring이 재시작·연결 단절 뒤 빠진 구간을 채우는 용도입니다(REST-18의 `after_event_id`와 같은 방식).

| 질의 | 의미 |
|---|---|
| `after_event_id` | 이 이벤트 다음부터(UUID). 생략하면 보관된 가장 오래된 것부터 |
| `limit` | 기본 200, 최대 1000. 범위 밖이면 `400 InvalidArgument` |

| 응답 | 본문 |
|---|---|
| `200` | `{"events": [복구 이벤트 객체], "next_after_event_id": "..." \| null, "oldest_event_id": "..." \| null, "gap": false}`. `next_after_event_id`는 돌려준 마지막 이벤트의 `event_id`(비어 있으면 `null`), `oldest_event_id`는 dom0가 보관 중인 가장 오래된 이벤트 |
| `200` + `gap: true` | `after_event_id`가 보관 범위 밖(만료·삭제)이라 **그 이후 이력을 잃었을 수 있음**. `events`는 보관된 가장 오래된 것부터이고 `oldest_event_id`가 그 첫 이벤트. 또는 커서와 돌려준 마지막 이벤트 사이의 보관 줄이 손상돼(기록 중 중단 등) 그 자리의 이벤트를 잃었을 수 있음(이때 `events`는 커서 다음부터). dom0는 손상 자리를 보관 범위 안에서 유지하므로 커서가 그 자리를 지나기 전까지 `gap: true`다. 빈 배열 + `gap: false`는 "새 이벤트 없음"이며 이와 구분된다 |
| `400 InvalidArgument` | `after_event_id`가 UUID 형식이 아니거나 `limit` 범위 밖 |
| `503 PlatformUnavailable` | dom0의 이벤트 파일을 읽을 수 없음(권한·I/O 오류). 빈 목록으로 숨기지 않는다. 재시도하고 그동안 받은 커서는 그대로 둔다. 파일이 아직 없으면(이벤트 없음·host 재부팅) `200` |

Spring의 페이지 규칙: (1) `gap: true`면 `recovery.failed` 유실 가능성을 서버 로그에 경고(로그 레벨 WARN, 커서와 `oldest_event_id` 포함)로 남기고(콘솔 이벤트 로그에는 기록하지 않음), (2) 그 응답의 `events`를 정상 처리한 뒤, (3) 다음 요청에는 `oldest_event_id`가 아니라 `next_after_event_id`를 보내며, (4) `events`가 비거나 `next_after_event_id`가 `null`이면 조회를 끝낸다. `oldest_event_id`는 유실 구간 진단용 정보일 뿐 커서로 쓰지 않는다(`after_event_id`는 "그 다음부터"라 다시 보내면 첫 이벤트 이후를 중복 조회한다). dom0는 최근 2,000건을 보관합니다(한도는 복구 정책 S15P31E110-125에서 조정).

### 4.8 `GET /api/v1/vms/{vm_id}/control-operations?request_key=`

최초 응답을 잃어 `operation_id` 를 받지 못했을 때, 제출 전에 정한 키로 원 제어 작업(4.3.1)을 읽는다. **읽기만 한다.** 짧은 수락 잠금만 쓰고 시작·정지·이행이나 Xen 관측을 하지 않는다. 실제 이행은 같은 키·같은 요청의 stop 재전달이다.

| 응답 | 의미 |
|---|---|
| `200` 작업 상태 객체 | `phase` 로 상태를 정한다: `prepared` → `unknown`(의도 게시 전, 수락 아님), `accepted`·`executing` → `pending`, `completed`, `failed`, `uncertain`. `superseded` 는 이 조회로 알 수 없다(현재 의도·상태는 VM 조회로 본다) |
| `404 OperationNotFound` | 조회 시점에 그 키의 기록이 없음. **앞선 제출이 아직 처리 중일 수 있으므로 미수락·미실행의 증거가 아니다.** 이것만으로 새 실행을 허가하지 않는다 |
| `504 DriverTimeout` (`DEADLINE_EXCEEDED`) | 조회 예산 소진. 없음·유일함을 확정하지 않는다 |
| `503 PlatformUnavailable` | `OBSERVATION_UNAVAILABLE`(기록 디렉터리를 다 읽지 못함, 존재 여부 미확인), `RECONCILE_REQUIRED`(같은 키에 기록 여럿·손상), `STORAGE_UNAVAILABLE` |
| `501 NotSupported` | 작업 기록이 없는 드라이버(mock·xen) 또는 조회를 모르는 옛 플랫폼 |

기본 한도는 5초(조회 분류)이고 요청 예산이 더 짧으면 그 값이다. 조회의 새 예산은 원 작업의 실행 기한을 늘리지 않는다.

### 4.9 요청 예산 `X-Request-Budget-Ms`

선택 헤더(OpenAPI `components.parameters.RequestBudget`). 호출자가 이 요청을 **기다릴 남은 시간**(ms, 정수 1..105000)이다. 다른 서버의 시각이 아니라 남은 기간만 보낸다. 형식이 틀리면 `400`. `/healthz` 는 받지 않는다(자체 xl 한도 5초. 예산 만료를 "Xen 불가"로 잘못 알리지 않기 위해서다).

- dom0-manager 는 요청이 도착한 순간(스레드풀 대기 전)부터 로컬 단조 시계로 기한을 잰다. 이 기한 하나를 VM 잠금·자원 잠금 대기, Manager 직렬화 대기, 하위 플랫폼 호출, xl·xentop 실행이 나눠 쓴다.
- 하위에는 남은 값에서 응답을 돌려줄 몫(`XENOVA_RESPONSE_RESERVE`, 기본 0.5초, 실측 전 임시값)을 뺀 만큼만 준다. 플랫폼 정지의 물리 기한은 기존 90초와 이 값 중 짧은 쪽이고, 조회(get·list·작업 조회)는 `timeout_ms` 도 남은 값으로 줄인다. xl·xentop 은 자체 한도와 남은 값 중 짧은 쪽이다.
- 기다리는 동안 예산을 다 쓰면 **실행·제출하지 않고** `504 DriverTimeout` 이다(`platform_code=DEADLINE_EXCEEDED` 면 플랫폼 제출 전 만료, 없으면 잠금 대기 만료나 xl·xentop 실행 전 만료).
- `create`(등록)·`retire`(보관)에는 아직 호출 예산을 넘기지 않고 제출 직전에만 만료를 검사한다. **알려진 제한(미해결)**: `create` 는 예산이 지나도 원 생성이 계속되고, 이어지는 시작은 예산 만료로 제출되지 않아 등록만 된 VM 이 남을 수 있다. 하위 `create` 는 예산으로 원 생성 기한을 줄이고 생성 정책 `call_ms` 보다 큰 값은 거절하므로 이 헤더 상한(105초)을 그대로 넘길 수 없다. 생성 예산의 상한·동기/비동기 접수는 플랫폼·Spring 과 정한 뒤 이 절을 고친다.
- 헤더가 없으면 지금처럼 직렬화 대기 5초, 하위 기본 90초·15초, xl 5초를 쓴다. 단 VM 목록·단건 조회는 하위에 `timeout_ms` 5000 을 명시한다(4.1).
- 105000ms 는 입력 상한이지 HTTP 를 그만큼 기다리라는 뜻이 아니다. 같은 키의 재전달은 원 작업 기한을 다시 받지 않는다.

### 4.10 `POST /api/v1/vms/{vm_id}/reboot`

실행 중인 VM 을 **게스트 정상 재부팅**으로 다시 시작한다. 같은 등록·디스크의 새 실행(새 `execution_id`)이 관측되면 완료다. stop 과 create 를 이어 부르는 것이 아니라 플랫폼 `reboot` 한 번이며, 의도(`desired_state: running`)는 바뀌지 않는다. 디스크는 보존된다.

- 성공 응답은 `204 No Content`. 새 실행의 VM 은 단건 조회(4.1·`GET /api/v1/vms/{vm_id}`)로 읽는다.
- **실행 중인 도메인만 대상이다.** 꺼져 있으면 `409 InvalidState`(재부팅이 아니라 시작한다), `paused`·`crashed`·`stopping` 도 `409`(crashed 는 정지 뒤 시작). 등록이 없으면 `404 VMNotFound` — stop 과 달리 목표(새 실행)를 달성할 수 없기 때문이다.
- 호출은 동기 방식이며 플랫폼은 기본 최대 90초까지 새 실행을 기다린다. **자동으로 강제 재부팅하지 않는다.**
- 시간·결과의 의미:
  - 재부팅 명령을 제출하기 **전에** 기한(4.9 예산 포함)이 끝나면 `504 DriverTimeout`. 아무것도 바뀌지 않았다.
  - 명령을 제출한 **뒤** 새 실행을 확인하지 못하면 `503 PlatformUnavailable`(`platform_code=RECONCILE_REQUIRED`, 키 있으면 `operation.status=uncertain`). 기존 도메인이 남았는지, 새 도메인이 생겼는지, 둘 다 없는지 **확정할 수 없다.** 자동 재시도·새 키·force 로 우회하지 않고 조사한다. 키 없는 호출은 원 작업을 다시 찾을 수 없으므로, 결과를 추적해야 하는 호출자는 키를 쓴다.
  - 성공(`204`·`200 completed`)은 새 `execution_id` 가 관측됐다는 뜻이다. 앱 준비·주행 권한 회복은 아니다.
- force 입력은 없다. 협력 제어 채널이 있는 Zephyr 는 그 채널로 재부팅하고, 없는 Zephyr 는 `501 NotSupported`(4.3 의 Zephyr 개별 force 규약과 같다).
- `Idempotency-Key` 가 있으면 정지와 같은 요청 추적 모드다(4.3.1 의 응답 표·`ControlOperation`·조회 4.8·예산 4.9 를 그대로 쓰고 `action: reboot`). **키는 재부팅 가능 상태를 바꾸지 않는다.** dom0-manager 는 같은 키의 원 작업을 먼저 조회(4.8)해서, 없으면 신규 요청으로 보고 키 없는 호출과 같이 실행 상태를 확인하고(`409`·`404`), 있으면 재전달로 보고 상태 확인 없이 원 작업으로 보낸다. 조회를 지원하지 않는 Manager 면 신규·재전달을 가를 수 없어 `501` 이고 아무것도 보내지 않는다. 재부팅 중에는 도메인이 잠깐 없어서 상태 확인이 재전달을 `409` 로 막기 때문이다. 같은 키가 다른 동작(stop 등)의 원 작업이면 플랫폼이 `409 IDEMPOTENCY_CONFLICT` 로 거절한다.
- mock 드라이버는 같은 VM 의 새 도메인(새 domid)으로 동기 재부팅하고 키 있으면 `200 completed`(`operation_id: null`)다. 재부팅을 제공하지 않는 드라이버는 `501`.
- 플랫폼은 꺼진 VM·force 없는 비협력 Zephyr 거절에 아직 전용 코드가 없어 `ARTIFACT_INVALID` 의 문구로만 알린다. dom0-manager 는 그 두 문구만 `409`·`501` 로 바꾸고 `platform_code` 는 원본(`ARTIFACT_INVALID`)을 둔다.
- Recovery Worker 에는 사용자 재부팅이다: 새 실행은 플랫폼 실행 소유자 `user` 로 기록되고, 감시기는 새 domid 로 부팅 유예부터 다시 본다. 복구 이벤트를 내지 않는다.


### 4.11 `GET /api/v1/cpu-layout`, `GET /api/v1/vms/{vm_id}/cpu-layout`

플랫폼 `cpu-layout` 으로 **정책이 정한 vCPU→pCPU 배치와 실제 Xen hard affinity·전용성 검사**를 읽는다. 조회만 하며 정책·affinity·VM 을 바꾸지 않는다. 한 VM 조회도 같은 스키마이고 `vms` 에 그 VM 하나만 담는다(전용성은 다른 모든 도메인의 vCPU 를 함께 관측해 판정한다).

- **`200` 은 조회 성공이지 배치 준수가 아니다.** `host.ready`·`affinity_matches`·`exclusive_verified`·`issues` 를 함께 본다.
- `null` 은 해당 없음·모름이다. 정지(`observed.presence: absent`)·공유(`allocation: shared` 의 `exclusive_verified`)·정책 미설정(`configured: false`)의 null 을 성공(true)으로 읽지 않는다. 정지는 불일치가 아니다.
- 불일치는 `200` 이고 `affinity_matches: false`·`exclusive_verified: false` 와 `issues` 로 알린다. issue 는 `code`(`AFFINITY_MISMATCH`·`EXCLUSIVE_CONFLICT`·`CPU_NOT_READY`·`POLICY_PUBLICATION_PENDING`)와 그 코드가 쓰는 필드만 담는다(충돌한 domid·vcpu·pcpus 등).
- `reservations` 는 전용 코어 예약이다. 플랫폼 정책 1.0 의 `{vm_id, pcpu}` 와 2.0 의 `{vm_id, mode, pcpus}` 를 `{vm_id, pcpus}` 로 맞춘다.
- `observed_monotonic_ms` 는 관측한 host 의 단조 시각이다. UTC 가 아니며 다른 시계와 빼지 않는다.
- 플랫폼은 공통 operation lock 을 **기다리지 않고** 시도한다. 긴 변경 작업(생성·정지·재부팅 등)이 잠금을 쥐고 있으면 바로 `503`(`platform_code=OPERATION_IN_PROGRESS`, 재시도 가능)이다. dom0-manager 도 이 조회를 요청별 Manager 로 불러 자체 직렬화 대기에 묶지 않는다.
- 요청 예산(4.9)은 하위 `timeout_ms`(기본 5000, 상한 15000)로만 줄인다. 하위 `cpu-layout` 은 `budget_ms` 를 받지 않는다.
- 오류: 미등록 VM `404`, 관측 실패 `503 OBSERVATION_UNAVAILABLE`(빈 마스크·0 으로 성공을 대체하지 않는다), 관측 도중 domain set·신원·정책이 바뀌면 `503 TARGET_CHANGED`(재시도 가능, 대상 충돌이 아니다), 기한 만료 `504`, 손상된 정책 `500`(`platform_code=CPU_POLICY_INVALID`, 재시도로 풀리지 않는다). 플랫폼 응답의 모양이나 의미값이 계약을 벗어나면 꾸미지 않고 `503` 이다: `presence`(present·absent)와 관측값의 짝, `allocation`(exclusive·shared·null), issue `code`(위 네 가지), 예약 형식(1.0 `pcpu` 와 2.0 `mode: exclusive`·`pcpus` 를 섞지 않음, 2.0 의 mode 는 exclusive 만). 한 VM 조회의 `vms` 가 정확히 그 VM 하나가 아니어도(빈 목록·다른 VM·여러 VM) `503` 이다.
- `exclusive_verified` 는 관측 시점의 hard affinity 중복 검사다. IRQ·캐시·메모리·I/O 격리나 실시간 보장이 아니다.
- CPU 정책이 없는 드라이버(mock 등)와 `cpu-layout` 을 모르는 옛 Manager 는 `501`.


### 4.12 `PUT /api/v1/vms/{vm_id}/resources` — 정지한 Linux VM 의 vCPU·메모리 변경

정지한 Linux VM 의 등록 설정에서 vCPU 수와 메모리를 바꾼다. 실행 중인 게스트를 바꾸지 않으며(hotplug·balloon 없음) **다음 시작부터** 적용된다. 게스트를 시작하지 않는다. 플랫폼 `update-resources` 한 번이다.

- 헤더 `Idempotency-Key` **필수**(ASCII 가시 문자 1..200자). 없거나 형식이 틀리면 `400`. 최초 전송 전에 정하고 재전송에 같은 값을 쓴다.
- 본문 `{"vcpus": 2, "memory_mib": 768}`. 두 필드 모두 필수. **범위는 vcpus 1..4, memory_mib 128..1024** 다(플랫폼 하한 128, API 상한 1024). 템플릿별 하한(Debian 256, Ubuntu 512)은 Spring 이 더 검사한다.
- 성공 `200`:

```json
{
  "vm_id": "550e8400-e29b-41d4-a716-446655440000",
  "operation_id": "5d1c0a2e-7b3f-4c1e-9a55-2f8e6b1d4c70",
  "before": { "vcpus": 1, "memory_mib": 512 },
  "after": { "vcpus": 2, "memory_mib": 768 },
  "applies_on_next_start": true
}
```

  같은 키·같은 본문의 재전송도 `200` 이고 원 결과(같은 `operation_id`·`before`·`after`)다. `operation_id` 는 플랫폼에서 항상 UUID 이고, 작업 기록이 없는 mock 만 `null` 이다.
- 대상: 등록된 **Linux** VM, 도메인이 없고 의도가 `stopped`. Zephyr 는 펌웨어 프로필이 고정이라 `400`.
- 처리 순서: dom0-manager 는 같은 키의 원 작업을 **자산 원장(asset operation)과 제어 원장(stop·reboot) 둘 다**에서 먼저 찾는다. 키는 동작·VM 전체에서 하나의 요청만 가리킨다.
  - 같은 VM·같은 값의 원 자원 변경이 있으면 원 요청을 그대로 다시 보낸다(완료면 원 결과, 미완료면 같은 작업을 이어 간다). 반영 뒤에는 설정 해시가 바뀌므로 지금 값으로 요청을 다시 만들지 않는다.
  - 같은 키가 다른 VM·다른 값·다른 동작(stop·reboot·retire 등)의 원 작업이면 `409 VMExists` 이고 아무것도 보내지 않는다.
  - 원 작업이 없으면 지금 등록의 generation·설정 해시를 기대값으로 신규 제출한다.
  - 키 조회를 지원하지 않는 옛 Manager 면 신규·재전달을 가를 수 없어 `501` 이고 아무것도 보내지 않는다.
  - 두 원장을 따로 조회하는 것은 플랫폼이 원장 전체의 키 충돌을 보장하기 전까지의 방안이다. 같은 키의 동시 제출 경합은 막지 못한다.
- `TARGET_CHANGED`(조회와 제출 사이에 설정·세대가 바뀜)를 받으면 dom0-manager 가 같은 키의 원 작업을 다시 찾는다. 원 작업이 있으면 같은 키로 이어서 끝내고, 없음이 확인됐을 때만 `409 InvalidState`(`platform_code=TARGET_CHANGED`)다. 이때만 호출자는 VM 을 다시 읽고 바꿀지 정한 뒤 **새 키**를 쓴다. 다시 찾지 못하면 `503`(같은 키로 재전송).
- 응답을 잃으면 **같은 키·같은 본문**으로 다시 보낸다. 새 키를 만들면 이중 변경이 될 수 있다.
- 요청 예산(4.9): 원 작업 조회는 예산 안에서 하고, 하위 `update-resources` 는 `budget_ms` 를 받지 않는다. **`504` 는 제출 전에 예산이 끝났을 때만**이며 아무것도 바뀌지 않았다. 제출한 뒤에는 바뀌었을 수 있으므로 `504` 로 답하지 않는다(제출 뒤 시한 초과는 `503`, 같은 키로 재전송).
- 오류:

| HTTP / `code` | `platform_code` | 경우 |
|---|---|---|
| `400 InvalidArgument` | — / `RESOURCE_UNSUPPORTED` | 범위 밖, 키 없음·형식 오류, nil UUID / Zephyr·플랫폼 범위 밖 |
| `404 VMNotFound` | `VM_NOT_FOUND` | 등록 없음 |
| `409 InvalidState` | — / `VM_NOT_STOPPED` / `INTENT_MISMATCH` / `TARGET_CHANGED` / `ASSET_IN_USE` | 도메인이 있음(실행·paused·crashed·stopping: 먼저 정지) / 의도가 stopped 아님 / 설정·세대가 바뀌었고 원 작업 없음 / 자산 사용 중 |
| `409 VMExists` | — / `IDEMPOTENCY_CONFLICT` | 같은 키가 다른 요청의 원 작업 |
| `503 PlatformUnavailable` | `OPERATION_IN_PROGRESS` / `RECONCILE_REQUIRED` / `RECOVERY_REQUIRED` / `STORAGE_UNAVAILABLE` | 다른 플랫폼 작업 진행 중(같은 키로 재시도) / 미해결 제어·복구·실행이 VM 을 소유 / 원 작업 미완료(같은 키로 재전송하면 이어 감) / 저장소 장애 |
| `504 DriverTimeout` | `DEADLINE_EXCEEDED` | 제출 전 예산 만료. 바뀐 것 없음 |
| `501 NotSupported` | — / `INVALID_SPEC` | 자원 변경을 제공하지 않는 드라이버, 키 조회를 모르는 옛 Manager |

- 플랫폼 응답은 그대로 믿지 않는다. 원 작업의 키·동작(`update-resources`)·`vm_id`·요청이 보낸 것과 같고, 봉투와 기록의 `operation_id` 가 같은 UUID 이고, 상태가 완료이고, `after` 가 요청값이고, `before`·`after` 가 정수(bool 아님)·플랫폼 범위 안이고, `starts_guest` 가 `false` 여야 한다. 하나라도 어긋나면 `503` 이다. 원 작업 조회 기록이 어긋나도 `503` 이고 아무것도 보내지 않는다.
- Spring 은 `200` 의 `after` 로 자기 VM 레코드의 `vcpus`·`memory_mib` 를 갱신한다. 꺼진 VM 의 공개 값은 Spring 레코드 값이고(2.2), 재시작(4.2)에도 이 값을 보낸다.
- CPU 배치는 바꾸지 않는다. 플랫폼 CPU 정책이 다음 시작 때 모든 vCPU 에 같은 마스크를 적용·검증한다(4.11 로 확인).
- 범위 밖: 실행 중 vCPU 핀·스케줄러 가중치·메모리 변경(플랫폼 미지원), CPU 배치 정책 변경(모든 게스트 정지가 필요한 유지보수 작업, 플랫폼 `cpu-policy-set`).
- mock 드라이버: 등록됐고 실행 중이 아닌 VM 의 값을 바꾸고 `200`(`operation_id: null`). 같은 키·같은 값은 원 결과, 다른 값은 `409 VMExists`.

### 4.13 `GET /api/v1/display/phone-link`, `POST /api/v1/display/phone-link/pairing-code` — USB 휴대폰 화면 중계

휴대폰을 보드에 USB 테더링으로 연결하면 휴대폰 DHCP 가 보드 `usb0` 주소를 정하고, 그 주소는 연결할 때마다 바뀔 수 있습니다. 관리 콘솔에서 지금 접속 주소를 확인하고 연결번호를 발급할 수 있게 dom0-manager 가 중계(`xenova-display-mobile`)의 상태를 읽어 줍니다. 중계 코드는 바꾸지 않고 밖에서 읽습니다.

| 알아내는 것 | 방법 |
|---|---|
| 중계 실행 여부 | `systemctl is-active xenova-display-mobile.service` |
| 접속 주소 | `/run/xenova-display-mobile/endpoint.json` 의 `url` (쓸 수 있는 링크가 없으면 파일 없음) |
| 연결번호 남은 시간 | `/run/xenova-display-mobile/pairing-code` 수정 시각 + `pairing_ttl_s`(기본 120초). 연결에 성공하면 파일이 지워진다 |
| 보드 USB 주소·휴대폰 주소 | `ip -j -4 addr/route show dev <usb_interface>`. 기본 경로의 게이트웨이가 휴대폰 |
| 새 번호 | `systemctl kill --kill-whom=main --signal=USR1` 후 바뀐 `pairing-code` 를 읽는다 |

`GET` 응답 `200`:

```json
{
  "configured": true,
  "relay": "listening",
  "url": "http://10.117.188.25:8084/display/mobile",
  "usb": {"interface": "usb0", "present": true, "ipv4": ["10.117.188.25/24"], "gateway": "10.117.188.86"},
  "configured_phone_ipv4": "10.117.188.86",
  "subnet_mismatch": false,
  "pairing": {"active": true, "expires_at": "2026-11-12T08:31:05Z"},
  "observed_at": "2026-11-12T08:29:10Z"
}
```

| 필드 | 의미 |
|---|---|
| `configured` | 중계 설정 파일 `/etc/xenova/display-mobile.json` 이 있음 |
| `relay` | `listening` 실행 중이고 접속 주소 있음 · `waiting_link` 실행 중이지만 쓸 수 있는 USB 링크 없음(케이블·테더링·주소) · `starting` 시작 중 또는 자동 재시작 대기 · `stopped` 꺼짐(재부팅 뒤 기본값) · `failed` · `unknown` |
| `url` | 휴대폰 브라우저에서 열 주소. `relay` 가 `listening` 일 때만 값이 있음 |
| `usb` | 보드 USB 링크. `present` 는 장치 유무, `ipv4` 는 전역 주소(주소/접두어), `gateway` 는 휴대폰 주소(없으면 `null`) |
| `configured_phone_ipv4` | 중계 설정의 휴대폰 주소 |
| `subnet_mismatch` | 보드 USB 주소가 설정된 휴대폰 대역 밖이면 `true`. 휴대폰이 테더링 대역을 바꾼 경우이며 중계는 시작하지 못한다. 판단할 수 없으면 `null` |
| `pairing` | 쓰지 않았고 만료되지 않은 번호가 있는가와 만료 시각. **번호 값은 담지 않는다** |

`POST .../pairing-code` 응답 `200`: `{"code": "123456", "expires_at": "..."}`.

- 이미 연결된 휴대폰의 세션도 끊긴다(중계의 번호 회전 규칙). 콘솔은 누르기 전에 알린다.
- 번호는 이 응답에만 있다. dom0-manager·Spring·브라우저 모두 저장하거나 로그에 남기지 않는다. 두 응답 모두 `Cache-Control: no-store`.
- 요청 하나가 번호 하나를 만들고 앞 번호를 무효로 하므로 Spring 은 자동 재시도하지 않는다. 동시 요청은 dom0-manager 안에서 하나씩 처리한다.
- `409 InvalidState`: 중계가 `listening` 이 아님(신호를 보내지 않음). `503 PlatformUnavailable`: `systemctl`·`ip`·파일 읽기 실패, 신호 거절, 또는 2초 안에 새 번호가 나오지 않음.
- 번호를 웹에 보여 주는 근거: 로그인한 운영자는 이미 SSH 로 같은 파일을 읽을 수 있고, 번호는 USB 로 연결된 휴대폰에서만(중계가 `usb0` 에 묶임) 2분·5회 안에 쓸 수 있다.

### 4.14 `GET /api/v1/display/mode`, `POST /api/v1/display/mode` — 표시 장치 모드(HDMI / 휴대폰) 전환

차량 통합 화면은 HDMI 모니터 또는 USB 테더링 휴대폰 중 하나에 나온다. 지금까지는 보드에 SSH 로 들어가 `sudo python3 -m xenova_display.display_mode mobile` 로 바꿨고, 휴대폰 중계는 재부팅 뒤 꺼져 있다. 관리 콘솔에서 바꿀 수 있게 dom0-manager 가 같은 명령을 대신 실행한다. 전환 로직(잠금, 순서, 실패 시 되돌리기)은 그 명령의 것이고 dom0-manager 는 바꾸지 않는다.

| 요청 | 보드에서 하는 일 |
|---|---|
| `GET` | `display_mode status` 결과를 옮긴다 (한도 10초) |
| `POST {"mode": "mobile"}` | USB 링크(4.13 의 `usb.present`)가 없으면 명령을 부르지 않고 `409 USB_LINK_ABSENT`. 있으면 `display_mode mobile`: 휴대폰 중계를 켜고 접속 주소가 생긴 뒤 HDMI 를 끈다 |
| `POST {"mode": "hdmi"}` | `display_mode hdmi`: HDMI 를 켜고 화면이 실제로 그려진 뒤 휴대폰 중계를 끈다 |

`GET`·`POST` 응답 `200` (`POST` 는 전환 뒤 상태):

```json
{
  "mode": "mobile",
  "boot_default": "hdmi",
  "services": {"hdmi": "inactive", "mobile": "active", "feed": "active"},
  "hdmi_renderer_ready": false,
  "observed_at": "2026-11-14T09:00:00Z"
}
```

| 필드 | 의미 |
|---|---|
| `mode` | 지금 선택된 표시 모드 `hdmi` · `mobile` |
| `boot_default` | 재부팅 뒤 모드. 지금은 항상 `hdmi` |
| `services.hdmi` | `xenova-display-hdmi.service` 의 systemd 상태(`active`·`inactive`·`activating`·`deactivating`·`failed` 등, 못 읽으면 `unknown`) |
| `services.mobile` | `xenova-display-mobile.service`(휴대폰 중계)의 상태 |
| `services.feed` | `xenova-display-feed.service`(두 모드가 함께 쓰는 화면 데이터)의 상태. `active` 가 아니면 전환을 거절한다 |
| `hdmi_renderer_ready` | HDMI 화면이 실제로 그려지고 있음(렌더러 상태 파일 기준) |
| `observed_at` | 보드에서 읽은 시각(UTC) |

- 휴대폰 모드가 되어도 휴대폰이 실제로 붙은 것은 아니다. 접속 주소와 연결번호는 4.13 으로 읽는다.
- 전환은 보통 수 초, 최악 240초(되돌리기 포함) 걸린다. 도중에 끊으면 두 화면이 모두 꺼질 수 있어 끊지 않는다. 요청 예산(4.9)은 시작 전에만 확인한다(소진이면 바꾸지 않고 `504`). 호출자는 240초보다 길게 기다린다.
- 동시 전환은 명령의 잠금이 막는다(`409 DISPLAY_MODE_BUSY`). 표시 장치를 바꿔도 VM·주행 모드·Gateway·MQTT 는 바뀌지 않는다.
- 응답은 `Cache-Control: no-store`.

오류 (`platform_code` 로 분기, `retryable` 은 다시 눌러 볼 만한지):

| HTTP | code | platform_code | 상황 |
|---|---|---|---|
| `400` | `InvalidArgument` | — | `mode` 가 `hdmi`·`mobile` 이 아님 |
| `409` | `InvalidState` | `USB_LINK_ABSENT` | (mobile) 케이블·USB 테더링 없음. 명령을 부르지 않음. retryable |
| `409` | `InvalidState` | `DISPLAY_MODE_BUSY` | 다른 전환이 진행 중. retryable |
| `409` | `InvalidState` | `MOBILE_ENDPOINT_NOT_READY` · `HDMI_RENDERER_NOT_READY` · `DISPLAY_SERVICE_TRANSITION_FAILED` · `DISPLAY_MODE_FAILED_RESTORED` | 전환 실패, 이전 모드로 되돌림. retryable |
| `409` | `InvalidState` | `DISPLAY_SERVICE_NOT_SETTLED` | 표시 서비스가 켜지거나 꺼지는 중. retryable |
| `409` | `InvalidState` | `SHARED_FEED_NOT_ACTIVE` · `MODE_GUARD_NOT_LOADED` · `NATIVE_DISPLAY_UNITS_REQUIRED` · `LEGACY_MOBILE_FIRST_REQUIRES_MIGRATION` · `FOREIGN_MODE_MARKER` · `UNSAFE_CONFIGURATION` · `UNSAFE_RUNTIME_DIRECTORY` | 보드 표시 설정 문제. 보드 작업 필요 |
| `500` | `DriverError` | `ROLLBACK_INCOMPLETE_CHECK_DISPLAY_SERVICES` | 실패 후 이전 모드로 완전히 되돌리지 못함. 보드에서 표시 서비스 확인 |
| `503` | `PlatformUnavailable` | 명령이 준 코드 또는 없음 | 명령 실행·결과 해석 실패 |
| `504` | `DriverTimeout` | `DISPLAY_MODE_TIMEOUT` 또는 없음 | 명령이 시한 안에 안 끝남(상태를 다시 읽어 확인), 또는 시작 전 예산 소진 |

dom0less 로 바뀌면 이 명령을 실행하는 곳이 dom0 에서 표시 장치를 가진 VM 으로 옮겨간다. 경로와 본문은 그대로 둔다.

---

## 5. Spring 공개 API

프론트에 노출되는 VM 관리 엔드포인트는 다음과 같습니다. `start`는 dom0-manager의 `POST /api/v1/vms` 재호출로 구현합니다.

| 메서드 | 경로 | 성공 응답 | 비고 |
|---|---|---|---|
| `GET` | `/api/v1/vms` | `200` `{items: VM[], observed_at}` | Spring의 등록 VM과 이번 dom0-manager 조회 결과를 결합합니다. 역할 순(primary_drive → backup_drive → interaction → 역할 없음), 역할 없는 VM 중에서는 Zephyr → Linux, 같은 묶음 안에서는 생성 순으로 정렬합니다. Spring에 등록되지 않은 도메인은 제외합니다. `observed_at`은 조회가 끝난 뒤 Spring이 응답을 구성할 때 기록하는 시각입니다. |
| `POST` | `/api/v1/vms` | 신규 `201` VM, 동일 명세 재시도 `200` VM | 같은 이름에 다른 명세면 `409 VM_NAME_CONFLICT` |
| `GET` | `/api/v1/vms/{vm_id}` | `200` VM | — |
| `PATCH` | `/api/v1/vms/{vm_id}` | `200` VM (조회 응답과 같은 모양) | 본문 `{"role": "primary_drive"}`, `{"role": null}`이면 역할을 지움. 역할만 바꿀 수 있고 dom0-manager를 호출하지 않으며 이벤트도 남기지 않음. 보관된 VM은 `404 VM_NOT_FOUND` |
| `POST` | `/api/v1/vms/{vm_id}/start` | `200` VM | dom0 단건 조회 후 도메인이 없을 때만 create 재호출. 이미 `running`이면 그대로 반환. `crashed`·`paused`·`stopping`이면 `409 INVALID_VM_STATE`로 거부(정지 후 다시 시작). 플랫폼 CLI와 같은 동작이며 자동 복구는 하지 않음 |
| `POST` | `/api/v1/vms/{vm_id}/stop` | `200` VM (`state: stopped`) | dom0 단건 조회로 정지 전 상태를 읽은 뒤 **runtime 유무와 관계없이** dom0 stop을 호출. 플랫폼이 `desired_state: stopped` 의도를 기록해야 Recovery Worker가 사용자가 끈 VM을 다시 켜지 않는다(1.2절). 이미 꺼져 있었으면 `vm.state.changed` 이벤트만 남기지 않음. dom0는 `204`지만 Spring은 레코드로 VM 객체를 만들어 반환 |
| `DELETE` | `/api/v1/vms/{vm_id}` | `204` | dom0 retire 성공 뒤 Spring 레코드를 `RETIRED`로 보존하고 v1 목록·단건 조회에서는 숨김. 보존 행의 이름은 `<이름>~<vm_id 앞 8자>`로 바꿔 같은 이름을 다시 쓸 수 있게 함 |
| `GET` | `/api/v1/templates` | `200` 템플릿 배열 | 보드 카탈로그(템플릿별 최신) + 설정 표시 정보 (2.3절). 보드 미조회 시 `503` |
| `POST` | `/api/v1/vms/{vm_id}/reboot` | `200`·`202` 작업 상태 | 선택 `Idempotency-Key`. 진행 중이면 `202` |
| `GET` | `/api/v1/vms/{vm_id}/control-operations?request_key=` | `200` 작업 상태 | 선택 `request_key`로 원 작업 조회 |
| `PUT` | `/api/v1/vms/{vm_id}/resources` | `200` 자원 변경 결과 | 정지한 Linux VM의 vCPU·메모리를 변경하며 다음 시작부터 적용(보드 계약 4.12) |
| `GET` | `/api/v1/vms/{vm_id}/resources` | `200` 자원 뷰 | vCPU·메모리, pCPU 수, `vcpu_pinning`(VM의 hard affinity), `shared_pcpus`(dom0 공유 집합), `exclusive_pcpus`, `limits`. `host_preparation_required`는 `null` |
| `PUT` | `/api/v1/vms/{vm_id}/resources/vcpu-pinning` | `200` 자원 뷰 | 본문 `{mappings:[{vcpu, pcpus}]}`(모든 vCPU에 같은 마스크). 공유 VM은 공유 집합 안, 전용 VM은 공유·다른 전용 코어 밖. 응답 `host_preparation_required`: `true`면 보드 호스트 준비 뒤 시작 가능(1차 보드는 늘 `true`), `false`면 바로 시작 가능(플랫폼 1e41ea73 이후 게스트 배치만 바뀐 변경). 공유·전용 방식과 공유 집합은 바꾸지 않는다 |

같은 `/api/v1/vms` 아래의 그 밖의 공개 엔드포인트(위 VM 관리와 별도. 형식의 기준은 각 계약·[백엔드 README](../../backend/README.md)):

| 메서드 | 경로 | 성공 응답 | 비고 |
|---|---|---|---|
| `GET` | `/api/v1/vms/{vm_id}/metrics?from=&to=&source=&names=` | `200` `{items: MetricSample[]}` | 메트릭 이력(5장 아래 저장 규칙). 잘못된 `source`·기간은 `422` (`VALIDATION_ERROR`·`TIME_RANGE_INVALID`) |
| `GET` | `/api/v1/vms/{vm_id}/processes?sort=&limit=` | `200` 최신 프로세스 목록 | `sort` 기본 `cpu_percent`, `limit` 1..20(기본 10). 범위 밖 `422 VALIDATION_ERROR` (MQTT 계약 3장 `processes[]`) |
| `GET` | `/api/v1/vms/{vm_id}/threads` | `200` Zephyr 스레드 표 | Zephyr Telemetry 의 스레드 창을 조합한 최신 값 |
| `GET` | `/api/v1/vms/{vm_id}/telemetry/heartbeat` | `200` Publisher 상태 | `status` `FRESH`·`STALE`·`UNKNOWN` (MQTT 계약 3장) |
| `POST` | `/api/v1/vms/registrations` | 신규 `201`, 기존 `200` `{vm_id, registration: "confirmed"}` | 사용자 JWT 가 아니라 등록 통지 전용 서비스 토큰. 본문 오류 `400 INVALID_REGISTRATION`, 충돌 `409 VM_REGISTRATION_CONFLICT` ([등록 계약](vm-provisioning-v1.md)) |
| `POST` | `/api/v1/vms/synchronizations` | `200` 동기화 결과 | dom0 등록 목록을 읽어 요청 한 번에 Spring 레코드를 보드와 같게 맞춘다. 보드에 없는 행은 즉시 RETIRED(작업 중이면 보류), 같은 `vm_id`가 다시 나타나면 복구 ([등록 계약](vm-provisioning-v1.md)) |

보고서 `GET /api/v1/reports/os-evidence`, `GET /api/v1/reports/performance` 와 실행 로그 `GET /api/v1/logs`(·`/sources`, [실행 로그 계약](runtime-logs-v1.md))도 같은 JWT 로 연다.

모든 공개 API는 `Authorization: Bearer {access_token}`이 필요합니다. 생성·시작·정지·삭제가 성공하면 이벤트(`vm.created`, `vm.state.changed`, `vm.deleted`)가 기록됩니다.

Spring은 dom0-manager `GET /api/v1/vms`를 주기적으로(기본 5초, `EVENT_VM_STATE_CHECK_INTERVAL_MS`) 조회하고, 공개 `GET /api/v1/vms` 요청이 들어올 때도 한 번 조회합니다. 두 경우 모두 VM 실행 상태를 동기화합니다. 보드의 VM 등록·은퇴를 Spring 등록 목록에 반영하는 작업은 `POST /api/v1/vms/synchronizations`에서 수행합니다. Spring은 등록 VM과 런타임을 결합해 최신 목록을 반환하고, 사용자 조작 없이 바뀐 상태(크래시, 스스로 종료 등)를 `vm.state.changed`(`cause: detected`)로 기록합니다. 조회 중 사용자 조작이 감지된 VM은 상태 변경 감지에서 제외합니다. dom0-manager 조회에 실패하면 기존 상태와 이벤트를 보존하며, 공개 목록 요청은 실패로 응답하고 주기 조회는 해당 회차를 건너뜁니다. 서버가 처음 보는 VM은 기준값만 저장합니다. 동시 실행 상태 동기화는 Spring 프로세스 안에서 순차 실행합니다.

**xentop 통계의 저장과 사용률 계산.** Spring은 4.6절 응답의 VM마다 `MetricSample(source = "xen", boot_id = null, window_ms = null, observed_at = sampled_at)`을 저장합니다. 값 이름과 단위는 다음과 같고, `unavailable`에 있는 필드는 값 `null`에 `unavailableReason`을 그대로 둡니다.

| 통계 객체 필드 | `MetricValue.name` | `unit` |
|---|---|---|
| `cpu_time_s` | `cpu_time` | `seconds` |
| `memory_mib` × 1048576 | `memory_current` | `bytes` |
| `max_memory_mib` × 1048576 | `memory_max` | `bytes` |
| `net_rx_bytes`, `net_tx_bytes` | `net_rx`, `net_tx` | `bytes` |
| `disk_rd_bytes`, `disk_wr_bytes` | `disk_read`, `disk_write` | `bytes` |

사용률은 같은 `vm_id`의 연속 두 표본으로 Spring이 계산합니다. `cpu_percent = 100 × Δcpu_time_s ÷ Δt초 ÷ vcpus`(0~100), 전송률은 `Δbytes ÷ Δt초`. `domid`가 바뀌었거나 누적값이 줄었으면 재시작으로 보고 그 구간의 사용률은 계산하지 않습니다. 에이전트 메트릭(MQTT-03)은 `source = "guest_agent"`로 따로 저장하며 두 계열을 섞어 계산하지 않습니다.

**복구 이벤트의 기록.** Spring은 MQTT-12로 받은 복구 이벤트(2.5절)를 `EventType` `recovery.started` / `recovery.completed` / `recovery.recovered` / `recovery.cancelled` / `recovery.failed`로 기록합니다. `operation_id = recovery_attempt_id`(장애 단위 `recovered`·`cancelled`처럼 `null`이면 `recovery_id`), `occurred_at`은 객체의 값, `payload`는 객체 전체이고, `event_id`가 같은 이벤트는 한 번만 기록합니다. 심각도는 `started`·`completed`·`recovered`·`cancelled` `INFO`, `failed` `WARNING`, `failed`이면서 `final`이면 `ERROR`입니다. 복구 실패율은 `final`인 `failed`만 실패로 세고 `recovered`·`cancelled`는 넣지 않습니다. `recovery.completed`의 `recovery_duration_ms`를 성능 지표 `vm_recovery_ms`로 기록하고, 같은 `recovery_id`로 시도들을 묶어 표시합니다. 복구로 바뀐 Xen 상태는 기존 주기 조회가 `vm.state.changed`(`cause: detected`)로 따로 기록하며, `cause`에 복구나 페일오버 값을 추가하지 않습니다. 제어권 전환은 주행 이벤트(`drive.mode.changed`, MQTT 계약 5·6장)로만 남깁니다. Spring은 복구 이벤트를 받아 표시할 뿐 재시작 명령을 내리지 않습니다(1.2절). 재시작 뒤 빠진 구간은 4.7절로 채웁니다.

---

## 6. 디스크 수명주기

| 연산 | 종료 방식 | 디스크 | 재시작 |
|---|---|---|---|
| `stop` | 게스트에 정상 종료 요청 | 보존 | 가능 |
| `DELETE` | 정지 상태에서만 허용 | 숨김 보관(retire) | restore로 가능 |
| `purge` | 해당 없음 | 영구 삭제 | 불가 |

retire는 복구 가능성을 위해 디스크를 보존하고, 실제 공간 회수는 별도 purge 생명주기에서 수행합니다.

---

## 7. 상태 전이표

| 현재 상태 | `POST /vms` | `POST /stop` | `DELETE` |
|---|---|---|---|
| 도메인 없음 + 등록 디스크 있음 | `201` — 보존된 디스크로 재시작 | `204` | `204` — retire |
| 도메인 없음 + 디스크 없음 | `201` — 템플릿에서 생성 | `204` | `404` |
| `running` | `409 VMExists` | `204` — 정상 종료, 디스크 보존 | `409` — 먼저 정지 필요 |
| `stopping` | `409 VMExists` | 소멸까지 동기 대기 | `409` |
| `paused` | `409 VMExists` | 플랫폼 정책에 따라 정상 종료 | `409` |
| `crashed` | `409 VMExists` | 명시적 force 정책 적용 | `409` |

`crashed` 도메인은 종료 요청에 응답할 수 없으므로 `stop`은 `destroy`로 목표 상태를 달성합니다. 이 경우 디스크는 보존되지만 unclean 상태일 수 있습니다.

---

## 8. 오류 코드

### 8.1 dom0-manager

| 코드 | 명명 | 조건 |
|---|---|---|
| `400` | `InvalidArgument` | 형식 또는 범위 오류 |
| `404` | `VMNotFound` | `GET` 대상 도메인 부재 |
| `404` | `OperationNotFound` | 요청 키(4.8) 또는 원 작업 ID(8.1.1)로 기록을 찾지 못함. 검색 부재이며 미수락의 증거가 아님 |
| `409` | `VMExists` | `vm_id` 충돌(같은 VM 이 이미 있음). 자원 변경(4.12)에서는 같은 `Idempotency-Key` 가 다른 요청의 원 작업 |
| `409` | `VMSpecConflict` | 같은 UUID의 기존 등록 또는 원 생성 작업과 명세가 충돌 |
| `409` | `VMNameConflict` | 플랫폼 `VM_NAME_CONFLICT`: 생성 중 내부 실행 이름·UUID 예약·MAC 등이 다른 VM 과 겹침. 같은 VM 이 아니므로 생성 성공으로 바꾸지 않는다 (2026-11-06) |
| `507` | `InsufficientResources` | 가용 자원 부족. `resource.name`: `memory_mib`, `vcpus`, `ipv4`(플랫폼 Linux IP 풀 고갈, `IP_POOL_EXHAUSTED`) |
| `401` | `Unauthorized` | Bearer 서비스 토큰 없음·불일치 (`/healthz` 포함) |
| `501` | `NotSupported` | 드라이버·플랫폼이 제공하지 않는 동작. xen 모드의 생성·정지·삭제 명령 |
| `503` | `PlatformUnavailable` | registry·저장소·Xen 일시 장애, 또는 다른 플랫폼 작업이 잠금을 쥐고 있어 대기 한도(`XENOVA_PLATFORM_BUSY_TIMEOUT`, 기본 5초) 안에 차례가 오지 않음(플랫폼 `OPERATION_IN_PROGRESS`). 재시도 가능. `/healthz`는 Manager를 부르지 않아 바쁜 상태의 영향을 받지 않는다 |
| `504` | `DriverTimeout` | `xl`·플랫폼 호출 시한 초과 (VM 목록·단건 조회 5초, 정지 90초 대기 포함) |
| `500` | `DriverError` | 그 밖의 실행 실패, 모르는 상태 문자 |
| `409` | `InvalidState` | 실행 중인 VM을 `DELETE` (4.4절). 먼저 정지해야 한다 |

플랫폼이 수락 또는 완료를 알렸어도 유효한 제어 작업 ID가 빠지면 `503 PlatformUnavailable`로 응답한다. 키 조회(4.8)에서는 원장의 작업 ID와 응답의 ID, VM·키·단계, 정상 정지 또는 재부팅 명세를 대조한다. 불일치는 `503`이다.

오류 본문:

```json
{
  "code": "InsufficientResources",
  "message": "insufficient memory_mib: requested 1024, available 512",
  "detail": null,
  "resource": { "name": "memory_mib", "requested": 1024, "available": 512 }
}
```

| 필드 | 의미 |
|---|---|
| `code` | 위 표의 명명 |
| `message` | 사람이 읽는 설명 |
| `detail` | 선택. **Spring 로그 전용**이며 프론트에 노출하지 않음. 생성·정지·보관에서 하위 실행 원문과 내부 경로는 dom0-manager 진단에 남기고 이 필드에서 제외한다 |
| `resource` | 선택. `InsufficientResources`일 때만 `{name, requested, available}` |
| `platform_code` | 선택. 플랫폼 Manager가 준 원본 오류 코드(`HOST_QUIESCING` 등). 플랫폼 오류에만 있고 xl·검증·인증 오류에는 없다. 모르는 플랫폼 코드는 `500 DriverError`로 내되 이 값은 보존한다. **코드로 분기할 때는 `detail` 문자열이 아니라 이 값을 쓴다** |
| `retryable` | 선택. 플랫폼이 준 원본 재시도 가능 여부. 코드의 고정 속성이 아니며 이것만으로 재시도 정책을 만들지 않는다(같은 작업이 아직 실행 중일 수 있으면 원 작업을 먼저 조회) |
| `operation` | 선택. 키 있는 제어 요청(4.3.1)의 오류에만 있는 원 작업 문맥(작업 상태 객체) |
| `context` | 선택. 생성·정지·보관에서 확인한 하위 작업 단계, 수락·결과 상태, 원 작업 ID, 다음 확인 절차(8.1.1) |

**플랫폼 오류 코드 → dom0-manager 응답** (플랫폼 복구 기반, `docs/build-guides/xenova-recovery-foundation.md` "오류와 관측")

| `platform_code` | HTTP / `code` | 의미 |
|---|---|---|
| `HOST_QUIESCING`, `BOOT_ADMISSION_REQUIRED`, `INTENT_MISMATCH` | `409 InvalidState` | host가 새 시작·복구를 닫음 / 현재 host의 실행 정책 미확정·거절 / running 의도·revision 불일치. 재전송으로 풀리지 않는 충돌 |
| `TARGET_CHANGED` | `409 InvalidState` | 이전 host·lifecycle·실행 관측이 더 이상 대상이 아님 |
| `RECOVERY_MODE_UNSUPPORTED` | `501 NotSupported` | graceful 미지원(Zephyr) 또는 per-VM force 승인 없음 |
| `DEADLINE_EXCEEDED` | `504 DriverTimeout` | 같은 host의 최초 deadline 만료·범위 초과 |
| `OBSERVATION_UNAVAILABLE`, `RECONCILE_REQUIRED`, `INTENT_RECONCILIATION_REQUIRED` | `503 PlatformUnavailable` | Xen 관측 실패(부재가 아님) / 실행·기록 결과를 확정하지 못해 운영자 재판정 필요 / 의도 원본과 journal 불일치. 재시도 가능하지만 자동 반복 대상은 아님 |
| `XEN_EXECUTION_FAILED` | `500 DriverError` | 수락 후 물리 제어 실패. 플랫폼은 accepted 의도를 유지 |
| `OPERATION_IN_PROGRESS`, `STORAGE_UNAVAILABLE`, `RECOVERY_REQUIRED`, `CLEANUP_INCOMPLETE` | `503 PlatformUnavailable` | 기존 매핑 |
| `INSUFFICIENT_STORAGE`, `IP_POOL_EXHAUSTED` | `507 InsufficientResources` | 기존 매핑 (`ipv4`는 `resource.name`) |
| `VM_NAME_CONFLICT` | `409 VMNameConflict` | 다른 VM 과 내부 이름·UUID 예약·MAC 충돌. 2026-11-06 전에는 `409 VMExists` 였다 |
| `IDEMPOTENCY_CONFLICT` | 생성 `409 VMSpecConflict`, 그 밖에는 기존 `409 VMExists` | 같은 키가 다른 요청의 원 작업 |
| 그 밖의 코드 | `500 DriverError` | `platform_code` 보존 |

정지 대기 만료는 플랫폼에 아직 전용 코드가 없어 `XEN_EXECUTION_FAILED`(복구 기반 제어 경로) 또는 `ARTIFACT_INVALID`(일반 오류 포장)에 메시지(`deadline`, `timed out`)로만 실린다. dom0-manager는 이 두 코드에 한해 메시지로 시한 초과를 가려 `504 DriverTimeout`으로 내며, 플랫폼이 `DEADLINE_EXCEEDED`를 내면 이 예외는 제거한다(플랫폼 파트에 요청).

FastAPI는 검증 실패 시 기본적으로 `422 HTTPValidationError`를 반환하므로 `400 InvalidArgument`로 정규화합니다. 예외 핸들러 교체만으로는 부족하며, **생성 스키마에서 422 응답 정의와 `HTTPValidationError` 모델까지 제거**해야 OpenAPI 차이 검사가 통과합니다.

생성의 `VMExists`는 같은 VM의 실행 도메인이 이미 있음을 뜻합니다. `VMSpecConflict`는 같은 UUID의 명세 충돌, `VMNameConflict`는 다른 VM과의 식별 충돌입니다. 자원 변경(4.12)의 `VMExists`는 같은 키가 다른 요청의 원 작업이라는 뜻입니다. 표시 이름 중복은 Spring이 판정합니다.

#### 8.1.1 생성·정지·보관의 원 작업 문맥

아래는 정상 종료 요청을 보냈으나 Xen 도메인 제거가 확인되지 않은 예다.

```json
{
  "code": "PlatformUnavailable",
  "message": "platform stop failed (RECONCILE_REQUIRED)",
  "platform_code": "RECONCILE_REQUIRED",
  "retryable": false,
  "context": {
    "action": "stop",
    "stage": "shutdown_confirmation",
    "accepted": true,
    "status": "uncertain",
    "operation_id": "e89d21fd-cc29-4dc4-9a63-951875bdc0d7",
    "next_action": "lookup_original"
  }
}
```

- `action`은 하위 작업이다. HTTP 생성 안에서 `create`는 자산 준비·등록, `start`는 등록 이후 Xen 시작을 뜻한다. HTTP 삭제는 `retire`다.
- `stage`는 확인된 처리 단계다. 기록을 저장하는 도중 실패할 수도 있으므로 해당 단계의 완료나 부작용의 유무는 이 필드 하나로 판정하지 않는다. 확인할 수 없으면 `null`이다.
- `accepted: true`는 하위 작업의 수락을 확인했다는 뜻이다. `false`는 이번 제출이 수락 전에 거절됐음을 확인한 경우다. 앞선 호출에서 응답을 잃었다면 이번 제출의 거절과 별개로 원 작업을 조회한다. 원장 저장 또는 원 키 확인이 불확실하면 `null`이다. 원 작업 ID가 할당됐어도 저장을 확인하기 전에는 수락을 단정하지 않는다.
- `status`는 `rejected`(접수 전 거절), `unknown`(접수 여부 미확인), `pending`(접수·진행), `completed`(원 작업 완료), `failed`(원 작업 실패 확정), `uncertain`(수락 후 결과 미확정), `superseded`(이후 의도로 대체)를 구분한다. `completed`는 게스트 OS·앱의 준비를 의미하지 않는다.
- `next_action`은 `fix_request`(명세/키 점검), `check_current_vm`(현재 등록·실행 확인), `lookup_original`(원 결과 확인), `reconcile_original`(원 자산 작업 조회 후 명시적 수습 판단) 중 하나다. `retryable`은 여전히 하위 원값이며, 새 키·강제 종료·자동 재실행을 결정하는 근거로 단독 사용하지 않는다.

등록을 완료한 뒤 HTTP 호출 기한이 끝나 Xen 시작을 제출하지 못하면 `504 DriverTimeout`을 반환한다. 확인된 원 등록 ID가 있으면 `platform_code: DEADLINE_EXCEEDED`와 함께 `context`에 `action: create`, `stage: completion`, `accepted: true`, `status: completed`, `operation_id: <원 등록 ID>`, `next_action: lookup_original`을 담는다. `start_submitted: false`는 이 HTTP 호출에서 시작을 보내지 않았음을 뜻한다. 시작이 이미 제출됐거나 이전 호출의 결과를 조회할 때는 이 필드를 생략한다.

`POST /api/v1/vms`에 선택 헤더 `Idempotency-Key`를 보내면 플랫폼의 시작 원장에 그 키와 정규화한 생성 본문을 결합한다. 등록은 기존 UUID 기준으로 회수한다. 최초 정상 실행 응답은 기존 `201 Vm`이다. 응답을 잃었으면 `GET /api/v1/vms/{vm_id}/control-operations?request_key=<key>`에서 `action: start`인 원 결과를 조회한다. 조회는 Xen 호출이나 원장 변경을 하지 않는다. 같은 키·본문의 POST 재전달은 이미 완료된 시작이면 `200 ControlOperation`으로 원 결과를 돌려준다. 미확정 시작은 동일 실행·의도·세대와 CPU·실행 공급을 확인한 경우에만 원 작업을 마감한다. 조회 시점에 시작 기록이 없다면 등록 중이거나 시작 전일 수 있다. 같은 키를 다른 본문에 재사용하면 `409 VMSpecConflict`다. 역사적 완료는 현재 실행 상태와 별개다.

`GET /api/v1/vms/{vm_id}/operations/{operation_id}?action={action}`에 실패 응답의 VM·`context.operation_id`·`context.action`을 전달한다. 기존 서비스 토큰과 요청 예산을 적용한다. 기본 한도는 5초다. 응답은 같은 상태 구조에 원장에 보존된 `platform_code`를 더한다. 읽기 시점의 원 작업 결과이며, 현재 VM 상태나 새로운 사용자 의도는 별도로 확인한다. 다른 VM·동작 또는 찾을 수 없는 ID는 `404 OperationNotFound`이며, 앞선 제출이 없었다는 근거가 아니다.

정지의 ID를 받지 못했다면 최초 `Idempotency-Key`로 기존 4.8 조회를 사용한다. 두 조회 모두 명령을 실행하거나 원 기한을 바꾸지 않는다. 요청 본문, 경로, 원장 원문, 자격은 ID 조회로 제공하지 않는다. 이 계약은 dom0-manager HTTP까지 적용한다.

### 8.2 Spring

공개 오류 본문은 `{code, message}`이며, 입력 필드 검증 실패일 때만 `errors: [{field, reason}]`가 붙습니다.

**Spring이 직접 판단하는 오류**

| 코드 | 명명 | 조건 |
|---|---|---|
| `401` | `UNAUTHORIZED` / `INVALID_TOKEN` / `TOKEN_REVOKED` | 토큰 없음 / 잘못됐거나 만료 / 로그아웃으로 폐기 |
| `422` | `VALIDATION_ERROR` | 입력값 규칙 위반 — dom0-manager를 호출하지 않고 거절 |
| `404` | `TEMPLATE_NOT_FOUND` | 보드에 공개되지 않았거나 표시 정보가 없는 `template_id`·`template_version` |
| `404` | `VM_NOT_FOUND` | Spring 레코드에 없는 `vm_id` |
| `409` | `VM_NAME_CONFLICT` | 같은 이름의 VM이 다른 명세로 이미 있음 |
| `409` | `INVALID_VM_STATE` | 시작 요청인데 도메인이 `crashed`·`paused`·`stopping` 상태로 남아 있음 |
| `409` | `VM_OPERATION_IN_PROGRESS` | 같은 VM에 생성(기존 VM 재요청)·시작·정지·삭제·자원 변경·재부팅·vCPU 핀닝 중 하나가 아직 진행 중. "이 VM에 다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요." dom0-manager를 호출하지 않으며 요청을 쌓지 않는다 (11장) |

입력 오류는 `422 VALIDATION_ERROR` 가 원칙이고(본문 필드 검증과 경로·질의 값의 형식 오류 모두), 구현상 아래 두 곳만 `400` 이다.

| 엔드포인트 | 응답 | 조건 |
|---|---|---|
| `GET /api/v1/logs` | `400 VALIDATION_ERROR` (`errors` 없음) | `limit` 1..500 밖, 음수 `after`, 128자를 넘는 `source_id`·`component`, 허용하지 않는 `level`. dom0 로그 조회도 같은 경우 `400 InvalidArgument` 다([실행 로그 계약](runtime-logs-v1.md)). 단 `vm_id` 가 UUID 형식이 아니거나 `after`·`limit` 이 정수가 아니면 공통 처리로 `422 VALIDATION_ERROR` |
| `POST /api/v1/vms/registrations` | `400 INVALID_REGISTRATION` | 등록 통지 본문 오류 (서비스 간 경로, 5장) |

**dom0-manager 오류 변환** (dom0 본문의 `code`·`platform_code`로 판단한다. dom0 `message`(영어, 내부 ID 포함)와 `detail`은 로그에만 남기고 **공개 `message`는 코드별 한글 문구**다. QA 1-B1, 2026-11-13)

| dom0 | Spring 공개 |
|---|---|
| `404 VMNotFound` | 단건 조회에서는 오류가 아니라 "도메인 없음"(`stopped`)으로 처리. 그 밖에는 `404 VM_NOT_FOUND` ("보드에서 VM을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.") |
| `409 VMExists` | 생성·시작에서는 성공으로 처리(10.1절). 그 밖에는 `409 VM_ALREADY_EXISTS` ("보드에 같은 VM이 이미 있습니다.") |
| `409 VMSpecConflict` | 현재 Spring 매핑이 없어 `502 VM_CONTROL_BAD_RESPONSE`로 반환. 명세 충돌을 공개 `409`로 전달하는 Spring 연결은 후속 작업 |
| `409 VMNameConflict` | `409 VM_NAME_CONFLICT`. 생성·시작에서도 **성공으로 처리하지 않는다**(같은 VM 이 아님) |
| `409 InvalidState` | `409 INVALID_VM_STATE`. message는 `platform_code`별: `VM_NOT_STOPPED` "VM을 정지한 뒤 다시 시도하세요.", `VM_NOT_RUNNING` "실행 중인 VM에서만 할 수 있는 작업입니다.", `HOST_QUIESCING` "보드가 종료 중이라 새 작업을 받을 수 없습니다.", `BOOT_ADMISSION_REQUIRED` "보드의 VM 실행 정책이 확정되지 않아 지금은 시작할 수 없습니다.", `TARGET_CHANGED`·`INTENT_MISMATCH` "그사이 VM 상태가 바뀌었습니다…", `ASSET_IN_USE` "VM 디스크를 다른 작업이 사용 중입니다…", `VM_PURGED` "이미 완전히 삭제된 VM입니다.", 그 밖·없음 "지금 VM 상태에서는 할 수 없는 작업입니다. VM 상태를 확인한 뒤 다시 시도하세요." 작업을 아는 곳은 더 구체적으로 바꾼다: 삭제 "실행 중인 VM은 정지한 뒤 삭제할 수 있습니다.", 재부팅 "실행 중인 VM만 재부팅할 수 있습니다. 꺼진 VM은 시작하고, 비정상 상태면 정지한 뒤 시작하세요." |
| `400 InvalidArgument` | `400 INVALID_ARGUMENT`. `platform_code` `TEMPLATE_NOT_FOUND`·`TEMPLATE_VERSION_NOT_FOUND` "보드에 해당 템플릿 버전이 없습니다. 템플릿을 다시 선택하세요.", `RESOURCE_UNSUPPORTED` "이 템플릿이 허용하지 않는 vCPU·메모리 값입니다.", 그 밖 "보드가 요청 값을 받아들이지 않았습니다. 입력값을 확인하세요." |
| `507 InsufficientResources` | `507 INSUFFICIENT_RESOURCES`. message는 `resource.name`에 따라 메모리·CPU 부족 또는 "VM에 할당할 수 있는 IP 주소가 없습니다."(`ipv4`, Linux VM IP 풀 고갈) |
| `500 DriverError` | `500 VM_DRIVER_ERROR` ("보드에서 VM 작업을 처리하지 못했습니다. 잠시 후 다시 시도하세요.") |
| `platform_code=CPU_NOT_READY` (dom0 code와 무관, 보통 `500 DriverError`) | `409 CPU_NOT_READY` "CPU 배치를 바꾼 뒤 보드 호스트 준비가 끝나지 않아 VM을 시작할 수 없습니다. 보드에서 호스트 준비를 실행하거나 보드를 재부팅해 주세요." 핀닝 변경이 `host_preparation_required=true`였는데 `xenova-vm cpu-host-prepare` 없이 시작한 경우. 호스트 준비는 웹 API가 없다 |
| `501 NotSupported` | `501 VM_CONTROL_NOT_SUPPORTED` ("보드가 아직 지원하지 않는 작업입니다."). 생성에서 받으면 dom0가 거부한 것이므로 Spring 기록을 지움 |
| `503 PlatformUnavailable` | `503 VM_CONTROL_UNAVAILABLE` (전송 실패와 같은 코드, 잠시 후 재시도 가능). 생성에서 받아도 기록은 유지 |
| `504 DriverTimeout` | `504 VM_CONTROL_TIMEOUT` (응답 타임아웃과 같은 코드) |
| `404 OperationNotFound` | `404 OPERATION_NOT_FOUND` ("보드에서 요청한 작업 기록을 찾을 수 없습니다.") |
| `401 Unauthorized` (또는 본문과 관계없이 401) | 서비스 토큰 설정 불일치. 사용자 로그인 문제가 아니므로 `502 VM_CONTROL_BAD_RESPONSE`로 전달하고 로그에 경고 |
| 해석할 수 없는 본문, 모르는 `code`, 본문이 필요한데 없음 | `502 VM_CONTROL_BAD_RESPONSE` |

**전송 실패**

| 상황 | Spring 공개 |
|---|---|
| 연결 실패 (재시도 후에도) | `503 VM_CONTROL_UNAVAILABLE` |
| 요청을 보낸 뒤 연결이 끊김 | `503 VM_CONTROL_UNAVAILABLE` (명령은 재시도하지 않음) |
| 응답 타임아웃 | `504 VM_CONTROL_TIMEOUT` |

---

## 9. 입력값 검증

| 항목 | 규칙 | 검증 계층 |
|---|---|---|
| `name` | 1~63자, 한글·공백 허용. 제어문자·줄 구분 문자와 앞뒤 공백 금지 | Spring 소유, create 전달 경로에서 동일 규칙 검증 |
| `vm_id` | UUID. nil UUID(`00000000-…`)는 Domain-0이므로 모든 엔드포인트에서 `400 InvalidArgument` | dom0-manager (백엔드가 막는 것과 별개로 거부) |
| `vcpus` | 1~4 | Spring, dom0-manager |
| `memory_mib` | 템플릿 `min_memory_mib` 이상 ~ 1024 이하 (공통 하한 16). 자원 변경(4.12)은 128~1024 | 템플릿별 하한은 Spring, 템플릿별 범위(`resource_limits`)는 플랫폼 카탈로그가 create 때 검사(벗어나면 `400 InvalidArgument`), 공통 범위는 dom0-manager. 자원 변경의 하한 128 은 플랫폼 `update-resources` 범위 |
| 배수 제약 | 없음. 화면 입력만 64 단위 | — |
| `template_id` | 공개 카탈로그 ID·버전을 그대로 전달 | Spring 목록 검사 + 플랫폼의 정확한 ID·버전·해시 검사 |
| `role` | 선택. `primary_drive` / `backup_drive` / `interaction` 또는 `null` (대소문자 구분). 그 밖의 값(예전 `autonomous`·`manual`·`message-board` 포함)은 `422 VALIDATION_ERROR` (`field: role`) | Spring 소유 (생성·`PATCH`) |

`name`은 create에 그대로 전달해 플랫폼의 기존 `display_name`에 저장합니다. UUID 기반 신원·내부 실행 이름·경로와 구분하며 재시작 요청을 rename으로 해석하지 않습니다.

공통 하한 16MiB는 게시된 Zephyr starter 템플릿 1.0.0의 고정 프로필(1 vCPU / 16MiB)을 통과시키기 위한 느슨한 안전망입니다. 실제 하한은 템플릿별 `min_memory_mib`이며 Linux 템플릿은 Ubuntu 512, Debian 256을 유지합니다.

---

## 10. 타임아웃 · 재시도

명령은 동기 처리합니다. 특히 graceful stop은 플랫폼에서 최대 90초까지 기다릴 수 있으므로 외부 계층의 제한 시간이 더 길어야 합니다.

| 요청 | 연결 실패 (2초) | 응답 타임아웃 |
|---|---|---|
| 조회 (GET) | 1회 재시도 → 또 실패하면 `503` | 1회 재시도 → 또 실패하면 `504` |
| 명령 (생성·시작·정지·삭제) | 1회 재시도 → 또 실패하면 `503` | **재시도 안 함** → `504` |

| 항목 | 값 |
|---|---|
| 연결 타임아웃 | 2초 |
| 재시도 간격 | 0.5초 |
| 응답 대기 — 조회 | 6초 |
| 응답 대기 — 명령 | 15분 (`DOM0_MANAGER_COMMAND_TIMEOUT`. 웹 프록시 `/api/` 도 15분) |
| 플랫폼 stop 응답 한도 | 90초 (플랫폼 고정. Manager stop은 대기 시간을 입력받지 않는다) |
| 전체 예산 — 조회 | 약 15초 |
| 전체 예산 — 명령 | 약 15분 |

플랫폼 stop은 대기 시간을 입력받지 않고(Manager stop 명세는 `vm_id`·`force`뿐) 90초 deadline 하나를 제거 관측까지 공유하므로 Manager 호출은 약 90초 안에 끝납니다. platform 모드의 dom0-manager는 `XENOVA_SHUTDOWN_GRACE`를 90으로만 허용하며 다른 값은 시작 시 설정 오류입니다. `XENOVA_XL_COMMAND_TIMEOUT`은 xl 서브프로세스에만 걸리고 Manager 호출을 제한하지 않습니다. Spring의 명령 응답 대기(15분)가 그 위에 있습니다. 15분은 정지 90초뿐 아니라 Linux VM 생성(이미지 준비 포함)처럼 오래 걸리는 명령을 끊지 않으려는 값입니다.

명령의 응답 타임아웃에서 재시도하지 않는 이유는 두 가지입니다. 연결이 성립했으므로 dom0-manager가 요청을 이미 받아 처리 중일 수 있고, `xl` 호출은 블로킹 subprocess이며 uvicorn 워커가 1개이므로 재전송한 요청은 처리 중인 요청 뒤에서 대기할 뿐입니다.

### 10.1 `504` 이후 처리

명령이 `504`로 종료되면 프론트가 `GET`으로 상태를 확인합니다. 그 뒤 동일 `vm_id`에 동일 명세의 요청이 재도달하고 dom0-manager가 `409 VMExists`를 반환하면, **Spring은 이를 성공으로 변환해 응답합니다.** dom0-manager의 `VMSpecConflict`는 8.2절의 현재 Spring 오류 변환을 따릅니다.

---

## 11. 동시성 · 실행 모델

- 제어 계층은 **`vm_id` 단위 락**으로 동일 VM에 대한 변경 연산을 직렬화한다. 같은 `vm_id`에 create가 동시에 도달하면 `xl create`가 중복 실행된다.
- **Spring은 같은 VM에 상태 변경 요청이 진행 중이면 다음 요청을 기다리게 하지 않고 즉시 `409 VM_OPERATION_IN_PROGRESS`로 거절한다**(2026-11-14, QA 1-B2). 대상은 생성(기존 VM 재요청)·시작·정지·삭제·자원 변경·재부팅·vCPU 핀닝이며, 같은 종류의 중복과 다른 종류(예: 정지 중 삭제)를 모두 포함한다. 다른 VM의 작업과 조회는 막지 않는다. 요청이 끝나면(성공·실패 모두) 바로 해제되므로 실패 뒤 같은 요청의 재전달은 플랫폼의 원 작업 이어 가기로 간다. 진행 중 목록은 Spring 인스턴스 안에서만 유지되므로 백엔드가 한 대일 때 유효하다. 이 거절이 없으면 뒤의 요청은 dom0-manager의 VM 잠금 뒤에서 기다렸다가 앞 작업이 남긴 상태 위에서 실행된다.
- `xl` 호출은 블로킹 subprocess이므로 **비동기 핸들러에서 직접 호출하지 않고 별도 스레드로 분리**한다. 워커 1개 구성에서 직접 호출하면 한 VM의 create 30초 동안 목록 조회를 포함한 모든 요청이 대기한다.

---

## 12. 자원 산정

`InsufficientResources`의 판정은 `xl info`의 `free_memory` 기준으로 하며, dom0 메모리는 `dom0_mem` 부트 파라미터로 고정합니다.

**Pi 4 8GB 기준**

| 항목 | 할당 |
|---|---|
| 실사용 가능 RAM | 약 7.8GB |
| Xen 하이퍼바이저 | 약 128MB |
| dom0 (고정 할당) | 1GB |
| domU 가용 | **약 6.5GB** |

계획된 4~5대 규모에서 메모리는 제약 조건이 아닙니다. VM 5대를 512MiB씩 할당해도 2.5GB로 약 4GB의 여유가 있습니다.

---

## 13. 미확정 항목

| 항목 | 담당 | 내용 |
|---|---|---|
| 조회 응답 대기 6초의 기산점 | 백엔드 | 연결 시간 포함 여부. 미포함이면 최악 소요가 16.5초로 예산 15초를 초과하므로 예산 18초 또는 대기 5초로 조정 필요 |
| 오류 코드 명명 규칙 | 백엔드 | `VALIDATION_ERROR` / `InvalidArgument` 중 한쪽으로 통일 권고. 현재 구현은 Spring `422 VALIDATION_ERROR`(로그 조회·등록 통지만 `400`, 8.2), dom0-manager `400 InvalidArgument`(8.1) |
| OS별 실제 버전 값 | 제어 계층 | 이미지 빌드 시 실측 |
| 템플릿별 `min_memory_mib` | 플랫폼·백엔드 | 플랫폼 카탈로그 `resource_limits`를 버전별 descriptor로 옮기고(Zephyr는 min=max) Spring 설정이 그 값을 따른다. Zephyr starter 1.0.0은 16 |
| 서비스 토큰 배포·회전 | 공통 | Spring과 dom0-manager에 같은 Bearer 서비스 토큰을 안전하게 배포하고 회전하는 운영 절차 |
| 수명주기·강제 종료 전달 | 제어 계층 + 플랫폼 | retire/restore/purge의 작업 키·operation_id·대상 세대와 개별 force 전달. 보드 종료 정책을 개별 강제 종료 승인으로 사용하지 않음 |

dom0-manager는 보드의 Tailscale 주소에 바인딩하고 `/healthz`를 포함한 내부 API에서 Bearer 서비스 토큰을 검사합니다. Spring은 `DOM0_MANAGER_BASE_URL`에 Tailscale 주소를 설정하고 `DOM0_MANAGER_SERVICE_TOKEN`을 `Authorization: Bearer`로 전송합니다. 연결 실패는 `503 VM_CONTROL_UNAVAILABLE`로 변환합니다.

`vm_id`는 도메인 설정의 `uuid`에 기록하고 `xl list -v`의 `UUID` 열(도메인 handle, `xl list -l`의 `config.c_info.uuid`와 같은 값)로 역조회합니다. 동일 VM의 stop/start 및 retire/restore는 같은 UUID를 유지하며, 템플릿 생성·복제·purge 이후 신규 생성은 새 UUID를 사용합니다.

---

## 14. 변경 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| v1 | 2026-10-16 | 계약 확정안 v1.0, 리뷰 v1.1, 백엔드 VM API 규약 v1, 리뷰 v1.2의 확정 사항 통합 |
| v1.1 | 2026-10-19 | 구현에 맞춰 보완: 공개 VM 객체에 `agent_status`, Spring 공개 state `stopped`(3.3), 템플릿 응답 필드 `version`, `xl` 플래그 없음(`------`) 매핑, `DriverError` 500/501, `/healthz` 본문, Spring 오류 변환표(8.2), 공개 API 응답 형식(5장), `vm_id` 저장 1안 확정, 배포 분리·복구 주체 미확정 항목 추가. Spring 시작은 실행 중이 아닌 도메인을 `409 INVALID_VM_STATE`로 거부, 정지는 이전 상태를 조회해 이벤트 `from`에 기록. Spring이 VM 상태를 주기 조회해 `cause: detected` 이벤트 기록. 템플릿을 플랫폼 기본값(Ubuntu·Debian·Zephyr)으로 교체하고 템플릿·공개 VM 객체에 `guest_kind` 추가 |
| v1.2 | 2026-10-19 | Spring 선발급 `vm_id`를 유일한 영구 식별자·생성 멱등성 키로 확정. 플랫폼 자동 IP 할당, 동기 stop 90초와 Spring 100초 제한, retire/restore/purge 생명주기, Spring `RETIRED` 명세 보존, Tailscale+Bearer 내부 연결을 반영. Spring이 플랫폼 공개 템플릿 ID·버전을 그대로 전달하고 제어 계층 변환을 제거 |
| v1.3 | 2026-10-19 | API 문서 통일(S15P31E110-230): `docs/dom0-backend-http-contract-v1.md`(v1.0 폐기본)와 `docs/backend-vm-api-rules-v1.md`(백엔드 VM API 규약)를 삭제하고 이 문서로 일원화. 코드 기준으로 정정 — dom0-manager는 기존 도메인에 명세와 관계없이 `409 VMExists`(4.2·7장), `InvalidState`는 실행 중 VM 삭제에 반환(8.1절), 템플릿 OS는 Ubuntu·Debian·Zephyr(2.3절) |
| v1.4 | 2026-10-19 | Spring 공개 API 보완: VM 이름에 한글·공백 허용(제어문자·앞뒤 공백 금지, 9장), 공개 VM 객체에 `ipv4` 추가(에이전트 보고 기준, Zephyr는 `null`, 2.2절), dom0-manager `507 InsufficientResources`를 공개 API에서도 `507`로 전달하고 IP 풀 고갈(`ipv4`) 메시지 추가(8.1·8.2절) |
| v1.5 | 2026-10-19 | 공통 Manager와 HTTP 계약을 분리하고 생성/시작·등록 목록·예약 IP·수명주기 세대·Zephyr 종료/메모리의 연결 경계를 명시. Spring의 현재 DTO·설정과 플랫폼 구현은 유지 |
| v1.6 | 2026-10-19 | dom0-manager 오류 code 분리(플랫폼 드라이버 S15P31E110-115 계약) 반영: `401 Unauthorized`, `501 NotSupported`, `503 PlatformUnavailable`, `504 DriverTimeout`과 Spring 변환(8.1·8.2절). 요청 메모리 공통 하한 128 → 16, Zephyr starter `min_memory_mib` 16(2.3절·9장) |
| v1.7 | 2026-10-19 | 플랫폼 드라이버(S15P31E110-115) 반영: 모든 엔드포인트 응답 목록에 `401`·`500`·`501`·`503`·`504` 명시(4장), OpenAPI에 `ServiceToken` bearer 보안 스킴·전역 security 선언, HTTP 모델의 `ResourceError.name`에 `ipv4`와 `memory_mib` 하한 16, 정지된 등록 VM에 다른 생성 명세는 `409 VMExists`(4.2절), 플랫폼 Manager 잠금 대기 한도(`XENOVA_PLATFORM_BUSY_TIMEOUT`, 기본 5초) 초과 시 `503`(재시도 가능)이며 `/healthz`는 바쁜 상태를 장애로 보지 않음(8.1절). 13장에서 해결된 Zephyr 자원 범위·IP 고갈 스키마 항목 정리 |
| v1.8 | 2026-10-19 | 플랫폼이 `create`에 `vm_id`를 필수로 받고 내부 실행 이름·MAC·Linux IP를 발급하게 되어 dom0-manager의 IP 풀 선택·재시도와 `INVALID_SPEC`→501 임시 처리를 제거. 플랫폼 `IP_POOL_EXHAUSTED`→`507 ipv4`, 인스턴스 디렉터리는 `configuration.id`, `/healthz`는 Manager를 거치지 않고 `xl info`(조회 한도)와 registry 저장소 존재만 확인해 조회 한도 안에서 끝남. 13장 HTTP→Manager 입력 항목 해결 |
| v1.9 | 2026-10-19 | Domain-0 보호(S15P31E110-206): nil UUID 대상 호출은 모든 엔드포인트에서 `400 InvalidArgument`, 목록은 domid 0 또는 이름 Domain-0 을 제외(9장) |
| v1.10 | 2026-10-20 | 실보드 `xl` 출력 확인(S15P31E110-36): 3.2절에 `-bp---`(blocked+paused 동시) 관측과 `------` 미관측, Domain-0의 `c_info.uuid` 부재를 기록. 매핑 변경 없음 |
| v1.11 | 2026-10-21 | 공개 VM 객체에 `role`(MQTT `app_id`: `autonomous`·`manual`·`message-board`, 없으면 `null`) 추가, 생성 요청에 선택 필드 `role`, 역할 변경 `PATCH /api/v1/vms/{vm_id}` 추가(2.2·5·9장). Spring 레코드에만 저장하며 dom0-manager 계약은 바뀌지 않음 (S15P31E110-55) |
| v1.12 | 2026-10-21 | 플랫폼 복구 기반(67b0e26) 반영(S15P31E110-245): 오류 본문에 선택 필드 `platform_code` 추가, 복구 기반 오류 코드의 HTTP 매핑표(8.1), 정지 대기 만료의 임시 메시지 판별 명시, stop 대기는 플랫폼 90초 고정이라 platform 모드는 `XENOVA_SHUTDOWN_GRACE=90`만 허용하고 `XENOVA_XL_COMMAND_TIMEOUT`은 Manager 호출을 제한하지 않음을 명시(10장). 자동 복구 주체를 dom0 Recovery Worker로 확정하고 13장 미확정 항목에서 제거(1.2·4.5) |
| v1.13 | 2026-10-21 | 메트릭·복구 이벤트 스키마(S15P31E110-33): xentop 원시값 통계 객체(2.4)와 `GET /api/v1/stats`(4.6), 복구 이벤트 객체(2.5)와 MQTT-12·`GET /api/v1/recovery/events`(4.7), Spring의 저장·사용률 계산·복구 이벤트 기록 규칙(5장). 사용률은 Spring 계산, 측정 실패는 `unavailable`, `source`는 Spring 필터 허용값 `xen`, 장애 단위 `recovery_id`와 `attempt_duration_ms`·`recovery_duration_ms` 분리, 재보충 커서 만료는 `gap: true`로 구분하고 페이지는 `next_after_event_id`로만 진행, `recovery_attempt_id`는 논리적 시도 단위(재전달 재사용), Spring stop은 runtime 유무와 관계없이 전달해 stopped 의도를 기록(4.3·5장), FAILOVER는 VM 이벤트가 아님 |
| v1.14 | 2026-10-22 | 복구 이벤트 `reason` 에 `APP_HEARTBEAT_LOST`(Zephyr VM, D8) 추가, `DOMAIN_ABSENT` 의 60초 유예 명시(2.5). VM 별 건강 스냅샷 `vm.health` 는 MQTT 계약 11.1절 (S15P31E110-124) |
| v1.15 | 2026-10-27 | `role` 허용값 `message-board` 를 `interaction` 으로 바꾼다(2.2·5장, MQTT 계약 v1.4 의 app_id 변경). 예전 값은 `422 VALIDATION_ERROR` (S15P31E110-303) |
| v1.16 | 2026-10-27 | 정지 요청 추적(S15P31E110-305): stop 의 선택 헤더 `Idempotency-Key` 와 작업 상태 응답 `200`·`202`(4.3.1), 원 작업 조회 `GET /api/v1/vms/{vm_id}/control-operations`(4.8), `/healthz` 를 뺀 전 엔드포인트의 선택 요청 예산 `X-Request-Budget-Ms`(4.9, 잠금 대기·하위 호출·xl·xentop), 오류 본문 `retryable`·`operation`, 오류 코드 `404 OperationNotFound`(8.1). 키·예산이 없는 기존 호출의 응답은 그대로 |
| v1.17 | 2026-11-02 | Spring 런타임 캐시(S15P31E110-344): `GET /api/v1/vms`·단건 조회는 마지막 dom0 목록으로 답하고 `observed_at`은 그 목록의 관측 시각, 전용 갱신 루프(조회 후 1초), stale-limit 10초 초과 시 직접 조회·실패 시 오류(5장). 필드 변경 없음 |
| v1.18 | 2026-11-04 | 정지한 Linux VM 의 자원 변경 `PUT /api/v1/vms/{vm_id}/resources`(4.12, S15P31E110-368): 필수 `Idempotency-Key`, 범위 vcpus 1..4·memory_mib 128..1024, 다음 시작부터 적용, 자산·제어 원장 키 조회, `TARGET_CHANGED` 뒤 원 작업 재확인. 재시작(4.2)의 자원 비교 기준을 지금 등록 설정으로 바꾼다. 실행 중 핀·가중치·메모리 변경은 범위 밖 |
| v1.19 | 2026-11-06 | 구현에 맞춤: `role` 값을 `primary_drive`·`backup_drive`·`interaction` 으로(2.2·5·9장, Spring `VmRole`), 복구 이벤트 심각도 `WARN` → `WARNING`(5장), 재보충 `gap` 은 서버 로그 경고일 뿐 이벤트 로그에 남기지 않음(4.7), Spring VM 관리 엔드포인트 수 7 → 8 과 `/api/v1/vms` 아래 그 밖의 공개 엔드포인트 표(5장), 입력 오류 400 예외(로그 조회·등록 통지, 8.2), Spring 명령 응답 대기 100초 → 15분(10장) |
| v1.20 | 2026-11-06 | 사용자 결정 반영: VM 목록·단건 조회 한도 5초를 하위 `list`·`get` 에 `timeout_ms` 로 명시(4.1·4.9, 초과 `504`), 플랫폼 `VM_NAME_CONFLICT` 를 `VMExists` 에서 분리해 `409 VMNameConflict`(8.1)로 내고 Spring 은 `409 VM_NAME_CONFLICT`(생성·시작에서도 성공 아님, 8.2), API 문서 경로(`/docs`·`/redoc`·`/openapi.json`) 기본 비활성·켜도 토큰 검사(4장) |
| v1.21 | 2026-11-12 | USB 휴대폰 화면 중계 상태 조회 `GET /api/v1/display/phone-link` 와 연결번호 발급 `POST /api/v1/display/phone-link/pairing-code`(4.13). 번호는 발급 응답에만 담고 저장·로그·자동 재시도 금지, `Cache-Control: no-store` |
| v1.22 | 2026-11-12 | v1.17의 런타임 자동 갱신 루프를 제거하고 공개 `GET /api/v1/vms` 요청 시 단일 동기화로 변경. 등록 VM과 dom0 런타임을 결합하고 상태 이벤트를 반영한 최신 목록을 반환하며, 경합 VM 제외·조회 실패 시 기존 상태 보존·프로세스 내 동시 요청 직렬화를 명시 |
| v1.23 | 2026-11-12 | VM 상태 5초 주기 조회(`EVENT_VM_STATE_CHECK_INTERVAL_MS`)를 되살린다. 공개 `GET /api/v1/vms` 요청 시 동기화와 함께 동작하며 실행 상태만 다룬다. 보드 등록 목록 → VM 테이블 등록 동기화는 주기 실행 없이 `POST /api/v1/vms/synchronizations` 요청 때만 실행한다 |
| v1.24 | 2026-11-13 | dom0-manager의 생성·정지·보관 오류에 선택 `context` 추가. 기존 원장으로 작업 ID 조회 제공(8.1.1) |
| v1.25 | 2026-11-14 | `POST /api/v1/vms/synchronizations`가 요청 한 번에 보드와 맞춘다: 연속 2회·생성 후 20분 유예 제거, 정리 후 등록 순서, 같은 `vm_id` 복구(`restored`), 작업 중 보류(`skipped`), 결과 항목에 `name`. `missing_from_board` 개수 필드 삭제. RETIRED 행은 이름을 `<이름>~<vm_id 앞 8자>`로 비움(V10) |
| v1.26 | 2026-11-14 | 표시 장치 모드 조회 `GET /api/v1/display/mode` 와 전환 `POST /api/v1/display/mode`(4.14). 보드 명령 `display_mode` 를 그대로 실행하고, mobile 은 USB 링크가 없으면 `409 USB_LINK_ABSENT`, 전환 도중 끊지 않음(최악 240초) |
