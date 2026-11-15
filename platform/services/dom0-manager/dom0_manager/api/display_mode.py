"""차량 표시 장치 모드(HDMI / 휴대폰) 조회와 전환 (계약 4.14). 로직은 dom0_manager.display_mode 에 있다."""

from fastapi import APIRouter, Request, Response

from ..models import DisplayModeStatus, Error, SetDisplayModeRequest

router = APIRouter(tags=["Display mode"])

NO_STORE = {"Cache-Control": "no-store"}


@router.get(
    "/display/mode",
    operation_id="getDisplayMode",
    summary="지금 표시 모드(HDMI / 휴대폰)와 표시 서비스 상태",
    response_model=DisplayModeStatus,
    responses={
        401: {"model": Error, "description": "Unauthorized: Bearer 서비스 토큰 없음·불일치 (미들웨어)"},
        503: {"model": Error, "description": "PlatformUnavailable: 표시 모드 명령 실행 또는 결과 해석 실패. 재시도 가능"},
        504: {"model": Error, "description": "DriverTimeout: 표시 모드 명령이 시한 안에 끝나지 않음, 또는 요청 예산 소진"},
    },
)
def get_display_mode(request: Request, response: Response) -> DisplayModeStatus:
    response.headers.update(NO_STORE)
    return DisplayModeStatus.model_validate(request.app.state.display_mode.status())


@router.post(
    "/display/mode",
    operation_id="setDisplayMode",
    summary="표시 모드 전환. mobile 은 휴대폰 중계를 켜고 HDMI 를 끈다",
    response_model=DisplayModeStatus,
    responses={
        400: {"model": Error, "description": "InvalidArgument: mode 가 hdmi·mobile 이 아님"},
        401: {"model": Error, "description": "Unauthorized: Bearer 서비스 토큰 없음·불일치 (미들웨어)"},
        409: {"model": Error, "description": "InvalidState: 전환하지 않았거나 실패 후 이전 모드로 되돌림. 사유는 platform_code"},
        500: {"model": Error, "description": "DriverError: 전환 실패 후 이전 모드로 완전히 되돌리지 못함(ROLLBACK_INCOMPLETE_CHECK_DISPLAY_SERVICES)"},
        503: {"model": Error, "description": "PlatformUnavailable: 표시 모드 명령 실행 또는 결과 해석 실패"},
        504: {"model": Error, "description": "DriverTimeout: 시작 전 요청 예산 소진(바꾸지 않음) 또는 명령이 240초 안에 끝나지 않음"},
    },
)
def set_display_mode(body: SetDisplayModeRequest, request: Request, response: Response) -> DisplayModeStatus:
    response.headers.update(NO_STORE)
    return DisplayModeStatus.model_validate(request.app.state.display_mode.switch(body.mode.value))
