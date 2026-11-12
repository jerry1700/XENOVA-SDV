# CPU 준비의 실패 private 자산 격리 — 2026-11-10

## 결과와 후보

기준 커밋은 `fd76f2c6ceff86e07d38bb0ea333666d618d6246`이며, 구현·회귀 시험은
`0a1cac70c4c2aef09d8288ef4435defbd31c5cd3`에 기록했다. 검증은 커밋 전 동일한 소스 바이트로 수행했다. 작업 위치는
`C:/Users/SSAFY/Documents/Codex/2026-11-10/dom0-cpu-clock-defaults/worktree`다.
CPU 준비의 전역 차단 범위만 보완했다. 실보드 접속·서비스 조작·자산 폐기는 하지 않았다.
검증 후 사용자 요청으로 구현과 문서를 나눠 로컬 커밋했으며, push는 하지 않았다.

기존 원 `holds()`는 실패 자산의 디스크·IP 예약과 원 UUID 보호에 계속 사용한다.
기존 `guard_stopped()`가 이를 CPU 준비까지 일괄 적용해, 실행자가 없는 실패 Linux private 디스크도
무관한 등록 VM의 부팅 전 CPU 준비를 차단하던 문제를 수정했다.

## 구현 경계

- 변경한 runtime은 `platform/yocto/meta-xenova/recipes-core/xenova-platform/files/xenova_cpu.py` 하나다.
  `CPU._isolated_creations()`가 현재 `cpu-host-prepare` 호출에 한해 사용 부재를 확인하고,
  `CPU.guard_stopped()`가 확인된 원 작업만 CPU 차단 대상에서 제외한다.
- 후보는 `create`/Linux/`RECOVERY_REQUIRED`이며 reconciliation이 없는 원 작업으로 제한한다.
  진행 중·불완전한 수습, 다른 자산 작업과 `RUNNING` 원 작업은 기존 차단을 유지한다.
- 기존 `xenova_creation.diagnose()`의 공개·등록 부재, owner, Xen 디스크 연결, mount, loop,
  inode/열린 FD 검사를 재사용한다. 관측 부재·실패를 사용 부재로 바꾸지 않는다.
- Xen·mount·loop·FD 관측 중에는 기존 `common_fd.outside()`로 공통 잠금을 반환한다.
  재획득 뒤 원장 집합·registry·host boot·admission barrier·CPU 정책·종료 gate와
  private inventory·소유권을 다시 확인한다. 그다음 새 Xen CPU 관측과 기존 정지/실행 보호를 적용한다.
  근거는 이번 호출의 로컬 값이며 다른 호출의 허가·TTL cache로 저장하지 않는다.
- 실패 자산·원 오류·원 key/UUID/spec·원 기한·예약을 수정하지 않는다. 실제 폐기·예약 반환은
  기존 명시 `operation-reconcile`만 수행한다. 자동 정리나 원 생성 재실행을 추가하지 않았다.
- `cpu-policy-set`에는 예외를 적용하지 않는다. 실행 중 관리 VM, 미확정 launch,
  진행 중 CPU/제어/복구 작업과 알 수 없는 도메인의 CPU 준수 보호도 유지한다.
- 일반 create/delete/start/조회·Worker 경로와 잠금 정책은 변경하지 않았다. 기존 준비 슬롯의
  수락 전 busy 거절은 유지하며, 모든 물리 변경을 병렬 실행한다고 보장하지 않는다.
- 기존 `xenova-platform_1.0.bb`가 CPU·creation·preparation 모듈을 이미 `/usr/lib/xenova`로 설치한다.
  신규 모듈·의존성·recipe/unit 변경은 없다. 이번에는 RPM/ARM 빌드·설치 검증을 실행하지 않았다.

## 실제 로컬 검증

WSL Ubuntu-24.04/Python 3.12의 독립된 소스 사본을 사용했다.
Linux 작업 폴더는 `/home/ssafy/xenova-cpu-asset-isolation-20261110`이며,
명령의 실행 위치는 그 아래 `source/platform/tests`다.

```sh
export PYTHONPATH="$PWD:$PWD/../first-install:$PWD/../guests/common:$PWD/../yocto/meta-xenova/recipes-core/xenova-platform/files:/tmp/xenova-clock-testdeps"
python3 -B -m pytest -q test_xenova_cpu_asset_isolation.py \
  test_xenova_creation_reconcile.py test_xenova_preparation_concurrency.py \
  test_xenova_lifecycle_ownership.py test_xenova_read_progress.py \
  test_xenova_cpu.py test_xenova_cpu_cross.py test_xenova_cpu_pinning.py
```

