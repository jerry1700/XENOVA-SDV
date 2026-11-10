# 개발용 Linux 프로세스 CPU 관측

`process_cpu_probe.py`는 운영자가 별도로 실행하는 stdlib 개발 도구다. Manager의
요청별 `last_trace`와 달리 선택한 worker·Manager·Gateway 프로세스 계보의 Linux CPU
ticks를 같은 유한 관측 기간에 기록한다. Xen guest CPU·실시간 보장·주행 준비 판정은 아니다.

제품 런타임에서 import하거나 자동 실행하지 않는다. 서비스·recipe·런타임 의존성에 넣지
않으며 health/READY/주행 권한·clock/E·lock 판정의 입력으로 사용하지 않는다. 현재 Yocto
`xenova-dom0-manager_0.1.0.bb`는 `dom0_manager`만 복사하므로 이 tools 폴더는 설치 대상이 아니다.
인증 환경·cmdline·토큰·MQTT payload를 읽지 않고 nice/affinity/서비스 상태를 변경하지 않는다.

## 별도 원인 분석 실행

Root가 검토한 두 Python 파일만 새 개발 디렉터리에 복사하고 원 SHA256을 확인한다.
계측은 Linux `/proc`를 쓰며 Windows 실행을 지원한다고 주장하지 않는다. 예시는 실보드에서
사용자가 승인한 분석 창에만 수행한다. 단위 시험도 실제 서비스 import 없이 가상 `/proc`만 쓴다.

```sh
TOOLS=/tmp/xenova-cpu-probe-dev
python3 -B -I -m unittest discover -s "$TOOLS" -p test_process_cpu_probe.py -v

WORKER_PID=$(systemctl show xenova-recovery-worker.service -p MainPID --value)
MANAGER_PID=$(systemctl show xenova-dom0-manager.service -p MainPID --value)
GATEWAY_PID=$(systemctl show xenova-gateway.service -p MainPID --value)
test "$WORKER_PID" -gt 1
test "$MANAGER_PID" -gt 1
test "$GATEWAY_PID" -gt 1
RUN=$(mktemp -d /tmp/xenova-cpu-probe.XXXXXX)
python3 -B -I "$TOOLS/process_cpu_probe.py" \
  --root worker="$WORKER_PID" --root manager="$MANAGER_PID" --root gateway="$GATEWAY_PID" \
  --duration-s 10 --interval-s 1 --output "$RUN/process-cpu.json"
```

셸의 `test` 실패 뒤에는 실행을 이어가지 않는다. 예시 PID를 상수로 복사하지 않는다.
도구의 기본값은 10초/0.5초이며 위 예시는 계측 비용을 줄이기 위해 1초를 명시한다.
`--root LABEL=PID`는 최대8개다. `--affinity`는 선택한 leader/TID의 mask를 추가 관측하며
syscall 비용이 늘어난다. 출력은 새0600 파일만 허용하고 기존 파일·symlink는 덮지 않는다.
실행이 겹치지 않도록 운영자가 분석 창을 관리하며 이 도구가 서비스 lock을 잡지는 않는다.

## 낮은 비용의 고정 PID 관측

서비스 프로세스 자체를 비교하려면 위 명령에 `--scope roots`를 명시한다. 이 모드는 선택한
root PID와 계측자만 직접 읽으며 전 PID 검색, 자식 발견, TID 열거를 하지 않는다. 각 PID의
starttime은 처음 고정하고 매 표본에서 stat을 두 번 대조한다. 3개 root와 계측자를 관측할 때
stat 읽기는 표본당 최대8회다. 권한 오류·PID 재사용·종료를 CPU0이나 새 실행의 누계로 바꾸지 않는다.

원 기본값인 `--scope tree`는 계보와 선택 TID를 자세히 관측하므로 비용이 커질 수 있다.
필요한 원인 분석에만 별도로 쓰고, 두 scope의 값을 같은 관측 범위인 것처럼 합치지 않는다.
roots 출력은 `descendants_observed=false`, `thread_detail_observed=false`이며 `threads`가 비어 있다.
자식의 CPU가 부모의 `cutime/cstime`에서 복구되거나 합산되는 일도 없다. `total_stat_read_attempts`는
원 PID 읽기·선택 PID 재검산·TID 읽기를 모두 센다. `stat_reads`는 기존 전체/고정 PID 1차 읽기 수다.

