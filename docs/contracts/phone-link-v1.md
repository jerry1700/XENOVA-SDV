# USB 휴대폰 화면 연결 API 계약 v1

휴대폰을 보드에 USB 테더링으로 연결하면, 휴대폰 DHCP 가 보드 `usb0` 주소를 정하고 그 주소는 연결할 때마다 바뀔 수 있습니다. 관리 콘솔에서 지금 접속 주소를 보고 연결번호를 발급할 수 있도록 Spring 이 dom0-manager 를 대신 불러 줍니다.

- 보드 쪽 계약: [VM 관리 API 계약](vm-api-contract-v1.md) 4.13 (`GET /api/v1/display/phone-link`, `POST /api/v1/display/phone-link/pairing-code`)
- 휴대폰 중계 동작: `platform/services/display/router/deploy/mobile-access/mobile-access.README.md`

```
관리 콘솔 ──사용자 JWT──▶ Spring ──서비스 토큰(Tailscale)──▶ dom0-manager ──▶ 중계 파일 / usb0 / systemctl
```

Spring 은 아무것도 저장하지 않고 dom0-manager 응답 본문을 그대로 돌려줍니다. 인증은 다른 콘솔 API 와 같은 사용자 JWT, 오류 본문은 VM 관리 API 계약 8.2 의 `{code, message}` 입니다.

| ID | 메서드 | 경로 | 요청 | 성공 | 오류 |
| --- | --- | --- | --- | --- | --- |
| REST-38 | GET | `/api/v1/display/phone-link` | — | 200 휴대폰 중계 상태 | 401, 502, 503, 504 |
| REST-39 | POST | `/api/v1/display/phone-link/pairing-code` | — | 200 `{code, expires_at}` | 401, 409, 502, 503, 504 |

두 응답 모두 `Cache-Control: no-store` 입니다.

## REST-38 `GET /api/v1/display/phone-link`

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

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `configured` | bool | 보드에 휴대폰 중계 설정(`/etc/xenova/display-mobile.json`)이 있음 |
| `relay` | string | `listening` 실행 중이고 접속 주소 있음 · `waiting_link` 실행 중이지만 쓸 수 있는 USB 링크 없음(케이블·테더링·주소) · `starting` 시작 중 또는 자동 재시작 대기 · `stopped` 꺼짐(재부팅 뒤 기본값) · `failed` 실패 · `unknown` 그 밖 |
| `url` | string \| null | 휴대폰 브라우저에서 열 주소. `relay` 가 `listening` 일 때만 값이 있음 |
| `usb.interface` | string | 보드 USB 네트워크 장치 이름 |
| `usb.present` | bool | 장치가 있음(테더링 켜짐) |
| `usb.ipv4` | string[] | 보드 쪽 주소(주소/접두어). 휴대폰 DHCP 가 준 값 |
| `usb.gateway` | string \| null | 휴대폰 주소(DHCP 기본 경로의 게이트웨이). 없으면 null |
| `configured_phone_ipv4` | string \| null | 보드 중계 설정의 휴대폰 주소 |
| `subnet_mismatch` | bool \| null | 보드 USB 주소가 설정된 휴대폰 대역 밖이면 true. 휴대폰이 테더링 대역을 바꾼 경우이며 중계는 시작하지 못한다. 판단할 수 없으면 null |
| `pairing.active` | bool | 쓰지 않았고 만료되지 않은 연결번호가 있음. **번호 값은 이 응답에 없다** |
| `pairing.expires_at` | string \| null | 그 번호의 만료 시각(UTC). 없으면 null |
| `observed_at` | string | 보드에서 읽은 시각(UTC) |

## REST-39 `POST /api/v1/display/phone-link/pairing-code`

요청 본문 없음. 새 6자리 연결번호를 만들어 돌려줍니다.

