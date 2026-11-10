# 실패 private 생성의 자동 수습 — 2026-11-10

## 후보와 제공 범위

아래 구현 검증은 `0969f566` 위 미커밋 작업트리에서 수행했다.
검증한 구현·시험은 `e9500282`, 포장·인계 보완은 `f0f57126`에 기록했다.
`C:/Users/SSAFY/Documents/Codex/2026-11-10/dom0-cpu-clock-defaults/worktree`에서 작업했다.
구현 검증 중 실보드·운영 서비스·기존 VM·운영 설정·E4 정책/원장·commit/push는 변경하지 않았다.
이후 사용자 요청에 따라 커밋·브랜치 push·MR 제출을 별도 수행하며, 제출 상태는 해당 Git 이력과 MR을 따른다.

신규 Linux create에만 명시 opt-in한 자동 private 수습을 연결했다. 일반 호출·원 key의 의미는 유지한다.
`/etc/xenova/creation-reconcile-policy.json`의 시험 프로필 `trial-1-plus-1`은 최초 1회와 조건 변화 시
추가 최대 1회를 허용한다. 이는 승인된 로컬 시험안이며 이미지의 활성 정책이나 운영 기본값이 아니다.
원 정책에 저장된 `cleanup_ms` 길이를 별도 수습 후속 한 번의 시간 예산으로 사용한다.
원 생성/종료 기한의 잔여 시간을 재발급하거나 원 실행을 갱신하지 않는다.

## 실제 호출과 소유

1. `xenova_creation.accept()`가 보호된 opt-in 정책을 읽고 신규 Linux 원 작업에 허용을 기록한다.
   과거 실패 원장은 변환하지 않는다. 미설정은 기존 수동 수습을 유지하고 잘못된 정책은 수락 전 거절한다.
2. 실제 private 준비가 실패하면 최초 오류를 기존 원장에 보존하면서 후속 대기 상태를 함께 저장한다.
3. `Manager._request()`는 원 준비의 FD를 반환한 뒤 `xenova_creation_auto.start()`를 호출한다.
   기존 Linux 슬롯을 상속하는 Python 자식 하나만 시작하고 create 응답은 정리 완료를 기다리지 않는다.
   새 daemon·영구 큐·주기적인 전체 원장 scan·전역 잠금을 추가하지 않았다.
4. 자식 `run()`은 관측 횟수를 원 operation에 먼저 내구 소비하고 현재 정리 receipt/owner를 확인한다.
   기존 private 진단을 거쳐 `xenova_creation.reconcile()`의 고정 계획·삭제·종결을 실행한다.
   자동 수습 ID는 원 operation ID이며 재전달로 새 ID·key·기한을 만들지 않는다.
5. 이후 같은 원 create 재전달은 작은 현재 조건만 비교한다. 조건이 같거나 2회를 소진했으면
   추가 probe·쓰기·자식을 만들지 않는다. 진행 계획이 중단되면 조건 변화와 남은 자동 기회가 있을 때
   같은 계획을 재개할 수 있다. 원 결과 조회는 정리를 시작하지 않는다.

조건은 현재 owner, root inode, 준비 정리 receipt, host/admission 문맥, 기존 수습 진행이다.
외부 FD만 닫힌 경우처럼 이 작은 근거로 변화를 확인할 수 없다면 비싼 전체 FD scan을 반복하지 않고
기존 명시 수습에 남긴다. `observing` 기록은 과거 시작 사실이며 현재 실행/완료 허가가 아니다.
저장/시간 효과가 미확정이면 원 실패와 예약을 유지한다. 부팅 때 일괄 폐기·무한 재시도는 없다.

## 기록·보호·동시성

- 자동 횟수·마지막 조건 hash·최초/마지막 후속 오류만 기존 operation에 고정 크기로 갱신한다.
  관측마다 별도 원장/파일/로그를 쌓지 않는다. 원 생성 오류와 후속 오류를 덮어 합치지 않는다.
- 정상 자동 종결 표시는 기존 최종 수습 저장에 합친다. 관측 횟수의 선행 내구 저장은 생략하지 않는다.
  실행 실패는 기존 응답의 제한된 `diagnostic_warnings`를 쓰며 원 생성 결과를 바꾸지 않는다.
- 동일 단계의 최초 진단을 후속 수습에 전달해 같은 진단을 중복 실행하지 않는다.
  자동 기회 1+1은 전체 Xen/FD 조회 2회를 뜻하지 않는다. 기존 파일별 삭제 직전 사용 검사를 유지한다.
