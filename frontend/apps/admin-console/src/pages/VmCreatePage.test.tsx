import { fireEvent, render, screen, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import type { Template, Vm } from '../api/types';
import { saveVmList } from '../api/vmListCache';
import { VmCreatePage } from './VmCreatePage';
import { finishVmCreation, VM_CREATION_LEAVE_MESSAGE } from './vmCreationProgress';

const PENDING_KEY = 'xenova.vm-create.pending';
const LINUX_TEMPLATE: Template = {
  template_id: 'linux-base',
  version: '1.0.0',
  display_name: 'Linux Base',
  min_memory_mib: 256,
  guest_kind: 'linux',
};

const createMutation = vi.hoisted(() => ({
  data: undefined as unknown,
  error: null as unknown,
  isPending: false,
  mutateAsync: vi.fn(),
  reset: vi.fn(),
}));

const templatesQuery = vi.hoisted(() => ({
  data: undefined as unknown,
  error: null as unknown,
  isPending: false,
}));

const listedVms = vi.hoisted(() => ({ items: [] as { vm_id: string; name: string; state: string }[] }));

vi.mock('../api/queries', () => ({
  useCreateVm: () => createMutation,
  useVms: () => ({ data: { items: listedVms.items }, error: null, isPending: false }),
  useTemplates: () => templatesQuery,
}));

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  createMutation.data = undefined;
  createMutation.error = null;
  createMutation.isPending = false;
  createMutation.mutateAsync.mockReset();
  templatesQuery.data = [LINUX_TEMPLATE];
  templatesQuery.error = null;
  templatesQuery.isPending = false;
  listedVms.items = [];
});

afterEach(() => {
  finishVmCreation('drive-vm-a');
});

function renderCreatePage() {
  const router = createMemoryRouter(
    [
      { path: '/control/vms/new', element: <VmCreatePage /> },
      { path: '/control', element: <div>VM 목록 화면</div> },
    ],
    { initialEntries: ['/control/vms/new'] },
  );
  return render(<RouterProvider router={router} />);
}

