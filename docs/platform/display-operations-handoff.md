# 차량 표시(HDMI 1대 / 휴대폰) 운영 인계 — 2026-11-12

> S15P31E110-394. 표시를 처음 맡는 팀원이 **켜기·확인·전환·장애 점검·되돌리기**를 이 문서만 보고 할 수 있게 정리했다.
> 비밀번호·토큰·연결번호는 이 문서와 채팅·로그·Git에 남기지 않는다.
> "확인 대기"로 표시한 항목은 아직 실물 확인이 남은 것이다(맨 아래 표).

## 1. 한눈에

표시 장치는 **HDMI 모니터 1대** 또는 **휴대폰(S24) 1대** 중 하나만 쓴다. 두 경우 모두 같은 통합 화면(`/display/combined`, 계기판 + 인포테인먼트)을 보여 준다.

```
dom0 feed (127.0.0.1:8082) ─▶ dom0 guest data (192.168.77.1:8085, 토큰) ─▶ Interaction VM 화면 앱 (<VM IP>:8086)
                                                                              │
                         HDMI 모드: dom0 Chromium 키오스크가 열어 HDMI로 출력 ◀┤
                         휴대폰 모드: dom0 휴대폰 서비스(USB, :8084)가 중계 ──▶ 폰 브라우저가 직접 그림
```

- 상태 수집·정리는 dom0, 화면 파일(vm-screens 빌드)은 **Interaction VM**에 있다. 화면 디자인은 frontend 담당, VM 템플릿은 템플릿 담당이다.
- 표시 장치를 바꿔도 VM·주행 모드·Gateway·MQTT는 바뀌지 않는다(393·377에서 확인).
- 케이블을 뽑는 것만으로 HDMI↔휴대폰이 자동 전환되지 않는다. 전환은 터미널 명령으로만 한다.

## 2. 미리 확인할 것

| 항목 | 확인 방법 | 비고 |
| --- | --- | --- |
| Interaction VM이 켜져 있음 | 관리 콘솔 `VM Control` → "Debian Interaction VM" **실행 중** | 꺼져 있으면 시작. 꺼져 있으면 HDMI·폰 모두 화면 대신 대기 안내가 뜬다 |
| VM IP | 같은 카드의 **예약 IPv4** | 템플릿마다 다를 수 있다(11/11 `.5`, 11/12 `.8`). 아래 두 설정의 `ui_origin`과 같아야 한다 |
| 화면 앱 응답 | dom0에서 `curl -s -o /dev/null -w '%{http_code}\n' http://<VM IP>:8086/display/combined` → `200` | dom0에서만 접근된다 |
| dom0 feed | `systemctl is-active xenova-display-feed` → `active` | |

### 설정 파일 (root 전용, **보드마다 따로 만든다**)

이미지에는 코드·서비스·**예제**만 들어 있다. 아래 dom0 표시 설정은 이미지에 들어 있지 않고, 첫 부팅 때 자동으로 만들어지지도 않는다. 새 이미지를 굽거나 새 보드를 쓰면 아래 "처음 설정"을 한 번 실행한다.

| 위치 | 파일 | 표시 관련 값 | 누가 만드나 |
| --- | --- | --- | --- |
| dom0 | `/etc/xenova/display-runtime.json` | HDMI 1대 출력(연결된 모니터의 EDID·해상도), `ui_origin: http://<VM IP>:8086`, `page: /display/combined` | `board_setup` |
| dom0 | `/etc/xenova/display-Xorg.conf` | 예제 `display-Xorg.conf.example` 그대로 | `board_setup` |
| dom0 | `/etc/xenova/display-kiosk.env` (0600) | feed와 같은 `XENOVA_DISPLAY_TOKEN` 한 줄 | `board_setup` (feed 설정에서 복사, 출력하지 않음) |
| dom0 | `/etc/xenova/display-mobile.json` (0600) | `usb_interface`, `phone_ipv4`, `port: 8084`, `ui_origin`. `phone_ipv4`는 폰 대역이 바뀌어도 자동으로 따라간다(!594) | 있으면 `board_setup`이 `ui_origin`만 맞춤. 없으면 `--phone-ipv4`로 만듦 |
| dom0 | `/etc/xenova/guest-display.json`, `/etc/xenova/guest-display/guest-data.token` | dom0→VM 데이터 전달. 토큰은 **다시 만들지 않는다** | 이미지·relay 담당 |
| VM | `/etc/xenova/guest-display-domu.json`, 같은 토큰 | VM 화면 앱 | 템플릿 담당 |

HDMI 서비스는 `display-runtime.json`·`display-Xorg.conf`·`display-kiosk.env` 중 하나라도 없으면 시작하지 않는다(조건부 시작이라 오류 없이 꺼져 있다).

### 처음 설정 (새 이미지·새 보드, 한 번)

1. HDMI 모니터를 연결하고, 관리 콘솔에서 Interaction VM을 켜고 **예약 IPv4**를 확인한다.
2. 미리 보기(아무것도 쓰지 않음):
   ```sh
   sudo python3 -m xenova_display.board_setup --vm-ip <VM IP> --dry-run
   ```