- Linux 슬롯 소유 아래 필수 원장/owner 기록 fsync를 공통 잠금 밖에서 수행하고 다시 대조한다.
  unlink 진입만 짧은 admission으로 보호하고, 삭제 뒤 디렉터리 fsync는 그 밖에서 수행한다.
- 수습 진입에서 원 작업 유일성을 확인한 뒤, 필수 진행 저장에서는 그 원 파일의 전체 digest만 다시
  비교한다. 삭제 항목마다 전체 operation 이력을 반복해서 순회하지 않는다.
- 완료 JSON이 보이더라도 수습 writer가 남아 있으면 예약을 반환하지 않는다. 다른 요청과 같은 key의
  재전달이 느린 writer의 fsync를 대신 기다리지 않도록 소유 상태를 먼저 확인한다.
- 공개/등록된 자산, active/residual owner, 사용 중 inode·mount·loop·신원/사용 관측 미확정,
  host/gate 변경의 기존 보호를 유지한다. optional create journal 1.2도 보존한다.
- 다른 VM의 조회·stop·start·조건부 recovery는 공통 수습 완료를 기다리지 않는다.
  대용량 Linux 준비는 기존 공용 Linux 슬롯의 수락 전 busy 제한을 계속 따른다.
  같은 매체/CPU의 간접 경합이 0이라는 보장은 하지 않는다.

## 로컬 검증과 외부 경계

WSL Ubuntu-24.04/Python 3.12의 독립 소스 사본을 사용했다.
Linux 작업 폴더는 `/home/ssafy/xenova-auto-reconcile-20261110`, 원 로그는 그 아래 `logs`다.
제품 Manager·원장·슬롯·reconcile·실제 파일/inode/FD·thread/flock·별도 Python 프로세스를 사용한다.
Xen·loop·mount/chroot와 작은 Linux 자산만 기존 fixture로 대체한다.

`platform/tests/test_xenova_creation_auto.py`는 실제 create 실패의 별도 자식 처리와 원 응답 독립성,
Debian/Ubuntu 자동 종결, 새 정상 생성, optional 의미/원 journal 보존, 미허용/잘못된 정책,
변화 없는 재전달 무기록, 조건 변화 뒤 2회 상한, 새 boot에도 상한 유지, 공개 자산·열린 FD 보호,
불완전 receipt 회복, unlink 뒤 중단/EIO의 같은 계획 재개, 실행 실패 원인 보존을 시험한다.
느린 필수 원장 저장·최종 저장·private 디렉터리 fsync·가시적 완료의 부모 fsync를 각각 멈춘 동안
B 조회·stop·start와 실제 Manager `recover_once(start-if-absent)`가 완료되는지 확인한다.
이는 Worker의 장애 판단이나 차량/Gateway 승계를 대체한 시험이 아니다.

| 실행 | 결과 |
|---|---|
| 최초 명시 수습·CPU 격리 호환 | 45개 통과, 23.17초 |
| 종합: 자동/명시 수습·생성/삭제/조회·CPU·복구·G1 | 278개 중 277개 통과, 207.93초. G1 문서 목록 누락 1개 실패는 아래 집중 실행에서 해결 |
| 최종 집중: 자동 20개 + 명시 수습 35개 + G1 14개 | 69개 통과, 33.38초. 원 파일 한정 대조와 누락 목록 보완까지 반영 |
| 기존 Worker E4·상태·재합류 기록 최적화 | 83개 통과, 실패/skip 0 |
| 중복 제거 집계 | 361개 사례의 최종 결과 모두 통과. 한 번의 실행에 361개를 돌렸다는 뜻은 아님 |
| recipe 실제 설치 명령·공통 모듈 루프 | core 20개 모듈 설치 후 creation/helper/예시 3개 payload 바이트와 0644 대조 통과 |
| 개발 PYTHONPATH 없는 설치본 import | `python3 -I -B` 통과. import만으로 worker 시작 0, 활성 정책 설치 0 |
| `git diff --check` | 통과 |

관련 명령은 독립 사본의 `platform/tests`에서 실행했다. `PYTHONPATH`는 같은 사본의 tests,
first-install, guests/common, Yocto 플랫폼 files, Manager와 공용 MQTT 패키지 및 기존
`/tmp/xenova-clock-testdeps`만 사용했다. 고정 systemd 검증기 argv는 기존
`/home/ssafy/xenova-build-20261105/TEST_ENV.json`에서 읽었다. 운영 패키지를 업그레이드하지 않았다.

