/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import dashboardSource from './DashboardPage.tsx?raw';
import demoSource from './DemoPage.tsx?raw';

// vitest 는 *.module.css?raw 도 CSS 모듈 proxy 로 바꾸므로 원문은 파일에서 직접 읽는다
const pagesCss = readFileSync(join(import.meta.dirname, 'pages.module.css'), 'utf8');

/** 줄 맨 앞(미디어 쿼리 밖)에서 이 클래스로 시작하는 규칙 수 */
const topLevelRules = (className: string) =>
  pagesCss.match(new RegExp(`^\\.${className}(?![\\w-])[^{]*\\{`, 'gm'))?.length ?? 0;

describe('Overview 요약과 장애 시연 요약 카드 클래스 (S15P31E110-471)', () => {
  it('Overview 요약 칸(.summaryItem)은 한 곳에서만 정의한다', () => {
    expect(pagesCss.match(/^\.summaryItem \{/gm)).toHaveLength(1);
  });

  it('장애 시연 요약 카드는 별도 클래스(.scenarioSummaryItem)를 쓴다', () => {
    expect(topLevelRules('scenarioSummaryItem')).toBeGreaterThan(0);
    // 시연 카드 규칙(높이 96px 박스·첫 span 회색)이 Overview 클래스에 걸리지 않는다
    expect(pagesCss).not.toMatch(/^\.summaryItem > span:first-child/m);
    expect(pagesCss).not.toMatch(/^\.summaryItem small/m);
  });

  it('Overview는 summaryItem, 장애 시연은 scenarioSummaryItem을 쓴다', () => {
    expect(dashboardSource).toContain('styles.summaryItem');
    expect(dashboardSource).not.toContain('styles.scenarioSummaryItem');
    expect(demoSource).toContain('styles.scenarioSummaryItem');
    expect(demoSource).not.toContain('styles.summaryItem');
  });
});
