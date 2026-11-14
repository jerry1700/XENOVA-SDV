import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Template, Vm } from '../api/types';
import { ResourceAdjustmentPanel } from './ResourceAdjustmentPanel';

const state = vi.hoisted(() => ({ templates: [] as Template[] }));

vi.mock('../api/queries', () => ({
  useTemplates: () => ({ data: state.templates }),
  useUpdateVmResources: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock('@xenova/ui', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@xenova/ui')>()),
  useToast: () => ({ show: vi.fn() }),
}));

const vm: Vm = {
  vm_id: 'vm-a',
  name: 'interaction',
  guest_kind: 'linux',
  state: 'stopped',
  agent_status: 'OFFLINE',
  ipv4: null,
  template_id: 'debian',
  template_version: '13',
  vcpus: 2,
  memory_mib: 512,
};

beforeEach(() => {
  state.templates = [
    {
      template_id: 'debian',
      version: '13',
      display_name: 'Debian',
      min_memory_mib: 256,
      guest_kind: 'linux',
    },
  ];
});

const memory = () => screen.getByRole('spinbutton');
const apply = () => screen.getByRole('button', { name: '리소스 적용' });

describe('vCPU·메모리 조정 규칙 (생성 화면과 공용, S15P31E110-469)', () => {
  it('16 MiB 단위가 아니면 이유를 보이고 적용을 막는다', () => {
    render(<ResourceAdjustmentPanel vm={vm} accent="red" />);
    fireEvent.change(memory(), { target: { value: '520' } });
    expect(screen.getByRole('alert')).toHaveTextContent('메모리는 16 MiB 단위로 입력해 주세요.');
    expect(apply()).toBeDisabled();
    fireEvent.change(memory(), { target: { value: '528' } });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(apply()).toBeEnabled();
  });

  it('하한은 템플릿 최소값, 템플릿을 모르면 기본 128 MiB', () => {
    render(<ResourceAdjustmentPanel vm={vm} accent="red" />);
    fireEvent.change(memory(), { target: { value: '240' } });
    expect(screen.getByRole('alert')).toHaveTextContent('메모리는 256~1024 MiB 사이로 입력해 주세요.');
  });

  it('템플릿 목록에 없으면 128~1024', () => {
    state.templates = [];
    render(<ResourceAdjustmentPanel vm={vm} accent="red" />);
    fireEvent.change(memory(), { target: { value: '112' } });
    expect(screen.getByRole('alert')).toHaveTextContent('메모리는 128~1024 MiB 사이로 입력해 주세요.');
    fireEvent.change(memory(), { target: { value: '144' } });
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