```sh
python3 -B -m pytest -q test_xenova_creation_auto.py test_xenova_creation_reconcile.py \
  test_xenova_preparation_concurrency.py test_xenova_lifecycle_ownership.py test_xenova_read_progress.py \
  test_xenova_cpu_asset_isolation.py test_xenova_cpu.py test_xenova_cpu_cross.py test_xenova_cpu_pinning.py \
  test_xenova_recovery_foundation.py test_xenova_g1_handoff.py
python3 -B -m pytest -q test_xenova_creation_auto.py test_xenova_creation_reconcile.py test_xenova_g1_handoff.py
# 같은 사본 platform/services/dom0-manager/tests에서:
python3 -B -m pytest -q test_recovery_budget.py test_recovery_status.py test_rejoin_record_cost.py
```

최초 종합에서는 기존 G1 runtime 목록의 `xenova_display_sync` 누락과 unit 검증기 미지정이 드러났다.
기존 모듈을 목록에 연결하고 고정 259 검증기를 공급한 뒤, 안내문의 MQTT README/과거 closure 보고서
두 직접 링크 대상 누락도 목록에 보완했다. 원 문서·기존 실패 로그는 보존했다.
최초 자동 시험의 2개 실패는 회복 결과의 `attempt.phase` 대신 `phase`를 읽던 시험 assertion이었다.
제품 복구가 실패한 사례로 집계하지 않으며 실제 응답 형태로 고쳐 최종 실행을 통과했다.
설치 import의 초기 fixture는 이미 recipe가 설치하던 trace 모듈을 누락했다. 개발 경로를 추가하지 않고
실제 recipe 공통 모듈 루프를 실행해 설치본만으로 재검증했다.

## 기록 비용의 로컬 계수

같은 opt-in을 가진 작은 실패 fixture 하나씩을 명시 수습과 자동 수습으로 종결해 계수했다.
원 기록 한 개가 이미 존재하는 시점부터의 비교이며, 운영 보드 성능이나 profile 미사용 대비 수치가 아니다.

| 항목 | 명시 진단+수습 | 자동 후속 |
|---|---:|---:|
| 원 operation 저장 횟수 | 9 | 10 |
| 초기 전체 private 진단 호출 | 2 | 1 |
| 삭제 보호를 포함한 사용 검사 | 7 | 6 |
| 최종 operation JSON 파일 수 | 1 | 1 |
| 최종 파일 크기 | 5,043 bytes | 5,106 bytes |
| 단일 fixture 소요 | 205.946ms | 210.693ms |

자동 수습의 추가 저장 1회는 관측 기회의 선행 내구 소비다. 정상 종결용 별도 쓰기는 없다.
위 시간은 한 표본이며 차이를 성능 보장으로 사용하지 않는다. 변화 없는 deferred 재전달 5회에서
추가 진단·operation 쓰기·자식 실행 0도 시험했다. 작은 원장/owner/receipt 읽기 자체는 남는다.

원 로그/XML·설치 payload·계수와 후보 해시는
`C:/Users/SSAFY/Documents/Codex/2026-11-10/automatic-private-reconciliation`에 보존한다.

## 포장과 미수행

`xenova-platform_1.0.bb`에 helper와 비활성 정책 예시를 설치한다. Manager·creation·automatic helper·
preparation은 같은 하위 후보로 배포한다. 외부 HTTP 소비자의 동시 업데이트는 요구하지 않는다.
최종 rootfs source gate에 creation/automatic 두 모듈을 포함하고, G1 하위 인계 목록에도 helper를 포함한다.
검사에서 발견한 기존 `xenova_display_sync` 인계 누락도 같은 목록에 실제 소스를 연결했다.
보드·전체 이미지·SD·ARM/RPM 재빌드·운영 설치·자동 정책 활성화는 수행하지 않았다.
기존 고정 ARM/systemd 검증기는 unit 정적 검사에만 사용하며 실제 Xen 부팅/운영 수락을 뜻하지 않는다.

완료 원장을 무제한 보존해도 된다는 뜻이 아니다. 원 결과·재전달 보존 기간과 기간 밖 key 처리의
변경은 별도 계약이며, 이번에는 기존 원장을 삭제·축약·이관하지 않았다.