```json
{"code": "123456", "expires_at": "2026-11-12T08:31:05Z"}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `code` | string | 휴대폰 연결 화면에 넣을 6자리 숫자. 발급 후 2분, 5회까지 |
| `expires_at` | string | 만료 시각(UTC) |

- **이미 연결된 휴대폰도 끊깁니다**(중계의 번호 교체 규칙). 콘솔은 누르기 전에 알립니다.
- 번호는 이 응답에만 있습니다. Spring 은 저장·로그하지 않고 "누가 발급했는지"만 로그에 남깁니다. 콘솔도 저장하거나 URL·로그에 넣지 않고 만료되면 화면에서 지웁니다.
- 요청 하나가 앞 번호를 무효로 하므로 Spring 은 요청을 보낸 뒤에는 재시도하지 않습니다(연결 자체가 안 된 경우만 1회).

## 오류

| HTTP | code | 상황 |
| --- | --- | --- |
| 401 | `UNAUTHORIZED` | 로그인 안 됨. 보드에 묻지 않는다 |
| 409 | `PHONE_RELAY_NOT_LISTENING` | (REST-39) 중계가 꺼져 있거나 휴대폰이 연결되지 않음. 번호를 만들지 않았다 |
| 502 | `PHONE_LINK_UNAVAILABLE` | dom0-manager 응답을 해석할 수 없음, 서비스 토큰 설정 불일치 |
| 503 | `PHONE_LINK_UNAVAILABLE` | 보드에 연결할 수 없음, 또는 보드가 상태를 읽지 못함·2초 안에 새 번호가 나오지 않음 |
| 504 | `PHONE_LINK_UNAVAILABLE` | 보드 응답 시간 초과. REST-39 는 번호가 만들어졌을 수 있으므로 상태를 다시 읽고 필요하면 사람이 다시 누른다 |

## 표시 모드 전환 (REST-40, REST-41)

휴대폰 중계는 재부팅 뒤 꺼져 있습니다. 지금까지는 SSH 로 보드에 들어가 `sudo python3 -m xenova_display.display_mode mobile` 을 실행해야 켤 수 있었는데, 콘솔에서 같은 일을 하도록 Spring 이 dom0-manager 를 대신 부릅니다. 휴대폰 모드로 바꾸면 중계가 켜지고 HDMI 가 꺼지며, HDMI 모드는 반대입니다. 따로 "중계 켜기" 버튼은 없습니다.

- 보드 쪽 계약: [VM 관리 API 계약](vm-api-contract-v1.md) 4.14 (`GET`·`POST /api/v1/display/mode`)

| ID | 메서드 | 경로 | 요청 | 성공 | 오류 |
| --- | --- | --- | --- | --- | --- |
| REST-40 | GET | `/api/v1/display/mode` | — | 200 표시 모드 | 401, 502, 503, 504 |
| REST-41 | POST | `/api/v1/display/mode` | `{"mode": "mobile" \| "hdmi"}` | 200 전환 뒤 표시 모드 | 401, 409, 422, 500, 502, 503, 504 |

두 응답 모두 `Cache-Control: no-store` 이고, 본문은 dom0-manager 응답 그대로입니다.

### REST-40 `GET /api/v1/display/mode`

```json
{
  "mode": "mobile",
  "boot_default": "hdmi",
  "services": {"hdmi": "inactive", "mobile": "active", "feed": "active"},
  "hdmi_renderer_ready": false,
  "observed_at": "2026-11-14T09:00:00Z"
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `mode` | string | 지금 표시 모드. `hdmi` HDMI 모니터 · `mobile` USB 테더링 휴대폰 |
| `boot_default` | string | 재부팅 뒤 모드. 지금은 항상 `hdmi` |
| `services.hdmi` | string | HDMI 화면 서비스의 systemd 상태(`active`·`inactive`·`activating`·`deactivating`·`failed`, 못 읽으면 `unknown`) |
| `services.mobile` | string | 휴대폰 중계 서비스의 상태. `active` 면 중계가 켜져 있다 |
| `services.feed` | string | 두 모드가 함께 쓰는 화면 데이터 서비스의 상태. `active` 가 아니면 전환할 수 없다 |
| `hdmi_renderer_ready` | bool | HDMI 화면이 실제로 그려지고 있음 |
| `observed_at` | string | 보드에서 읽은 시각(UTC) |

### REST-41 `POST /api/v1/display/mode`

요청 `{"mode": "mobile"}` 또는 `{"mode": "hdmi"}`. 응답 본문은 REST-40 과 같은 형식의 **전환 뒤** 상태입니다.

```json
{
  "mode": "mobile",
  "boot_default": "hdmi",
  "services": {"hdmi": "inactive", "mobile": "active", "feed": "active"},
  "hdmi_renderer_ready": false,
  "observed_at": "2026-11-14T09:00:05Z"
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `mode` | string | 전환 뒤 표시 모드 `hdmi` · `mobile` |
| `boot_default` | string | 재부팅 뒤 모드. 지금은 항상 `hdmi` |
| `services.hdmi` | string | HDMI 화면 서비스의 systemd 상태 |
| `services.mobile` | string | 휴대폰 중계 서비스의 상태 |
| `services.feed` | string | 화면 데이터 서비스의 상태 |
| `hdmi_renderer_ready` | bool | HDMI 화면이 실제로 그려지고 있음 |
| `observed_at` | string | 보드에서 읽은 시각(UTC) |

- 휴대폰 모드로 바꾸기 전에 휴대폰을 USB 로 연결하고 **USB 테더링**을 켜야 합니다. 없으면 보드가 바로 거절합니다(`PHONE_NOT_CONNECTED`).
- 휴대폰 모드가 되어도 휴대폰이 접속한 것은 아닙니다. 접속 주소와 연결번호는 REST-38·39 로 받습니다.
- 전환은 보통 수 초, 최악 240초 걸립니다. Spring 은 260초까지 기다리고, 요청을 보낸 뒤에는 재시도하지 않습니다(두 번째 요청이 첫 전환과 겹치므로). 실패하면 보드가 이전 모드로 되돌립니다.
- 누가 어느 모드로 바꿨는지 Spring 로그에 남깁니다.

### 오류 (REST-40·41)

| HTTP | code | 상황 |
| --- | --- | --- |
| 401 | `UNAUTHORIZED` | 로그인 안 됨. 보드에 묻지 않는다 |
| 409 | `PHONE_NOT_CONNECTED` | (mobile) 휴대폰 USB 링크·테더링 없음. 아무것도 바꾸지 않았다 |
| 409 | `DISPLAY_MODE_BUSY` | 다른 전환이 진행 중이거나 표시 서비스가 켜지고 꺼지는 중. 잠시 뒤 다시 |
| 409 | `DISPLAY_MODE_NOT_APPLIED` | 전환 실패, 보드가 이전 모드로 되돌림 |
| 409 | `DISPLAY_SETUP_REQUIRED` | 보드 표시 설정 문제(설정 파일·서비스 미설치 등). 보드 작업 필요 |
| 422 | `VALIDATION_ERROR` | `mode` 가 없거나 `hdmi`·`mobile` 이 아님. 보드에 묻지 않는다 |
| 500 | `DISPLAY_MODE_ROLLBACK_INCOMPLETE` | 전환 실패 후 이전 모드로 완전히 되돌리지 못함. 보드에서 표시 서비스 확인 |
| 502 | `DISPLAY_MODE_UNAVAILABLE` | dom0-manager 응답을 해석할 수 없음, 서비스 토큰 설정 불일치 |
| 503 | `DISPLAY_MODE_UNAVAILABLE` | 보드에 연결할 수 없음, 또는 보드가 표시 모드 명령을 실행하지 못함 |
| 504 | `DISPLAY_MODE_TIMEOUT` | 보드 응답 시간 초과. 전환이 끝났을 수 있으므로 REST-40 으로 다시 확인한다 |

## 연결번호를 웹에 보여 주는 근거

로그인한 운영자는 이미 SSH 로 보드의 같은 파일을 읽을 수 있고, 번호는 USB 로 연결된 휴대폰에서만(중계가 `usb0` 에 묶임) 2분·5회 안에 쓸 수 있습니다. 그래서 인증된 콘솔 화면에는 보여 주되, 어디에도 남기지 않습니다.

## 변경 이력

| 버전 | 일자 | 내용 |
| --- | --- | --- |
| v1 | 2026-11-12 | REST-38 상태 조회, REST-39 연결번호 발급 |
| v1.1 | 2026-11-14 | REST-40 표시 모드 조회, REST-41 표시 모드 전환(휴대폰 모드가 중계를 켬). 보드 계약 4.14 |
