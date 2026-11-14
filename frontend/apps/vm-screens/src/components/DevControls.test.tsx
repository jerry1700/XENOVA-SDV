import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DevControls } from './DevControls';
import { devControlsVisible } from './devControlsVisible';

vi.mock('../telemetry/TelemetryProvider', () => ({
  useTelemetry: () => ({ source: 'live', connection: 'unauthorized' }),
}));

afterEach(() => {
  window.history.replaceState(null, '', '/');
});

describe('DevControls 표시 조건 (S15P31E110-470)', () => {
  it.each([
    ['', false, true, '개발 모드는 보인다'],
    ['?kiosk=1', false, false, '개발 모드라도 키오스크는 숨긴다'],
    ['', true, false, '운영 빌드는 숨긴다'],
    ['?dev=1', true, true, '운영 빌드도 ?dev=1 이면 보인다'],
    ['?dev=0', true, false, '?dev=1 이 아니면 숨긴다'],
    ['?dev=1&kiosk=1', true, false, '키오스크가 우선'],
    ['?kiosk&dev=1', false, false, '값 없는 ?kiosk 도 키오스크'],
  ])('%s (운영=%s) → %s : %s', (search, prod, visible) => {
    expect(devControlsVisible(search, prod)).toBe(visible);
  });
});

describe('DevControls 문구', () => {
  it("인증 실패는 '인증 필요'만 (예전 '관리 콘솔 로그인'·'.env.local' 안내 없음)", () => {
    render(
      <MemoryRouter>
        <DevControls />
      </MemoryRouter>,
    );
    expect(screen.getByText('백엔드: 인증 필요')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/\.env\.local|관리 콘솔 로그인/);
  });

  it('키오스크 주소에서는 그리지 않는다', () => {
    window.history.replaceState(null, '', '/split?kiosk=1');
    const { container } = render(
      <MemoryRouter>
        <DevControls />
      </MemoryRouter>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