| 검증 | 실제 결과 |
|---|---|
| 수정 전 CPU 모듈 + 새 Debian/Ubuntu 재현 시험 | 2개 모두 `OPERATION_IN_PROGRESS: unfinished asset operation blocks CPU policy`로 예상 실패 |
| 위 8개 파일의 회귀 | 172개 통과, 104.95초 |
| 추가한 retire 경합 시험을 포함한 최종 lifecycle 파일 | 10개 통과, 9.50초 |
| 차단 원 작업 ID 표시까지 반영한 최종 CPU/격리/교차/pinning 4개 파일 | 65개 통과, 50.34초 |
| 세 JUnit 결과를 중복 제거한 전체 사례 | 173개 모두 통과, 실패·skip 없음 |
| `git diff --check` | 통과 |

새 `test_xenova_cpu_asset_isolation.py`는 다음을 실제 Manager 경로로 고정한다.

1. Debian·Ubuntu copy EIO 뒤 private 자산과 원장 바이트/inode를 보존한 채 CPU 준비 성공.
   `holds()`와 IP 예약, 미복사 디스크 약정량은 유지되고 원 create 재전달은 원 오류를 반환한다.
   새 CPU 정책 변경은 계속 거절되며, 명시 수습 뒤에만 예약이 반환된다.
2. 열린 실제 FD, loop 사용, 관측 EIO, active/residual/reconciling 소유자와 진행 중 수습은
   pin 전에 거절한다. 원인 제거 후 정상 준비가 가능하다.
3. 느린 private 사용 검사를 실제 thread barrier로 멈춘 동안 B의 list/get/원 작업 조회와
   실제 stop/control-operation 완료가 먼저 진행된다. barrier 해제 뒤 CPU 준비도 정상 완료된다.
4. 관측 중 host·barrier·CPU 정책·private namespace·원 journal·종료 marker·기한 변경과
   소유자 재등장은 pin 전에 거절한다. 문맥 복원 후 정상 준비를 확인한다.
5. 다음 호출은 사용 여부를 다시 확인한다. 한 번 성공한 뒤 열린 FD가 생기면 거절된다.
   fixture의 새 host boot에서는 이전 실패 자산을 재관측해 준비하되, 원 create의 다른 boot 재개와
   원 기한 갱신은 허용하지 않는다. 이는 실제 보드 재부팅 시험이 아니다.

기존 시험도 재사용했다. 생성 copy barrier와 명시 수습 삭제 barrier 중 B 조회·정지가 진행된다.
`test_xenova_lifecycle_ownership.py`에는 accepted retire의 두 번째 usage 검사 barrier 중
B 조회·정지 완료를 추가했고, purge unlink barrier에도 list/get을 추가했다.
수습/삭제의 중단·상속 FD·같은 key 재개와 현재 CPU 실행 신원·pinning 보호 회귀도 통과했다.

## 대역과 비용·한계

원장·파일·inode·실제 FD·flock·thread·하위 프로세스·원 요청 재전달은 제품 경로를 사용했다.
Xen/CPU hardware·mount/loop/chroot와 작은 Linux 템플릿 payload는 기존 외부 fixture다.
planner·실차·새 이미지 부팅·실제 CPU affinity 적용을 검증한 결과로 확대하지 않는다.

추가 검사는 CPU host 준비 중 해당 실패 작업이 있을 때만 수행한다. 일반 관리 요청에 전체
Xen/FD 검사를 추가하지 않았다. CPU 준비에는 원 사용 진단과 재획득 후 작은 metadata 재확인 비용이
추가되지만, 느린 진단 동안 공통 잠금은 보유하지 않는다. 로컬 barrier 시험은 독립 진행의 증거이며
보드 API 지연·hard realtime 수치나 모든 병렬 생성의 성공을 보장하지 않는다.
지원 경로 밖의 외부 root 변경과 원자적으로 직렬화하지 않는 기존 제한은 유지한다.

초기 시험 사본을 hardlink로 만들었을 때 기존 `st_nlink == 1` 보호가 이를 거절했다.
제품 검사를 완화하지 않고 독립 파일 사본으로 바꿨다. 첫 종합 실행의 자식 프로세스에서는
시험용 `PYTHONPATH`에 제품 모듈 경로가 빠져 실패했으며, 동일 경로를 명시해 위 최종 실행을 통과했다.
이 두 실패 로그도 보존하며 제품 결함 수정이나 통과 결과로 집계하지 않는다.

비밀 없는 원 로그/XML·소스 해시는
`C:/Users/SSAFY/Documents/Codex/2026-11-10/cpu-asset-isolation`에 보존한다.
미수습 private 자산의 자동 폐기·자동 수습 재개·보드 수락은 이번 완료 범위에 포함하지 않는다.
