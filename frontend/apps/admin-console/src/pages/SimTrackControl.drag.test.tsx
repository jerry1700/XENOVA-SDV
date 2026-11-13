import type { TrackPointer } from '@xenova/ui';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SimView } from '../api/sim';
import { initialSim, snapshotOf } from '../mocks/sim';
import { SimTrackControl } from './SimTrackControl';

const hoisted = vi.hoisted(() => ({
  view: null as unknown as SimView,
  onPointer: null as ((e: TrackPointer) => void) | null,
  move: vi.fn(async () => null),
  place: vi.fn(async () => null),
}));

vi.mock('../api/sim', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/sim')>()),
  useSimState: () => hoisted.view,
  moveSimObstacle: hoisted.move,
  placeSimObstacle: hoisted.place,
}));
vi.mock('../layout/DriveContext', () => ({ useDrive: () => ({ drive: null }) }));
vi.mock('@xenova/ui', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@xenova/ui')>()),
  useToast: () => ({ show: vi.fn() }),
  TrackView: (props: { onPointer?: (e: TrackPointer) => void }) => {
    hoisted.onPointer = props.onPointer ?? null;
    return <div data-testid="track" />;
  },
}));

const snap = snapshotOf(initialSim(0));
const world = snap.world!;
const obstacle = { id: 'o1', kind: 'cone', x: 1, y: 1, w: 0.3 };

const pointer = (type: TrackPointer['type'], x: number, y: number) =>
  act(() => hoisted.onPointer!({ type, x, y, pressed: type === 'move' }));

describe('트랙 조작 지도 — 장애물 끌어 옮기기', () => {
  let now = 1000;
  beforeEach(() => {
    now = 1000;
    vi.spyOn(performance, 'now').mockImplementation(() => now);
    hoisted.move.mockClear();
    hoisted.place.mockClear();
    hoisted.view = {
      available: true,
      pose: snap.pose ?? null,
      world: { ...world, obstacles: [obstacle] } as SimView['world'],
      trail: [],
    };
    render(<SimTrackControl />);
  });
  afterEach(() => vi.restoreAllMocks());

  it('끄는 동안은 120ms 에 한 번만 보내도, 놓은 자리는 반드시 보낸다', () => {
    pointer('down', 1, 1);
    pointer('move', 1.2, 1.1);
    now += 50; // 스로틀 안 — 이 이동은 보내지 않는다
    pointer('move', 1.6, 1.4);
    expect(hoisted.move).toHaveBeenCalledTimes(1);
    pointer('up', 1.6, 1.4);
    expect(hoisted.move).toHaveBeenLastCalledWith('o1', 1.6, 1.4);
    expect(hoisted.place).not.toHaveBeenCalled();
  });

  it('끌지 않고 눌렀다 뗀 장애물은 옮기지 않는다', () => {
    pointer('down', 1, 1);
    pointer('up', 1, 1);
    expect(hoisted.move).not.toHaveBeenCalled();
    expect(hoisted.place).not.toHaveBeenCalled();
  });
});
