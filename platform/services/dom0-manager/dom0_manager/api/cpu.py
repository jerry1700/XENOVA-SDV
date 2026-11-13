"""CPU 배치 조회 (S15P31E110-353, 계약 4.11). 조회 전용이며 정책·affinity·VM 을 바꾸지 않는다."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header

from ..models import CpuLayout, CpuPolicyUpdate, Error, UpdateCpuPolicyRequest
from ..service import VmService
from . import get_service
from .vms import _COMMAND_ERRS, _request_key

router = APIRouter(tags=["Resources"])

_DESCRIPTION = (
    "플랫폼 cpu-layout. 200 은 조회 성공이지 배치 준수가 아니다: host.ready·affinity_matches·exclusive_verified·issues 를 함께 본다. "
    "null 은 해당 없음·모름(정지·공유·정책 미설정)이며 성공이 아니다. 긴 변경 작업이 플랫폼 잠금을 쥐고 있으면 기다리지 않고 503(재시도 가능)"
)


@router.get("/cpu-layout", operation_id="getCpuLayout", summary="전체 VM 의 CPU 배치(정책·실제 Xen)", description=_DESCRIPTION,
            response_model=CpuLayout, responses=_COMMAND_ERRS)
def get_cpu_layout(service: VmService = Depends(get_service)) -> CpuLayout:
    return service.cpu_layout()


@router.get("/vms/{vm_id}/cpu-layout", operation_id="getVmCpuLayout", summary="한 VM 의 CPU 배치(정책·실제 Xen)", description=_DESCRIPTION,
            response_model=CpuLayout, responses={400: {"model": Error}, 404: {"model": Error}, **_COMMAND_ERRS})
def get_vm_cpu_layout(vm_id: UUID, service: VmService = Depends(get_service)) -> CpuLayout:
    return service.cpu_layout(vm_id)


@router.put(
    "/cpu-policy", operation_id="updateCpuPolicy", summary="전체 VM CPU 핀닝 정책 변경",
    description=("모든 관리 VM이 정지한 상태에서 전체 정책을 교체한다. 한 VM의 모든 vCPU에는 같은 pCPU 마스크가 적용된다. "
                 "성공 뒤 host_preparation_required가 true이므로 cpu-host-prepare가 완료돼야 다시 VM을 시작할 수 있다."),
    response_model=CpuPolicyUpdate,
    responses={400: {"model": Error}, 404: {"model": Error}, 409: {"model": Error}, **_COMMAND_ERRS},
)
def update_cpu_policy(
    request: UpdateCpuPolicyRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    service: VmService = Depends(get_service),
) -> CpuPolicyUpdate:
    return service.update_cpu_policy(request, _request_key(idempotency_key, "Idempotency-Key"))
