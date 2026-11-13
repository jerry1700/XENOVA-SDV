"""보드 템플릿 카탈로그 조회(GET /api/v1/templates): 하위 templates 응답을 버전별로 전부 옮기는지,
보드 내부 경로·해시는 빼는지, 요청 예산은 budget_ms 로만 넘기는지, 오류를 계약 코드로 내는지 본다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dom0_manager.config import Settings
from dom0_manager.drivers.mock import MockDriver, MockProvisioner
from dom0_manager.main import create_app
from test_control_operations import Manager, app_for, envelope, failure


def entry(template_id, version, kind="linux"):
    """하위 xenova_catalog.describe() 결과 모양. 응답에 나가면 안 되는 보드 내부 필드를 함께 둔다."""
    linux = kind == "linux"
    return {
        "schema_version": "1.0",
        "template_id": template_id,
        "template_version": version,
        "guest_kind": kind,
        "asset_directory": f"{'templates' if linux else 'rtos-templates'}/{template_id}-{version}",
        "manifest_sha256": "0" * 64,
        "source_template_id": template_id,
        "default_resources": {"vcpus": 1, "memory_mib": 512 if linux else 16},
        "resource_limits": {"vcpus": [1, 4] if linux else [1, 1], "memory_mib": [128, 8192] if linux else [16, 16]},
        "provisioning_profile": {"signals": {"app_heartbeat": None}},
        "capabilities": {"graceful_shutdown": linux, "ssh": linux},
        "source": {"distribution": "debian"} if linux else {},
        "directory": f"/srv/xenova/templates/{template_id}-{version}",
    }


CATALOG = [
    entry("debian-13-arm64", "1.0.7"),
    entry("debian-13-arm64", "1.0.8"),
    entry("ubuntu-24.04-arm64", "1.0.5"),
    entry("zephyr-drive-lattice-lab-arm64", "1.0.8", kind="zephyr"),
]


@pytest.fixture
def manager():
    manager = Manager()
    manager.responses["templates"] = envelope(data={"templates": CATALOG})
    return manager


@pytest.fixture
def client(manager):
    return TestClient(app_for(manager))


def template_calls(manager):
    return [c for c in manager.calls if c["action"] == "templates"]


def test_returns_every_published_version_as_is(client):
    response = client.get("/api/v1/templates")
    assert response.status_code == 200
    body = response.json()
    assert [(t["template_id"], t["template_version"]) for t in body] == [
        (e["template_id"], e["template_version"]) for e in CATALOG
    ]
    assert body[0] == {
        "template_id": "debian-13-arm64",
        "template_version": "1.0.7",
        "guest_kind": "linux",
        "default_resources": {"vcpus": 1, "memory_mib": 512},
        "resource_limits": {"vcpus": [1, 4], "memory_mib": [128, 8192]},
        "capabilities": {"graceful_shutdown": True, "ssh": True},
    }


def test_board_paths_and_hashes_are_not_exposed(client):
    for template in client.get("/api/v1/templates").json():
        assert not {"directory", "asset_directory", "manifest_sha256", "source", "provisioning_profile"} & set(template)


def test_empty_catalog_is_an_empty_list(client, manager):
    manager.responses["templates"] = envelope(data={"templates": []})
    response = client.get("/api/v1/templates")
    assert response.status_code == 200 and response.json() == []


def test_reads_only_templates_without_timeout_spec(client, manager):
    client.get("/api/v1/templates")
    assert {c["action"] for c in manager.calls} == {"templates"}
    # 하위 templates 는 spec timeout_ms 를 받지 않는다
    assert all("timeout_ms" not in c["spec"] for c in template_calls(manager))


def test_request_budget_is_passed_as_budget_ms(client, manager):
    client.get("/api/v1/templates", headers={"X-Request-Budget-Ms": "3000"})
    (call,) = template_calls(manager)
    assert call["budget_ms"] is not None and 0 < call["budget_ms"] <= 3000


@pytest.mark.parametrize("code, status", [
    ("STORAGE_UNAVAILABLE", 503),
    ("OPERATION_IN_PROGRESS", 503),
    ("DEADLINE_EXCEEDED", 504),
    ("ARTIFACT_INVALID", 500),
])
def test_platform_errors_map_to_contract_codes(client, manager, code, status):
    manager.responses["templates"] = failure(code)
    response = client.get("/api/v1/templates")
    assert response.status_code == status
    assert response.json()["platform_code"] == code


def test_drivers_without_templates_are_501():
    client = TestClient(create_app(Settings(driver="mock", log_level="WARNING"), driver=MockDriver(host_cpus=4, total_memory_mib=1024),
                                   provisioner=MockProvisioner()))
    response = client.get("/api/v1/templates")
    assert response.status_code == 501 and response.json()["code"] == "NotSupported"


def test_service_token_is_required(manager):
    client = TestClient(app_for(manager, service_token="secret"))
    assert client.get("/api/v1/templates").status_code == 401
    assert client.get("/api/v1/templates", headers={"Authorization": "Bearer secret"}).status_code == 200
