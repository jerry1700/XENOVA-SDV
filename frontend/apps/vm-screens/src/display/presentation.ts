import type { Presentation } from './model';

const MODES: Record<string, string> = {
  AUTO: '자율주행',
  MANUAL: '수동주행',
  AUTO_READY: '자율 재개 대기',
  SAFE_STOP: '안전 정지',
};

export function driveSummary(p: Presentation | null, connected: boolean, valid = true) {
  const fresh = connected && valid && p?.state.drive.source.status === 'fresh';
  const mode = fresh ? p.state.drive.mode : 'unknown';
  const authority = fresh ? p.state.drive.authority : null;
  const preparing =
    authority &&
    mode !== 'unknown' &&
    ['AUTO', 'MANUAL'].includes(mode) &&
    (authority.phase !== 'APPLIED' || !authority.output_permitted || !p?.state.drive.active_vm_id);
  const modeLabel = preparing
    ? `${mode === 'AUTO' ? '자율' : '수동'} 모드 선택 · ${authority.phase === 'FAILED' ? '구동 확인 필요' : '구동 준비 중'}`
    : authority && mode === 'SAFE_STOP'
      ? '안전 정지 모드'
      : (MODES[mode] ?? '주행 모드 미확인');
  const active = fresh ? p.state.vms.find((v) => v.vm_id === p.state.drive.active_vm_id) : null;
  const selectedDrive =
    authority && !authority.storage_uncertain
      ? p?.state.vms.find((v) => v.vm_id === authority.selected_vm_id)
      : null;
  return { mode, modeLabel, preparing, active, selectedDrive, authority };
}
