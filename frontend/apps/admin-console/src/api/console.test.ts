import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleTransport, consoleCloseMessage } from './console';

const auth = vi.hoisted(() => ({ getSession: vi.fn(), refreshSession: vi.fn() }));
vi.mock('./client', () => auth);

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 1;
  bufferedAmount = 0;
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  sent: unknown[] = [];
  close = vi.fn();
  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: unknown) {
    this.sent.push(data);
  }
  ready() {
    this.onmessage?.({ data: JSON.stringify({ type: 'connected', vm_id: 'vm-a' }) });
  }
}

describe('console transport', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeSocket);
    auth.getSession.mockReturnValue({ access_token: 'user-jwt', expires_at: '2099-01-01T00:00:00Z' });
    auth.refreshSession.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function setup() {
    const callbacks = { state: vi.fn(), data: vi.fn() };
    return { transport: new ConsoleTransport('vm-a', callbacks), callbacks };
  }

  it('JWT is sent in the first frame, never in the URL; input waits for upstream readiness', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    expect(ws.url).not.toContain('user-jwt');
    expect(transport.send('ls\r')).toBe(false);
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0] as string)).toEqual({ type: 'auth', access_token: 'user-jwt' });
    ws.ready();
    expect(callbacks.state).toHaveBeenLastCalledWith('connected', expect.any(String));
    expect(transport.send('한글\r\x03')).toBe(true);
    expect(ws.sent[1]).toEqual(new TextEncoder().encode('한글\r\x03'));
    const bytes = new Uint8Array([0xe3, 0x81]).buffer;
    ws.onmessage?.({ data: bytes });
    expect(callbacks.data).toHaveBeenCalledWith(new Uint8Array(bytes));
    transport.dispose();
  });

  it('large paste is split at frame limit, while an excessive backlog sends nothing', async () => {
    const { transport } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    ws.ready();
    expect(transport.send('x'.repeat(65537))).toBe(true);
    expect((ws.sent[0] as Uint8Array).length).toBe(65536);
    expect((ws.sent[1] as Uint8Array).length).toBe(1);
    const count = ws.sent.length;
    ws.bufferedAmount = 262144;
    expect(transport.send('do not partially paste')).toBe(false);
    expect(ws.sent).toHaveLength(count);
    expect(ws.close).toHaveBeenCalledWith(4009, expect.any(String));
    transport.dispose();
  });

  it('disconnect during token refresh does not open a late socket', async () => {
    auth.getSession.mockReturnValue(null);
    let resolve!: (value: boolean) => void;
    auth.refreshSession.mockReturnValue(
      new Promise<boolean>((done) => {
        resolve = done;
      }),
    );
    const { transport } = setup();
    const pending = transport.connect();
    transport.dispose();
    resolve(true);
    await pending;
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it('duplicate rejection is shown without automatic reconnect or input replay', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    ws.onclose?.({ code: 4409 });
    expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(4409));
    expect(transport.send('old input')).toBe(false);
    expect(FakeSocket.instances).toHaveLength(1);
    transport.dispose();
  });

  it('reconnect waits for upstream cleanup acknowledgement, including cancellation during setup', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    transport.disconnect();
    expect(callbacks.state).toHaveBeenLastCalledWith('closing', expect.any(String));
    ws.onopen?.();
    expect(ws.sent.map((frame) => JSON.parse(frame as string).type)).toEqual(['auth', 'disconnect']);
    ws.ready(); // A connected frame already in flight must not re-enable terminal input.
    expect(transport.send('do not send')).toBe(false);
    await transport.connect();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(ws.close).not.toHaveBeenCalled();
    ws.onclose?.({ code: 1000 }); // Server closes only after releasing PTY and VM lease.
    expect(callbacks.state).toHaveBeenLastCalledWith('disconnected', expect.any(String));
    await transport.connect();
    expect(FakeSocket.instances).toHaveLength(2);
    transport.dispose();
  });

  it('disposing releases callbacks and closes the socket; stale frames cannot update UI', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    const stale = ws.onmessage;
    transport.dispose();
    stale?.({ data: JSON.stringify({ type: 'connected', vm_id: 'vm-a' }) });
    expect(ws.close).toHaveBeenCalled();
    expect(callbacks.state).not.toHaveBeenCalledWith('connected', expect.any(String));
  });

  it.each(['opening', 'authenticating'])(
    'bounds %s wait and ignores late readiness on manual retry',
    async (phase) => {
      const { transport, callbacks } = setup();
      await transport.connect();
      const ws = FakeSocket.instances[0]!;
      const lateMessage = ws.onmessage;
      const lateClose = ws.onclose;
      if (phase === 'authenticating') ws.onopen?.();
      await vi.advanceTimersByTimeAsync(15000);
      expect(callbacks.state).toHaveBeenLastCalledWith('error', expect.stringContaining('시간이 초과'));
      expect(ws.close).toHaveBeenCalledOnce();
      expect(transport.send('must not be queued')).toBe(false);
      expect(FakeSocket.instances).toHaveLength(1);
      await transport.connect();
      lateMessage?.({ data: JSON.stringify({ type: 'connected', vm_id: 'vm-a' }) });
      lateClose?.({ code: 1000 });
      expect(callbacks.state).toHaveBeenLastCalledWith('connecting', expect.any(String));
      const retry = FakeSocket.instances[1]!;
      retry.onopen?.();
      retry.ready();
      expect(retry.sent).toHaveLength(1); // Authentication only; no old input replay.
      expect(callbacks.state).toHaveBeenLastCalledWith('connected', expect.any(String));
      transport.dispose();
    },
  );

  it('expires a hung refresh without opening a socket when it later completes', async () => {
    auth.getSession.mockReturnValue(null);
    let finish!: (value: boolean) => void;
    auth.refreshSession.mockReturnValue(
      new Promise<boolean>((resolve) => {
        finish = resolve;
      }),
    );
    const { transport, callbacks } = setup();
    const pending = transport.connect();
    await vi.advanceTimersByTimeAsync(15000);
    expect(callbacks.state).toHaveBeenLastCalledWith('error', expect.stringContaining('시간이 초과'));
    auth.getSession.mockReturnValue({ access_token: 'late-token', expires_at: '2099-01-01T00:00:00Z' });
    finish(true);
    await pending;
    expect(FakeSocket.instances).toHaveLength(0);
    transport.dispose();
  });

  it('a rejected refresh produces an authentication error instead of an unhandled promise', async () => {
    auth.getSession.mockReturnValue(null);
    auth.refreshSession.mockRejectedValue(new Error('offline'));
    const { transport, callbacks } = setup();
    await expect(transport.connect()).resolves.toBeUndefined();
    expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(4401));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('a quiet connected console is not mistaken for a connection timeout', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    FakeSocket.instances[0]!.ready();
    await vi.advanceTimersByTimeAsync(60000);
    expect(callbacks.state).toHaveBeenLastCalledWith('connected', expect.any(String));
    expect(callbacks.data).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    transport.dispose();
  });

  it('late readiness after a server close cannot resurrect the old connection', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    const lateMessage = ws.onmessage;
    ws.onclose?.({ code: 4409 });
    lateMessage?.({ data: JSON.stringify({ type: 'connected', vm_id: 'vm-a' }) });
    expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(4409));
    expect(transport.send('must not send')).toBe(false);
    transport.dispose();
  });

  it('cancelling before the socket opens has a bounded cleanup wait', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    transport.disconnect();
    await vi.advanceTimersByTimeAsync(10000);
    expect(callbacks.state).toHaveBeenLastCalledWith(
      'error',
      expect.stringContaining('점유 해제는 확인되지'),
    );
    expect(FakeSocket.instances[0]!.sent).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
    transport.dispose();
  });

  it('dispose clears pending timers and does not update an unmounted screen', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    transport.dispose();
    const calls = callbacks.state.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(callbacks.state).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('bounds cleanup wait without claiming that the server lease was released', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    ws.onopen?.();
    ws.ready();
    transport.disconnect();
    await vi.advanceTimersByTimeAsync(9999);
    expect(callbacks.state).toHaveBeenLastCalledWith('closing', expect.any(String));
    await transport.connect();
    expect(FakeSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(callbacks.state).toHaveBeenLastCalledWith(
      'error',
      expect.stringContaining('점유 해제는 확인되지'),
    );
    expect(ws.close).toHaveBeenCalledOnce();
    expect(transport.send('stale input')).toBe(false);
    await transport.connect();
    expect(FakeSocket.instances).toHaveLength(2);
    transport.dispose();
  });

  it('rejects a readiness frame for another VM without enabling input', async () => {
    const { transport, callbacks } = setup();
    await transport.connect();
    const ws = FakeSocket.instances[0]!;
    ws.onmessage?.({ data: JSON.stringify({ type: 'connected', vm_id: 'other-vm' }) });
    expect(callbacks.state).toHaveBeenLastCalledWith('error', expect.stringContaining('응답 형식'));
    expect(transport.send('must not send')).toBe(false);
    expect(ws.close).toHaveBeenCalledWith(4003, expect.any(String));
    expect(vi.getTimerCount()).toBe(0);
    transport.dispose();
  });

  it.each([4404, 4409, 1006, 1011])('shows close %s and retries only on explicit action', async (code) => {
    const { transport, callbacks } = setup();
    await transport.connect();
    FakeSocket.instances[0]!.onclose?.({ code });
    await vi.advanceTimersByTimeAsync(30000);
    expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(code));
    expect(FakeSocket.instances).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
    await transport.connect();
    expect(FakeSocket.instances).toHaveLength(2);
    transport.dispose();
  });

  describe('replayed console output', () => {
    async function connected() {
      const { transport } = setup();
      await transport.connect();
      const ws = FakeSocket.instances[0]!;
      ws.onopen?.();
      ws.ready();
      return { transport, ws };
    }

    it('drops terminal answers to replayed queries right after connecting', async () => {
      const { transport, ws } = await connected();
      // xterm answering ESC[6n / DA found in the replayed boot output.
      for (const report of [
        '\x1b[24;1R',
        '\x1b[24;174R',
        '\x1b[?1;2c',
        '\x1b[>0;276;0c',
        '\x1b[0n',
        '\x1b[24;1R\x1b[24;174R',
      ]) {
        expect(transport.send(report)).toBe(true);
      }
      expect(ws.sent).toHaveLength(1); // auth only
      transport.dispose();
    });

    it('still sends what a person types during the settle window', async () => {
      const { transport, ws } = await connected();
      transport.send('\r');
      transport.send('root');
      transport.send('\x1b[A'); // arrow key
      transport.send('24;1R'); // plain text that only looks like a report
      expect(ws.sent.slice(1)).toEqual(
        ['\r', 'root', '\x1b[A', '24;1R'].map((t) => new TextEncoder().encode(t)),
      );
      transport.dispose();
    });

    it('answers live terminal queries after the replay has settled', async () => {
      const { transport, ws } = await connected();
      await vi.advanceTimersByTimeAsync(2000);
      expect(transport.send('\x1b[24;174R')).toBe(true);
      expect(ws.sent[1]).toEqual(new TextEncoder().encode('\x1b[24;174R'));
      transport.dispose();
    });
  });

  describe('login expiry (4401)', () => {
    const soon = (ms: number) => new Date(Date.now() + ms).toISOString();

    it('renews a token that expires within two minutes before connecting', async () => {
      auth.getSession.mockReturnValue({ access_token: 'old-jwt', expires_at: soon(60000) });
      auth.refreshSession.mockImplementation(async () => {
        auth.getSession.mockReturnValue({ access_token: 'new-jwt', expires_at: soon(900000) });
        return true;
      });
      const { transport } = setup();
      await transport.connect();
      expect(auth.refreshSession).toHaveBeenCalledOnce();
      const ws = FakeSocket.instances[0]!;
      ws.onopen?.();
      expect(JSON.parse(ws.sent[0] as string)).toEqual({ type: 'auth', access_token: 'new-jwt' });
      transport.dispose();
    });

    it('a token with more than two minutes left is used without renewal', async () => {
      auth.getSession.mockReturnValue({ access_token: 'user-jwt', expires_at: soon(180000) });
      const { transport } = setup();
      await transport.connect();
      expect(auth.refreshSession).not.toHaveBeenCalled();
      expect(FakeSocket.instances).toHaveLength(1);
      transport.dispose();
    });

    it('expiry close renews once and reconnects without replaying input', async () => {
      const { transport, callbacks } = setup();
      await transport.connect();
      const first = FakeSocket.instances[0]!;
      first.onopen?.();
      first.ready();
      expect(transport.send('typed before expiry')).toBe(true);
      auth.refreshSession.mockImplementation(async () => {
        auth.getSession.mockReturnValue({ access_token: 'renewed-jwt', expires_at: '2099-01-01T00:00:00Z' });
        return true;
      });
      first.onclose?.({ code: 4401 });
      expect(callbacks.state).toHaveBeenLastCalledWith(
        'connecting',
        expect.stringContaining('로그인을 갱신'),
      );
      await vi.advanceTimersByTimeAsync(0);
      expect(auth.refreshSession).toHaveBeenCalledOnce(); // forced even though the local clock says valid
      const second = FakeSocket.instances[1]!;
      second.onopen?.();
      second.ready();
      expect(second.sent).toEqual([JSON.stringify({ type: 'auth', access_token: 'renewed-jwt' })]);
      expect(callbacks.state).toHaveBeenLastCalledWith('connected', expect.any(String));
      // A later expiry of the renewed session may renew again.
      second.onclose?.({ code: 4401 });
      await vi.advanceTimersByTimeAsync(0);
      expect(FakeSocket.instances).toHaveLength(3);
      transport.dispose();
    });

    it('a renewed connection that is rejected again shows the login error and stops', async () => {
      auth.refreshSession.mockResolvedValue(true);
      const { transport, callbacks } = setup();
      await transport.connect();
      FakeSocket.instances[0]!.onclose?.({ code: 4401 });
      await vi.advanceTimersByTimeAsync(0);
      FakeSocket.instances[1]!.onclose?.({ code: 4401 });
      await vi.advanceTimersByTimeAsync(30000);
      expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(4401));
      expect(FakeSocket.instances).toHaveLength(2);
      expect(vi.getTimerCount()).toBe(0);
      transport.dispose();
    });

    it('a failed renewal shows the login error without opening a socket', async () => {
      const { transport, callbacks } = setup();
      await transport.connect();
      auth.refreshSession.mockImplementation(async () => {
        auth.getSession.mockReturnValue(null);
        return false;
      });
      FakeSocket.instances[0]!.onclose?.({ code: 4401 });
      await vi.advanceTimersByTimeAsync(0);
      expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(4401));
      expect(FakeSocket.instances).toHaveLength(1);
      transport.dispose();
    });

    it('waits for the previous lease (4409) only inside the renewal window', async () => {
      auth.refreshSession.mockResolvedValue(true);
      const { transport, callbacks } = setup();
      await transport.connect();
      FakeSocket.instances[0]!.onclose?.({ code: 4401 });
      await vi.advanceTimersByTimeAsync(0);
      for (let i = 1; i <= 5; i++) {
        FakeSocket.instances[i]!.onclose?.({ code: 4409 });
        expect(callbacks.state).toHaveBeenLastCalledWith(
          'connecting',
          expect.stringContaining('정리를 기다렸다가'),
        );
        await vi.advanceTimersByTimeAsync(2000);
      }
      expect(FakeSocket.instances).toHaveLength(7);
      await vi.advanceTimersByTimeAsync(2001); // past the 12 s window opened by the expiry close
      FakeSocket.instances[6]!.onclose?.({ code: 4409 });
      await vi.advanceTimersByTimeAsync(30000);
      expect(callbacks.state).toHaveBeenLastCalledWith('error', consoleCloseMessage(4409));
      expect(FakeSocket.instances).toHaveLength(7);
      transport.dispose();
    });

    it('dispose or a user disconnect cancels a pending renewal reconnect', async () => {
      auth.refreshSession.mockResolvedValue(true);
      const { transport } = setup();
      await transport.connect();
      FakeSocket.instances[0]!.onclose?.({ code: 4401 });
      transport.disconnect();
      await vi.advanceTimersByTimeAsync(30000);
      expect(FakeSocket.instances).toHaveLength(1);
      expect(vi.getTimerCount()).toBe(0);
    });
  });
});
