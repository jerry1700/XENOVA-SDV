import type { PhoneLinkStatus } from '../api/types';

/** 휴대폰 화면 연결 상태에서 운영자가 할 일. 할 일이 없으면 null */
export function phoneLinkHint(status: PhoneLinkStatus): string | null {
  if (!status.configured) return '보드에 휴대폰 화면 설정(/etc/xenova/display-mobile.json)이 없습니다.';
  if (status.subnet_mismatch) {
    return `휴대폰이 테더링 대역을 바꿨습니다. 보드 설정의 휴대폰 주소(${status.configured_phone_ipv4 ?? '-'})를 지금 휴대폰 주소(${status.usb.gateway ?? '-'})로 고쳐야 합니다.`;
  }
  switch (status.relay) {
    case 'listening':
      return null;
    case 'waiting_link':
      return status.usb.present
        ? 'USB 연결은 보이지만 쓸 수 있는 주소가 없습니다. 휴대폰의 USB 테더링을 껐다 켜 보세요.'
        : 'USB 케이블과 휴대폰의 USB 테더링을 확인해 주세요.';
    case 'starting':
      return '중계를 시작하는 중입니다. 계속되면 USB 테더링과 보드 로그를 확인해 주세요.';
    case 'stopped':
      return '휴대폰 화면 중계가 꺼져 있습니다. 보드에서 모바일 모드를 켜 주세요 (python3 -m xenova_display.display_mode mobile).';
    default:
      return '중계 상태를 확인할 수 없습니다. 보드의 xenova-display-mobile 서비스를 확인해 주세요.';
  }
}
