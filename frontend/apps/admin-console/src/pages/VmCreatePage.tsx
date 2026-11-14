import { Button, Card, Modal } from '@xenova/ui';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { Link, useBlocker, useNavigate } from 'react-router';
import { ApiError } from '../api/client';
import { errorText } from '../api/errorText';
import { useCreateVm, useTemplates, useVms } from '../api/queries';
import type { Template } from '../api/types';
import { loadVmList } from '../api/vmListCache';
import {
  sameVmName,
  MEMORY_MAX_MIB,
  MEMORY_STEP_MIB,
  memoryMinMib,
  memoryProblem,
  VCPUS_MAX,
  VCPUS_MIN,
  vcpusProblem,
  vmNameProblem,
} from '../domain/vmLimits';
import styles from './pages.module.css';
import {
  blockVmCreationUnload,
  estimatedRemainingTime,
  finishVmCreation,
  formatVmCreationTime,
  readLastVmCreationDuration,
  saveLastVmCreationDuration,
  startVmCreation,
  VM_CREATION_LEAVE_MESSAGE,
} from './vmCreationProgress';

const STEPS = ['템플릿 선택', 'VM 정보 및 리소스', '생성 정보 확인'] as const;
const TEMPLATE_ACCENTS = [
  'var(--x-app-autonomous)',
  'var(--x-app-manual)',
  'var(--x-app-interaction)',
] as const;
/** 입력칸 화살표 한 번의 증감. 검증 단위는 vmLimits.MEMORY_STEP_MIB(16) */
const MEMORY_STEP = 64;
const ZEPHYR_PROFILE = { vcpus: 1, memory_mib: 16 };

