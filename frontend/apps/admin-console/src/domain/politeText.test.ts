import { describe, expect, it } from 'vitest';
import { AGENT_ERROR_TEXT, ERROR_TEXT } from '../api/errorText';
import { SIM_DONE_TEXT, SIM_REJECT_TEXT } from '../api/sim';
import { SAFETY } from './roles';

/** 문장 끝 평서 반말(~다, ~한다)과 해요체. 니다(습니다·입니다)와 '보다'·'껐다 켜' 같은 문장 중간은 제외 (S15P31E110-471) */
const BANMAL = /(?<!니)다(?=[.)]|\s*$|\s—)|(?:어요|아요|해요|에요|예요)(?=[.)]|\s*$)/;

describe('화면 안내 문구는 합니다체', () => {
  it.each([
    ['오류 문구표', Object.values(ERROR_TEXT)],
    ['Agent 오류 문구', Object.values(AGENT_ERROR_TEXT)],
    ['시뮬레이터 거부 문구', Object.values(SIM_REJECT_TEXT)],
    ['시뮬레이터 적용 문구', Object.values(SIM_DONE_TEXT)],
    ['차량 안전 플래그 설명', SAFETY.map((flag) => flag.hint)],
  ])('%s', (_name, texts) => {
    for (const text of texts) expect(text, text).not.toMatch(BANMAL);
  });

  it('검사식이 반말·해요체를 잡고 합니다체·문장 중간 "보다"는 통과시킨다', () => {
    expect('소프트웨어로는 풀 수 없다').toMatch(BANMAL);
    expect('전진을 막는다 (후진은 된다)').toMatch(BANMAL);
    expect('장애물을 놓았어요').toMatch(BANMAL);
    expect('전진을 막습니다 (후진은 됩니다)').not.toMatch(BANMAL);
    expect('시작 시각이 끝 시각보다 앞서야 합니다.').not.toMatch(BANMAL);
  });
});
