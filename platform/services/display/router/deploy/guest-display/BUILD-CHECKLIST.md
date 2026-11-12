# HDMI A안 — 이미지 빌드 때 챙길 항목 (S15P31E110-414)

현재 소스는 dom0 relay unit·enable·설정 예제를 `xenova-display` recipe에 연결했다. 구형 이미지를 위한 추가 설치 번들은 [`README.md`](README.md#재부팅-후-기동과-이미지-없는-갱신)에 설명한다. **recipe 연결, 로컬 설치 재생, 보드 적용, 실제 재부팅 수락은 서로 다르다.** 아래 체크는 실제 이미지/설치본에서 확인한다. 4절의 임시 제공 내용은 당시 이력이다.

## 1. dom0 (Yocto 이미지)

| ✔ | 항목 | 위치·값 | 담당 |
| --- | --- | --- | --- |
| ☐ | `xenova_display` 최신 패키지 (414 이후, `guest_frames` 포함) | `/usr/lib/python3.*/site-packages/xenova_display/` | 주소정(코드)·recipe 담당 |
| ☐ | guest data 서비스 unit | `xenova-display-guest-relay.service`: bridge 성공 후 기동, feed 요청, multi-user enable. recipe 연결 완료; 실제 이미지에서 확인 | 주소정·recipe 담당 |
| ☐ | guest data 설정 (A안, 프레임 중계 없음) | `/etc/xenova/guest-display.json` ← `guest-display-data-only.json.example` | 주소정 |
| ☐ | HDMI 런타임 설정: HDMI 1대, `ui_origin`, `page` | `/etc/xenova/display-runtime.json` ← `examples/single-hdmi-plan-a-runtime.json`, **EDID는 설치할 때 실제 값** | 주소정 |
| ☐ | **guest data 토큰** | `/etc/xenova/guest-display/guest-data.token`, root 0600. 새 factory 첫 설치에서 한 번 준비·Debian에 공급. **업데이트·재부팅은 기존 값 보존**, 이미지·Git에 넣지 않음 | 플랫폼 first-boot 공급 |
| ☐ | 방화벽 | `192.168.77.1:8085`와 선택한 Interaction VM의 실제 할당 IP를 대조. 제한 방화벽을 사용하면 역할 교체 시 해당 규칙도 운영자가 정합화 | 배포 담당 |
| ☐ | **HDMI KMS (Pi4)** | 10-28 방식대로 `/boot/overlays/vc4-kms-v3d-pi4.dtbo`를 넣고 `/boot/config.txt`에 `dtoverlay=vc4-kms-v3d-pi4,cma-64`를 쓴다. 2026-11-09 이미지에는 이 설정이 빠져 있다. 그래서 `/dev/dri`, HDMI 커넥터, EDID가 없고 `xenova-display-hdmi`를 시작할 수 없다. **11-09 보드에 수동으로 넣었고 재부팅 뒤 HDMI-A-1이 잡혔다(§4)** | 박신영(이미지) |
| ☐ | **휴대폰 USB 테더링 네트워크** | `/etc/systemd/network/20-xenova-usb-phone.network`: rndis_host·cdc_ncm·cdc_ether 링크는 DHCP로 IP만 받고 gateway·routes·DNS·NTP는 받지 않는다. 이 파일이 없으면 `80-wired.network`가 휴대폰 기본 경로를 metric 10으로 잡아 wlan0(600)보다 우선이 된다. 11-09 보드에 수동으로 넣었다(§4) | 박신영(이미지) |
| ☐ | **HDMI 모드 guard** | `/usr/lib/systemd/system/xenova-display-hdmi.service.d/60-display-mode.conf`. 레시피는 de4cdf69(11-05)부터 설치하지만 현재 이미지에는 없다. 없으면 `display_mode`가 `DISPLAY_MODE_CHECK_FAILED`로 멈춘다. 11-09 보드에 수동으로 넣었다(§4) | 박신영(이미지 재빌드) |
| ☐ | 휴대폰 접속 설정 | `/etc/xenova/display-mobile.json`(root 0600, `usb_interface`, `phone_ipv4`, `port`). **이미지에 넣지 않는다.** 설치할 때 실제 휴대폰 IP로 만든다 | 주소정(값) |
| ☐ | HDMI 설정 파일 | `/etc/xenova/display-runtime.json`(실제 EDID), `display-Xorg.conf`, `display-kiosk.env`. 2026-11-09 보드에는 셋 다 없다 | 주소정(값)·배포 담당 |
| ☐ | dom0 CPU 여유 | A안은 dom0에서 Chromium을 돌린다. 2026-11-09 측정: 평소 load 약 10, Chromium 실행 중 load 약 21, idle 1%. GPU가 없는 fbdev에서는 화면을 다 그리지 못했다 | 박신영과 협의 |

## 2. VM3 (Debian 템플릿)

2026-11-09 정수환이 Debian Interaction 템플릿(1.0.6)의 화면 설치 구성을 platform에 병합했다(`a612bcbc`, `platform/guests/common/guest_display_template.py`). 템플릿을 다시 빌드·배포한 뒤 아래 항목과 §3을 실제 보드에서 다시 확인한다.

| ✔ | 항목 | 위치·값 | 담당 |
| --- | --- | --- | --- |
| ☐ | 새 apt 패키지 | **없음** (python3만 사용, 이미 있음) | — |
| ☐ | 계정 | 시스템 계정 `xenova-display` (로그인 셸 없음) | 박신영 |
| ☐ | Python 모듈 (표준 라이브러리만) | `/opt/xenova/display-guest/xenova_display/`: `__init__.py`, `capture_limits.py`, `mjpeg.py`, `guest_sender.py`(토큰 읽기), `guest_app.py` | 주소정(파일)·박신영(설치) |
| ☐ | 화면 빌드 결과 | `/opt/xenova/display-web` (`index.html`, `assets/`), **빌드 커밋·해시 기록** | 프론트 담당(빌드)·박신영(설치) |
| ☐ | 설정 | `/etc/xenova/guest-display-domu.json` ← `domu/guest-display-domu.json.example` | 주소정 |
| ☐ | unit·target | `xenova-guest-display-app.service`, `xenova-guest-display.target` enable | 박신영 |
| ☐ | **guest data 토큰** | `/etc/xenova/guest-display/guest-data.token`, xenova-display 0600, **dom0와 같은 값**. 기존 private root 공급자가 실제 할당 IP와 보드 토큰을 전달. 공용 템플릿에는 비밀 없음 | 박신영 |
| ☐ | 템플릿 버전·lock·`defaults.json`·첫 설치 이미지 재생성 | Debian 1.0.7 템플릿 생성·검증 완료. lock/defaults/조립 예제는 1.0.7과 §6 manifest hash로 정합화. 보드 catalog 게시와 실제 부팅 검증은 별도 | 박신영 |

## 3. 빌드 뒤 확인

2026-11-11 소스의 조립 검사는 relay/feed/check unit·enable·모듈·설치 도구와 공용 이미지의
활성 설정/비밀 부재를 확인한다. 새 factory의 토큰·relay·binding 공급과 첫 부팅 뒤 연결 검사는
[현재 절차](README.md#표시-담당-선택과-설치-확인)를 따른다. 아래 실제 보드 체크는 로컬 통과로 대신하지 않는다.


| ✔ | 확인 |
| --- | --- |
| ☐ | VM3 `xenova-guest-display-app` active, dom0에서 `http://192.168.77.20:8086/display/mobile` 200 |
| ☐ | 다른 domU에서 같은 주소 요청 → 403 |
| ☐ | dom0 `192.168.77.1:8085`는 토큰 없으면 401 |
| ☐ | `display_mode hdmi` → HDMI 1대 준비 완료, 화면이 가로로 표시 |
| ☐ | VM3 재시작 중 HDMI 화면: 연결 끊김 표시 → 복귀 |
| ☐ | 부팅 직후: dom0 guest data 서비스는 부하가 높으면 **약 30초 뒤에** 8085를 연다(2026-11-09 load 10~12에서 import 21초, 수신 29초). 그동안 화면은 연결 대기로 보이고, 열린 뒤 복귀해야 한다 |
| ☐ | 토큰·설정이 이미지·로그·Git에 남지 않음 |
| ☐ | relay enable 링크·bridge Requires/After·전용 import 경로, restart 뒤 data 200/무인증 401. 기존 feed PID·토큰·활성 설정 보존 |
| ☐ | 실제 재부팅 후 relay 자동 기동 (enable·restart 확인만으로 이 항목을 완료하지 않음) |

## 4. 과거 임시 제공 이력 — 현재 설치 지시 아님

아래는 최초 수동 제공 당시 기록이며 후속 갱신의 삭제 지시가 아니다. 토큰·설정을 재생성하거나 지우는 절차로 사용하지 않는다. 후속 Debian 1.0.6 검증은 `xenova-test-docs/2026-11-09-debian-interaction-1.0.6-a612bcbc/result.md`에 보존한다. dom0 relay 영구 전환의 실제 적용 결과는 별도 갱신 기록을 따른다.

| 날짜 | 위치 | 내용 | 되돌리기 |
| --- | --- | --- | --- |
| 2026-11-09 | VM3 | 시스템 계정 `xenova-display`. `/opt/xenova/display-guest`: 모듈 5개, 커밋 `c6978ced`. `/opt/xenova/display-web`: vm-screens 빌드, 커밋 `c509b733`, `index.html` sha256 `d33ec04774c7…`, `index-Dnfi9W5P.js` sha256 `5ec231c39fe5…`. 설정 `/etc/xenova/guest-display-domu.json`. 토큰 `/etc/xenova/guest-display/guest-data.token`(xenova-display 0600, 임시 값). unit은 `/etc/systemd/system/xenova-guest-display-app.service`이고 **enable하지 않음**: VM3가 재시작되면 `systemctl start`를 다시 해야 한다. 표시 파일: `/opt/xenova/display-guest/.s15p31e110-414-temporary` | `systemctl stop xenova-guest-display-app` → unit 파일 삭제, `daemon-reload` → `/opt/xenova/display-guest`, `/opt/xenova/display-web`, `/etc/xenova/guest-display-domu.json`, `/etc/xenova/guest-display` 삭제 → `userdel xenova-display` |
| 2026-11-09 | dom0 | `/opt/xenova/display-414`: `xenova_display`, 커밋 `c6978ced`, 별도 PYTHONPATH, 시스템 site-packages는 그대로. 설정 `/etc/xenova/guest-display.json`(데이터 전용). 토큰 `/etc/xenova/guest-display/guest-data.token`(root 0600, VM3와 같은 임시 값). 서비스는 `systemd-run`으로 띄운 임시 unit `xenova-display-guest-data-414`라 **재부팅하면 사라진다**. feed 토큰은 `/etc/xenova/display.env`를 EnvironmentFile로 읽는다. kiosk·HDMI 설정은 바꾸지 않았다 | `systemctl stop xenova-display-guest-data-414` → `/opt/xenova/display-414`, `/etc/xenova/guest-display.json`, 토큰 파일 삭제 → 비어 있으면 `/etc/xenova/guest-display` 삭제 |
| 2026-11-09 11:26 | dom0 `/boot` | **Pi4 KMS** (박신영 확인: 재부팅 없이 설정만 넣기). `/boot/overlays/vc4-kms-v3d-pi4.dtbo`(3913 B, sha256 `155ec071…`, 10-28 빌드와 같은 파일)를 추가했다. `/boot/config.txt`는 247번째 줄 `dtoverlay=vc4-kms-v3d`를 `dtoverlay=vc4-kms-v3d-pi4,cma-64`로 바꿨다. 결과 sha256은 `9f8dc214…`로 10-28 최종본과 같다. 사전 확인: 바꾸기 전 config(`d12f36ef…`)와 DTB(`75761b73…`)가 10-28 원본과 같았다. `boot.scr`은 `dom0_mem=3072M` 한 줄만 다르고, 펌웨어 DTB(`fdtcontroladdr`)를 그대로 쓴다. 11:45 재부팅(팀원 요청) 뒤 결과: `/dev/dri/card0`, `HDMI-A-1` connected(EDID `92a52d12…`, 1920×1080), VM 4대·서비스 정상. `v3d`는 probe에 실패해 3D 가속은 없다 | `cp /root/xenova-hdmi-backup/20261109-112607/config.txt /boot/config.txt && sync` 후 재부팅. 부팅이 안 되면 SD 카드에서 `/boot/config-before-kms-20261109.txt`로 config.txt를 덮어쓴다 |
| 2026-11-09 11:45 | dom0 `/etc/systemd/network` | `20-xenova-usb-phone.network`(0644, sha256 `1c1bc7190ee5…`): `[Match] Driver=rndis_host cdc_ncm cdc_ether`, `[Link] RequiredForOnline=no`, `[Network] DHCP=ipv4`, `LinkLocalAddressing=no`, `IPv6AcceptRA=no`, `[DHCPv4] UseGateway=no`, `UseRoutes=no`, `UseDNS=no`, `UseNTP=no`, `UseDomains=no`. 재부팅과 휴대폰 재연결 뒤에도 기본 경로가 wlan0으로 유지되는 것을 확인했다 | `rm` 파일 → `networkctl reload` |
| 2026-11-09 11:56 | dom0 `/etc/xenova` | `display-mobile.json`(root 0600): `usb_interface: usb0`, `phone_ipv4: 10.104.83.148`, `port: 8084`. 휴대폰 IP가 바뀌면 이 값을 고쳐야 한다. 보드 usb0 주소는 연결할 때마다 바뀔 수 있지만, 접속 서비스가 시작할 때 다시 읽는다 | `rm` 파일 |
| 2026-11-09 12:04 | dom0 `/usr/lib/systemd` | `xenova-display-hdmi.service.d/60-display-mode.conf`(sha256 `f9c20dc45cae…`): 저장소 `deploy/mobile/60-display-mode.conf`와 같은 내용에 주석 한 줄을 더했다. 그 뒤 `daemon-reload`했다. 결과: `display_mode mobile`이 `applied: true`를 반환했다 | `rm` 파일 → `systemctl daemon-reload` |
| 2026-11-09 11:45 | 재부팅 뒤 상태 | dom0 임시 unit `xenova-display-guest-data-414`는 사라졌다. VM3 `xenova-guest-display-app`은 enable하지 않아서 꺼져 있다. 휴대폰 모드 marker는 `/run`에 있어서 재부팅하면 HDMI 기본으로 돌아간다 | — |

## 5. 2026-11-09 후속 제공 — dom0 relay 영구 서비스

Debian 첫 설치 선택은 `linux/debian-13-20261015/1.0.6`이다. manifest SHA-256은 `144ebb333832e2a3c56c08bdc0d178462160b532fae295bb0a900c9344a133e9`, template archive SHA-256은 `272f7dd34ead962adddc8779b8c76e52e4f1c65948bbb175e1c9aad4a3a3e731`이다. 화면 자산은 frontend `447d0d06dea1e0e27eafcbd0b80788c1fe97266f`의 vm-screens 빌드이며 tree SHA-256은 `b3bff1d0bd36705820d00d5243fffd4fc2437ed5124f1d5d976dcbb32eb36b9f`다. recipe를 다시 돌려 새 산출물을 만들면 hash를 재확인한다. 버전 문자열만 같다고 다른 산출물을 이 lock으로 통과시키지 않는다.

이미지 재빌드 없이 board `100.108.226.65`에 정규 unit과 relay 전용 추가 번들 14개 파일(24,058 bytes 압축)을 설치했다. runtime은 `/usr/lib/xenova-display-guest-relay/58e471353d0f0b55/`이며 기존 site-packages를 덮지 않는다. unit은 `enabled/active`, bridge `Requires/After`와 `multi-user.target.wants` 링크를 실제 PID1에서 확인했다. 기존 임시 서비스는 inactive다.

서비스 restart 전후 토큰·활성 설정의 내용/inode/소유/권한, feed PID, HDMI 상태가 유지됐다. 8085 무인증 401, 인증 요청과 VM3 경유 presentation 200, VM3 UI 200을 확인했다. 최초 상태는 사용자 재부팅 뒤 relay 부재/VM3 presentation 503이었고, 설치 뒤 200으로 회복했다. 토큰 재발급·VM 재기동은 하지 않았다.

recipe do_install 재생·별도 설치 경로 import·기존 데이터 경로 회귀 47개를 로컬에서 통과했다. 로컬 unit 로더는 systemd 255.4이며 실제 보드 PID1은 259.5다. **새 RPM/전체 이미지 빌드와 이 설치 이후의 실제 재부팅 시험은 미수행**이다. 결과 원본: `xenova-test-docs/2026-11-09-dom0-guest-relay/result.md`. 설치 receipt와 원상 복구 자료는 보드 `/root/xenova-updates/dom0-guest-relay-20261109/`에 보존한다.

## 6. Debian Interaction 1.0.7 빌드

WSL2 Ubuntu 준비 호스트에서 공식 Debian 13 ARM64 입력의 SHA-512를 `sources.lock.json`과 대조한 뒤 새 템플릿을 생성했다. 기존 1.0.6은 수정하지 않았다.

- template manifest SHA-256: `2fa9454b4a528a6b47e68179c0fe2ca6a63f8d3e6de7fd618549e3f8af5fa689`
- transfer archive SHA-256: `e3c5e88767cb6d42da6cdfc96acdd339068582c12962ddc9e746c11c2fa8349a` (`debian-13-arm64-1.0.7.tar.zst`, 495 MiB)
- root SHA-256: `667a94887874223edccba5f9791e4054e1ee8f37fe88589656000f8383aae594`
- frontend revision: `b4f0d9a163fcaf94a08fa9b6e97986cc4bf69d5a`
- frontend tree SHA-256: `1f4493c495b9286cacd83755408e762382ebe18924c339cefb8a8d7598ab06bf`
- frontend bytes: `932255`
- `audio/interaction-theme.mp3`: 289226 bytes, SHA-256 `a5acc1edfd36502a024116fdff8b5b5877763563fb9c29ce172aa31dc662199c`

`verify-xenova-template.py`의 전체 root 해시, read-only ext4 검사, Xenova 준비 상태와 공유 신원 제거 검사를 통과했다. MP3와 `xenova-guest-display.target`, `xenova-guest-display-app.service` enable 링크도 root.raw 내부에서 확인했다. 실제 보드 catalog 게시, VM 부팅, SSH listener와 HDMI 재생 검증은 아직 수행하지 않았다.

## 7. Debian Interaction 1.0.8 빌드

1.0.7 보드 검증에서 음원이 재생되지 않았다. 화면은 `public/`의 고정 경로 `/audio/interaction-theme.mp3`를 불렀지만, VM guest app은 `/assets/` 아래 허용 확장자만 서비스해 404를 냈다. 이미지 안에 MP3가 있는지만 확인하고 실제로 서비스되는지는 확인하지 않아 놓쳤다.

- frontend `40e5980f`: 음원을 import해 `assets/interaction-theme-<hash>.mp3`로 내보낸다.
- platform: `1a6dec7f`(guest app·휴대폰 relay가 `/assets/*.mp3` 허용)가 들어간 `platform` 기준으로 빌드했다.
- 템플릿 준비 도구가 guest app의 `ASSET` 허용 규칙을 읽어, 서비스할 수 없는 화면 파일(`index.html`, `favicon.*` 제외)이 있으면 빌드를 거부한다. 1.0.7 화면 빌드(`b4f0d9a1`)는 이 검사에서 `audio/README.txt`로 거부되고, `40e5980f`는 통과한다.

WSL2 Ubuntu 준비 호스트에서 1.0.7과 같은 공식 Debian 입력(`sources.lock.json` 대조)과 명령으로 생성했다. 기존 1.0.7은 수정하지 않았다.

- template manifest SHA-256: `69b5d0677090f57f2ecc2d8aa245c4385a1abbe892e20d5b18209e57ff13b36e`
- root SHA-256: `da4083afe63ef917e56d0b5712ccd6379c97abf1d7e955a8be13c31223492913`
- board bundle SHA-256: `97888631cc2fe28e620954f2d961d88cb9c87a4f7441bb58d9855ef1e2f44418` (`debian-13-arm64-1.0.8.tar.gz`, GNU sparse tar, 534267491 bytes). 보드에 zstd가 없어 gzip으로 만들고, busybox tar 대신 키트의 `extract-template.py`(Python tarfile, sparse 유지)로 푼다.
- frontend revision: `40e5980ff024eedc529c40ccc9e2c8eb57366e6f`
- frontend tree SHA-256: `0278a688397ef6069151819524670554b7b23496ef9af6cb2cee496661cd15f4`
- frontend bytes: `931835`
- `assets/interaction-theme-BL9Fpv_x.mp3`: 289226 bytes, SHA-256 `a5acc1edfd36502a024116fdff8b5b5877763563fb9c29ce172aa31dc662199c`(1.0.7과 같은 음원)

`verify-xenova-template.py`를 통과했다(`xenova_layout`, `shared_guest_identity_absent` true). root.raw 안에서 `/opt/xenova/display-web/assets/interaction-theme-BL9Fpv_x.mp3`, mp3를 허용하는 `guest_app.py`, `xenova-guest-display.target` enable 링크를 확인했다. 번들은 키트 스크립트로 다시 풀어 `template.json`과 root.raw 해시가 위 값과 같음을 확인했다. 보드 catalog 게시, VM 부팅, HDMI 재생 검증은 아직 수행하지 않았다(`guest_boot_tested`, `ssh_listener_tested` false).