export function VmCreatePage() {
  const templates = useTemplates();
  const create = useCreateVm();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [template, setTemplate] = useState<Template | null>(null);
  const [name, setName] = useState('');
  const [vcpus, setVcpus] = useState(1);
  const [memory, setMemory] = useState(512);
  const [creationStartedAt, setCreationStartedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [lastCreationDuration, setLastCreationDuration] = useState(readLastVmCreationDuration);
  const mounted = useRef(false);
  const leaveBlocker = useBlocker(create.isPending);
  const zephyr = template?.guest_kind === 'zephyr';
  const spec = zephyr ? ZEPHYR_PROFILE : { vcpus, memory_mib: memory };
  const vms = useVms();
  /**
   * 응답 시간이 초과된 생성의 이름 (S15P31E110-473). 서버는 레코드를 남기고 생성을 이어 가므로 그 이름의 VM 이
   * 목록에 생겨도 내 생성이다. 화면 상태로만 기억한다 — 새로고침하면 잊고, 그때는 목록에서 확인하도록 안내한다
   */
  const [timedOutName, setTimedOutName] = useState<string | null>(null);
  /** 이번 요청이 시간 초과된 생성을 같은 이름으로 다시 보낸 것인가 (200 이면 "이어서 마쳤습니다") */
  const [resuming, setResuming] = useState(false);
  const isTimedOutName = (candidate: string) => timedOutName !== null && sameVmName(candidate, timedOutName);
  // 같은 이름이 있으면 서버는 409(사양 다름) 또는 200(같은 사양 = 기존 VM 시작)이라 새 VM 이 생기지 않는다 (QA 1-F2)
  // 비교는 DB 기준(대소문자·악센트 무시) — 서버는 Drive-VM 요청에 drive-vm 을 찾는다. 시간 초과된 내 생성은 빼고 본다
  const duplicateName =
    name.length > 0 &&
    (vms.data?.items ?? []).some((vm) => sameVmName(vm.name, name) && !isTimedOutName(vm.name));
  /** 서버 422 errors[] 를 입력칸별 한글 이유로 바꾼 것. 그 칸을 고치면 지운다 */
  const [serverReasons, setServerReasons] = useState<FieldReasons>({});
  /** 한 번도 입력하지 않은 빈 이름에는 이유를 띄우지 않는다 */
  const [nameTouched, setNameTouched] = useState(false);
  const nameRule = vmNameProblem(name);
  const nameReason =
    serverReasons.name ?? (duplicateName ? DUPLICATE_NAME_TEXT : nameTouched ? nameRule : null);
  const nameOk = nameRule === null && !duplicateName && !serverReasons.name;
  // Zephyr 는 고정 프로필이라 입력을 검사하지 않는다
  const memoryMin = memoryMinMib(template?.min_memory_mib);
  const vcpusReason = template && !zephyr ? (serverReasons.vcpus ?? vcpusProblem(vcpus)) : null;
  const memoryReason =
    template && !zephyr ? (serverReasons.memory_mib ?? memoryProblem(memory, memoryMin)) : null;
  const resourcesOk = template !== null && vcpusReason === null && memoryReason === null;
  const clearServerReason = (field: keyof FieldReasons) =>
    setServerReasons(({ [field]: _cleared, ...rest }) => rest);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!create.isPending) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [create.isPending]);

  useEffect(() => {
    if (!create.isPending) return;
    window.addEventListener('beforeunload', blockVmCreationUnload);
    return () => window.removeEventListener('beforeunload', blockVmCreationUnload);
  }, [create.isPending]);

  useEffect(() => {
    if (!create.isPending && leaveBlocker.state === 'blocked') leaveBlocker.reset();
  }, [create.isPending, leaveBlocker]);

  const pick = (selected: Template) => {
    setTemplate(selected);
    setMemory(Math.max(selected.min_memory_mib, Math.min(memory, MEMORY_MAX_MIB)));
    create.reset();
  };

  const submit = () => {
    if (!template) return;
    const startedAt = Date.now();
    setCreationStartedAt(startedAt);
    setNow(startedAt);
    const retry = isTimedOutName(name);
    setResuming(retry);
    // 같은 이름의 VM이 이미 있으면 409(사양 불일치) 또는 200(같은 사양 재시도)이므로 새로 생성되는 VM이 아니다.
    // 시간 초과된 내 생성을 다시 보내는 것이면 그 VM 은 이번 생성의 결과다
    startVmCreation(name, startedAt, retry ? [] : existingVmIdsNamed(name));
    void create
      .mutateAsync({
        name,
        template_id: template.template_id,
        template_version: template.version,
        ...spec,
      })
      .then(() => {
        const duration = Date.now() - startedAt;
        saveLastVmCreationDuration(duration);
        // 화면을 벗어난 뒤에 끝나도 목록 카드가 생성 중에 남지 않도록 항상 기록을 지운다
        finishVmCreation(name);
        if (mounted.current) setLastCreationDuration(duration);
      })
      .catch((failure: unknown) => {
        finishVmCreation(name);
        if (mounted.current && failure instanceof ApiError && isCreationTimeout(failure))
          setTimedOutName(name);
        // 서버가 입력칸 규칙으로 거절했으면(422 errors[]) 입력 단계로 돌아가 해당 칸 아래에 이유를 보인다
        const reasons = fieldReasonsOf(failure, memoryMinMib(template.min_memory_mib));
        if (!reasons || !mounted.current) return;
        setServerReasons(reasons);
        setNameTouched(true);
        setStep(1);
        create.reset();
      });
  };

  const error = create.error instanceof ApiError ? create.error : null;
  /** 시간 초과 뒤 다시 보냈는데 서버가 아직 그 생성을 붙잡고 있음 (nginx 15분 < 백엔드 20분 사이) */
  const stillCreating = resuming && error?.code === 'VM_OPERATION_IN_PROGRESS';
  const timedOut = error !== null && (isCreationTimeout(error) || stillCreating);
  const resumedExisting = resuming && create.data?.created === false;
  const goToList = () => navigate('/control');

  return (
    <div className={styles.createPage}>
      <header className={styles.contentHeading}>
        <div>
          <span className={styles.contentEyebrow}>VM CONTROL</span>
          <h2>VM 생성</h2>
          <p className={styles.headingDescription}>템플릿과 자원을 선택해 새로운 가상 머신을 생성합니다.</p>
        </div>
        <Link className={styles.headingLink} to="/control">
          ← VM 목록
        </Link>
      </header>

      <nav className={styles.createSteps} aria-label="VM 생성 단계">
        {STEPS.map((label, index) => (
          <div
            key={label}
            className={`${styles.createStep} ${index === step ? styles.createStepCurrent : ''} ${
              index < step ? styles.createStepDone : ''
            }`}
            aria-current={index === step ? 'step' : undefined}
          >
            <span>{index < step ? '✓' : index + 1}</span>
            <strong>{label}</strong>
          </div>
        ))}
      </nav>

      {step === 0 && (
        <section className={styles.createStage} aria-labelledby="template-stage-title">
          <div className={styles.createStageHead}>
            <div>
              <span>STEP 1</span>
              <h3 id="template-stage-title">템플릿을 선택해 주세요</h3>
            </div>
            <p>VM에 설치할 운영체제와 기본 실행 환경을 선택합니다.</p>
          </div>

          {templates.isPending && <div className={styles.statePanel}>템플릿을 불러오는 중입니다…</div>}
          {templates.error && (
            <div className={styles.statePanel} role="alert">
              {templateLoadMessage(templates.error)}
            </div>
          )}
          {templates.data && templates.data.length === 0 && (
            <div className={styles.statePanel}>
              보드에 공개된 템플릿이 없습니다. 보드에 템플릿 이미지가 설치됐는지 확인해 주세요.
            </div>
          )}
          <div className={styles.templateGrid}>
            {(templates.data ?? []).map((item, index) => {
              const selected =
                template?.template_id === item.template_id && template.version === item.version;
              const accent = TEMPLATE_ACCENTS[index % TEMPLATE_ACCENTS.length];
              return (
                <button
                  key={`${item.template_id}@${item.version}`}
                  type="button"
                  className={`${styles.templateCard} ${selected ? styles.templateCardSelected : ''}`}
                  style={{ '--template-accent': accent } as CSSProperties}
                  aria-pressed={selected}
                  onClick={() => pick(item)}
                >
                  <span className={styles.templateKind}>
                    {item.guest_kind === 'zephyr' ? 'RTOS' : 'LINUX'}
                  </span>
                  <strong>{item.display_name}</strong>
                  <p>
                    {item.guest_kind === 'zephyr'
                      ? '고정 프로필 · 1 vCPU · 16 MiB'
                      : `사용자 설정 가능 · 최소 ${item.min_memory_mib} MiB`}
                  </p>
                  <small>
                    {item.template_id} · v{item.version}
                  </small>
                  <i>{selected ? '선택됨' : '선택'}</i>
                </button>
              );
            })}
          </div>
          <div className={styles.createStageActions}>
            <Button onClick={() => setStep(1)} disabled={!template}>
              다음
            </Button>
          </div>
        </section>
      )}

      {step === 1 && template && (
        <section className={styles.createStage} aria-labelledby="info-stage-title">
          <div className={styles.createStageHead}>
            <div>
              <span>STEP 2</span>
              <h3 id="info-stage-title">VM 정보 및 리소스</h3>
            </div>
            <p>{template.display_name} 템플릿에 적용할 이름과 초기 자원을 설정합니다.</p>
          </div>

          {Object.keys(serverReasons).length > 0 && (
            <div className={styles.createError} role="status">
              <strong>서버가 입력값을 받아들이지 않았습니다. 표시된 칸을 고쳐 주세요.</strong>
            </div>
          )}

          <div className={styles.createFormLayout}>
            <Card className={styles.createFormCard} title="기본 정보">
              <div className={styles.createForm}>
                <label htmlFor="vm-name">
                  <span>VM 이름</span>
                  <small>1~63자, 앞뒤 공백 없이 입력</small>
                </label>
                <input
                  id="vm-name"
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    setNameTouched(true);
                    clearServerReason('name');
                  }}
                  placeholder="예: drive-vm-a"
                  aria-invalid={nameReason !== null}
                  aria-describedby={nameReason ? 'vm-name-reason' : undefined}
                />
                {nameReason && (
                  <small id="vm-name-reason" role="alert">
                    {nameReason}
                  </small>
                )}

                {zephyr ? (
                  <div className={styles.fixedProfile}>
                    <span>Zephyr 고정 프로필</span>
                    <strong>1 vCPU · 16 MiB</strong>
                    <small>vchan을 통해 dom0와 통신하며 자원 값은 변경할 수 없습니다.</small>
                  </div>
                ) : (
                  <div className={styles.resourceInputs}>
                    <div>
                      <label htmlFor="vm-vcpus">
                        <span>vCPU</span>
                        <small>1~4개</small>
                      </label>
                      <input
                        id="vm-vcpus"
                        type="number"
                        min={1}
                        max={4}
                        value={vcpus}
                        aria-invalid={vcpusReason !== null}
                        aria-describedby={vcpusReason ? 'vm-vcpus-reason' : undefined}
                        onChange={(event) => {
                          setVcpus(Number(event.target.value));
                          clearServerReason('vcpus');
                        }}
                      />
                      {vcpusReason && (
                        <small id="vm-vcpus-reason" role="alert">
                          {vcpusReason}
                        </small>
                      )}
                    </div>
                    <div>
                      <label htmlFor="vm-memory">
                        <span>메모리</span>
                        <small>
                          {template.min_memory_mib}~{MEMORY_MAX_MIB} MiB · {MEMORY_STEP_MIB} MiB 단위
                        </small>
                      </label>
                      <div className={styles.inputWithUnit}>
                        <input
                          id="vm-memory"
                          type="number"
                          min={template.min_memory_mib}
                          max={MEMORY_MAX_MIB}
                          step={MEMORY_STEP}
                          value={memory}
                          aria-invalid={memoryReason !== null}
                          aria-describedby={memoryReason ? 'vm-memory-reason' : undefined}
                          onChange={(event) => {
                            setMemory(Number(event.target.value));
                            clearServerReason('memory_mib');
                          }}
                        />
                        <span>MiB</span>
                      </div>
                      {memoryReason && (
                        <small id="vm-memory-reason" role="alert">
                          {memoryReason}
                        </small>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </Card>

            <aside className={styles.templateSummary}>
              <span>SELECTED TEMPLATE</span>
              <h3>{template.display_name}</h3>
              <dl>
                <div>
                  <dt>유형</dt>
                  <dd>{zephyr ? 'RTOS · Zephyr' : 'Linux'}</dd>
                </div>
                <div>
                  <dt>버전</dt>
                  <dd>{template.version}</dd>
                </div>
                <div>
                  <dt>네트워크</dt>
                  <dd>{zephyr ? 'vchan · dom0 중계' : '가상 네트워크 · Agent'}</dd>
                </div>
              </dl>
            </aside>
          </div>

          <div className={styles.createStageActions}>
            <Button variant="secondary" onClick={() => setStep(0)}>
              이전
            </Button>
            <Button onClick={() => setStep(2)} disabled={!nameOk || !resourcesOk}>
              다음
            </Button>
          </div>
        </section>
      )}

      {step === 2 && template && (
        <section className={styles.createStage} aria-labelledby="review-stage-title">
          <div className={styles.createStageHead}>
            <div>
              <span>STEP 3</span>
              <h3 id="review-stage-title">생성 정보를 확인해 주세요</h3>
            </div>
            <p>VM 생성 후에는 리소스 설정을 별도 관리 화면에서 변경할 수 있습니다.</p>
          </div>

          <div className={styles.reviewCard}>
            <div className={styles.reviewIntro}>
              <span>NEW VIRTUAL MACHINE</span>
              <h3>{name}</h3>
              <p>{template.display_name}</p>
            </div>
            <dl className={styles.reviewGrid}>
              <div>
                <dt>템플릿</dt>
                <dd>{template.display_name}</dd>
                <small>{template.template_id}</small>
              </div>
              <div>
                <dt>버전</dt>
                <dd>{template.version}</dd>
              </div>
              <div>
                <dt>vCPU</dt>
                <dd>{spec.vcpus}개</dd>
              </div>
              <div>
                <dt>메모리</dt>
                <dd>{spec.memory_mib} MiB</dd>
              </div>
              <div>
                <dt>연결 방식</dt>
                <dd>{zephyr ? 'vchan · dom0 중계' : '가상 네트워크 · Agent'}</dd>
              </div>
            </dl>
          </div>

          {timedOut && (
            <div className={styles.createProgress} role="status">
              <div className={styles.createProgressHeading}>
                <div>
                  <strong>생성 요청의 응답 시간이 초과되었습니다</strong>
                  <span>
                    dom0에서 VM 생성이 계속 진행 중일 수 있습니다. 생성이 실패한 것은 아닐 수 있으니 VM
                    목록에서 상태를 먼저 확인해 주세요.
                  </span>
                </div>
              </div>
              {stillCreating && (
                <p role="alert">보드에서 생성이 아직 진행 중입니다. 잠시 후 목록에서 확인해 주세요.</p>
              )}
              <p>목록에 VM이 없거나 시작되지 않았다면 같은 이름·사양으로 다시 생성하면 이어서 복구됩니다.</p>
              <p>이 화면을 새로고침하면 이 안내가 사라집니다. 그때는 VM 목록에서 상태를 확인해 주세요.</p>
              <div className={styles.createTimeoutActions}>
                <Button variant="secondary" size="sm" onClick={goToList}>
                  VM 목록에서 확인
                </Button>
              </div>
            </div>
          )}

          {create.error && !timedOut && (
            <div className={styles.createError} role="alert">
              <strong title={error?.code}>{errorText(create.error, 'VM을 생성하지 못했습니다.')}</strong>
            </div>
          )}

          {create.isPending && creationStartedAt !== null && (
            <div className={styles.createProgress} role="status" aria-live="polite">
              <div className={styles.createProgressHeading}>
                <i aria-hidden="true" />
                <div>
                  <strong>VM을 생성하고 있습니다</strong>
                  <span>dom0 작업이 끝날 때까지 최대 15분 정도 걸릴 수 있습니다.</span>
                </div>
              </div>
              <dl>
                <div>
                  <dt>경과 시간</dt>
                  <dd>{formatVmCreationTime(now - creationStartedAt)}</dd>
                </div>
                {lastCreationDuration !== null && (
                  <>
                    <div>
                      <dt>직전 생성 소요</dt>
                      <dd>{formatVmCreationTime(lastCreationDuration)}</dd>
                    </div>
                    <div>
                      <dt>예상 남은 시간</dt>
                      <dd>
                        {formatVmCreationTime(
                          estimatedRemainingTime(now - creationStartedAt, lastCreationDuration),
                        )}
                      </dd>
                    </div>
                  </>
                )}
              </dl>
              <p>{VM_CREATION_LEAVE_MESSAGE}</p>
            </div>
          )}

          <div className={styles.createStageActions}>
            <Button variant="secondary" onClick={() => setStep(1)} disabled={create.isPending}>
              이전
            </Button>
            <Button onClick={submit} disabled={create.isPending}>
              {create.isPending ? 'VM 생성 중…' : timedOut ? '같은 사양으로 다시 생성' : 'VM 생성'}
            </Button>
          </div>
        </section>
      )}

      <Modal
        open={create.data !== undefined}
        title={create.data?.created === false && !resumedExisting ? '기존 VM 시작' : 'VM 생성 완료'}
        onClose={goToList}
        footer={<Button onClick={goToList}>VM 목록으로</Button>}
      >
        {create.data && (
          <div className={styles.createSuccess}>
            <span aria-hidden="true">✓</span>
            <div>
              <strong>‘{create.data.vm.name}’</strong>
              <p>
                {create.data.created
                  ? '새 VM이 성공적으로 생성되었습니다.'
                  : resumedExisting
                    ? '앞서 시간이 초과된 생성을 이어서 마쳤습니다.'
                    : '같은 이름의 기존 VM을 시작했습니다. 새 VM은 만들지 않았습니다.'}
              </p>
            </div>
            <dl className={styles.createSuccessMeta}>
              <div>
                <dt>템플릿</dt>
                <dd>{template?.display_name}</dd>
              </div>
              <div>
                <dt>할당 자원</dt>
                <dd>
                  {spec.vcpus} vCPU · {spec.memory_mib} MiB
                </dd>
              </div>
            </dl>
          </div>
        )}
      </Modal>

      <Modal
        open={leaveBlocker.state === 'blocked'}
        title="VM 생성이 진행 중입니다"
        onClose={() => leaveBlocker.reset?.()}
        footer={
          <>
            <Button variant="secondary" onClick={() => leaveBlocker.reset?.()}>
              계속 생성 보기
            </Button>
            <Button onClick={() => leaveBlocker.proceed?.()}>이동</Button>
          </>
        }
      >
        <div className={styles.createLeaveNotice}>
          <strong>{VM_CREATION_LEAVE_MESSAGE}</strong>
          <p>이 화면을 벗어나도 서버에 전달된 생성 요청은 취소되지 않습니다.</p>
        </div>
      </Modal>
    </div>
  );
}

const DUPLICATE_NAME_TEXT = '같은 이름의 VM이 이미 있습니다. 다른 이름을 입력해 주세요.';

type FieldReasons = Partial<Record<'name' | 'vcpus' | 'memory_mib', string>>;

/**
 * 422 VALIDATION_ERROR 의 errors[].field 를 입력칸별 한글 이유로. 서버 reason 은 `name은 …` 처럼 필드 영문명이
 * 섞여 있어 쓰지 않는다. 입력칸이 없는 필드뿐이면 null (확인 단계의 공용 오류 문구로 보인다)
 */
function fieldReasonsOf(error: unknown, memoryMin: number): FieldReasons | null {
  if (!(error instanceof ApiError) || error.status !== 422 || !error.fieldErrors?.length) return null;
  const reasons: FieldReasons = {};
  for (const { field } of error.fieldErrors) {
    if (field === 'name') {
      reasons.name = 'VM 이름은 1~63자이며 앞뒤 공백과 제어문자를 쓸 수 없습니다.';
    } else if (field === 'vcpus') {
      reasons.vcpus = `vCPU는 ${VCPUS_MIN}~${VCPUS_MAX}개 사이 정수로 입력해 주세요.`;
    } else if (field === 'memory_mib') {
      reasons.memory_mib = `메모리는 ${memoryMin}~${MEMORY_MAX_MIB} MiB 사이로 입력해 주세요.`;
    }
  }
  return Object.keys(reasons).length ? reasons : null;
}

/** 템플릿 버전은 보드 카탈로그에서 읽는다. 보드에 한 번도 닿지 못하면 백엔드가 dom0-manager 오류를 그대로 준다 */
function templateLoadMessage(error: unknown): string {
  if (error instanceof ApiError && error.code.startsWith('VM_CONTROL_')) {
    return '보드에 연결할 수 없어 템플릿을 불러오지 못했습니다. 보드 상태를 확인한 뒤 다시 시도해 주세요.';
  }
  return '템플릿을 불러오지 못했습니다.';
}

/** 백엔드는 dom0-manager 응답을 command-timeout(20m)까지 기다린 뒤 504 VM_CONTROL_TIMEOUT으로 끝낸다 */
function isCreationTimeout(error: ApiError): boolean {
  return error.code === 'VM_CONTROL_TIMEOUT' || error.status === 504;
}

/** 이 탭이 마지막으로 받은 VM 목록에서 같은 이름의 VM. 목록이 없으면 비워 두고 응답으로 정리한다 */
function existingVmIdsNamed(name: string): string[] {
  return (loadVmList()?.data.items ?? []).filter((vm) => sameVmName(vm.name, name)).map((vm) => vm.vm_id);
}
