import { fireEvent, render, screen } from '@testing-library/react';
import { ToastProvider } from '@xenova/ui';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DriveKey } from '../api/drive';
import { DemoPage } from './DemoPage';

const sendKeys = vi.hoisted(() => vi.fn<(keys: DriveKey[]) => boolean>(() => true));

vi.mock('../api/drive', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/drive')>()),
  mockDemo: null,
  useManualInput: () => ({ socket: 'open', result: null, sendKeys }),
}));
vi.mock('../api/queries', () => ({ useVms: () => ({ data: [] }) }));
vi.mock('../layout/DriveContext', () => ({
  useDrive: () => ({ drive: { control: { mode: 'MANUAL' }, apps: {} }, socket: 'open' }),
}));
// 같은 화면에 있는 폼 요소 (실제 카드에는 트랙 고르기 select 가 있다)
vi.mock('./SimTrackControl', () => ({
  SimTrackControl: () => (
    <select aria-label="트랙 고르기">
      <option>spa</option>
    </select>
  ),
}));

const lastKeys = () => sendKeys.mock.calls.at(-1)?.[0];

describe('시연 제어 수동주행 키 입력 — 놓친 keyup 으로 키가 눌린 채 남지 않는다', () => {
  beforeEach(() => {
    sendKeys.mockClear();
    render(
      <ToastProvider>
        <MemoryRouter>
          <DemoPage />
        </MemoryRouter>
      </ToastProvider>,
    );
  });

  it('키를 누른 채 포커스가 폼 요소로 넘어가도 떼면 해제된다', () => {
    fireEvent.keyDown(window, { key: 'w' });
    expect(lastKeys()).toEqual(['up']);
    fireEvent.keyUp(screen.getByLabelText('트랙 고르기'), { key: 'w' });
    expect(lastKeys()).toEqual([]);
  });

  it('폼 요소 안에서 누른 키는 주행 입력으로 보지 않는다', () => {
    fireEvent.keyDown(screen.getByLabelText('트랙 고르기'), { key: 'ArrowDown' });
    expect(sendKeys).not.toHaveBeenCalled();
  });

  it('Ctrl·Meta·Alt 조합(Ctrl+S 등)은 주행 입력으로 보지 않는다', () => {
    fireEvent.keyDown(window, { key: 's', ctrlKey: true });
    fireEvent.keyDown(window, { key: 'a', metaKey: true });
    fireEvent.keyDown(window, { key: 'd', altKey: true });
    expect(sendKeys).not.toHaveBeenCalled();
  });
});
