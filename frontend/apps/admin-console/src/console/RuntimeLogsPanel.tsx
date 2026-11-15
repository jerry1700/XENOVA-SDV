import { Card } from '@xenova/ui';
import { useEffect, useState } from 'react';
import { errorText } from '../api/errorText';
import { appendLogs, fetchLogs, fetchLogSources, type RuntimeLog, type LogSource } from '../api/logs';
import styles from './RuntimeLogsPanel.module.css';

type Tone = 'ok' | 'recovering' | 'fault' | 'muted';

/** 조회 상태. 오류 메시지는 상태 문구와 섞지 않고 보조 줄·툴팁으로만 보인다 */
type PollState = { kind: 'connecting' } | { kind: 'live' } | { kind: 'error'; detail: string };

const LEVEL_TONE: Record<string, Tone> = {
  DEBUG: 'muted',
  INFO: 'ok',
  WARN: 'recovering',
  ERROR: 'fault',
  FATAL: 'fault',
};

const pad = (value: number, size = 2) => String(value).padStart(size, '0');

/** 관측 시각 → 브라우저 시간대의 HH:mm:ss.SSS. 읽을 수 없으면 원문 그대로 */
function logTime(value: string): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}.${pad(at.getMilliseconds(), 3)}`;
}

function statusOf(paused: boolean, poll: PollState): { tone: Tone; label: string; detail?: string } {
  if (paused) return { tone: 'muted', label: '일시정지' };
  if (poll.kind === 'live') return { tone: 'ok', label: '수신 중 · 1초 간격' };
  if (poll.kind === 'error')
    return { tone: 'fault', label: '연결 끊김 · 마지막 기록 유지', detail: poll.detail };
  return { tone: 'muted', label: '연결 중' };
}

export function RuntimeLogsPanel({ vmId }: { vmId?: string }) {
  const [onlyVm, setOnlyVm] = useState(false);
  const [paused, setPaused] = useState(false);
  const [records, setRecords] = useState<RuntimeLog[]>([]);
  const [sources, setSources] = useState<LogSource[]>([]);
  const [poll, setPoll] = useState<PollState>({ kind: 'connecting' });
  const [gap, setGap] = useState(false);
  const filter = onlyVm ? vmId : undefined;

  useEffect(() => {
    let active = true;
    let cursor: number | undefined;
    let epoch: string | undefined;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();
    if (paused) {
      return () => abort.abort();
    }
    async function poll() {
      let delay = 1000;
      try {
        const page = await fetchLogs({ after: cursor, vmId: filter, signal: abort.signal });
        if (!active) return;
        if (epoch && epoch !== page.epoch) {
          // A replaced DB must not leave a cursor from the previous log history.
          epoch = page.epoch;
          cursor = undefined;
          setRecords([]);
          setGap(true);
          delay = 50;
        } else {
          epoch = page.epoch;
          cursor = page.next_cursor;
          setRecords((old) => appendLogs(old, page.items));
          if (page.history_truncated) setGap(true);
          setPoll({ kind: 'live' });
          if (page.has_more) delay = 100;
          try {
            const known = await fetchLogSources(abort.signal);
            if (active) setSources(known.items);
          } catch {
            if (active) setSources([]);
          }
        }
      } catch (error) {
        if (active) setPoll({ kind: 'error', detail: errorText(error, '조회에 실패했습니다.') });
        delay = 3000;
      } finally {
        if (active)
          timer = setTimeout(() => {
            void poll();
          }, delay);
      }
    }
    void poll();
    return () => {
      active = false;
      abort.abort();
      clearTimeout(timer);
    };
  }, [filter, paused]);

  function resetView() {
    setRecords([]);
    setGap(false);
    setSources([]);
    setPoll({ kind: 'connecting' });
  }

  const status = statusOf(paused, poll);

  return (
    <Card>
      <section aria-label="실행 로그" className={styles.panel}>
        <header className={styles.head}>
          <div className={styles.heading}>
            <span className={styles.eyebrow}>RUNTIME LOGS</span>
            <h2>실행 로그</h2>
          </div>
          <div className={styles.controls}>
            {vmId && (
              <label>
                <input
                  type="checkbox"
                  checked={onlyVm}
                  onChange={(e) => {
                    resetView();
                    setOnlyVm(e.target.checked);
                  }}
                />
                선택 VM만
              </label>
            )}
            <label>
              <input
                type="checkbox"
                checked={paused}
                onChange={(e) => {
                  if (!e.target.checked) resetView();
                  setPaused(e.target.checked);
                }}
              />
              일시정지
            </label>
          </div>
        </header>

        <div className={styles.statusRow}>
          <span className={styles.dot} data-tone={status.tone} role="status" title={status.detail}>
            <i aria-hidden="true" />
            {status.label}
          </span>
          <ul className={styles.sources} aria-label="수집 소스">
            {sources.length ? (
              sources.map((s) => (
                <li
                  key={s.source_id}
                  className={styles.dot}
                  data-tone={s.state === 'connected' ? 'ok' : 'muted'}
                  title={`${s.source_id}: ${s.state === 'connected' ? '수집 연결됨' : '수집 미확인'}`}
                >
                  <i aria-hidden="true" />
                  {s.source_id}
                </li>
              ))
            ) : (
              <li className={styles.dot} data-tone="muted">
                <i aria-hidden="true" />
                수집 소스 미확인
              </li>
            )}
          </ul>
        </div>
        {status.detail && <p className={styles.detail}>오류: {status.detail}</p>}
        <p
          className={styles.hint}
          title="다른 SSH 터미널의 출력은 서비스 로그로 전달해야 모을 수 있습니다. 빈 목록이 시스템 정상을 뜻하지는 않습니다."
        >
          등록된 수집 소스의 기록만 표시합니다.
        </p>
        {gap && (
          <p role="alert" className={styles.alert}>
            보존 한도나 기록 저장소 변경으로 이전 기록 일부를 표시할 수 없습니다.
          </p>
        )}

        <div className={styles.output} aria-label="수집된 로그" role="log">
          {records.length ? (
            <ol>
              {records.map((r) => (
                <li key={r.id} className={styles.row}>
                  <time dateTime={r.observed_at} title={r.observed_at}>
                    {logTime(r.observed_at)}
                  </time>
                  <span className={styles.level} data-tone={LEVEL_TONE[r.level] ?? 'muted'}>
                    {r.level}
                  </span>
                  <span className={styles.cell} title={`${r.source_id}/${r.component}`}>
                    {r.source_id}/{r.component}
                  </span>
                  <span className={styles.cell} title={r.vm_id ?? undefined}>
                    {r.vm_id ?? '—'}
                  </span>
                  <span className={styles.message}>{r.message}</span>
                </li>
              ))}
            </ol>
          ) : (
            <p className={styles.empty}>수집된 로그가 없습니다.</p>
          )}
        </div>
      </section>
    </Card>
  );
}