3. 적용:
   ```sh
   sudo python3 -m xenova_display.board_setup --vm-ip <VM IP>
   sudo python3 -m xenova_display.display_mode hdmi
   ```
   - 휴대폰 설정이 아직 없으면 폰을 USB 테더링으로 연결한 뒤 `--phone-ipv4 <폰 주소>`를 더한다(폰 주소: `sudo grep SERVER_ADDRESS /run/systemd/netif/leases/$(cat /sys/class/net/usb0/ifindex)`. 보드에는 폰을 거치는 경로가 없어 route로는 보이지 않는다. 이후 대역이 바뀌어도 자동으로 따라간다).
   - 이미 있는 파일은 지우거나 덮어쓰지 않고 `ui_origin`만 맞춘다. 서비스는 다시 시작하지 않는다.
4. VM을 다른 템플릿으로 바꿔 IP가 달라졌으면 2~3을 새 IP로 다시 실행한다.

자세한 설치: `platform/services/display/router/deploy/guest-display/README.md`, `deploy/mobile-access/mobile-access.README.md`, `deploy/mobile/display-mode.README.md`.

## 3. HDMI 모드 (기본)

### 켜기
- 부팅하면 HDMI가 기본이다(`xenova-display-hdmi.service`, 390에서 재부팅으로 확인). 위 "처음 설정"이 끝나 있어야 한다.
- 휴대폰 모드에서 돌아올 때: `sudo python3 -m xenova_display.display_mode hdmi`

### 정상 확인
- `sudo python3 -m xenova_display.display_mode status`
- 모니터에 통합 화면이 보이고, 오른쪽 STATUS의 주행 모드가 관리 콘솔과 같다(375).
- 화면 상태 파일: `/run/xenova-display-graphics/client/display-runtime.json`의 출력이 `mapped`.

### 장애 점검 (위에서부터)
1. **화면이 아예 안 나옴** → 모니터 전원·입력 확인 → `systemctl status xenova-display-hdmi`.
2. **"연결 대기"/"화면 준비 중"만 보임** → Interaction VM이 켜져 있는지, VM IP와 `ui_origin`이 같은지, `:8086` 응답이 200인지.
3. **값이 "수신 대기"·"미확인"** → 구동 VM이 앱 보고를 안 보내는 상태(안전 정지 등)일 수 있다. 관리 콘솔의 VM 상태와 비교한다. 화면 고장이 아니다.
4. **케이블을 다시 꽂은 뒤 좌우 절반이 바뀌어 보임** → **모니터 전원을 껐다 켠다**(모니터 쪽 현상, 380). 보드는 약 1초 안에 분리를 감지하고 약 11초 안에 다시 그린다.
5. 이상이 계속되면 `journalctl -u xenova-display-hdmi -n 50`.

## 4. 휴대폰 모드

### 전환
```sh
sudo python3 -m xenova_display.display_mode mobile
```
HDMI는 꺼지고 휴대폰 서비스(`xenova-display-mobile`)만 켜진다. 실패하면(폰 미연결 등) 전환하지 않고 HDMI를 그대로 둔다(390).

### 폰 연결 순서
1. 폰을 USB 케이블로 보드에 연결하고 **USB 테더링**을 켠다.
2. 접속 주소: `sudo cat /run/xenova-display-mobile/endpoint.json`의 `url` (예: `http://10.58.23.26:8084/display/mobile`).
3. 연결번호: `sudo cat /run/xenova-display-mobile/pairing-code` (6자리, 2분, 1회용).
   번호가 만료되면 `sudo systemctl kill -s USR1 --kill-whom=main xenova-display-mobile`로 새로 받는다.
4. 폰에서 **평소 쓰는 브라우저 주소창에 직접** 주소를 입력하고 번호를 넣은 뒤 **연결 버튼은 한 번만** 누른다.
5. 가로로 거치한다(운영은 가로 전용, 주소창은 숨김 — 387).

### 다시 연결할 때
| 상황 | 할 일 |
| --- | --- |
| 테더링을 껐다 켬·케이블이 잠깐 빠짐(같은 주소) | 그대로 기다리면 화면이 돌아온다 |
| 다시 연결해서 보드 주소가 바뀜 | `endpoint.json`의 **새 주소만 열면 된다. 번호는 다시 넣지 않는다**(!581) |
| 서비스 재시작·보드 재부팅·번호 교체·8시간 경과 | 새 주소·새 번호로 다시 연결 |
| 폰이 테더링 대역 자체를 바꿈(날이 바뀜 등) | 서비스가 폰 주소를 자동으로 찾는다(!594). 새 주소·새 번호로 연결 |

