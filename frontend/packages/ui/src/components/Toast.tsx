import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Health } from './StatusBadge';
import styles from './Toast.module.css';

interface ToastItem {
  id: number;
  tone: Health | 'info';
  text: ReactNode;
}

interface ToastApi {
  show: (text: ReactNode, tone?: ToastItem['tone'], durationMs?: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** 한 번에 보이는 토스트 수. 넘치면 오래된 것부터 정리한다 (오류가 몰려도 화면을 덮지 않게) */
const MAX_TOASTS = 3;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const show = useCallback<ToastApi['show']>((text, tone = 'info', durationMs = 3000) => {
    const id = nextId.current++;
    setItems((current) => [...current, { id, tone, text }].slice(-MAX_TOASTS));
    window.setTimeout(() => setItems((current) => current.filter((item) => item.id !== id)), durationMs);
  }, []);

  const close = (id: number) => setItems((current) => current.filter((item) => item.id !== id));

  const api = useMemo(() => ({ show }), [show]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className={styles.stack} aria-live="polite">
        {items.map((item) => (
          <div key={item.id} className={[styles.toast, styles[item.tone]].join(' ')}>
            {/* 긴 문구는 약 6줄까지, 넘치면 이 안에서 스크롤 */}
            <div className={styles.text}>{item.text}</div>
            <button
              type="button"
              className={styles.close}
              aria-label="알림 닫기"
              onClick={() => close(item.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error('useToast 는 <ToastProvider> 안에서만 쓴다');
  return api;
}
