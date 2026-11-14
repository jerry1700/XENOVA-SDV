import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ToastProvider } from '@xenova/ui';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DriveState } from '../api/drive';
import type { MetricSample } from '../api/types';
import { memoryFixture } from '../domain/memoryContractFixtures';
import { initialVms, VM_IDS } from '../mocks/data';
import { DashboardPage } from './DashboardPage';
import { ResourcesPage } from './ResourcesPage';
import { VmDetailPage } from './VmDetailPage';

const { useDrive } = vi.hoisted(() => ({ useDrive: vi.fn() }));
vi.mock('../layout/DriveContext', () => ({ useDrive }));
vi.mock('../console/ConsolePanel', () => ({ ConsolePanel: () => null }));

const received = '2026-11-03T00:00:00Z';
const vms = initialVms().map((vm, i) => ({ ...vm, assigned_ipv4: `192.168.77.${i + 2}` }));
const drive: DriveState = {
  version: 1,
  observed_at: received,
  control: null,
  vehicle: null,
  perception: null,
  apps: {},
  vm_apps: {
    [VM_IDS.autonomous]: {
      drive: {
        lifecycle: { phase: 'WAITING', readiness: 'NOT_READY', reason: 'awaiting_input' },
        lifecycle_source: { status: 'fresh', age_ms: 100 },
        heartbeat: null,
        heartbeat_source: { status: 'unknown', age_ms: null },
      },
    },
    [VM_IDS.manual]: {
      drive: {
        lifecycle: { phase: 'EXECUTING', readiness: 'READY', reason: null },
        lifecycle_source: { status: 'fresh', age_ms: 100 },
        heartbeat: { state: 'active' },
        heartbeat_source: { status: 'stale', age_ms: 9000 },
      },
    },
  },
};