### 장애 점검
1. **주소가 안 열림** → `ip -4 -br addr show usb0`에 주소가 있는지(없으면 테더링/케이블), `endpoint.json`의 주소를 다시 확인(재연결마다 바뀐다).
2. **"번호가 다르거나 만료됐어요"** → 최신 번호인지, 버튼을 두 번 누르지 않았는지 확인. 이미 연결됐을 수 있으니 주소를 다시 열어 본다.
3. **링크를 눌렀더니 다시 번호를 물음** → 다른 앱/브라우저로 열린 것. 연결한 브라우저에서 주소창에 직접 연다.
4. **"화면 준비 중"** → 연결은 됐고 Interaction VM 화면을 못 받는 상태. VM과 `ui_origin`을 확인.
5. **화면 "연결 끊김 · 현재 값은 미확인"** → 폰↔보드 연결이 끊긴 상태의 정상 안내. 다시 연결하면 최신 상태로 돌아온다(393).
6. 서비스 상태: `systemctl status xenova-display-mobile`, `journalctl -u xenova-display-mobile -n 30`. 로그에는 주소·번호가 남지 않는다.

## 5. 되돌리기

- 휴대폰 → HDMI: `sudo python3 -m xenova_display.display_mode hdmi`. 실패하면 이전 상태로 되돌린다.
- HDMI가 VM 화면 대신 예전 dom0 화면을 열게 하려면 `/etc/xenova/display-runtime.json`에서 `ui_origin`을 빼고 HDMI 서비스를 다시 시작한다.
- 시험용으로 만든 임시 설치(`/opt/xenova/display-phone-tmp`, `/run/xenova-phone-387` 등)는 시험 후 지우고, 제품 설정(`/etc`)은 시험 때 고치지 않는다.

## 6. 확인된 것 / 확인 대기

| 항목 | 상태 | 근거 |
| --- | --- | --- |
| USB 연결·인터페이스·주소 (S24) | 확인 | 348·382 |
| 폰 인증 접속·데이터 연결 | 확인 | 349·385 |
| 폰 통합 화면 (가로) | 확인 | 350·387 (11/12) |
| 폰 10분 안정성·자원 | 확인 | 392 |
| 폰 케이블·브라우저·인증 끊김 안내와 복귀, 제어/VM 영향 없음 | 확인 | 393 (11/11) |
| 연결번호 처음 한 번만 | 확인 | !581, 11/11 실보드(임시 설치), 11/12 정식 경로 배포 |
| 폰 대역이 바뀌어도 자동으로 폰 찾기 | 확인 | !594, 11/12 실보드(임시 설치), 11/12 정식 경로 배포 |
| HDMI 1대 10분 연속·분리 복귀 | 확인(임시 세션) | 380, `docs/verification/display-380-hdmi-acceptance-20261111.md` |
| HDMI·폰 자원·갱신 성능 | 확인 | 379 |
| 외부 웹과 표시 화면 UUID·상태 일치 | 확인 | 375 |
| 상태 수집 최신성 | 확인 | 374·253 |
| HDMI 분리·복귀 때 VM·주행 무영향 | 확인 | 377 (11/11) |
| 제품 명령으로 HDMI↔폰 왕복·실패 복원 | 확인 | 390/351 (11/12, 정식 경로: hdmi 39 s, mobile 13 s, hdmi 29 s, 폰 미연결 시 mobile 거부·HDMI 유지) |
| 재부팅 후 HDMI 기본 | 확인 | 390 (11/12) |
| HDMI 모니터 재조사를 연결 변화 때만 | 확인 | !622 (11/12): Xorg CPU 약 4.9% → 0.7%, 분리 감지 0.5 s 안, 복귀 8.7 s |
| 보드 표시 코드가 platform 최신과 같음 | 확인 | 11/12 정식 경로 배포(`/root/xenova-display-deploy-20261112/`에 백업). 새 이미지 빌드에는 platform 그대로 들어간다 |
| dom0 표시 설정(`display-runtime.json` 등) | **보드마다 만듦** | 이미지·첫 부팅이 만들지 않는다. 새 이미지 후 2절 "처음 설정" 실행 |
| relay 코드 경로 | 참고 | 현재 보드의 relay는 `/usr/lib/xenova-display-guest-relay/<cohort>/` 사본을 쓴다(사본 내용은 정식 경로와 같고 `mobile_upstream.py`만 예전 판). platform의 새 relay·feed 서비스 파일은 `xenova-first-boot.service`가 있어야 시작되므로 새 이미지와 함께 바뀐다 |
| 외부 웹 끊김 때 표시 영향 없음 | **확인 대기** | 377 기준 1 |
| 주행 모드·활성 VM·출력 허가·복구 알림 변화 표시 | **확인 대기** | 376 |

## 7. 알려진 제한

- 세로 화면은 운영 범위가 아니다(글자 줄바꿈이 심하다).
- 폰 화면이 시간 초과로 끊기면 영어 브라우저 오류 문장이 경고에 섞여 나온다(frontend 담당 개선 대상).
- 테더링을 다시 켤 때마다 보드 주소가 바뀌어 접속 주소가 바뀐다. 번호는 다시 넣지 않는다.
- 음원 재생은 Interaction VM 1.0.8 이상에서 된다(1.0.7은 mp3를 내보내지 않음). 자동 재생은 하지 않고 재생 버튼으로 시작한다.
