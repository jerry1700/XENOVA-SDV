"""장애 주입 강제 종료 (POST /api/v1/vms/{vm_id}/kill).

정지(stop)는 의도를 stopped 로 기록해 복구 워커가 다시 띄우지 않는다. kill 은 도메인만 즉시 없애고 등록·의도는
그대로 둔다 → 복구 워커가 DOMAIN_ABSENT 로 판정해 다시 띄우는지 시연·검증한다.
"""

from __future__ import annotations

from conftest import VM_A, VM_B, create_body
from dom0_manager.drivers.platform import PlatformDriver
from dom0_manager.drivers.xl import Xl
from dom0_manager.service import DOM0_UUID
from test_platform_driver import FakeManager
from test_xl import XL_INFO, FakeRunner


def test_kill_removes_only_the_domain_and_keeps_registration(client, driver, provisioner):
    assert client.post("/api/v1/vms", json=create_body()).status_code == 201

    killed = client.post(f"/api/v1/vms/{VM_A}/kill")

    assert killed.status_code == 204
    assert client.get(f"/api/v1/vms/{VM_A}").status_code == 404  # 도메인 없음
    assert provisioner.instance_exists(VM_A)  # 등록·디스크 그대로 — 정지와 다르다
    assert driver.events[-1][0] == "kill"
    assert "shutdown" not in [event[0] for event in driver.events]


def test_kill_without_domain_is_409(client):
    assert client.post("/api/v1/vms", json=create_body()).status_code == 201
    assert client.post(f"/api/v1/vms/{VM_A}/stop").status_code == 204

    response = client.post(f"/api/v1/vms/{VM_A}/kill")

    assert response.status_code == 409
    assert response.json()["code"] == "InvalidState"


def test_kill_unregistered_vm_is_404(client):
    response = client.post(f"/api/v1/vms/{VM_B}/kill")
    assert response.status_code == 404
    assert response.json()["code"] == "VMNotFound"


def test_kill_never_touches_dom0(client):
    response = client.post(f"/api/v1/vms/{DOM0_UUID}/kill")
    assert response.status_code in (400, 404)
    assert response.status_code != 204


def test_platform_kill_runs_xl_destroy_on_the_domid_without_platform_stop():
    manager = FakeManager()
    view = manager.register(VM_A, running=True)
    runner = FakeRunner({("xl", "info"): XL_INFO, ("xl", "destroy", str(view["domid"])): ""})
    driver = PlatformDriver(manager, Xl(runner))

    driver.kill(VM_A)

    assert ("xl", "destroy", str(view["domid"])) in [call[0] for call in runner.calls]
    # 플랫폼 stop 을 부르면 의도가 stopped 로 바뀌어 복구가 일어나지 않는다
    assert all(call[0] != "stop" for call in manager.calls)