function fixture(stackSupported = true) {
  useDrive.mockReturnValue({ drive, socket: 'open' });
  const fetch = vi.fn(async (url: string) => {
    const path = new URL(url, 'http://fixture').pathname;
    const [, , , , id, endpoint] = path.split('/');
    const vm = vms.find((item) => item.vm_id === id);
    if (path === '/api/v1/events') return Response.json({ items: [] });
    if (path === '/api/v1/vms') return Response.json({ items: vms });
    if (!vm) return Response.json({}, { status: 404 });
    const linux = vm.guest_kind === 'linux';
    if (endpoint === 'telemetry' && path.endsWith('/heartbeat')) {
      const status = linux ? 'STALE' : id === VM_IDS.autonomous ? 'FRESH' : 'UNKNOWN';
      return Response.json({
        vm_id: id,
        status,
        publisher_state: status === 'UNKNOWN' ? null : 'running',
        last_collection_success_uptime_ms: status === 'UNKNOWN' ? null : '12000',
        last_collection_success_age_ms: status === 'UNKNOWN' ? null : 1200,
        collection_failures_total: status === 'UNKNOWN' ? null : 2,
        received_at: status === 'UNKNOWN' ? null : received,
      });
    }
    if (endpoint === 'metrics') {
      const metric = (name: string, value: number, unit: string) => ({
        name,
        value,
        unit,
        raw_value: String(value),
        status: 'ok',
        source_rc: 0,
        labels: {},
        unavailable_reason: null,
      });
      return Response.json({
        items: [
          {
            observed_at: received,
            ...(!linux
              ? {
                  vm_id: id,
                  boot_id: `boot-${id}`,
                  sample_id: `sample-${id}`,
                  source: 'zephyr_telemetry',
                  received_at: new Date(Date.now() - 1000).toISOString(),
                  telemetry: {
                    family: 'memory',
                    publisher_instance_id: `publisher-${id}`,
                    source_sample_seq: '1',
                    observed_uptime_ms: '1000',
                    sent_uptime_ms: '1002',
                    sent_at: null,
                    utc_quality: 'unavailable',
                    utc_source: null,
                    uncertainty_ms: null,
                    collection: {
                      sample_id: 'heap-collection',
                      start_uptime_ms: '999',
                      end_uptime_ms: '1000',
                      elapsed_us: '20',
                      status: 'ok',
                      source_rc: 0,
                    },
                    context: {
                      binding: 'host_provisioned',
                      host_boot_id: 'host',
                      execution_id: `execution-${id}`,
                      receipt_id: `receipt-${id}`,
                    },
                  },
                }
              : {}),
            values: linux
              ? [metric('memory_used_bytes', 1048576, 'bytes')]
              : [
                  metric('memory.kernel_heap.used_bytes', 512, 'bytes'),
                  metric('memory.kernel_heap.free_bytes', 512, 'bytes'),
                  metric('rx_bytes_per_s', 2048, 'bytes/s'),
                ],
          },
        ],
      });
    }
    if (endpoint === 'threads' && !linux) {
      return Response.json({
        vm_id: id,
        window_ms: 1000,
        received_at: received,
        stale: false,
        threads_status: 'ok',
        parts_received: 1,
        part_count: 2,
        complete: false,
        conflicting_pages: false,
        items: [{ thread_id: '1', thread_name: 'telemetry', cpu_percent: 0 }],
        stack_snapshot: {
          snapshot_id: 'stack-snapshot',
          source_status: stackSupported ? 'ok' : 'unsupported',
          rows_seen: stackSupported ? 2 : null,
          received_at: received,
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          items: stackSupported
            ? [
                {
                  row_id: 0,
                  name: 'telemetry',
                  name_truncated: false,
                  reserved_bytes: 8192,
                  peak_used_bytes: 4096,
                  status: 'ok',
                },
                {
                  row_id: 1,
                  name: 'telemetry',
                  name_truncated: false,
                  reserved_bytes: 4096,
                  peak_used_bytes: null,
                  status: 'scan_limit',
                },
              ]
            : [],
        },
      });
    }
    if (endpoint === 'processes' && linux) {
      return Response.json({
        vm_id: id,
        observed_at: received,
        stale: false,
        items: [{ pid: 1, name: 'systemd', cpu_percent: 0, rss_bytes: 1048576 }],
      });
    }
    if (!endpoint) return Response.json(vm);
    return Response.json({}, { status: 404 });
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

function EventsRouteProbe() {
  const { search } = useLocation();
  return <p>events route {search}</p>;
}

function renderPage(path = '/dashboard/memory') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const page = render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/dashboard/overview" element={<DashboardPage />} />
            <Route path="/dashboard/vms/:vmId" element={<VmDetailPage />} />
            <Route path="/dashboard/memory" element={<ResourcesPage view="memory" />} />
            <Route path="/dashboard/events" element={<EventsRouteProbe />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  return { ...page, client };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('VM telemetry dashboard', () => {
  it('keeps Memory cards and original observations through query failure, expiry and recovery', async () => {
    let now = Date.now();
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    const sample = memoryFixture('ram-committed', now - 500);
    sample.vm_id = VM_IDS.autonomous;
    let failed = false;
    let report = sample;
    fetch.mockImplementation((url: string) => {
      const path = new URL(url, 'http://fixture').pathname;
      if (failed && (path === '/api/v1/vms' || path.endsWith('/metrics')))
        return Promise.resolve(
          Response.json({ code: 'UNAVAILABLE', message: 'fixture failure' }, { status: 503 }),
        );
      return path.endsWith(`/vms/${VM_IDS.autonomous}/metrics`)
        ? Promise.resolve(Response.json({ items: [report] }))
        : original(url);
    });
    const page = renderPage();
    const card = within(await screen.findByRole('region', { name: 'autonomous 메모리 사용률' }));
    expect(await card.findByText('25.0%')).toBeInTheDocument();
    failed = true;
    now += 1000;
    await act(() => page.client.refetchQueries());
    expect(screen.getByText(/실시간 데이터를 불러오지 못하고 있습니다/)).toBeInTheDocument();
    expect(card.getByText('25.0%')).toBeInTheDocument();
    now += 7000;
    act(() => vi.advanceTimersByTime(1000));
    expect(card.getByText('25.0%')).toBeInTheDocument();
    expect(card.getByText('관측 만료 · 최근 관측값')).toBeInTheDocument();
    expect(card.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
    failed = false;
    report = memoryFixture('ram-reserved-increase', now - 500);
    report.vm_id = sample.vm_id;
    await act(() => page.client.refetchQueries());
    expect(card.getByText('50.0%')).toBeInTheDocument();
    expect(card.queryByText('관측 만료 · 최근 관측값')).not.toBeInTheDocument();
    expect(screen.queryByText(/실시간 데이터를 불러오지 못하고 있습니다/)).not.toBeInTheDocument();
    page.unmount();
  });

  it('uses the same managed RAM percentage on Overview and Memory while keeping Linux unchanged', async () => {
    const now = Date.now();
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      if (!new URL(url, 'http://fixture').pathname.endsWith('/metrics') || url.includes(VM_IDS.interaction))
        return original(url);
      const sample = memoryFixture(
        url.includes(VM_IDS.autonomous) ? 'ram-committed' : 'ram-valid-zero',
        now - 500,
      );
      sample.vm_id = url.includes(VM_IDS.autonomous) ? VM_IDS.autonomous : VM_IDS.manual;
      return Response.json({ items: [sample] });
    });
    const page = renderPage('/dashboard/overview');
    const autonomous = within(await screen.findByRole('article', { name: /autonomous/ }));
    expect(await autonomous.findByLabelText('Memory 25.000%')).toBeInTheDocument();
    expect(autonomous.getByText(/정적 예약 포함/)).toBeInTheDocument();
    expect(autonomous.queryByText('Kernel heap')).not.toBeInTheDocument();
    expect(
      await within(screen.getByRole('article', { name: /manual/ })).findByLabelText('Memory 0.000%'),
    ).toBeInTheDocument();
    expect(
      await within(screen.getByRole('article', { name: /interaction/ })).findByLabelText('Memory 0%'),
    ).toBeInTheDocument();
    page.unmount();
    renderPage();
    expect(
      await within(await screen.findByRole('region', { name: 'autonomous 메모리 사용률' })).findByText(
        '25.0%',
      ),
    ).toBeInTheDocument();
  });

  it('expires Overview RAM without waiting for another successful query', async () => {
    let now = Date.now();
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    const sample = memoryFixture('ram-committed', now - 500);
    sample.vm_id = VM_IDS.autonomous;
    fetch.mockImplementation((url: string) =>
      new URL(url, 'http://fixture').pathname.endsWith(`/vms/${VM_IDS.autonomous}/metrics`)
        ? Promise.resolve(Response.json({ items: [sample] }))
        : original(url),
    );
    const page = renderPage('/dashboard/overview');
    const card = within(await screen.findByRole('article', { name: /autonomous/ }));
    expect(await card.findByLabelText('Memory 25.000%')).toBeInTheDocument();
    now += 6001;
    act(() => vi.advanceTimersByTime(1000));
    expect(card.getByText('관측 만료 · 최근 관측값')).toBeInTheDocument();
    expect(card.getByLabelText('Memory 25.000%')).toBeInTheDocument();
    page.unmount();
  });

  it.each(['heap-only', 'unsupported', 'expired', 'new-boot'])(
    'does not replace %s RAM with the heap percentage',
    async (state) => {
      const now = Date.now();
      const fetch = fixture();
      const original = fetch.getMockImplementation()!;
      fetch.mockImplementation(async (url: string) => {
        const response = await original(url);
        if (!new URL(url, 'http://fixture').pathname.endsWith(`/vms/${VM_IDS.autonomous}/metrics`))
          return response;
        if (state === 'heap-only') return response;
        const sample = memoryFixture(
          state === 'unsupported' ? 'ram-unsupported' : 'ram-committed',
          now - (state === 'expired' ? 20_000 : 1000),
        );
        sample.vm_id = VM_IDS.autonomous;
        const items = [sample];
        if (state === 'new-boot') {
          const next = memoryFixture('manual-memory-pending-full', now - 500);
          next.vm_id = sample.vm_id;
          next.boot_id = 'new-boot';
          items.push(next);
        }
        return Response.json({ items });
      });
      renderPage('/dashboard/overview');
      const card = within(await screen.findByRole('article', { name: /autonomous/ }));
      const reason =
        state === 'heap-only'
          ? '관리 RAM 표본을 확인할 수 없습니다.'
          : state === 'unsupported'
            ? '관리 RAM 수집 불가'
            : state === 'expired'
              ? '관측 만료 · 최근 관측값'
              : '관리 RAM 표본 미수신';
      expect(await card.findByText(reason)).toBeInTheDocument();
      expect(
        card.getByLabelText(state === 'expired' ? 'Memory 25.000%' : 'Memory 데이터 없음'),
      ).toBeInTheDocument();
      expect(card.queryByLabelText('Memory 50.000%')).not.toBeInTheDocument();
    },
  );

  it.each([false, true])(
    // S15P31E110-409: Memory 탭은 그래프 없이 요약만 보인다. 패킷 처리율은 Network 탭 표에서 본다
    'does not chart packet rates in Memory even with network samples (reboot=%s)',
    async (reboot) => {
      const fetch = fixture();
      const original = fetch.getMockImplementation()!;
      fetch.mockImplementation(async (url: string) => {
        const response = await original(url);
        if (!new URL(url, 'http://fixture').pathname.endsWith(`/vms/${VM_IDS.autonomous}/metrics`))
          return response;
        const body: { items: MetricSample[] } = await response.json();
        body.items.push(
          ...[0, 1, 2, 3, 4, 5].map((index): MetricSample => ({
            sample_id: `network-${index}`,
            vm_id: VM_IDS.autonomous,
            boot_id: reboot && index >= 3 ? 'next-boot' : body.items[0]!.boot_id,
            source: 'zephyr_telemetry',
            observed_at: new Date(Date.now() - 12_000 + index * 2000).toISOString(),
            values: ['rx', 'tx'].map((direction) => ({
              name: `network.netfront.${direction}_packets`,
              value: (direction === 'rx' ? 100 : 200) + index * (direction === 'rx' ? 40 : 80),
              unit: 'packets',
              labels: { interface: 'if1' },
              unavailable_reason: null,
            })),
          })),
        );
        return Response.json(body);
      });
      renderPage();
      await waitFor(() =>
        expect(screen.getByRole('progressbar', { name: 'autonomous 커널 힙 사용률' })).toHaveAttribute(
          'aria-valuenow',
          '50',
        ),
      );
      expect(screen.queryByText('패킷 처리율')).not.toBeInTheDocument();
      expect(document.querySelectorAll('svg')).toHaveLength(0);
    },
  );

  it('shows VM / APP / LINK and uses Agent for Linux and telemetry heartbeat for Zephyr', async () => {
    const fetch = fixture();
    renderPage('/dashboard/overview');
    const autonomous = within(await screen.findByRole('article', { name: /autonomous/ }));
    expect(autonomous.getByText('대기')).toBeInTheDocument();
    const aLink = within(autonomous.getByTitle('Zephyr Telemetry heartbeat (v2)').parentElement!);
    expect(await aLink.findByText('연결됨')).toBeInTheDocument();
    expect(aLink.getByText('Telemetry')).toBeInTheDocument();
    expect(autonomous.queryByText('앱 HB')).not.toBeInTheDocument();
    const manual = within(screen.getByRole('article', { name: /manual/ }));
    expect(within(manual.getByText('APP').parentElement!).getByText('실행 중')).toBeInTheDocument();
    expect(await within(manual.getByText('LINK').parentElement!).findByText('미수신')).toBeInTheDocument();
    const linux = within(screen.getByRole('article', { name: /interaction/ }));
    const agentLink = within(linux.getByTitle('Linux Agent heartbeat (v1)').parentElement!);
    expect(agentLink.getByText('연결됨')).toBeInTheDocument();
    expect(agentLink.getByText('Agent')).toBeInTheDocument();
    for (const card of [autonomous, manual, linux]) {
      const rows = card.getByText('VM', { exact: true }).parentElement!.parentElement!.children;
      expect(Array.from(rows).map((row) => row.firstElementChild?.textContent)).toEqual([
        'VM',
        'APP',
        'LINK',
      ]);
    }
    const summary = within(screen.getByRole('region', { name: '주행 및 관측 요약' }));
    expect(summary.getByText('DRIVE MODE')).toBeInTheDocument();
    expect(summary.getByText('CONTROL AUTHORITY')).toBeInTheDocument();
    expect(summary.getByText('LAST OBSERVED')).toBeInTheDocument();
    expect(summary.queryByText('상태 스트림')).not.toBeInTheDocument();
    expect(summary.queryByText('TELEMETRY')).not.toBeInTheDocument();
    expect(
      fetch.mock.calls.some(
        ([url]) => url.includes(VM_IDS.interaction) && url.endsWith('/telemetry/heartbeat'),
      ),
    ).toBe(false);
  });

  it.each([
    ['degraded', '수집 저하'],
    ['stopping', '종료 중'],
  ])(
    'keeps a fresh %s publisher connected while retaining detailed collection diagnostics',
    async (state, label) => {
      const fetch = fixture();
      const original = fetch.getMockImplementation()!;
      fetch.mockImplementation(async (url: string) => {
        const response = await original(url);
        if (!url.endsWith(`/vms/${VM_IDS.autonomous}/telemetry/heartbeat`)) return response;
        return Response.json({
          ...(await response.json()),
          publisher_state: state,
          collection_failures_total: 7,
        });
      });
      const page = renderPage('/dashboard/overview');
      const card = within(await screen.findByRole('article', { name: /autonomous/ }));
      const row = card.getByText('LINK').parentElement!;
      expect(await within(row).findByText('연결됨')).toBeInTheDocument();
      expect(row).toHaveAttribute('data-tone', 'ok');
      expect(card.queryByText(label)).not.toBeInTheDocument();
      page.unmount();
      renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
      const diagnostics = within(await screen.findByRole('region', { name: '텔레메트리 수집기 상태' }));
      expect(await diagnostics.findByText(label)).toBeInTheDocument();
      expect(diagnostics.getByText(/실패 7회/)).toBeInTheDocument();
      expect(screen.queryByText(/앱 heartbeat/)).not.toBeInTheDocument();
    },
  );

  it.each([
    ['STALE', '관측 만료'],
    ['UNKNOWN', '미수신'],
    ['unavailable', '조회 불가'],
    ['other-vm', '미수신'],
  ])('keeps Zephyr LINK %s distinct from a live VM and waiting app', async (state, label) => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      const response = await original(url);
      if (!url.endsWith(`/vms/${VM_IDS.autonomous}/telemetry/heartbeat`)) return response;
      if (state === 'unavailable') return Response.json({}, { status: 503 });
      const report = await response.json();
      return Response.json(
        state === 'other-vm' ? { ...report, vm_id: VM_IDS.manual } : { ...report, status: state },
      );
    });
    renderPage('/dashboard/overview');
    const card = within(await screen.findByRole('article', { name: /autonomous/ }));
    expect(within(card.getByText('VM').parentElement!).getByText('실행 중')).toBeInTheDocument();
    expect(within(card.getByText('APP').parentElement!).getByText('대기')).toBeInTheDocument();
    const link = within(card.getByText('LINK').parentElement!);
    expect(await link.findByText(label)).toBeInTheDocument();
    expect(link.queryByText('연결됨')).not.toBeInTheDocument();
  });

  it('keeps an expired app report separate from a fresh telemetry link', async () => {
    fixture();
    const stale = structuredClone(drive);
    stale.vm_apps![VM_IDS.autonomous]!.drive!.lifecycle_source.status = 'stale';
    useDrive.mockReturnValue({ drive: stale, socket: 'open' });
    renderPage('/dashboard/overview');
    const card = within(await screen.findByRole('article', { name: /autonomous/ }));
    expect(within(card.getByText('APP').parentElement!).getByText('관측 만료')).toBeInTheDocument();
    expect(await within(card.getByText('LINK').parentElement!).findByText('연결됨')).toBeInTheDocument();
  });

  it('keeps the overview card clickable without nesting the recent event link inside another link', async () => {
    fixture();
    renderPage('/dashboard/overview');
    const article = await screen.findByRole('article', { name: /autonomous/ });
    // 카드 자체는 링크가 아니고, 안쪽 링크끼리도 중첩되지 않는다
    expect(article.closest('a')).toBeNull();
    expect(article.querySelector('a a')).toBeNull();
    const card = within(article);
    const links = card.getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      `/dashboard/vms/${VM_IDS.autonomous}`,
      `/dashboard/events?vm=${VM_IDS.autonomous}`,
    ]);
    // 이벤트 줄은 해당 VM 필터가 켜진 이벤트 화면으로
    fireEvent.click(links[1]!);
    expect(await screen.findByText(`events route ?vm=${VM_IDS.autonomous}`)).toBeInTheDocument();
  });

  it('opens the VM detail when the card body is clicked, as the whole-card link did', async () => {
    fixture();
    renderPage('/dashboard/overview');
    const article = await screen.findByRole('article', { name: /autonomous/ });
    fireEvent.click(within(article).getByText('RECENT SIGNAL'));
    const name = vms.find((vm) => vm.vm_id === VM_IDS.autonomous)!.name;
    expect(await screen.findByRole('heading', { level: 1, name })).toBeInTheDocument();
  });

  it('does not present cached Linux Agent status as a current connection when VM lookup fails', async () => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    let failList = false;
    fetch.mockImplementation(async (url: string) =>
      failList && url === '/api/v1/vms' ? Response.json({}, { status: 503 }) : original(url),
    );
    const page = renderPage('/dashboard/overview');
    const card = within(await screen.findByRole('article', { name: /interaction/ }));
    const link = within(card.getByText('LINK').parentElement!);
    expect(await link.findByText('연결됨')).toBeInTheDocument();
    failList = true;
    await act(() => page.client.invalidateQueries({ queryKey: ['vms'], exact: true }));
    expect(await link.findByText('조회 불가')).toBeInTheDocument();
    expect(link.queryByText('연결됨')).not.toBeInTheDocument();
  });

  it('shows the detail header like VM Control with the authority badge beside a driving VM title', async () => {
    fixture();
    useDrive.mockReturnValue({
      drive: {
        ...drive,
        control: {
          mode: 'AUTO',
          generation: 1,
          active_app: 'autonomous',
          active_vm_id: VM_IDS.autonomous,
          reason: null,
          received_ago_ms: 100,
          source: { status: 'fresh', age_ms: 100 },
        },
      },
      socket: 'open',
    });
    const name = vms.find((vm) => vm.vm_id === VM_IDS.autonomous)!.name;
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    const title = await screen.findByRole('heading', { level: 1, name });
    expect(screen.getByRole('link', { name: /VM Dashboard/ })).toHaveAttribute('href', '/dashboard/overview');
    expect(screen.getByText('VIRTUAL MACHINE MONITORING')).toBeInTheDocument();
    expect(screen.getByText('주 구동 VM · Zephyr')).toBeInTheDocument();
    // 제어권 배지는 제목 줄 오른쪽 (같은 줄 묶음 안)
    expect(within(title.parentElement!).getByText('ACTIVE')).toBeInTheDocument();
    // 왼쪽 "운영 상태" 타일 2×2, 오른쪽 "구성" 타일 3개 + 짧은 목록
    const ops = within(screen.getByRole('heading', { name: '운영 상태' }).closest('section')!);
    for (const label of ['VM 상태', '제어권', '주행 모드']) expect(ops.getByText(label)).toBeInTheDocument();
    // 머리말에는 앱 ID 를 붙이지 않고, 줄마다 앞에 앱 라벨 (모르는 앱 ID 는 원문)
    expect(ops.getByText('앱 상태').querySelector('small')).toBeNull();
    expect(ops.getByText('앱 상태').nextElementSibling).toHaveTextContent(
      /^drive · 대기 · 준비 미충족 · awaiting_input$/,
    );
    expect(ops.getByText('자율주행')).toBeInTheDocument();
    const config = within(screen.getByRole('heading', { name: 'VM 구성' }).closest('section')!);
    for (const label of ['vCPU', '메모리', '예약 IPv4', '역할', '템플릿', '버전', 'VM ID'])
      expect(config.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText('VM 기본 정보')).not.toBeInTheDocument();
    expect(screen.queryByText('할당 자원')).not.toBeInTheDocument();
    expect(screen.queryByText('Identity & state')).not.toBeInTheDocument();
    expect(screen.queryByText(/앱 heartbeat/)).not.toBeInTheDocument();
    // 맨 아래 "생명주기 제어" 안내 카드와 VM Control 링크는 없앴다 (머리말 뒤로가기만 남음)
    expect(screen.queryByText('생명주기 제어')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /VM Control에서 관리/ })).not.toBeInTheDocument();
  });

  it('shows the INFOTAINMENT badge beside the title of the Linux interaction VM', async () => {
    fixture();
    const name = vms.find((vm) => vm.vm_id === VM_IDS.interaction)!.name;
    renderPage(`/dashboard/vms/${VM_IDS.interaction}`);
    const title = await screen.findByRole('heading', { level: 1, name });
    // Memory·Network 탭 카드와 같은 배지·색 규칙 (data-authority)
    const badge = within(title.parentElement!).getByText('INFOTAINMENT');
    expect(badge).toHaveAttribute('data-authority', 'infotainment');
    // 주행 VM 이 아니면 제어권·주행 모드 타일은 "해당 없음"
    const ops = within(screen.getByRole('heading', { name: '운영 상태' }).closest('section')!);
    expect(ops.getByText('제어권').nextElementSibling).toHaveTextContent('해당 없음');
    expect(ops.getByText('주행 모드').nextElementSibling).toHaveTextContent('해당 없음');
    expect(ops.getByText('앱 상태').nextElementSibling).toHaveTextContent('미확인');
  });

  it('shows no badge beside the title of a VM without a role', async () => {
    fixture();
    const unassigned = {
      ...vms[0]!,
      vm_id: '44444444-4444-4444-8444-444444444444',
      name: 'spare',
      role: null,
    };
    vms.push(unassigned);
    try {
      renderPage(`/dashboard/vms/${unassigned.vm_id}`);
      const title = await screen.findByRole('heading', { level: 1, name: 'spare' });
      expect(title.parentElement!.children).toHaveLength(1);
      expect(screen.getByText('역할 미지정 · Zephyr')).toBeInTheDocument();
    } finally {
      vms.pop();
    }
  });

  it('shows Zephyr thread pages, heap, RX rate, reserved IP and only its own app reports', async () => {
    const fetch = fixture();
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    const cpuTable = (await screen.findByText('telemetry')).closest('table')!;
    expect(within(cpuTable).getByRole('columnheader', { name: 'ID' })).toBeInTheDocument();
    expect(within(cpuTable).getByText('telemetry').nextElementSibling).toHaveTextContent('1');
    expect(within(cpuTable).getByText('telemetry')).toBeInTheDocument();
    expect(within(cpuTable).queryByText('8.00 KiB')).not.toBeInTheDocument();
    expect(screen.queryByRole('table', { name: '스레드 스택 스냅샷' })).not.toBeInTheDocument();
    expect(screen.queryByText('스레드 스택 스냅샷')).not.toBeInTheDocument();
    // 페이지 누락은 이상 경고라 주의 색, 계측 구간 문구는 화면에서 뺐다
    expect(screen.getByText(/페이지 1\/2 수신/)).toHaveAttribute('data-warn', 'true');
    expect(screen.queryByText(/계측 구간/)).not.toBeInTheDocument();
    // 모르는 사유 코드는 원문 그대로 (title 도 원문)
    expect(screen.getByTitle('awaiting_input')).toHaveTextContent(/^awaiting_input$/);
    expect(screen.getByTitle('awaiting_input').closest('strong')).toHaveTextContent(
      /^drive · 대기 · 준비 미충족 · awaiting_input$/,
    );
    expect(screen.queryByText(/실행 중 · 계산 준비 확인/)).not.toBeInTheDocument();
    expect(screen.getByText('192.168.77.2')).toBeInTheDocument();
    expect(await screen.findByText('KiB/s')).toBeInTheDocument();
    expect(screen.getByText('2.0')).toBeInTheDocument();
    // 커널 힙은 Memory 탭과 같은 현재값의 용량 대비 % + 사용 / 전체 · 여유
    // 이름 옆 같은 줄에 현재 값과 보조 문구, 그래프 라벨에 "· 최근 5분" 없음
    const heap = within(screen.getByText('커널 힙').closest('section')!);
    expect(screen.getByRole('heading', { name: 'CPU·메모리 추이 · 최근 5분' })).toBeInTheDocument();
    for (const old of ['CPU · 최근 5분', '커널 힙 · 최근 5분', '최근 5분 · 실제 수신 데이터'])
      expect(screen.queryByText(old)).not.toBeInTheDocument();
    expect(heap.getByText('5분 전')).toBeInTheDocument();
    expect(heap.getByText('50.0')).toBeInTheDocument();
    expect(heap.getByText('0.50 KiB / 1.00 KiB · 여유 0.50 KiB')).toBeInTheDocument();
    // 수집기는 운영 상태 카드 아래 한 줄 요약. 시각은 모두 상대 시간
    const ops = screen.getByRole('heading', { name: '운영 상태' }).closest('section')!;
    const collector = within(within(ops).getByRole('region', { name: '텔레메트리 수집기 상태' }));
    expect(collector.getByText('수집기')).toBeInTheDocument();
    expect(await collector.findByText('수집 중')).toBeInTheDocument();
    expect(collector.getByText('수집 중')).toHaveAttribute('data-tone', 'ok');
    // 둘째 줄: 정수 초·분 또는 "방금" (소수점 없음)
    const when = String.raw`(방금|\d+초 전|\d+분 전|\d+시간 \d+분 전)`;
    expect(
      collector.getByText(new RegExp(`^heartbeat ${when} · 마지막 성공 ${when} · 실패 2회$`)),
    ).toBeInTheDocument();
    expect(collector.queryByText(/\d\.\d초/)).not.toBeInTheDocument();
    expect(collector.queryByText(/오후|오전|12000 ms|참고/)).not.toBeInTheDocument();
    // 숫자·ID·버전은 고정폭, 한글 문구 값은 일반 글꼴
    expect(screen.getByText(VM_IDS.autonomous)).toHaveAttribute('data-mono', 'true');
    expect(screen.getByText('주 구동 VM', { selector: 'span' })).not.toHaveAttribute('data-mono');
    expect(screen.getByText('192.168.77.2')).toHaveAttribute('data-mono', 'true');
    expect(within(ops).getByText('실행 중').closest('strong')).not.toHaveAttribute('data-mono');
    const config = screen.getByRole('heading', { name: 'VM 구성' }).closest('section')!;
    expect(within(config).queryByRole('region', { name: '텔레메트리 수집기 상태' })).not.toBeInTheDocument();
    expect(fetch.mock.calls.some(([url]) => url.endsWith('/processes'))).toBe(false);
  });

  it('shows the fresh heartbeat in place of a missing lifecycle with its source noted', async () => {
    fixture();
    const missing = structuredClone(drive);
    missing.vm_apps![VM_IDS.manual]!.drive = {
      lifecycle: null,
      lifecycle_source: { status: 'unknown', age_ms: null },
      heartbeat: { state: 'standby' },
      heartbeat_source: { status: 'fresh', age_ms: 100 },
    };
    useDrive.mockReturnValue({ drive: missing, socket: 'open' });
    renderPage(`/dashboard/vms/${VM_IDS.manual}`);
    const ops = within((await screen.findByRole('heading', { name: '운영 상태' })).closest('section')!);
    const value = ops.getByText('앱 상태').nextElementSibling!;
    expect(value).toHaveTextContent(/^drive · 대기heartbeat 기준 · 단계 미수신$/);
    expect(ops.getByText('heartbeat 기준 · 단계 미수신')).toBeInTheDocument();
  });

  it.each([
    ['fresh', '대기'],
    ['stale', '관측 만료'],
  ] as const)('shows the %s Interaction heartbeat on Overview and VM detail', async (status, expected) => {
    fixture();
    const snapshot = structuredClone(drive);
    snapshot.vm_apps![VM_IDS.interaction] = {
      interaction: {
        lifecycle: null,
        lifecycle_source: { status: 'unknown', age_ms: null },
        heartbeat: { state: 'idle' },
        heartbeat_source: { status, age_ms: status === 'fresh' ? 150 : 3500 },
      },
    };
    useDrive.mockReturnValue({ drive: snapshot, socket: 'open' });
    const overview = renderPage('/dashboard/overview');
    const card = within(await screen.findByRole('article', { name: 'interaction' }));
    expect(card.getByText(expected)).toBeInTheDocument();
    overview.unmount();

    renderPage(`/dashboard/vms/${VM_IDS.interaction}`);
    const detail = within((await screen.findByRole('heading', { name: '운영 상태' })).closest('section')!);
    expect(detail.getByText(new RegExp(`인터랙션 · ${expected}`))).toBeInTheDocument();
  });

  it('labels each app line in Korean and shows the reason in Korean with the raw code as title', async () => {
    fixture();
    const labelled = structuredClone(drive);
    const report = (reason: string) => ({
      lifecycle: { phase: 'WAITING' as const, readiness: 'NOT_READY' as const, reason },
      lifecycle_source: { status: 'fresh' as const, age_ms: 100 },
      heartbeat: null,
      heartbeat_source: { status: 'unknown' as const, age_ms: null },
    });
    labelled.vm_apps![VM_IDS.autonomous] = {
      autonomous: report('STATE_PREPARING'),
      manual: report('RESOURCE_RETAINED'),
    };
    useDrive.mockReturnValue({ drive: labelled, socket: 'open' });
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    const ops = within((await screen.findByRole('heading', { name: '운영 상태' })).closest('section')!);
    const lines = ops.getByText('앱 상태').parentElement!.querySelectorAll('strong[data-app]');
    expect([...lines].map((line) => line.textContent)).toEqual([
      '자율주행 · 대기 · 준비 미충족 · 상태 준비 중',
      '수동주행 · 대기 · 준비 미충족 · 자원 회수 대기',
    ]);
    expect(ops.getByText('상태 준비 중')).toHaveAttribute('title', 'STATE_PREPARING');
    expect(ops.getByText('자원 회수 대기')).toHaveAttribute('title', 'RESOURCE_RETAINED');
  });

  // 접힌 표 영역은 상태와 관계없이 같은 틀(표 머리 + VISIBLE_ROWS 행 = 11행 최소 높이) 안에 그린다
  const frameOf = (inside: Element) => inside.closest('[data-state]')!;
  const expectFrame = (frame: Element, state: string) => {
    expect(frame).toHaveAttribute('data-state', state);
    expect((frame as HTMLElement).style.getPropertyValue('--table-rows')).toBe('11');
  };

  it.each([
    ['loading', () => new Promise<Response>(() => {}), '스레드 정보를 불러오는 중…'],
    ['error', async () => Response.json({}, { status: 500 }), '스레드 정보를 불러오지 못했습니다.'],
    [
      'empty',
      async () => Response.json({ vm_id: VM_IDS.autonomous, received_at: null, items: [] }),
      '스레드 보고 없음',
    ],
  ] as const)('keeps the thread table area height while %s', async (state, reply, message) => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) =>
      url.endsWith(`/vms/${VM_IDS.autonomous}/threads`) ? reply() : original(url),
    );
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    const text = await screen.findByText(message);
    expectFrame(frameOf(text), state);
    // 상태 줄 자리는 비어 있어도 표 영역 위에 그대로 있다
    expect(frameOf(text).previousElementSibling?.tagName).toBe('P');
  });

  it.each([
    ['few rows', 3, 'ok'],
    ['ten or more rows', 12, 'ok'],
    ['no rows', 0, 'empty'],
  ] as const)('keeps the same thread table frame with %s', async (_label, count, state) => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) =>
      url.endsWith(`/vms/${VM_IDS.autonomous}/threads`)
        ? Response.json({
            vm_id: VM_IDS.autonomous,
            window_ms: 1000,
            received_at: received,
            stale: false,
            threads_status: 'ok',
            parts_received: 1,
            part_count: 1,
            complete: true,
            conflicting_pages: false,
            items: Array.from({ length: count }, (_, i) => ({
              thread_id: String(i + 1),
              thread_name: `worker_${i + 1}`,
              name_truncated: false,
              cpu_percent: i,
              unavailable_reason: null,
            })),
          })
        : original(url),
    );
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    const status = await screen.findByText(`CPU 높은 순 · ${count}개`);
    const frame = status.parentElement!.nextElementSibling!;
    expectFrame(frame, state);
    expect(frame.querySelector('table')).not.toBeNull();
    // 상태 줄(개수·경고)은 표 영역 밖 위쪽
    expect(frame.contains(status)).toBe(false);
    expect(within(frame as HTMLElement).getAllByRole('row')).toHaveLength(
      // 표 머리 + 보이는 행 (행이 없으면 "표시할 스레드 없음" 한 줄)
      count === 0 ? 2 : Math.min(count, 10) + 1,
    );
  });

  it.each([
    ['loading', () => new Promise<Response>(() => {}), '불러오는 중…'],
    ['error', async () => Response.json({}, { status: 500 }), '프로세스 정보를 불러오지 못했습니다.'],
  ] as const)('keeps the Linux process table area height while %s', async (state, reply, message) => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) =>
      url.endsWith(`/vms/${VM_IDS.interaction}/processes`) ? reply() : original(url),
    );
    renderPage(`/dashboard/vms/${VM_IDS.interaction}`);
    const text = await screen.findByText(message);
    expectFrame(frameOf(text), state);
  });

  it('keeps the same Linux process table frame with few rows', async () => {
    fixture();
    renderPage(`/dashboard/vms/${VM_IDS.interaction}`);
    const status = await screen.findByText('CPU 높은 순 · 1개');
    expectFrame(status.parentElement!.nextElementSibling!, 'ok');
  });

  it('sorts threads by CPU, shows ten rows first and expands the rest', async () => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      if (!url.endsWith(`/vms/${VM_IDS.autonomous}/threads`)) return original(url);
      return Response.json({
        vm_id: VM_IDS.autonomous,
        window_ms: 1000,
        received_at: received,
        stale: false,
        threads_status: 'ok',
        parts_received: 1,
        part_count: 1,
        complete: true,
        conflicting_pages: false,
        items: [
          ...Array.from({ length: 12 }, (_, i) => ({
            thread_id: String(i + 1),
            thread_name: `worker_${i + 1}`,
            name_truncated: false,
            cpu_percent: i === 3 ? null : i * 9,
            unavailable_reason: null,
          })),
          // Zephyr idle 스레드는 목록·개수·더 보기에서 빠지고 상태 줄에 "유휴 N%"
          {
            thread_id: '20',
            thread_name: 'idle',
            name_truncated: false,
            cpu_percent: 99.5,
            unavailable_reason: null,
          },
        ],
      });
    });
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    const table = (await screen.findByText('worker_12')).closest('table')!;
    // [이름 + 작은 회색 ID] [CPU %] [짧은 CPU 막대] (막대 열 제목은 비움)
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((th) => th.textContent),
    ).toEqual(['이름', 'ID', 'CPU %']);
    const rows = () => within(table).getAllByRole('row').slice(1);
    const names = () => rows().map((row) => row.querySelector('td')!.firstChild!.textContent);
    expect(names()).toHaveLength(10);
    expect(names()[0]).toBe('worker_12');
    // ID 는 이름 뒤가 아니라 별도 열 (가운데 정렬)
    expect(rows()[0]!.querySelectorAll('td')[1]).toHaveTextContent('12');
    expect(rows()[0]!.querySelectorAll('td')[1]).toHaveAttribute('data-id', 'true');
    expect(rows()[0]!.querySelector('td')).toHaveTextContent(/^worker_12$/);
    // 상태 줄: 계측 구간·유휴는 빼고 "CPU 높은 순 · N개"만 (이상 경고는 이상할 때만)
    expect(screen.queryByText(/유휴/)).not.toBeInTheDocument();
    expect(screen.queryByText(/계측 구간/)).not.toBeInTheDocument();
    expect(screen.getByText('CPU 높은 순 · 12개')).toBeInTheDocument();
    expect(within(table).queryByText('idle')).not.toBeInTheDocument();
    // 막대 색: Memory 탭과 같은 80% 주의, 95% 위험
    const level = (index: number) => rows()[index]!.querySelector('[data-level]')!.getAttribute('data-level');
    expect([level(0), level(1), level(2), level(3)]).toEqual(['fault', 'warn', 'warn', 'ok']);
    expect(rows()[0]!.querySelectorAll('td')[2]).toHaveTextContent('99.0');
    expect(rows()[0]!.querySelectorAll('td')[3]!.querySelector('[data-level]')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '2개 더 보기' }));
    expect(names()).toHaveLength(12);
    // 값이 없는 스레드는 맨 뒤
    expect(names().at(-1)).toBe('worker_4');
    fireEvent.click(screen.getByRole('button', { name: '접기' }));
    expect(names()).toHaveLength(10);
  });

  it('sorts Linux processes by CPU with right-aligned number columns', async () => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      if (!url.endsWith(`/vms/${VM_IDS.interaction}/processes`)) return original(url);
      return Response.json({
        vm_id: VM_IDS.interaction,
        observed_at: received,
        stale: false,
        items: [
          { pid: 1, name: 'systemd', cpu_percent: 0.2, rss_bytes: 1048576 },
          { pid: 412, name: 'interaction', cpu_percent: 4.2, rss_bytes: 2097152 },
          { pid: 388, name: 'xenova-agent', cpu_percent: 1.1, rss_bytes: null },
        ],
      });
    });
    renderPage(`/dashboard/vms/${VM_IDS.interaction}`);
    const table = (await screen.findByRole('columnheader', { name: '메모리 (RSS)' })).closest('table')!;
    const rows = within(table).getAllByRole('row').slice(1);
    // [이름 + 작은 회색 PID] [CPU 막대] [CPU %] [메모리 RSS]
    expect(rows.map((row) => row.querySelector('td')!.firstChild!.textContent)).toEqual([
      'interaction',
      'xenova-agent',
      'systemd',
    ]);
    expect(rows[0]!.querySelector('td')).toHaveTextContent(/^interaction$/);
    const cells = rows[0]!.querySelectorAll('td');
    expect(cells[1]).toHaveAttribute('data-id', 'true');
    expect(cells[1]).toHaveTextContent('412');
    expect(cells[2]).toHaveAttribute('data-num', 'true');
    expect(cells[2]).toHaveTextContent('4.2');
    expect(cells[3]!.querySelector('[data-level]')).toHaveAttribute('data-level', 'ok');
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((th) => th.textContent),
    ).toEqual(['이름', 'PID', 'CPU %', '메모리 (RSS)']);
    expect(cells[4]).toHaveTextContent('2.0 MiB');
    expect(screen.queryByRole('button', { name: /더 보기/ })).not.toBeInTheDocument();
  });

  it('defers stack diagnostics display while preserving CPU rows', async () => {
    fixture(false);
    renderPage(`/dashboard/vms/${VM_IDS.autonomous}`);
    expect(await screen.findByText('telemetry')).toBeInTheDocument();
    expect(screen.queryByText('현재 Zephyr 펌웨어에서 스택 진단이 꺼져 있습니다.')).not.toBeInTheDocument();
    expect(screen.queryByRole('table', { name: '스레드 스택 스냅샷' })).not.toBeInTheDocument();
  });

  it('keeps Linux on the process API and does not show stale publisher state as current', async () => {
    const fetch = fixture();
    renderPage(`/dashboard/vms/${VM_IDS.interaction}`);
    expect(await screen.findByText('systemd')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '메모리 (RSS)' })).toBeInTheDocument();
    expect(screen.getByText('Linux Agent 생존 (v1)')).toBeInTheDocument();
    const status = within(screen.getByRole('region', { name: '텔레메트리 수집기 상태' }));
    expect(await status.findByText('관측 만료')).toBeInTheDocument();
    expect(status.queryByText('수집 중')).not.toBeInTheDocument();
    expect(status.getByText(/실패 2회/)).toBeInTheDocument();
    expect(fetch.mock.calls.some(([url]) => url.endsWith('/threads'))).toBe(false);
  });

  it('shows Zephyr allocation and heap separately without heartbeat or counter cards in Memory', async () => {
    const fetch = fixture();
    renderPage();
    for (const name of ['autonomous', 'manual']) {
      await waitFor(() =>
        expect(screen.getByRole('progressbar', { name: `${name} 커널 힙 사용률` })).toHaveAttribute(
          'aria-valuenow',
          '50',
        ),
      );
      expect(screen.queryByRole('progressbar', { name: `${name} VM 메모리 사용률` })).not.toBeInTheDocument();
      expect(screen.getByRole('region', { name: `${name} 앱 버퍼 점유` })).toBeVisible();
      for (const group of ['자동', '공통', '수동'])
        expect(screen.getByRole('region', { name: `${name} ${group} 메모리` })).toBeVisible();
    }
    // S15P31E110-409: Memory 탭에는 그래프와 안내 문구가 없다
    expect(await screen.findByRole('progressbar', { name: 'interaction 메모리 사용률' })).toBeInTheDocument();
    expect(document.querySelectorAll('svg')).toHaveLength(0);
    expect(screen.queryByText(/관리 RAM과 내부 버퍼 점유를 분리합니다/)).not.toBeInTheDocument();
    expect(
      screen.queryByText('Zephyr VM 전체 메모리 사용량은 현재 계측되지 않습니다.'),
    ).not.toBeInTheDocument();
    // 커널 힙 세부 값은 행을 펼쳐서 본다 (S15P31E110-409)
    for (const name of ['autonomous', 'manual']) {
      const heap = within(screen.getByRole('group', { name: `${name} 공통 커널 힙` }));
      fireEvent.click(heap.getByRole('button'));
      expect(heap.getAllByText('0.50 KiB').length).toBeGreaterThan(0);
    }
    expect(screen.queryByText('수집기 상태 (v2)')).not.toBeInTheDocument();
    expect(screen.queryByText('TX drop')).not.toBeInTheDocument();
    expect(fetch.mock.calls.some(([url]) => url.endsWith('/telemetry/heartbeat'))).toBe(false);
  });

  it('preserves Linux MemAvailable and disk I/O beside Zephyr RAM, accounted heap and manual buffers', async () => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      const response = await original(url);
      if (!new URL(url, 'http://fixture').pathname.endsWith('/metrics')) return response;
      const body: { items: MetricSample[] } = await response.json();
      const base = body.items[0]!;
      if (url.includes(VM_IDS.interaction)) {
        body.items = [0, 1].map((index) => ({
          ...base,
          boot_id: 'linux-boot',
          observed_at: new Date(Date.now() - 2000 + index * 1000).toISOString(),
          values: [
            ...base.values,
            {
              name: 'memory_available_bytes',
              value: 2 * 1024 * 1024,
              labels: {},
              unit: 'bytes',
              unavailable_reason: null,
            },
            {
              name: 'disk_read_bytes_total',
              value: 1024 + index * 2048,
              labels: { device: 'xvda' },
              unit: 'bytes',
              unavailable_reason: null,
            },
            {
              name: 'disk_write_bytes_total',
              value: 0,
              labels: { device: 'xvda' },
              unit: 'bytes',
              unavailable_reason: null,
            },
          ],
        }));
      } else {
        const ram = memoryFixture('ram-committed');
        base.values.push(...ram.values.filter((value) => !value.name.startsWith('memory.kernel_heap.')), {
          ...base.values[0]!,
          name: 'memory.kernel_heap.accounted_bytes',
          value: 4096,
          raw_value: '4096',
        });
        const result = memoryFixture('manual-memory-result-revoked', Date.now() - 1000);
        body.items.push({
          ...result,
          vm_id: base.vm_id,
          boot_id: base.boot_id,
          telemetry: {
            ...result.telemetry!,
            publisher_instance_id: base.telemetry!.publisher_instance_id,
            context: base.telemetry!.context,
          },
        });
      }
      return Response.json(body);
    });
    renderPage();
    const ram = within(await screen.findByRole('region', { name: 'autonomous 메모리 사용률' }));
    expect(await ram.findByText('25.0%')).toBeInTheDocument();
    expect(ram.queryByText('정적 예약 포함')).not.toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'autonomous 커널 힙 사용률' })).toHaveAttribute(
      'aria-valuenow',
      '13',
    );
    expect(
      within(screen.getByRole('group', { name: 'autonomous 결과 보관' })).getByRole('img'),
    ).toHaveAccessibleName(/철회 관측/);
    // Linux 메모리 사용률 요약: 여유와 그 기준(툴팁)
    const linux = within(screen.getByRole('region', { name: 'interaction 메모리 사용률' }));
    expect(linux.getByText('2 MiB')).toBeInTheDocument();
    expect(linux.getByTitle('여유 = MemAvailable (memory_available_bytes)')).toBeInTheDocument();
    const disk = within(screen.getByRole('region', { name: 'interaction 디스크 I/O' }));
    // 디스크 I/O 는 큰 숫자 + 작은 단위로 나눠 보인다 (S15P31E110-409)
    expect(disk.getByText('3.0').closest('strong')).toHaveTextContent('3.0KiB');
    expect(disk.getByText('2.0 KiB/s')).toBeInTheDocument();
    expect(screen.queryByText('스레드 스택 스냅샷')).not.toBeInTheDocument();
  });

  it('does not resurrect cached five-minute heap data after a new boot in the buffer query', async () => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      const response = await original(url);
      const parsed = new URL(url, 'http://fixture');
      if (!parsed.pathname.endsWith(`/vms/${VM_IDS.autonomous}/metrics`)) return response;
      const from = Date.parse(parsed.searchParams.get('from')!);
      if (Date.now() - from > 90_000) return response;
      const next = memoryFixture('manual-memory-pending-full', Date.now() - 500);
      next.vm_id = VM_IDS.autonomous;
      next.boot_id = 'new-boot';
      return Response.json({ items: [next] });
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole('progressbar', { name: 'manual 커널 힙 사용률' })).toHaveAttribute(
        'aria-valuenow',
        '50',
      ),
    );
    expect(screen.getByRole('progressbar', { name: 'autonomous 커널 힙 사용률' })).not.toHaveAttribute(
      'aria-valuenow',
    );
    expect(
      within(screen.getByRole('group', { name: 'autonomous 대기 작업' })).getByText('작업 대기'),
    ).toBeInTheDocument();
  });

  it('retains an expired heap observation with an explicit label instead of blanking its summary', async () => {
    const fetch = fixture();
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url: string) => {
      const response = await original(url);
      if (!new URL(url, 'http://fixture').pathname.endsWith(`/vms/${VM_IDS.autonomous}/metrics`))
        return response;
      const body: { items: MetricSample[] } = await response.json();
      body.items[0]!.received_at = new Date(Date.now() - 20_000).toISOString();
      return Response.json(body);
    });
    renderPage();
    const heapRow = await screen.findByRole('group', { name: 'autonomous 공통 커널 힙' });
    const heap = within(heapRow);
    expect(await heap.findByText('50.0%')).toBeInTheDocument();
    expect(heap.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
    expect(heap.getByText('50.0%')).toBeInTheDocument();
    expect(heap.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '최근 관측 50.0% · 관측 만료');
    // 접힌 상태에서는 ⚠ 아이콘(aria-label), 펼치면 관측 당시 여유와 주의 색 사유
    expect(heap.getByRole('img')).toHaveAccessibleName('이상: 관측 만료 · 최근 관측값');
    fireEvent.click(heap.getByRole('button'));
    expect(heap.getByText('관측 당시 여유')).toBeVisible();
    expect(heap.getByText('관측 만료 · 최근 관측값')).toBeVisible();
    expect(heapRow.querySelector('[title]')).toBeNull();
  });
});
