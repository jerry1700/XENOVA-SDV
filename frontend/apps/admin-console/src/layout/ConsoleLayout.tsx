import { AppShell } from '@xenova/ui';
import logo from '@xenova/ui/assets/xenova-logo.png';
import { Link, NavLink, Outlet } from 'react-router';
import { DriveProvider } from './DriveContext';
import { DriveSocketAlert } from './DriveSocketAlert';
import { LiveDataAlertHost } from './LiveDataAlert';
import { MockBoardSwitch } from './MockBoardSwitch';
import { ResumeNotice } from './ResumeNotice';
import styles from './layout.module.css';

/**
 * 관리자 웹 공통 틀: 핵심 업무 영역 메뉴. 계정이 없는 콘솔이라 사용자·로그아웃 영역은 두지 않는다.
 * Vision(/vision)·휴대폰 연결(/phone-link)·Simulator 장애 시연(/simulator)은 라우트는 남겨 두고
 * 메뉴에서만 뺀다 — 주소로 직접 들어가면 열린다. 부하 테스트(/simulator/stress)는 단독 메뉴로 둔다.
 */
export function ConsoleLayout() {
  return (
    <DriveProvider>
      <AppShell
        brand={
          <Link
            to="/dashboard/overview"
            className={styles.brandLink}
            aria-label="VM Dashboard Overview로 이동"
          >
            <img src={logo} alt="Xenova" className={styles.logo} />
            <small className={styles.brandText}>Admin Console</small>
          </Link>
        }
        nav={
          <>
            <NavLink to="/dashboard">VM Dashboard</NavLink>
            <NavLink to="/control">VM Control</NavLink>
            <NavLink to="/simulator/stress">부하 테스트</NavLink>
          </>
        }
      >
        <ResumeNotice />
        <DriveSocketAlert />
        <LiveDataAlertHost />
        <Outlet />
        <MockBoardSwitch />
      </AppShell>
    </DriveProvider>
  );
}
