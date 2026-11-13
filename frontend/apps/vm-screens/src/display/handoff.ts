import { useEffect, useRef, useState } from 'react';
import { playChime } from './chime';

/** 운전자가 반응해야 하는 모드 전환 (2026-11-12 결정: 운전자는 관리자 화면이 아니라 HDMI 화면을 본다) */
export interface HandoffNotice {
  key: number;
  mode: 'MANUAL' | 'AUTO' | 'AUTO_READY' | 'SAFE_STOP';
  tone: 'manual' | 'auto' | 'warning' | 'danger';
  title: string;
  detail: string;
  /** 화면에 머무는 시간 (ms) */
  ms: number;
}

const NOTICE: Record<HandoffNotice['mode'], Omit<HandoffNotice, 'key' | 'mode'>> = {
  MANUAL: { tone: 'manual', title: '수동 전환', detail: '조종기를 잡으세요', ms: 3000 },
  SAFE_STOP: { tone: 'danger', title: '안전 정지', detail: '차량이 멈춥니다 · 조종기 대기', ms: 3000 },
  AUTO_READY: {
    tone: 'warning',
    title: '자율 재개 대기',
    detail: '잠시 뒤 자율주행으로 돌아갑니다',
    ms: 2000,
  },
  AUTO: { tone: 'auto', title: '자율 주행 복귀', detail: '손을 떼도 됩니다', ms: 2000 },
};

const isNoticeMode = (mode: string): mode is HandoffNotice['mode'] => mode in NOTICE;

/**
 * 모드가 바뀐 순간 한 번 안내한다. 'unknown'(연결 끊김·신선하지 않음)은 전환으로 세지 않는다 —
 * 끊겼다 돌아와 같은 모드면 안내하지 않고, 끊긴 사이에 바뀌었으면 돌아온 순간 안내한다.
 * 첫 관측(이전 모드 없음)은 안내하지 않는다.
 */
export function useHandoffNotice(mode: string): HandoffNotice | null {
  const last = useRef<string | null>(null);
  const seq = useRef(0);
  const [notice, setNotice] = useState<HandoffNotice | null>(null);

  useEffect(() => {
    if (mode === 'unknown') return;
    const previous = last.current;
    last.current = mode;
    if (previous === null || previous === mode || !isNoticeMode(mode)) return;
    const key = ++seq.current;
    setNotice({ key, mode, ...NOTICE[mode] });
    playChime(NOTICE[mode].tone);
  }, [mode]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(
      () => setNotice((current) => (current?.key === notice.key ? null : current)),
      notice.ms,
    );
    return () => clearTimeout(timer);
  }, [notice]);

  return notice;
}