/** 템플릿 선택 → 이름 입력 → 확인 단계까지 이동 */
function goToReviewStep(name = 'drive-vm-a') {
  fireEvent.click(screen.getByRole('button', { name: /Linux Base/ }));
  fireEvent.click(screen.getByRole('button', { name: '다음' }));
  fireEvent.change(screen.getByLabelText(/VM 이름/), { target: { value: name } });
  fireEvent.click(screen.getByRole('button', { name: '다음' }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function pendingNames(): string[] {
  const parsed = JSON.parse(localStorage.getItem(PENDING_KEY) ?? '[]') as { name: string }[];
  return parsed.map((creation) => creation.name);
}

function pendingExcluded(): string[] | undefined {
  const parsed = JSON.parse(localStorage.getItem(PENDING_KEY) ?? '[]') as { excludedVmIds?: string[] }[];
  return parsed[0]?.excludedVmIds;
}

describe('VM 생성 화면 이탈 안내', () => {
  it('생성 중 화면 이동을 막고 실제 동작을 안내한 뒤 사용자가 선택하면 이동한다', async () => {
    createMutation.isPending = true;
    renderCreatePage();

    fireEvent.click(screen.getByRole('link', { name: /VM 목록/ }));

    const dialog = await screen.findByRole('dialog', { name: 'VM 생성이 진행 중입니다' });
    expect(within(dialog).getByText(VM_CREATION_LEAVE_MESSAGE)).toBeInTheDocument();
    expect(
      within(dialog).getByText('이 화면을 벗어나도 서버에 전달된 생성 요청은 취소되지 않습니다.'),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: '이동' }));
    expect(await screen.findByText('VM 목록 화면')).toBeInTheDocument();
  });
});

describe('VM 생성 중 기록 정리', () => {
  it('생성 화면을 벗어난 뒤 생성이 끝나도 생성 중 기록을 지운다', async () => {
    const request = deferred<Vm>();
    createMutation.mutateAsync.mockReturnValue(request.promise);
    const view = renderCreatePage();
    goToReviewStep();

    fireEvent.click(screen.getByRole('button', { name: 'VM 생성' }));
    expect(pendingNames()).toEqual(['drive-vm-a']);

    view.unmount();
    request.resolve({ vm_id: 'vm-new', name: 'drive-vm-a', state: 'stopped' } as Vm);
    await request.promise;
    await Promise.resolve();

    expect(pendingNames()).toEqual([]);
  });

  it('같은 이름의 기존 VM은 생성 기록에서 제외하고, 409로 실패하면 기록을 지운다', async () => {
    saveVmList({
      items: [{ vm_id: 'vm-existing', name: 'drive-vm-a', state: 'stopped' } as Vm],
      observed_at: new Date().toISOString(),
    });
    const request = deferred<Vm>();
    createMutation.mutateAsync.mockReturnValue(request.promise);
    renderCreatePage();
    goToReviewStep();

    fireEvent.click(screen.getByRole('button', { name: 'VM 생성' }));
    expect(pendingExcluded()).toEqual(['vm-existing']);

    request.reject(new ApiError(409, { code: 'VM_NAME_CONFLICT', message: '이미 사용 중인 VM 이름입니다.' }));
    await request.promise.catch(() => undefined);
    await Promise.resolve();

    expect(pendingNames()).toEqual([]);
  });

  it('생성 실패는 한글 문구만 보이고 서버 원문·(코드)를 붙이지 않는다 (코드는 title 에만)', () => {
    createMutation.error = new ApiError(409, {
      code: 'VM_NAME_CONFLICT',
      message: 'name already used by a retained VM',
    });
    renderCreatePage();
    goToReviewStep();

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('같은 이름의 VM이 있습니다. 다른 이름을 입력해 주세요.');
    expect(alert).not.toHaveTextContent('VM_NAME_CONFLICT');
    expect(alert).not.toHaveTextContent('name already used');
    expect(within(alert).getByText('같은 이름의 VM이 있습니다. 다른 이름을 입력해 주세요.')).toHaveAttribute(
      'title',
      'VM_NAME_CONFLICT',
    );
  });

  it('504 타임아웃이면 실패 대신 진행 중일 수 있음과 재시도 방법을 안내한다', () => {
    createMutation.error = new ApiError(504, {
      code: 'VM_CONTROL_TIMEOUT',
      message: 'VM 제어 서비스의 응답 시간이 초과되었습니다.',
    });
    renderCreatePage();
    goToReviewStep();

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    const notice = screen.getByRole('status');
    expect(within(notice).getByText('생성 요청의 응답 시간이 초과되었습니다')).toBeInTheDocument();
    expect(within(notice).getByText(/VM 생성이 계속 진행 중일 수 있습니다/)).toBeInTheDocument();
    expect(
      within(notice).getByText(/같은 이름·사양으로 다시 생성하면 이어서 복구됩니다/),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '같은 사양으로 다시 생성' })).toBeEnabled();
  });
});

