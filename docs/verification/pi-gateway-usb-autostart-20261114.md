# Pi 실보드 Gateway 자동 기동 검증

2026-11-14 KST. Raspberry Pi 4 실보드에 `7a6c1302`의
[자동 시작 설정](../../platform/services/gateway/README.md#pi-실차-gateway-자동-시작)을 설치한 상태에서
CP2102 USB 어댑터를 연결해 두고 `systemctl reboot`를 한 번 실행했다.
운영 Gateway는 `warm / serial / feedback-3` 구성이었다.

| 재부팅 후 관측 | 결과 |
| --- | --- |
| 부팅 ID | `3798949c-a74c-462d-b2c4-ca5920476423` → `f1f12009-da60-450e-aad3-4c40851ec96b` |
| 6.656초 | 커널이 CP2102를 `ttyUSB0`에 연결. 고정 `/dev/serial/by-id/…` 링크와 udev의 `SYSTEMD_WANTS=xenova-gateway.service` 확인 |
| 42.140초 | systemd가 Gateway 자동 기동. 확인 시 `active / running`, `NRestarts=0` |
| 48.561초 | Gateway의 로컬 MQTT 접속 확인. 이번 부팅 ID로 생성한 유효한 Zephyr A/B 바인딩 2개 확인 |
| 구동 상태 | Gateway 조회 결과 `SAFE_STOP`, `phase=FAILED`, `output_permitted=false`. 저장된 `rearm_required=true` 확인 |

부팅 시점은 새 부팅의 `journalctl -k -b -o short-monotonic` 및
`journalctl -b -u xenova-gateway -o short-monotonic`으로 확인했다.
서비스와 구동 상태는 `systemctl show xenova-gateway`와
`python3 -m xenova_gateway.client context`로 조회했다.

이 시험은 기존 SD 이미지에 보드별 설정을 설치한 상태에서 수행했다.
새 Yocto 이미지 설치와 실차 주행은 검증 범위에 포함되지 않았다.
재무장과 주행 명령은 보내지 않았다.
