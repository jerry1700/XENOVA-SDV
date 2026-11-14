# VM 부하·장애 주입 명령 API v1

**대상:** 사용자 JWT로 호출하는 Spring Backend REST API  
**MQTT 연계:** [`mqtt-contract-v1.md`](mqtt-contract-v1.md)의 MQTT-04·05

## 1. 공통 처리

- 모든 경로는 사용자 JWT 인증이 필요하다.
- 명령 제출 성공은 `202 Accepted`이며, 응답의 `command_id`로 상태를 조회한다.
- 백엔드는 MQTT 발행 전에 명령을 `PENDING`으로 저장한다. 발행에 실패하면 해당 행을 제거하고
  `503 MQTT_UNAVAILABLE`을 반환한다.
- Linux 부하 명령은 v1 Agent heartbeat가 신선하고 `boot_id`가 있어야 한다.
  없거나 만료되면 `409 AGENT_UNAVAILABLE`이다. Zephyr 연산 지연은 v2 telemetry heartbeat를 사용한다.
- MQTT 토픽은 `xenova/v1/vms/{vm_id}/command`, QoS 1, retain=false다.

## 2. CPU 부하

```http
POST /api/v1/vms/{vmId}/stress/cpu
Content-Type: application/json
```

```json
{"workers":1,"load_percent":80,"duration_s":30}
```

| 필드 | 범위 | Linux |
| --- | --- | --- |
| `workers` | 1..4 | `stress.parameters.cpu_workers` |
| `load_percent` | 10..100, 선택 | 호환성을 위해 입력은 받지만 사용하지 않으며 응답은 null |
| `duration_s` | 1..120 | 실행 시간 |

Zephyr VM은 `400 UNSUPPORTED_GUEST_KIND`이며 `/faults/auto-delay`를 사용한다.

## 3. 메모리 부하

```http
POST /api/v1/vms/{vmId}/stress/memory
Content-Type: application/json
```

```json
{"memory_mib":8,"duration_s":30}
```

- Linux 전용이며 `memory_mib`는 1 이상, **그 VM 메모리의 70%(소수점 버림)와 512 중 작은 값 이하**다. 512는 에이전트 자체 상한(`MAX_MEMORY_MIB`)과 같아 어떤 VM에서도 넘지 않는다. 넘으면 에이전트에 보내지 않고 `422 VALIDATION_ERROR`("이 VM에는 최대 N MiB까지…"). 예: 256 MiB VM → 179, 512 MiB VM → 358, 832·1024 MiB VM → 512. MQTT action은 `stress`다.
- Zephyr VM은 `400 UNSUPPORTED_GUEST_KIND`이며 `/faults/auto-delay`를 사용한다.

## 4. Zephyr AUTO 연산 지연

```http
POST /api/v1/vms/{vmId}/faults/auto-delay
Content-Type: application/json
```

```json
{"delay_ms":50,"duration_s":30}
```

- `delay_ms`: 1..100
- `duration_s`: 1..120
- Zephyr VM 전용이며 MQTT action은 `auto_delay`다.
- Linux VM은 `400 UNSUPPORTED_GUEST_KIND`다.

## 5. Linux Agent 종료

```http
POST /api/v1/vms/{vmId}/faults/agent-exit
```

- 요청 본문은 없다.
- Linux VM 전용이며 MQTT action은 `agent_exit`, parameters는 빈 객체다.
- Agent heartbeat가 온라인이고 `boot_id`가 있어야 한다.
- Zephyr VM은 `400 UNSUPPORTED_GUEST_KIND`다.
- 대상 Agent에서 `XENOVA_AGENT_DEMO_COMMANDS=1`이어야 실제 종료된다(배포 유닛이 항상 1로 켠다. 그 전 이미지는 꺼져 있을 수 있다). 비활성화 상태이면
  명령 결과가 `REJECTED`와 `DEMO_DISABLED`로 저장된다.
- Agent는 `ACCEPTED` 결과의 MQTT PUBACK을 확인한 뒤 종료한다. 이후 heartbeat 손실은 플랫폼
  Recovery Worker의 자동복구 판정 대상이다.

## 5-1. VM 강제 종료 (장애 주입)

```http
POST /api/v1/vms/{vmId}/faults/domain-kill
```

- 요청 본문은 없다. 게스트 종류와 관계없이 쓴다 (에이전트 명령이 아니라 dom0 동작이다).
- 백엔드는 dom0-manager `POST /api/v1/vms/{vm_id}/kill` 을 부른다. dom0-manager 는 `xl destroy` 로 도메인만 없애고
  등록·디스크·의도(desired=running)는 바꾸지 않는다. 그래서 정지(`/stop`, 의도를 stopped 로 기록)와 달리 복구 워커가
  `DOMAIN_ABSENT`(60초 유예) 로 판정해 다시 띄운다.
- 실행 중이 아니면 `409 INVALID_VM_STATE`, 등록이 없으면 `404 VM_NOT_FOUND`.
- 성공하면 `202` 와 `{operation_id, vm_id, submitted_at}` 를 준다. 명령 상태 조회(7장)의 대상이 아니다 — 결과는 VM 상태
  (`vm.state.changed`)와 복구 이벤트(`recovery.*`)로 본다. 요청 자체는 `demo.command.sent`(`command=force_kill`)로 기록한다.
- 사용자 정지가 아니므로 `user_stop` 상태 변경으로 기록하지 않는다.

## 6. 제출 응답

CPU·메모리 응답은 다음 형식이다. `amount`는 CPU에서는 workers, 메모리에서는 MiB다.
Linux Agent 계약에는 duty cycle이 없으므로 `load_percent`는 null이다.

```json
{
  "command_id": "...",
  "operation_id": "...",
  "vm_id": "...",
  "type": "CPU",
  "amount": 1,
  "load_percent": null,
  "duration_s": 30,
  "submitted_at": "2026-11-07T06:00:00Z",
  "expires_at": "2026-11-07T06:00:30Z"
}
```

`auto-delay` 응답은 `type`, `amount`, `load_percent` 대신 `delay_ms`를 제공한다.
`agent-exit` 응답은 추적용 `command_id`, `operation_id`, `vm_id`, `submitted_at`, `expires_at`을 제공한다.

## 7. 명령 상태 조회

```http
GET /api/v1/vms/{vmId}/commands/{commandId}
```

```json
{
  "vm_id": "...",
  "command_id": "...",
  "operation_id": "...",
  "status": "RUNNING",
  "submitted_at": "2026-11-07T06:00:00Z",
  "started_at": "2026-11-07T06:00:01Z",
  "finished_at": null,
  "duration_ms": null,
  "result": {"cpu_workers":1,"memory_mib":0,"duration_s":30},
  "error": null,
  "received_at": "2026-11-07T06:00:01Z",
  "contract_violation": false,
  "violation_detail": null
}
```

상태는 `PENDING`, `ACCEPTED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `REJECTED`다.
없는 `(vm_id, command_id)`는 `404 COMMAND_NOT_FOUND`다. `result`와 `error`는 MQTT-05의 JSON을
그대로 객체로 반환한다.

QoS 1 동일 재전달은 상태를 바꾸지 않는다. `RUNNING → SUCCEEDED/FAILED`만 정상 실행 전이다.
종결 상태 뒤 다른 종결 상태를 받으면 원래 종결 결과를 보존하고 `contract_violation=true`와
`violation_detail`을 기록한다.