describe('같은 이름의 VM (QA 1-F2)', () => {
  it('목록에 같은 이름이 있으면 2단계에서 막는다', () => {
    listedVms.items = [{ vm_id: 'vm-existing', name: 'drive-vm-a', state: 'stopped' }];
    renderCreatePage();
    fireEvent.click(screen.getByRole('button', { name: /Linux Base/ }));
    fireEvent.click(screen.getByRole('button', { name: '다음' }));
    fireEvent.change(screen.getByLabelText(/VM 이름/), { target: { value: 'drive-vm-a' } });
    expect(
      screen.getByText('같은 이름의 VM이 이미 있습니다. 다른 이름을 입력해 주세요.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/VM 이름/), { target: { value: 'drive-vm-b' } });
    expect(screen.getByRole('button', { name: '다음' })).toBeEnabled();
  });

  it('서버가 200(같은 사양 재요청 = 기존 VM 시작)이면 "생성 완료"가 아니라 기존 VM 을 시작했다고 알린다', () => {
    createMutation.data = { vm: { vm_id: 'vm-existing', name: 'drive-vm-a' }, created: false };
    renderCreatePage();
    goToReviewStep();
    expect(
      screen.getByText('같은 이름의 기존 VM을 시작했습니다. 새 VM은 만들지 않았습니다.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('새 VM이 성공적으로 생성되었습니다.')).toBeNull();
  });

  it('201 이면 생성 완료', () => {
    createMutation.data = { vm: { vm_id: 'vm-new', name: 'drive-vm-a' }, created: true };
    renderCreatePage();
    goToReviewStep();
    expect(screen.getByText('새 VM이 성공적으로 생성되었습니다.')).toBeInTheDocument();
  });
});

describe('메모리 입력 규칙 (S15P31E110-469, 조정 화면과 같은 16 MiB 단위)', () => {
  const goToInfoStep = () => {
    renderCreatePage();
    fireEvent.click(screen.getByRole('button', { name: /Linux Base/ }));
    fireEvent.click(screen.getByRole('button', { name: '다음' }));
    fireEvent.change(screen.getByLabelText(/VM 이름/), { target: { value: 'drive-vm-a' } });
  };
  const memory = () => screen.getByLabelText(/메모리/);

  it.each([
    ['300', '메모리는 16 MiB 단위로 입력해 주세요.'],
    ['128', '메모리는 256~1024 MiB 사이로 입력해 주세요.'],
    ['2048', '메모리는 256~1024 MiB 사이로 입력해 주세요.'],
  ])('%s MiB 는 입력칸 아래에 "%s" 를 보이고 다음 단계를 막는다', (value, reason) => {
    goToInfoStep();
    fireEvent.change(memory(), { target: { value } });
    expect(screen.getByText(reason)).toHaveAttribute('id', 'vm-memory-reason');
    expect(memory()).toHaveAttribute('aria-invalid', 'true');
    expect(memory()).toHaveAttribute('aria-describedby', 'vm-memory-reason');
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();
  });

  it('16 MiB 단위면 통과 (화살표 단위 64 와 별개)', () => {
    goToInfoStep();
    fireEvent.change(memory(), { target: { value: '272' } });
    expect(memory()).toHaveAttribute('step', '64');
    expect(memory()).toHaveAttribute('aria-invalid', 'false');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: '다음' })).toBeEnabled();
  });
});

describe('VM 이름 규칙별 이유 (S15P31E110-469)', () => {
  const goToInfoStep = () => {
    renderCreatePage();
    fireEvent.click(screen.getByRole('button', { name: /Linux Base/ }));
    fireEvent.click(screen.getByRole('button', { name: '다음' }));
  };
  const nameInput = () => screen.getByLabelText(/VM 이름/);

  it('처음 빈 칸에는 이유를 띄우지 않고 다음만 막는다', () => {
    goToInfoStep();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(nameInput()).toHaveAttribute('aria-invalid', 'false');
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();
  });

  it.each([
    ['', 'VM 이름을 입력해 주세요.'],
    ['a'.repeat(64), 'VM 이름은 63자 이하로 입력해 주세요. (지금 64자)'],
    [' drive', 'VM 이름 앞뒤에는 공백을 넣을 수 없습니다.'],
    ['drive ', 'VM 이름 앞뒤에는 공백을 넣을 수 없습니다.'],
    ['dr\u0001ive', 'VM 이름에는 줄바꿈·탭 같은 제어문자를 쓸 수 없습니다.'],
  ])('%j → "%s"', (value, reason) => {
    goToInfoStep();
    fireEvent.change(nameInput(), { target: { value: 'x' } });
    fireEvent.change(nameInput(), { target: { value } });
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(reason);
    expect(nameInput()).toHaveAttribute('aria-invalid', 'true');
    expect(nameInput()).toHaveAttribute('aria-describedby', alert.id);
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();
  });

  it('vCPU 가 1~4 밖이면 입력칸 아래에 이유', () => {
    goToInfoStep();
    fireEvent.change(nameInput(), { target: { value: 'drive-vm-a' } });
    fireEvent.change(screen.getByLabelText(/vCPU/), { target: { value: '5' } });
    expect(screen.getByRole('alert')).toHaveTextContent('vCPU는 1~4개 사이 정수로 입력해 주세요.');
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();
  });
});

describe('서버 422 필드 오류를 입력칸에 (S15P31E110-469)', () => {
  it('errors[].field 를 입력 단계의 해당 칸 아래에 한글로 보이고(서버 영문 문구 없음), 고치면 지운다', async () => {
    createMutation.mutateAsync.mockRejectedValue(
      new ApiError(422, {
        code: 'VALIDATION_ERROR',
        message: '요청 값이 올바르지 않습니다.',
        errors: [
          { field: 'name', reason: 'name은 1~63자이며 제어문자와 앞뒤 공백은 사용할 수 없습니다.' },
          { field: 'memory_mib', reason: 'memory_mib는 16~1024여야 합니다.' },
        ],
      }),
    );
    renderCreatePage();
    goToReviewStep();
    fireEvent.click(screen.getByRole('button', { name: 'VM 생성' }));

    const nameReason = await screen.findByText('VM 이름은 1~63자이며 앞뒤 공백과 제어문자를 쓸 수 없습니다.');
    expect(screen.getByRole('heading', { name: 'VM 정보 및 리소스' })).toBeInTheDocument();
    expect(screen.getByLabelText(/VM 이름/)).toHaveAttribute('aria-describedby', nameReason.id);
    const memoryReason = screen.getByText('메모리는 256~1024 MiB 사이로 입력해 주세요.');
    expect(screen.getByLabelText(/메모리/)).toHaveAttribute('aria-describedby', memoryReason.id);
    expect(screen.getByRole('status')).toHaveTextContent('서버가 입력값을 받아들이지 않았습니다.');
    expect(document.body).not.toHaveTextContent(/name은|memory_mib|요청 값이 올바르지/);
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/VM 이름/), { target: { value: 'drive-vm-b' } });
    expect(screen.queryByText(/VM 이름은 1~63자/)).toBeNull();
    fireEvent.change(screen.getByLabelText(/메모리/), { target: { value: '528' } });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: '다음' })).toBeEnabled();
  });

  it('입력칸이 없는 필드만 오면 확인 단계에 공용 문구', async () => {
    const failure = new ApiError(422, {
      code: 'VALIDATION_ERROR',
      message: '요청 값이 올바르지 않습니다.',
      errors: [{ field: 'template_id', reason: 'template_id는 필수입니다.' }],
    });
    createMutation.error = failure;
    createMutation.mutateAsync.mockRejectedValue(failure);
    renderCreatePage();
    goToReviewStep();
    fireEvent.click(screen.getByRole('button', { name: 'VM 생성' }));
    await Promise.resolve();
    expect(screen.getByRole('heading', { name: '생성 정보를 확인해 주세요' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('입력한 값을 다시 확인해 주세요.');
  });
});

describe('템플릿 목록 안내', () => {
  it('보드에 닿지 못해 템플릿을 못 읽으면 보드 연결 문제라고 안내한다', () => {
    templatesQuery.data = undefined;
    templatesQuery.error = new ApiError(503, {
      code: 'VM_CONTROL_UNAVAILABLE',
      message: 'VM 제어 서비스에 연결할 수 없습니다.',
    });
    renderCreatePage();

    expect(screen.getByRole('alert')).toHaveTextContent('보드에 연결할 수 없어 템플릿을 불러오지 못했습니다');
  });

  it('그 밖의 오류는 일반 실패 문구를 보여 준다', () => {
    templatesQuery.data = undefined;
    templatesQuery.error = new ApiError(500, { code: 'INTERNAL_ERROR' });
    renderCreatePage();

    expect(screen.getByRole('alert')).toHaveTextContent('템플릿을 불러오지 못했습니다.');
    expect(screen.queryByText(/보드에 연결할 수 없어/)).not.toBeInTheDocument();
  });

  it('보드에 공개된 템플릿이 없으면 빈 목록 안내를 보여 준다', () => {
    templatesQuery.data = [];
    renderCreatePage();

    expect(screen.getByText(/보드에 공개된 템플릿이 없습니다/)).toBeInTheDocument();
  });
});
