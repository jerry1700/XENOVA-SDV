import { useEffect, useState } from 'react';
import { fetchVisionFrame, type VisionFrame } from '../api/vision';

/** Jetson limit is 5 frames/s; 4/s leaves headroom for the shared backend cache. */
export const FRAME_INTERVAL_MS = 250;
/** While the camera is missing, ask again slowly. */
export const RETRY_INTERVAL_MS = 3000;

export type VisionStreamState =
  | { status: 'loading' }
  | { status: 'live'; url: string; updatedAt: number }
  | { status: 'unavailable'; code: Extract<VisionFrame, { kind: 'unavailable' }>['code'] };

/** Polls the backend snapshot while the page is visible. Object URLs are revoked as frames are replaced. */
export function useVisionStream(): VisionStreamState {
  const [state, setState] = useState<VisionStreamState>({ status: 'loading' });

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;
    let url: string | null = null;
    let busy = false; // one request at a time

    const schedule = (ms: number) => {
      if (stopped) return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(tick, ms);
    };

    async function tick() {
      timer = null;
      if (stopped) return;
      if (document.visibilityState === 'hidden') return; // resumed by the visibility listener
      controller = new AbortController();
      busy = true;
      let frame: VisionFrame;
      try {
        frame = await fetchVisionFrame(controller.signal);
      } catch {
        return; // aborted on unmount
      } finally {
        busy = false;
      }
      if (stopped) return;
      if (frame.kind === 'frame') {
        const next = URL.createObjectURL(frame.blob);
        if (url) URL.revokeObjectURL(url);
        url = next;
        setState({ status: 'live', url: next, updatedAt: Date.now() });
        schedule(FRAME_INTERVAL_MS);
      } else {
        setState({ status: 'unavailable', code: frame.code });
        schedule(RETRY_INTERVAL_MS);
      }
    }

    const onVisibility = () => {
      if (document.visibilityState === 'visible' && timer === null && !busy) schedule(0);
    };
    document.addEventListener('visibilitychange', onVisibility);
    schedule(0);

    return () => {
      stopped = true;
      document.removeEventListener('visibilitychange', onVisibility);
      if (timer !== null) clearTimeout(timer);
      controller?.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, []);

  return state;
}
