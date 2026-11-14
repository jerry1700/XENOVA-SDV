# Xenova 배포판 선언·캐시 영향 검증

## 기준과 변경

기준 HEAD는 `5d05101efd39c457cb78a05b473952bc38e7a317`,
브랜치는 `feat/platform/image-build-20261105`다.
선행 USB 테더링 모듈·display 의존성 보완을 포함한 작업트리를 변경 전 기준으로 사용했다.

`conf/distro/xenova.conf`는 고정 `poky.conf`를 계승한다.
기존 `:poky` override를 유지하고 `:xenova`를 뒤에 추가했다.
표시 버전 `6.0.4`·codename `wrynose`는 기반 설정을 유지하며,
별도의 Xenova 제품 릴리스 번호나 Zephyr firmware 버전이 아니다.
컴파일/SDK vendor를 바꾸지 않아 `aarch64-poky-linux` 등 기존 대상·작업 경로를 유지한다.
기존 빌드 설정을 백업하고 관리 블록의 DISTRO 한 줄만 전환했다.
`bblayers.conf`·외부 레이어 lock·공유 캐시 경로는 바꾸지 않았다.
현재 사용 절차는 [빌드 가이드](../platform/build-guide.md)를 따른다.

## 실제 부분 패키징

```text
ID=xenova
ID_LIKE="poky"
NAME="Xenova"
VERSION="6.0.4 (wrynose)"
PRETTY_NAME="Xenova 6.0.4 (wrynose)"
CPE_NAME="cpe:/o:openembedded:poky:6.0.4"
```

기존 CPE와 `ID_LIKE`는 기반 계보를 보존한다.
`/etc/issue`는 `Xenova 6.0.4`와 기존 tty escape를,
`/etc/issue.net`은 같은 이름·버전과 기존 hostname 형식을 유지한다.
`/etc/motd`는 `Welcome to Xenova.` 한 줄이다.

원래 base-files sstate 패키지와 새 패키지의 17개 파일·링크를 직접 비교했다.
`etc/issue`·`etc/issue.net`·`etc/motd`만 변경됐고 나머지 14개는 내용·권한이 같다.
기존 이미지의 계정·SSH·getty·hostname 등 보호 파일 13개도 내용·권한이 유지됐다.
기존 이미지 rootfs는 새로 만들지 않았다.

## 수행한 검증

| 검증 | 결과 |
|---|---|
| `bitbake -e xenova-xen-bringup-image` 전후 대조 | DISTRO·DISTRO_NAME·DISTROOVERRIDES만 변경. 이미지 패키지·기능·systemd·커널 선택·3GiB dom0·vendor·캐시 경로 유지 |
| `bitbake -S none xenova-xen-bringup-image` 전후 | 성공. 두 번 모두 물리 빌드 태스크 실행 0 |
| `bitbake base-files os-release -c package_write_rpm` | 성공. 748개 중 727개 태스크는 재실행 불필요 |
| `bash platform/scripts/setup-yocto.sh --verify` | 고정 레이어·이미지 3종의 `DISTRO=xenova`와 기존 설정 검사 통과 |
| `python3 -B -m unittest discover -s platform/tests -p test_yocto_setup.py -v` | 8개 통과 |
| `python3 -B -m unittest discover -s platform/tests -p test_xenova_branding.py -v` | 5개 통과 |
| `bash -n platform/scripts/setup-yocto.sh`·`git diff --check` | 통과 |

초기 파일 비교는 후처리가 끝난 rootfs와 단독 base-files 패키지의 `/etc/shells`를 비교해 실패했다.
Bash·systemd 후처리로 `shells`·`nsswitch.conf`가 달라진 것을 확인하고,
동일한 단계인 변경 전 sstate 패키지와 새 패키지를 직접 비교했다. 제품 설정을 바꿔 비교를 맞추지 않았다.
서명 비교 CLI의 첫 호출은 빌드 환경 밖에서 실행해 BBPATH 오류가 났으며,
최종 차이 분석은 BitBake의 `bb.siggen.compare_sigfiles`로 완료했다.

## 캐시 재사용 영향

동일한 입력·패키지 목록에서 전체 11,862개 태스크의 sigdata 파일명에 기록된 unihash를 대조했다.
11,764개는 같고 98개는 달랐다. 추가·삭제된 태스크는 없다.
컴파일 태스크 684개 중 서명이 바뀐 것은 5개다. 서명 개수는 시간이나 비용의 비율을 뜻하지 않는다.

| 대상 | 전후 결과 |
|---|---|
| Chromium·Node.js·FastAPI·Pydantic Core·RPi 커널·GCC/Clang cross | 컴파일 서명 동일 |
| Xen·Xen tools | `XEN_DOMAIN=${DISTRO}`에 쓰는 배포판 표기 입력 변경으로 서명 변경 |
| QEMU | Xen tools의 sysroot 의존성 변경이 configure·compile로 전달 |
| base-files·os-release | 안내문·OS 식별 정보 갱신. 위 부분 패키징으로 확인 |
| 플랫폼/Manager 일부 포장·U-Boot 배포·initramfs·최종 이미지 | 후속 의존성·배포 태스크에 영향. 전체 이미지 빌드는 미수행 |

download·sstate를 수동 삭제하거나 초기화하지 않았다.
변경 전 Chromium·Node.js·FastAPI·base-files 패키지 sstate 아카이브가 남아 있음을 확인했다.
BitBake의 stale 정리는 해당 작업의 staging manifest·stamp 정리이며 공유 아카이브 전체 삭제가 아니다.
서명 일치는 캐시 재사용 조건의 근거이며 전체 이미지 성공이나 실제 재빌드 시간을 보장하지 않는다.

## 원자료와 남은 확인

원자료는 WSL의 `/home/ssafy/xenova-distro-20261105-7hx1i2n3`에 보관했다.
전후 정책·sigdata 목록과 비교, 구성 백업, 패키징·setup·회귀 로그,
패키지 비교, 최종 소스 해시를 포함한다. HEAD와 미커밋 diff·새 파일을 함께 보관해 후보를 식별한다.

전체 dom0/최종 이미지 빌드·WIC 검사·SD 기록·실보드 배포·실제 로그인은 수행하지 않았다.
새 이미지에서 SSH/UART/HDMI 로그인과 실제 OS 식별 정보를 확인해야 한다.
게스트 firmware·VM 신원·CPU 정책·네트워크·로그인 인증 경로는 이번 변경 대상이 아니다.
