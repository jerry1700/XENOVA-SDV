"""보드 템플릿 카탈로그 조회. 하위 `templates` action 을 HTTP 로 그대로 연다.

보드 /srv/xenova/catalog 에 공개된 템플릿을 버전별로 전부 돌려준다. 최신 고르기·표시 이름은 Spring 몫이고 여기서는 하지 않는다.
보드 내부 경로·해시(directory, asset_directory, manifest_sha256 등)는 응답에서 뺀다.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StrictInt

from ..errors import NotSupportedByPlatform
from .vms import _COMMAND_ERRS

router = APIRouter(tags=["Templates"])


class TemplateResources(BaseModel):
    vcpus: StrictInt
    memory_mib: StrictInt


class TemplateResourceLimits(BaseModel):
    # [최소, 최대]
    vcpus: list[StrictInt]
    memory_mib: list[StrictInt]


class Template(BaseModel):
    # 하위 응답의 나머지 필드(보드 경로·해시·provisioning_profile 등)는 버린다
    model_config = ConfigDict(extra="ignore")
    template_id: str
    template_version: str
    guest_kind: Literal["linux", "zephyr"]
    default_resources: TemplateResources
    resource_limits: TemplateResourceLimits
    capabilities: dict[str, Any]


@router.get(
    "/templates", operation_id="listTemplates", summary="보드에 공개된 템플릿 목록(버전별 전부)",
    description="보드 카탈로그를 변환 없이 버전별로 전부 돌려준다. 같은 template_id 의 여러 버전이 함께 나온다. platform 드라이버 전용(그 외 501)",
    response_model=list[Template], responses=_COMMAND_ERRS,
)
def list_templates(request: Request) -> list[Template]:
    callback = getattr(request.app.state.driver, "templates", None)
    if not callable(callback):
        raise NotSupportedByPlatform("template catalog requires the platform driver")
    return [Template.model_validate(entry) for entry in callback()]