scope가 가벼워져도 실제 계측자 CPU·scan skew·coverage를 확인해야 한다. 계측기 OFF의 주행
latency/FPS 수락 조건은 동일하며, 계측자 CPU를 단순 차감한 정상 성능 수치를 만들지 않는다.

## 기록 해석

- 공통 host boot ID/HZ와 각 PID/TID의 starttime을 함께 남긴다. root의 첫 신원을 고정하고
  PID가 재사용되면 `REUSED`로 표시하며 새 프로세스를 옛 서비스로 따라가지 않는다.
  최초 표본이 없으면 나중의 같은 PID를 자동 채택하지 않는다. 권한 오류·관측 중 소멸·부재를 구분한다.
- 전체 PID의 `stat`만 부모 관계 발견용으로 읽는다. TID 상세는 선택한 roots/descendants와
  observer에만 한정한다. 실제 발견된 자식이 reparent돼도 같은 starttime 동안 원 계보로 추적한다.
  관측 사이에 태어나 종료한 자식·스레드는 놓칠 수 있다. 누락 CPU를 0이나 부모의 자식 누계로 메우지 않는다.
- 프로세스 CPU는 **자기 utime+stime**만 차분한다. `cutime/cstime`과 TID CPU를 합계에 더하지
  않는다. `union`은 각 프로세스 실행 신원을 한 번만 더한다. 중첩 root별 subtotal은 서로 겹칠 수
  있으므로 다시 합산하지 않는다. observer는 service union에서 제외한다.
- `cpu_pct_one_core`의 100%는 Linux CPU 한 개를 그 기간 사용한 값이다. 다중 스레드 프로세스는
  100%를 넘을 수 있다. Dom0 프로세스 사용량을 Xen guest 사용량·호스트 전체 점유율로 바꾸지 않는다.
- 각 stat read 시각과 scan 시작·끝, 공통 회차 간 실제 wall 시간, 주기 지연·건너뛴 주기를
  보존한다. `/proc`는 원자 snapshot이 아니며 TID와 프로세스 값이 같은 순간의 완전한 합은 아니다.
  집계는 일치하는 실행에서 관측한 **known subtotal**이다. `coverage_flags`·identity events·
  selected_errors와 같이 읽고, process_count=0의 subtotal을 실제 CPU=0으로 해석하지 않는다.
- 전체 PID8192, 서비스 계보256+observer1, 회차 TID1024, 저장 row8000, 샘플601,
  JSON8MiB 상한이 있다. 포화되면 사유를 남기고 종료한다. 최종 JSON 크기를 넘으면 끝부분
  샘플을 생략하고 `OUTPUT_LIMIT/omitted_tail_samples`를 기록한다. 관측이 늦어도 지난 주기를
  burst로 따라잡지 않는다. 부분 결과·잘린 coverage를 정상 baseline으로 쓰지 않는다.

`observer_process_cpu_seconds`는 실제 계측자 CPU 시간이며 scan/조립 비용을 포함한다.
시작 전 import와 최종 JSON 인코딩·파일 쓰기는 이 수치 밖이다. 회차별 observer `/proc`
delta·scan_seconds도 함께 남긴다. 종료 코드0은 요청 기간의 결과 파일이 작성됐다는 뜻이며
모든 프로세스가 계속 존재했다거나 정확한 전체 CPU를 수집했다는 뜻은 아니다. 상한·boot변경은2다.

주행 지연/FPS 수락 시험에서는 **probe OFF**를 유지한다. CPU 원인 분석은 별도 probe ON
실행으로 나누고 같은 입력·부하 조건의 OFF 대조를 남긴다. 관측 지연에서 observer CPU를
단순 차감해 보정 성능 수치를 만들지 않는다. 정상 런타임에 per-frame `/proc`·fsync·동기
진단 로그를 추가하는 용도가 아니다.

현재 산출물은 AST 정적 확인과 14개 stdlib 회귀 준비 범위다. 실제 시험 PASS·RPi CPU
baseline·성능 개선·HIL 수락은 주장하지 않는다. 실제 계측은 Root가 별도 창에서 수행한다.
