"""USB 휴대폰 화면 중계 상태 조회와 연결번호 발급 (계약 4.13). 로직은 dom0_manager.phone_link 에 있다."""

from fastapi import APIRouter, Request, Response

from ..models import Error, PhoneLinkStatus, PhonePairingCode

router = APIRouter(tags=["Phone display"])

NO_STORE = {"Cache-Control": "no-store"}


@router.get(
    "/display/phone-link",
    operation_id="getPhoneLink",
    summary="휴대폰 화면 중계 상태와 접속 주소",
    response_model=PhoneLinkStatus,
    responses={
        401: {"model": Error, "description": "Unauthorized: Bearer 서비스 토큰 없음·불일치 (미들웨어)"},
        503: {"model": Error, "description": "PlatformUnavailable: systemctl·ip 실행 또는 중계 파일 읽기 실패. 재시도 가능"},
    },
)
def get_phone_link(request: Request, response: Response) -> PhoneLinkStatus:
    response.headers.update(NO_STORE)
    return PhoneLinkStatus.model_validate(request.app.state.phone_link.status())


@router.post(
    "/display/phone-link/pairing-code",
    operation_id="issuePhonePairingCode",
    summary="휴대폰 연결번호 새로 발급 (연결된 휴대폰은 끊김)",
    response_model=PhonePairingCode,
    responses={
        401: {"model": Error, "description": "Unauthorized: Bearer 서비스 토큰 없음·불일치 (미들웨어)"},
        409: {"model": Error, "description": "InvalidState: 중계가 꺼져 있거나 쓸 수 있는 USB 링크가 없음"},
        503: {"model": Error, "description": "PlatformUnavailable: 신호 전달 실패 또는 2초 안에 새 번호가 나오지 않음. 재시도 가능"},
    },
)
def issue_phone_pairing_code(request: Request, response: Response) -> PhonePairingCode:
    response.headers.update(NO_STORE)
    return PhonePairingCode.model_validate(request.app.state.phone_link.issue_code())
