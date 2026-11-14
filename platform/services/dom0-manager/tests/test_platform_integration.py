"""플랫폼 파트의 실제 xenova_management.Manager 를 가짜 Xen(FakeXen)·임시 registry·임시 카탈로그로 돌려 PlatformDriver 를 검증한다.

저장소(모노레포) 안의 platform/… 모듈과 platform/tests 픽스처를 그대로 빌린다. 보드가 없어도 registry 읽기·조회·시작·정지·보관·생성
경로가 실제 플랫폼 코드와 맞물리는지 확인하는 것이 목적이다.

건너뛰는 경우는 플랫폼 소스 디렉터리가 없을 때(배포 패키지 단독 테스트)와 fcntl 이 없는 비 POSIX 환경뿐이다.
소스가 있는데 import 가 깨지면 테스트 실패다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
PLATFORM_TESTS = REPO / "platform" / "tests"
PLATFORM_FILES = REPO / "platform" / "yocto" / "meta-xenova" / "recipes-core" / "xenova-platform" / "files"
DEFAULTS = PLATFORM_FILES / "defaults.json"  # 공장 VM 예약 IP·내부 IP 풀. 플랫폼이 create 때 IP 를 고르는 근거

if not (PLATFORM_TESTS / "test_xenova_platform.py").is_file() or not (PLATFORM_FILES / "xenova_management.py").is_file():
    pytest.skip("platform sources are not part of this checkout", allow_module_level=True)
try:
    import fcntl  # noqa: F401 - 플랫폼 모듈의 파일 잠금. Linux 전용
except ImportError:
    # Windows 에서는 돌릴 수 없다. CI(Linux)·WSL 에서 돈다. 그 밖의 import 오류는 숨기지 않고 실패로 둔다.
    pytest.skip("platform modules need fcntl (POSIX); run this file on Linux", allow_module_level=True)

for entry in (PLATFORM_TESTS, PLATFORM_FILES, REPO / "platform" / "first-install", REPO / "platform" / "guests" / "common"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import xenova_catalog as catalog  # noqa: E402
import xenova_management as management  # noqa: E402
import xenova_vm as vm  # noqa: E402
from test_xenova_platform import FakeXen, domain, fixture, record  # noqa: E402

from dom0_manager.config import Settings  # noqa: E402
from dom0_manager.drivers.platform import PlatformDriver, PlatformProvisioner, SerializedManager  # noqa: E402
from dom0_manager.drivers.xl import Xl  # noqa: E402
from dom0_manager.main import create_app  # noqa: E402
from test_xl import XL_INFO, FakeRunner  # noqa: E402

UBUNTU = UUID("aaaa0000-1111-4222-8333-123456789abc")  # record('ubuntu-dev') 의 고정 uuid
DEBIAN = UUID("bbbb0000-1111-4222-8333-123456789abc")
NEW_VM = UUID("cccc0000-1111-4222-8333-123456789abc")  # Spring 이 발급했다고 가정하는 UUID
TEMPLATE_ID, TEMPLATE_VERSION = "ubuntu-24.04-arm64", "1.0.0"
DEBIAN_TEMPLATE_ID = "debian-13-arm64"


def _publish_template(data: Path) -> None:
    """플랫폼 파트 테스트의 setUp 과 같은 최소 템플릿·카탈로그. create 가 통과하는 데 필요한 것만 둔다."""
    source = data / "templates" / "source"
    source.mkdir(parents=True)
    for name in ("root.raw", "Image", "initrd", "kernel.config"):
        (source / name).write_bytes(b"fixture")
    with (source / "root.raw").open("r+b") as stream:
        stream.seek(1080)
        stream.write(b"\x53\xef")  # ext4 매직
    h = catalog.sha(source / "Image")
    boot = {"source": {"distribution": "ubuntu"}, "kernel_release": "test", "root_device": "/dev/xvda",
            "kernel_sha256": h, "initrd_sha256": h, "config_sha256": h}
    (source / "boot.json").write_text(json.dumps(boot))
    meta = {"id": "source", "version": "1.0.0", "source": boot["source"], "root_sha256": catalog.sha(source / "root.raw"),
            "files": {p.name: {"sha256": catalog.sha(p), "bytes": p.stat().st_size} for p in source.iterdir()}}
    (source / "template.json").write_text(json.dumps(meta))
    for path in source.iterdir():
        path.chmod(0o444)
    descriptor = {"schema_version": "1.0", "template_id": TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
                  "guest_kind": "linux", "asset_directory": "templates/source", "source_template_id": "source",
                  "manifest_sha256": catalog.sha(source / "template.json")}
    d = data / "catalog" / TEMPLATE_ID
    d.mkdir(parents=True)
    (d / f"{TEMPLATE_VERSION}.json").write_text(json.dumps(descriptor))


class Board:
    """임시 registry 에 ubuntu-dev(실행 중)·debian-dev(등록만) 를 두고 실제 Manager 를 만든다."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        (self.data / "instances").mkdir(parents=True)
        self.run = Path(self.tmp.name) / "run"
        ubuntu, debian = record("ubuntu-dev"), record("debian-dev")
        self.records = {ubuntu["id"]: ubuntu, debian["id"]: debian}
        self.xen = FakeXen(self.data, self.records)
        ubuntu_dir = fixture(self.data, ubuntu)
        debian_dir = fixture(self.data, debian)
        for directory in (ubuntu_dir, debian_dir):
            with (directory / "root.raw").open("r+b") as stream:
                stream.seek(1080)
                stream.write(b"\x53\xef")
        self.xen.rows.append(domain(ubuntu, ubuntu_dir / "root.raw", 1))
        _publish_template(self.data)
        self.platform = vm.Platform(self.data, self.run, command=self.xen, defaults_path=DEFAULTS)
        # 명시적 로컬 시험 프로필이며 서비스/운영 기본 정책으로 배포하지 않는다.
        self.platform.creation_policy_path=self.data/'creation-policy-test.json'
        self.platform.creation_policy_path.write_text(json.dumps({'schema_version':'1.0','call_ms':3600000,
                                                                 'execution_ms':3500000,'cleanup_ms':5000}))
        self.manager = management.Manager(self.platform, preparer=self._prepare, usage_check=lambda *a: None, defaults_path=DEFAULTS)

    def _prepare(self, op):
        """플랫폼 파트 테스트의 prepare() 와 같은 대역: 디스크 복사 대신 작은 픽스처 파일을 만든다.
        내부 실행 이름·MAC·IP 는 플랫폼이 op['assigned'] 로 넘겨준 값을 그대로 쓴다."""
        spec = op["initial_spec"]
        name = op["assigned"]["internal_name"]
        r = record()
        r.update(guest_kind="linux", id=name, name=name, hostname=name, display_name=spec["name"], uuid=op["vm_id"],
                 mac=op["assigned"]["mac"], ipv4=op["assigned"]["ipv4"], memory_mib=spec["memory_mib"], vcpus=spec["vcpus"],
                 autostart=False, creation_spec=spec, creation_operation_id=op["operation_id"],
                 template_manifest_sha256=op["template"]["manifest_sha256"],
                 template_id=spec["template_id"], template_version=spec["template_version"])
        directory = fixture(self.data, r)
        with (directory / "root.raw").open("wb") as stream:
            stream.write(b"\0" * 2048)
            stream.seek(1080)
            stream.write(b"\x53\xef")
        self.records[r["id"]] = r
        return r

    def close(self) -> None:
        self.tmp.cleanup()


@pytest.fixture
def board():
    with patch.object(vm, "storage_check"), patch.object(vm.Platform, "check_network"), patch.object(vm.Platform, "check_xen"):
        b = Board()
        try:
            yield b
        finally:
            b.close()


def make_client(board):
    manager = SerializedManager(board.manager)  # 실제 조립 경로(load_manager)와 같은 포장
    driver = PlatformDriver(manager, Xl(FakeRunner({("xl", "info"): XL_INFO})))
    app = create_app(Settings(driver="platform", log_level="WARNING"), driver=driver, provisioner=PlatformProvisioner(manager))
    return TestClient(app)


@pytest.fixture
def client(board):
    return make_client(board)


def test_real_manager_list_and_get(client):
    listed = client.get("/api/v1/vms")
    assert listed.status_code == 200, listed.text
    assert [(v["vm_id"], v["state"]) for v in listed.json()] == [(str(UBUNTU), "running")]
    assert client.get(f"/api/v1/vms/{UBUNTU}").json()["vm_id"] == str(UBUNTU)
    assert client.get(f"/api/v1/vms/{DEBIAN}").status_code == 404  # 등록만 돼 있고 Xen 에 없음
    assert client.get("/healthz").json() == {"status": "ok"}


def test_real_manager_stop_then_delete(client, board):
    assert client.post(f"/api/v1/vms/{UBUNTU}/stop").status_code == 204
    assert any(call[:2] == ["xl", "shutdown"] for call in board.xen.calls)
    assert client.get("/api/v1/vms").json() == []
    assert client.delete(f"/api/v1/vms/{DEBIAN}").status_code == 204  # 정지 상태 등록 VM → retire
    assert client.get(f"/api/v1/vms/{DEBIAN}").status_code == 404


def test_delete_preserves_assets_in_use(client, board):
    failure = management.ManagementError("ASSET_IN_USE", "test disk is in use")
    with patch.object(board.manager, "usage_check", side_effect=failure):
        response = client.delete(f"/api/v1/vms/{DEBIAN}")
    assert (response.status_code, response.json()["platform_code"]) == (409, "ASSET_IN_USE")
    assert response.json()["context"] == {
        "action": "retire", "stage": "preflight", "accepted": False, "status": "rejected",
        "operation_id": None, "next_action": "check_current_vm",
    }
    assert (board.data / "instances" / "debian-dev" / "root.raw").is_file()
    assert not list((board.data / "operations").glob("*.json"))


@pytest.mark.parametrize("mode", ["implicit", "legacy", "guarded"])
@pytest.mark.parametrize("phase", ["moved", "dhcp-pending", "dhcp-applied"])
def test_delete_resumes_original_retire_after_interruption(client, board, mode, phase):
    spec = {"vm_id": str(DEBIAN)}
    path = f"/api/v1/vms/{DEBIAN}"
    source = board.data / "instances" / "debian-dev"
    disk_inode = (source / "root.raw").stat().st_ino
    operation_ids = []
    options = {"headers": {"Idempotency-Key": "fixture-interrupted-retire"},
               "params": {"expected_lifecycle_generation": 0}} if mode == "guarded" else {}

    def interrupt(saved_phase, op):
        if saved_phase == phase and not operation_ids:
            operation_ids.append(op["operation_id"])
            raise OSError("fixture interrupted retirement")

    with patch.object(board.manager, "checkpoint", interrupt):
        if mode == "legacy":
            failure = board.manager.call("retire", spec, execute=True, idempotency_key=f"retire-{DEBIAN}")
            assert not failure["ok"] and failure["error"]["code"] == "RECOVERY_REQUIRED"
        else:
            failure = client.delete(path, **options)
            assert failure.status_code == 503 and failure.json()["platform_code"] == "RECOVERY_REQUIRED"
    original, = operation_ids
    lookup_path = f"{path}/operations/{original}"
    original_file = board.data / "operations" / f"{original}.json"
    original_bytes = original_file.read_bytes()
    calls = list(board.xen.calls)
    read = client.get(lookup_path, params={"action": "retire"})
    stage = "network_apply" if phase == "dhcp-pending" else "archive_commit"
    assert read.json() == {
        "action": "retire", "stage": stage, "accepted": True, "status": "uncertain",
        "operation_id": original, "next_action": "reconcile_original", "platform_code": "RECOVERY_REQUIRED",
    }
    assert read.status_code == 200
    if mode != "legacy":
        assert failure.json()["context"] == {k: v for k, v in read.json().items() if k != "platform_code"}
        assert "detail" not in failure.json()
    assert client.get(lookup_path, params={"action": "create"}).status_code == 404
    assert client.get(f"/api/v1/vms/{UBUNTU}/operations/{original}", params={"action": "retire"}).status_code == 404
    assert client.get(lookup_path, params={"action": "purge"}).status_code == 400
    assert original_file.read_bytes() == original_bytes and board.xen.calls == calls
    assert not source.exists()
    with patch.object(board.manager, "_prepare_dhcp", wraps=board.manager._prepare_dhcp) as dhcp:
        assert client.delete(path, **options).status_code == 204
        assert dhcp.call_count == (0 if phase == "dhcp-applied" else 1)
    operation_path, = (board.data / "operations").glob("*.json")
    op = json.loads(operation_path.read_text())
    assert (op["operation_id"], op["status"], op["phase"]) == (original, "SUCCEEDED", "complete")
    assert (board.data / op["target"] / "root.raw").stat().st_ino == disk_inode
    assert client.get(lookup_path, params={"action": "retire"}).json()["status"] == "completed"


@pytest.mark.parametrize("parent", ["instances", "tombstones", "operations"])
def test_delete_reconfirms_failed_parent_sync(client, board, parent):
    import errno
    import artifact_io
    import xenova_runtime

    path = f"/api/v1/vms/{DEBIAN}"
    source = board.data / "instances" / "debian-dev"
    sync = xenova_runtime.sync_directory
    if parent == "instances":
        def rename_then_fail(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError(errno.EIO, "archive parent sync unconfirmed")

        publish = artifact_io.publish
        fault = patch.object(management, "publish", side_effect=rename_then_fail)
    else:
        def fail_sync(directory):
            if directory == board.data / parent:
                record = directory / f"{DEBIAN}.json" if parent == "tombstones" else next(directory.glob("*.json"))
                if record.exists() and json.loads(record.read_text()).get("status") in (None, "SUCCEEDED"):
                    raise OSError(errno.EIO, "record parent sync unconfirmed")
            sync(directory)

        fault = patch.object(xenova_runtime, "sync_directory", side_effect=fail_sync)
    with fault:
        assert client.delete(path).status_code == 503
    assert not source.exists()
    original_path, = (board.data / "operations").glob("*.json")
    def fail_retry_sync(directory):
        if directory == board.data / parent:
            raise OSError(errno.EIO, "parent sync still failing")
        sync(directory)

    with patch.object(management, "sync_directory", side_effect=fail_retry_sync) as retry_sync:
        assert client.delete(path).status_code == 503
        retry_sync.assert_any_call(board.data / parent)
    assert client.delete(path).status_code == 204
    assert list((board.data / "operations").glob("*.json")) == [original_path]
    assert json.loads(original_path.read_text())["status"] == "SUCCEEDED"


@pytest.mark.parametrize("legacy_key", [False, True])
def test_delete_after_lost_response_and_after_restore(client, board, legacy_key):
    spec = {"vm_id": str(NEW_VM)}
    body = {**spec, "name": "retire-restore", "template_id": TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
            "vcpus": 1, "memory_mib": 512}
    path = f"/api/v1/vms/{NEW_VM}"
    assert client.post("/api/v1/vms", json=body).status_code == 201
    running = client.delete(path)
    assert (running.status_code, running.json()["platform_code"]) == (409, "VM_NOT_STOPPED")
    assert client.post(path + "/stop").status_code == 204
    source = board.data / "instances" / management.internal_name(str(NEW_VM))
    disk_inode = (source / "root.raw").stat().st_ino

    # The caller does not consume the first success. A new service instance reads the same journal.
    if legacy_key:
        assert board.manager.call("retire", spec, execute=True, idempotency_key=f"retire-{NEW_VM}")["ok"]
    else:
        async def lose_response(scope, receive, send):
            async def drop(message):
                if message["type"] == "http.response.start":
                    assert message["status"] == 204
                    raise ConnectionResetError("fixture lost delete response")

            await client.app(scope, receive, drop)

        with pytest.raises(ConnectionResetError, match="fixture lost delete response"):
            TestClient(lose_response).delete(path)
    first_path, = [p for p in (board.data / "operations").glob("*.json") if json.loads(p.read_text())["action"] == "retire"]
    first_bytes = first_path.read_bytes()
    first = json.loads(first_bytes)
    board.manager = board.manager.fork()
    with patch.object(management, "publish", side_effect=AssertionError("completed retire must not move assets again")):
        retry_client = make_client(board)
        assert retry_client.delete(path).status_code == 204
    assert first_path.read_bytes() == first_bytes
    assert not source.exists()
    assert (board.data / first["target"] / "root.raw").stat().st_ino == disk_inode

    restored = board.manager.call("restore", spec, execute=True, idempotency_key="restore-before-next-delete")
    assert restored["ok"], restored
    second = retry_client.delete(path)
    assert second.status_code == 204, second.text
    view = board.manager.call("get", spec)["data"]["vm"]
    assert (view["lifecycle"], view["generation"]) == ("RETIRED", 3)
    assert not source.exists()
    retires = [json.loads(p.read_text()) for p in (board.data / "operations").glob("*.json")]
    retires = [op for op in retires if op["action"] == "retire"]
    assert len(retires) == 2
    latest, = [op for op in retires if op["expected_generation"] == 2]
    assert latest["operation_id"] != first["operation_id"] and latest["status"] == "SUCCEEDED"
    assert (board.data / latest["target"] / "root.raw").stat().st_ino == disk_inode
    assert first_path.read_bytes() == first_bytes


def test_delete_does_not_report_success_if_completed_archive_is_missing(client, board):
    path = f"/api/v1/vms/{DEBIAN}"
    assert client.delete(path).status_code == 204
    operation_path, = (board.data / "operations").glob("*.json")
    op = json.loads(operation_path.read_text())
    archive = board.data / op["target"]
    archive.rename(archive.with_name(".fixture-hidden"))
    assert client.delete(path).status_code == 503
    assert json.loads(operation_path.read_text()) == op


def test_guarded_delete_recovers_lost_result_and_blocks_old_generation_after_restore(client, board):
    path = f"/api/v1/vms/{NEW_VM}"
    spec = {"vm_id": str(NEW_VM)}
    body = {**spec, "name": "guarded-retire", "template_id": TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
            "vcpus": 1, "memory_mib": 512}
    assert client.post("/api/v1/vms", json=body).status_code == 201
    assert client.post(path + "/stop").status_code == 204
    registration, = (board.data / "operations").glob("*.json")
    old = {"Idempotency-Key": "retire-before-restore"}
    generation = client.get(path + "/lifecycle")
    assert generation.json() == {**spec, "lifecycle": "REGISTERED", "generation": 0}
    assert client.delete(path, headers=old).status_code == 400
    assert client.delete(path, params={"expected_lifecycle_generation": 0}).status_code == 400
    assert client.delete(path, headers=old, params={"expected_lifecycle_generation": -1}).status_code == 400
    assert list((board.data / "operations").glob("*.json")) == [registration]

    async def lose_response(scope, receive, send):
        async def drop(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 204
                raise ConnectionResetError("fixture lost guarded DELETE response")

        await client.app(scope, receive, drop)

    with pytest.raises(ConnectionResetError, match="lost guarded DELETE response"):
        TestClient(lose_response).delete(path, headers=old, params={"expected_lifecycle_generation": 0})
    board.manager = board.manager.fork()
    client = make_client(board)
    journal, = [p for p in (board.data / "operations").glob("*.json") if p != registration]
    original = journal.read_bytes()
    with patch.object(management, "publish", side_effect=AssertionError("same DELETE cannot move twice")):
        found = client.get(path + "/operations", params={"request_key": old["Idempotency-Key"]})
        assert found.status_code == 200 and found.json()["status"] == "completed"
        assert found.json()["operation_id"] == journal.stem
        assert client.delete(path, headers=old, params={"expected_lifecycle_generation": 0}).status_code == 204
        assert client.delete(path).status_code == 204
    assert journal.read_bytes() == original
    restored = board.manager.call("restore", spec, execute=True, idempotency_key="fixture-restore")
    assert restored["ok"], restored
    current = client.get(path + "/lifecycle").json()
    assert current == {**spec, "lifecycle": "REGISTERED", "generation": 2}

    with patch.object(management, "publish", side_effect=AssertionError("old generation cannot move assets")):
        for key in (old["Idempotency-Key"], "old-delete-that-never-arrived"):
            refused = client.delete(path, headers={"Idempotency-Key": key}, params={"expected_lifecycle_generation": 0})
            assert (refused.status_code, refused.json()["platform_code"]) == (409, "TARGET_CHANGED")
        conflict = client.delete(path, headers=old, params={"expected_lifecycle_generation": 2})
        assert (conflict.status_code, conflict.json()["platform_code"]) == (409, "IDEMPOTENCY_CONFLICT")
    assert len(list((board.data / "operations").glob("*.json"))) == 3  # Registration, original retire and restore.
    assert client.get(path + "/operations", params={"request_key": old["Idempotency-Key"]}).json() == found.json()
    assert client.get(path + "/lifecycle").json() == current
    new = {"Idempotency-Key": "new-delete"}
    assert client.post("/api/v1/vms", json=body).status_code == 201
    running = client.delete(path, headers=new, params={"expected_lifecycle_generation": 2})
    assert (running.status_code, running.json()["platform_code"]) == (409, "VM_NOT_STOPPED")
    assert client.post(path + "/stop").status_code == 204
    assert client.delete(path, headers=new, params={"expected_lifecycle_generation": 2}).status_code == 204
    assert client.get(path + "/lifecycle").json() == {**spec, "lifecycle": "RETIRED", "generation": 3}
    assert journal.read_bytes() == original


def test_real_manager_restart_registered_vm(client, board):
    """등록만 된 VM 에 POST /vms 가 오면 create 없이 start 만 한다 (디스크 재사용)."""
    debian = board.records["debian-dev"]
    body = restart_body(debian["vcpus"], debian["memory_mib"])
    wrong_template = client.post("/api/v1/vms", json={**body, "template_id": TEMPLATE_ID})
    assert (wrong_template.status_code, wrong_template.json()["code"]) == (409, "VMSpecConflict")
    assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]
    restarted = client.post("/api/v1/vms", json=body)
    assert restarted.status_code == 201, restarted.text
    assert restarted.json()["vm_id"] == str(DEBIAN)
    assert any(call[:2] == ["xl", "create"] for call in board.xen.calls)


@pytest.mark.parametrize('display_name', ['주행 실험용 Ubuntu', '../경로가 아닌 표시 이름'])
def test_real_manager_create_with_spring_issued_vm_id(client, board, display_name):
    """팀 결정: Spring 발급 vm_id 가 registry·Xen UUID 가 된다. 플랫폼 create 는 vm_id 를 필수로 받고 내부 실행 이름·MAC·IP 를
    스스로 발급한다. 지원 여부 probe 나 조건부 건너뜀 없이 항상 201 과 UUID 일치를 요구한다 (회귀를 숨기지 않는다)."""
    body = {"vm_id": str(NEW_VM), "name": display_name, "template_id": TEMPLATE_ID, "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    created = client.post("/api/v1/vms", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["vm_id"] == str(NEW_VM)
    assert any(c[:2] == ["xl", "create"] for c in board.xen.calls), board.xen.calls
    registered = board.manager.call("get", {"vm_id": str(NEW_VM)})
    assert registered["ok"], registered
    view = registered["data"]["vm"]
    assert view["vm_id"] == str(NEW_VM) and view["configuration"]["uuid"] == str(NEW_VM)
    assert view["name"] == body["name"]
    assert view["configuration"]["display_name"] == body["name"]
    assert view["configuration"]["id"] == management.internal_name(str(NEW_VM))  # 디렉터리 = 플랫폼 내부 실행 이름
    assert view["configuration"]["ipv4"] not in (None, "192.168.77.10", "192.168.77.20")  # 플랫폼이 공장 예약을 피해 할당
    assert any(row["config"]["c_info"]["uuid"] == str(NEW_VM) for row in board.xen.rows)  # Xen 도메인 UUID 도 요청 UUID
    assert client.get(f"/api/v1/vms/{NEW_VM}").json()["vm_id"] == str(NEW_VM)

def test_create_retransmission_does_not_rename_existing_instance(client, board):
    body = {"vm_id": str(NEW_VM), "name": "최초 표시 이름", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    assert client.post("/api/v1/vms", json=body).status_code == 201
    saved = board.manager.call("get", {"vm_id": str(NEW_VM)})["data"]["vm"]
    directory = board.data / "instances" / saved["configuration"]["id"]
    disk_inode = (directory / "root.raw").stat().st_ino
    initial = saved["initial_spec"].copy()
    assert client.post(f"/api/v1/vms/{NEW_VM}/stop").status_code == 204
    assert client.post("/api/v1/vms", json={**body, "name": "변경을 요청하지 않는 재전달"}).status_code == 201
    current = board.manager.call("get", {"vm_id": str(NEW_VM)})["data"]["vm"]
    assert current["name"] == body["name"]
    assert current["configuration"]["display_name"] == body["name"]
    assert current["initial_spec"] == initial
    assert (directory / "root.raw").stat().st_ino == disk_inode


def test_create_conflicts_are_distinct_from_running_replay(client, board):
    body = {"vm_id": str(NEW_VM), "name": "create-spec-conflict", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    assert client.post("/api/v1/vms", json=body).status_code == 201
    original, = (board.data / "operations").glob("*.json")
    registered = original.read_bytes()
    physical_calls = len([call for call in board.xen.calls if call[:2] == ["xl", "create"]])
    conflicts = {"template_id": "debian-13-arm64", "template_version": "1.0.1", "vcpus": 2, "memory_mib": 768}

    for field, value in conflicts.items():
        response = client.post("/api/v1/vms", json={**body, field: value})
        assert (response.status_code, response.json()["code"]) == (409, "VMSpecConflict")
        assert field in response.json()["message"]
    replay = client.post("/api/v1/vms", json={**body, "name": "다른 표시 이름"})
    assert (replay.status_code, replay.json()["code"]) == (409, "VMExists")

    assert client.post(f"/api/v1/vms/{NEW_VM}/stop").status_code == 204
    conflict = client.post("/api/v1/vms", json={**body, "memory_mib": 768})
    assert (conflict.status_code, conflict.json()["code"]) == (409, "VMSpecConflict")
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == physical_calls
    assert original.read_bytes() == registered
    assert client.post("/api/v1/vms", json=body).status_code == 201


def test_create_conflict_uses_original_unregistered_request(client, board):
    body = {"vm_id": str(NEW_VM), "name": "incomplete-create", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    with patch.object(board.manager, "preparer", side_effect=OSError("fixture interrupted before workspace")):
        assert client.post("/api/v1/vms", json=body).status_code == 503
    original, = (board.data / "operations").glob("*.json")
    before = original.read_bytes()
    conflict = client.post("/api/v1/vms", json={**body, "memory_mib": 768})
    assert conflict.status_code == 409
    assert (conflict.json()["code"], conflict.json()["platform_code"]) == ("VMSpecConflict", "IDEMPOTENCY_CONFLICT")
    assert conflict.json()["context"]["operation_id"] is None
    assert conflict.json()["context"]["accepted"] is False
    assert list((board.data / "operations").glob("*.json")) == [original]
    assert original.read_bytes() == before
    assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]
    assert client.post("/api/v1/vms", json=body).status_code == 201


def test_registration_save_failure_does_not_report_acceptance(client, board):
    body = {"vm_id": str(NEW_VM), "name": "record-save-error", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    write = board.manager._write

    def fail_operation_save(path, value):
        if path.parent == board.data / "operations":
            raise OSError("fixture/private/path")
        return write(path, value)

    with patch.object(board.manager, "_write", side_effect=fail_operation_save):
        response = client.post("/api/v1/vms", json=body)
    assert response.status_code == 503
    context = response.json()["context"]
    assert (context["action"], context["stage"], context["accepted"], context["status"]) == ("create", "acceptance", None, "unknown")
    assert "start_submitted" not in context
    assert context["next_action"] == "lookup_original" and context["operation_id"]
    assert "fixture/private/path" not in response.text and "detail" not in response.json()
    missing = client.get(f"/api/v1/vms/{NEW_VM}/operations/{context['operation_id']}", params={"action": "create"})
    assert missing.status_code == 404 and missing.json()["code"] == "OperationNotFound"
    assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]


def test_xen_start_failure_is_separate_from_successful_registration(client, board):
    body = {"vm_id": str(NEW_VM), "name": "start-error", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    board.xen.fail_create = True
    response = client.post("/api/v1/vms", json=body)
    assert response.status_code == 503, response.text
    context = response.json()["context"]
    assert (context["action"], context["stage"], context["accepted"], context["status"]) == ("start", "start_submit", True, "uncertain")
    assert "start_submitted" not in context
    original, = (board.data / "operations").glob("*.json")
    registered = client.get(f"/api/v1/vms/{NEW_VM}/operations/{original.stem}", params={"action": "create"})
    assert registered.json()["status"] == "completed"
    started = client.get(f"/api/v1/vms/{NEW_VM}/operations/{context['operation_id']}", params={"action": "start"})
    assert started.json()["status"] == "uncertain"
    assert started.json()["platform_code"] == response.json()["platform_code"] == "RECONCILE_REQUIRED"
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1


@pytest.mark.parametrize("condition", ["normal", "start-budget-expired", "new-stop-intent"])
def test_create_replay_after_dhcp_deadline_finishes_original_registration(client, board, condition):
    """The same HTTP request can finish registration before a distinct start."""
    from contextlib import ExitStack

    from xenova_execution import short_lock

    from dom0_manager import budget

    now = [100.]
    board.platform.clock = lambda: now[0]
    body = {"vm_id": str(NEW_VM), "name": "registration-deadline", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    with ExitStack() as held:
        def expire(phase, _):
            if phase == "dhcp-applied":
                held.enter_context(short_lock(board.run, "admission"))
                now[0] = 3800.

        with patch.object(board.manager, "checkpoint", expire):
            failed = client.post("/api/v1/vms", json=body)
    assert failed.status_code == 504, failed.text
    context = failed.json()["context"]
    assert (context["action"], context["stage"], context["accepted"]) == ("create", "registration", True)
    journal = board.data / "operations" / f"{context['operation_id']}.json"
    original = json.loads(journal.read_text())
    assert original["phase"] == "dhcp-applied"
    assert original["error"]["code"] == "DEADLINE_EXCEEDED"
    assert now[0] * 1000 >= original["creation"]["deadline_monotonic_ms"]
    disk = board.data / "instances" / original["assigned"]["internal_name"] / "root.raw"
    inode = disk.stat().st_ino
    credential = board.data / "provisioning" / str(NEW_VM) / "agent.env"
    credential.parent.mkdir(parents=True, mode=0o700)
    credential.write_bytes(b"fixture private supply\n")
    credential.chmod(0o600)
    supply_before = credential.read_bytes()
    supply_stat = credential.stat()
    assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]
    lookup = client.get(f"/api/v1/vms/{NEW_VM}/operations/{context['operation_id']}", params={"action": "create"})
    assert lookup.json()["status"] == "uncertain"
    original_bytes = journal.read_bytes()
    pending = client.get(f"/api/v1/vms/{NEW_VM}")
    assert (pending.status_code, pending.json()["platform_code"]) == (409, "TARGET_CHANGED")
    assert "pending_registration" not in pending.text
    unmanaged = domain(board.records[original["assigned"]["internal_name"]], disk, 91)
    board.xen.rows.append(unmanaged)
    try:
        refused = client.post("/api/v1/vms", json=body)
        assert (refused.status_code, refused.json()["platform_code"]) == (409, "TARGET_CHANGED")
        assert journal.read_bytes() == original_bytes
    finally:
        board.xen.rows.remove(unmanaged)
    conflict = client.post("/api/v1/vms", json={**body, "memory_mib": 768})
    assert (conflict.status_code, conflict.json()["code"]) == (409, "VMSpecConflict"), conflict.text
    assert journal.read_bytes() == original_bytes

    if condition == "new-stop-intent":
        record = board.records[original["assigned"]["internal_name"]]
        execution = board.platform.execution()
        execution.accept(record, {"vm_id": str(NEW_VM), "action": "stop", "force": False},
                         key="fixture-new-stop", deadline=now[0] + 10.)
        intent = execution.intent(record)
        refused = client.post("/api/v1/vms", json=body)
        assert (refused.status_code, refused.json()["platform_code"]) == (409, "TARGET_CHANGED")
        assert execution.intent(record) == intent and intent["desired_state"] == "stopped"
        assert journal.read_bytes() == original_bytes
        assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]
        return

    real_budget = budget.start
    save_tomb = board.manager._save_tomb
    with patch.object(board.manager, "preparer", side_effect=AssertionError("must reuse published disk")), \
            patch.object(board.manager, "_prepare_dhcp", side_effect=AssertionError("must reuse applied DHCP")), \
            patch.object(budget, "start", side_effect=lambda ms, **kwargs: real_budget(ms, clock=lambda: now[0], **kwargs)):
        headers = {"X-Request-Budget-Ms": "10000"}
        with ExitStack() as held:
            def stall_registration(*args, **kwargs):
                held.enter_context(short_lock(board.run, "admission"))
                now[0] += 3601.  # The lower create call keeps its own policy budget.
                return save_tomb(*args, **kwargs)

            with patch.object(board.manager, "_save_tomb", side_effect=stall_registration):
                delayed = client.post("/api/v1/vms", json=body, headers=headers)
        assert delayed.status_code == 504, delayed.text
        assert delayed.json()["context"]["operation_id"] == original["operation_id"]
        assert journal.read_bytes() == original_bytes

        def expire_before_start(phase, _):
            if phase == "complete":
                now[0] += 11.

        if condition == "start-budget-expired":
            with patch.object(board.manager, "checkpoint", expire_before_start):
                registration_only = client.post("/api/v1/vms", json=body, headers=headers)
            assert registration_only.status_code == 504, registration_only.text
            context = registration_only.json()["context"]
            assert (context["status"], context["operation_id"], context["start_submitted"]) == (
                "completed", original["operation_id"], False,
            )
            assert json.loads(journal.read_text())["status"] == "SUCCEEDED"
            assert not list((board.data / "control-requests").glob("*.json"))
            assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]
        recovered = client.post("/api/v1/vms", json=body, headers=headers)
        assert recovered.status_code == 201, recovered.text

        completed = client.get(f"/api/v1/vms/{NEW_VM}/operations/{original['operation_id']}", params={"action": "create"})
        assert completed.json()["status"] == "completed"
    finished = json.loads(journal.read_text())
    assert finished["status"] == "SUCCEEDED" and finished["first_error"] == original["error"]
    assert finished["creation"] == original["creation"]
    assert finished["assigned"] == original["assigned"]
    assert disk.stat().st_ino == inode
    assert credential.read_bytes() == supply_before
    assert (credential.stat().st_ino, credential.stat().st_mtime_ns) == (supply_stat.st_ino, supply_stat.st_mtime_ns)
    assert len(list((board.data / "operations").glob("*.json"))) == 1
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1


def test_create_start_timeout_preserves_registration_and_never_launches_over_a_live_domain(client, board):
    now = [100.]
    board.platform.clock = lambda: now[0]
    body = {"vm_id": str(NEW_VM), "name": "start-deadline", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}

    def timeout_after_create(argv, *args, **kwargs):
        result = board.xen(argv, *args, **kwargs)
        if argv[:2] == ["xl", "create"]:
            now[0] += 91.
        return result

    board.platform.command = timeout_after_create
    failed = client.post("/api/v1/vms", json=body)
    assert failed.status_code == 503, failed.text
    context = failed.json()["context"]
    assert (context["action"], context["stage"], context["accepted"], context["status"]) == (
        "start", "start_confirmation", True, "uncertain",
    )
    assert failed.json()["platform_code"] == "RECONCILE_REQUIRED"
    asset, = (board.data / "operations").glob("*.json")
    record = json.loads(asset.read_text())
    assert record["status"] == "SUCCEEDED"
    before = asset.read_bytes()
    start = board.data / "control-requests" / f"{context['operation_id']}.json"
    start_before = start.read_bytes()
    lookup = client.get(f"/api/v1/vms/{NEW_VM}/operations/{context['operation_id']}", params={"action": "start"})
    assert lookup.json()["status"] == "uncertain"
    disk = board.data / "instances" / record["assigned"]["internal_name"] / "root.raw"
    inode = disk.stat().st_ino
    replay = client.post("/api/v1/vms", json=body)
    assert replay.status_code == 409 and replay.json()["code"] == "VMExists", replay.text
    assert asset.read_bytes() == before and start.read_bytes() == start_before
    assert disk.stat().st_ino == inode
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1


@pytest.mark.parametrize("lost_result", ["completed", "uncertain", "unbound", "replaced", "newer-intent"])
def test_keyed_create_recovers_original_start_after_lost_response(client, board, lost_result):
    path = f"/api/v1/vms/{NEW_VM}"
    body = {"vm_id": str(NEW_VM), "name": "tracked-start", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    headers = {"Idempotency-Key": "fixture-original-start"}
    now = [100.]
    board.platform.clock = lambda: now[0]

    if lost_result in ("uncertain", "replaced", "newer-intent"):
        def expire_after_binding(phase, _):
            if phase == "launch-observed":
                now[0] += 91.

        board.platform.execution_checkpoint = expire_after_binding
    elif lost_result == "unbound":
        def expire_after_create(argv, *args, **kwargs):
            result = board.xen(argv, *args, **kwargs)
            if argv[:2] == ["xl", "create"]:
                now[0] += 91.
            return result

        board.platform.command = expire_after_create

    async def lose_response(scope, receive, send):
        async def drop(message):
            if message["type"] == "http.response.start":
                assert message["status"] == (201 if lost_result == "completed" else 503 if lost_result == "unbound" else 500)
                raise ConnectionResetError("fixture lost start response")

        await client.app(scope, receive, drop)

    with pytest.raises(ConnectionResetError, match="lost start response"):
        TestClient(lose_response).post("/api/v1/vms", json=body, headers=headers)
    board.manager = board.manager.fork()
    client = make_client(board)
    journal, = (board.data / "control-requests").glob("*.json")
    asset, = (board.data / "operations").glob("*.json")
    registration = asset.read_bytes()
    original = journal.read_bytes()
    calls = list(board.xen.calls)

    def lookup(vm_id=NEW_VM):
        return client.get(f"/api/v1/vms/{vm_id}/control-operations", params={"request_key": headers["Idempotency-Key"]})

    found = lookup()
    assert found.status_code == 200 and found.json()["status"] == ("completed" if lost_result == "completed" else "uncertain")
    assert (found.json()["action"], found.json()["operation_id"]) == ("start", journal.stem)
    assert lookup(DEBIAN).status_code == 404
    assert journal.read_bytes() == original and board.xen.calls == calls
    changed = client.post("/api/v1/vms", json={**body, "memory_mib": 768}, headers=headers)
    assert (changed.status_code, changed.json()["code"]) == (409, "VMSpecConflict")
    other = client.post("/api/v1/vms", json={**body, "vm_id": str(DEBIAN)}, headers=headers)
    assert (other.status_code, other.json()["code"]) == (409, "VMSpecConflict")

    if lost_result == "replaced":
        # Same domid/name/UUID, different Xen incarnation.
        board.xen.rows[-1] = dict(board.xen.rows[-1])
    if lost_result == "newer-intent":
        assert client.post(path + "/stop", headers={"Idempotency-Key": "newer-stop"}).status_code == 200

    with patch.object(board.manager, "preparer", side_effect=AssertionError("must not prepare a new disk")):
        replay = client.post("/api/v1/vms", json=body, headers=headers)
    if lost_result in ("unbound", "replaced", "newer-intent"):
        expected_error = (409, "TARGET_CHANGED") if lost_result == "newer-intent" else (503, "RECONCILE_REQUIRED")
        assert (replay.status_code, replay.json()["platform_code"]) == expected_error
        assert replay.json()["context"]["operation_id"] == journal.stem
        assert lookup().json()["status"] == "uncertain"
        assert journal.read_bytes() == original and asset.read_bytes() == registration
        assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1
        return
    assert replay.status_code == 200, replay.text
    assert (replay.json()["operation_id"], replay.json()["status"]) == (journal.stem, "completed")
    assert lookup().json()["status"] == "completed"
    assert client.get(path + f"/operations/{journal.stem}", params={"action": "start"}).json()["status"] == "completed"
    after = json.loads(journal.read_bytes())
    before = json.loads(original)
    for field in ("operation_id", "request", "intent_revision", "deadline_monotonic_ms"):
        assert after[field] == before[field]
    if lost_result == "uncertain":
        assert after["reconciliation"]["original_error"] == before["error"]
    assert asset.read_bytes() == registration
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1

    assert client.post(path + "/stop").status_code == 204
    assert client.post("/api/v1/vms", json=body, headers=headers).json()["operation_id"] == journal.stem
    assert client.get(path).status_code == 404
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1


@pytest.mark.parametrize("expiry", ["after-registration", "before-manager-start"])
def test_create_http_budget_can_expire_after_registration_before_xen_start(board, expiry):
    from dom0_manager import budget

    now = [100.]
    board.platform.clock = lambda: now[0]
    real_budget = budget.start
    body = {"vm_id": str(NEW_VM), "name": "start-not-submitted", "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION, "vcpus": 1, "memory_mib": 512}
    with make_client(board) as client, patch.object(
        budget, "start", side_effect=lambda ms, **kwargs: real_budget(ms, clock=lambda: now[0], **kwargs),
    ):
        def expire(phase, _):
            if phase == "complete" and expiry == "after-registration":
                now[0] += 11.

        call = SerializedManager.call

        def wait_for_manager(manager, action, *args, **kwargs):
            if action == "start" and expiry == "before-manager-start":
                now[0] += 11.
            return call(manager, action, *args, **kwargs)

        with patch.object(board.manager, "checkpoint", expire), \
                patch.object(SerializedManager, "call", wait_for_manager), \
                patch.object(board.manager, "call", wraps=board.manager.call) as manager_call:
            failed = client.post("/api/v1/vms", json=body, headers={"X-Request-Budget-Ms": "10000"})
        assert [entry.args[0] for entry in manager_call.call_args_list] == ["get", "get", "create"]
        assert failed.status_code == 504, failed.text
        assert not list((board.data / "control-requests").glob("*.json"))
        assert not [call for call in board.xen.calls if call[:2] == ["xl", "create"]]
        journal, = (board.data / "operations").glob("*.json")
        original = journal.read_bytes()
        assert json.loads(original)["status"] == "SUCCEEDED"
        error = failed.json()
        assert (error["code"], error["platform_code"]) == ("DriverTimeout", "DEADLINE_EXCEEDED")
        assert error["context"] == {
            "action": "create", "stage": "completion", "accepted": True, "status": "completed",
            "operation_id": journal.stem, "next_action": "lookup_original", "start_submitted": False,
        }
        completed = client.get(f"/api/v1/vms/{NEW_VM}/operations/{journal.stem}", params={"action": "create"})
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"
        assert "start_submitted" not in completed.json()
        wrong_action = client.get(f"/api/v1/vms/{NEW_VM}/operations/{journal.stem}", params={"action": "start"})
        assert wrong_action.status_code == 404
        with patch.object(board.manager, "preparer", side_effect=AssertionError("registered disk must not be copied again")):
            recovered = client.post("/api/v1/vms", json=body, headers={"X-Request-Budget-Ms": "10000"})
        assert recovered.status_code == 201, recovered.text
    assert journal.read_bytes() == original
    assert len([call for call in board.xen.calls if call[:2] == ["xl", "create"]]) == 1


# ---- 정지 요청 추적 (S15P31E110-305): 실제 Manager.fork·budget_ms·control-operation ----


def keyed_stop(client, vm_id, key, **headers):
    return client.post(f"/api/v1/vms/{vm_id}/stop", headers={"Idempotency-Key": key, **headers})


def lookup(client, vm_id, key):
    return client.get(f"/api/v1/vms/{vm_id}/control-operations", params={"request_key": key})


def shutdowns(board):
    return [call for call in board.xen.calls if call[:2] == ["xl", "shutdown"]]


def test_stop_admission_failure_after_durable_intent_is_not_a_rejection(client, board):
    from xenova_execution import Execution

    with patch.object(Execution, "set_admission", side_effect=OSError("fixture/private/admission")):
        response = keyed_stop(client, UBUNTU, "stop-admission-error")
    assert response.status_code == 503, response.text
    context = response.json()["context"]
    assert (context["action"], context["stage"], context["accepted"], context["status"]) == (
        "stop", "acceptance", True, "uncertain",
    )
    assert context["operation_id"] and shutdowns(board) == []
    assert "fixture/private/admission" not in response.text
    original = lookup(client, UBUNTU, "stop-admission-error").json()
    assert (original["operation_id"], original["phase"]) == (context["operation_id"], "accepted")


@pytest.mark.parametrize("key", [None, "stop-timeout"])
def test_shutdown_timeout_reports_confirmation_and_original_id(client, board, key):
    now = [100.]
    board.platform.clock = lambda: now[0]
    board.platform.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    board.xen.hang_shutdown = True
    response = client.post(f"/api/v1/vms/{UBUNTU}/stop", headers={"Idempotency-Key": key} if key else {})
    assert response.status_code == 503, response.text
    context = response.json()["context"]
    assert (context["action"], context["stage"], context["accepted"], context["status"]) == (
        "stop", "shutdown_confirmation", True, "uncertain",
    )
    operation_id = context["operation_id"]
    assert context["next_action"] == "lookup_original" and operation_id
    read = client.get(f"/api/v1/vms/{UBUNTU}/operations/{operation_id}", params={"action": "stop"})
    assert read.json()["status"] == "uncertain" and read.json()["operation_id"] == operation_id
    if key:
        assert lookup(client, UBUNTU, key).json()["operation_id"] == operation_id
    assert len(shutdowns(board)) == 1


@pytest.mark.parametrize("key", [None, "late-stop"])
def test_late_shutdown_is_settled_by_replaying_the_original_stop(client, board, key):
    now = [100.]
    board.platform.clock = lambda: now[0]
    board.platform.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    board.xen.hang_shutdown = True
    path = f"/api/v1/vms/{UBUNTU}/stop"
    headers = {"Idempotency-Key": key} if key else {}
    failed = client.post(path, headers=headers)
    assert failed.status_code == 503, failed.text
    operation_id = failed.json()["context"]["operation_id"]
    journal = board.data / "control-requests" / f"{operation_id}.json"
    before = json.loads(journal.read_text())
    intent = board.data / "control" / f"{UBUNTU}.json"
    original_intent = intent.read_bytes()
    if key:
        present = client.post(path, headers=headers)
        assert present.status_code == 503 and present.json()["context"]["status"] == "uncertain"
    board.xen.rows.clear()  # Xen fixture: the already-issued shutdown takes effect after its deadline.
    read = client.get(f"/api/v1/vms/{UBUNTU}/operations/{operation_id}", params={"action": "stop"})
    assert read.json()["status"] == "uncertain"
    assert json.loads(journal.read_text()) == before
    recovered = client.post(path, headers=headers)
    assert recovered.status_code == (200 if key else 204), recovered.text
    if key:
        assert (recovered.json()["operation_id"], recovered.json()["status"]) == (operation_id, "completed")
        assert lookup(client, UBUNTU, key).json()["status"] == "completed"
    after = json.loads(journal.read_text())
    assert after["phase"] == "completed"
    assert after["reconciliation"]["original_error"] == before["error"]
    assert all(after[field] == before[field] for field in (
        "operation_id", "idempotency_key", "request_hash", "intent_revision", "deadline_monotonic_ms",
    ))
    assert intent.read_bytes() == original_intent and len(shutdowns(board)) == 1
    assert not [call for call in board.xen.calls if call[:2] == ["xl", "destroy"]]


def test_real_keyed_stop_replay_lookup_and_conflict(client, board):
    first = keyed_stop(client, UBUNTU, "stop-ubuntu-1", **{"X-Request-Budget-Ms": "20000"})  # 실제 Manager 가 budget_ms 를 받는다
    assert first.status_code == 200, first.text
    original = first.json()
    assert (original["status"], original["accepted"]) == ("completed", True) and original["operation_id"]
    assert len(shutdowns(board)) == 1

    again = keyed_stop(client, UBUNTU, "stop-ubuntu-1")  # 응답을 잃었다고 보고 같은 키로 재전달
    assert again.status_code == 200, again.text
    assert (again.json()["status"], again.json()["operation_id"]) == ("completed", original["operation_id"])
    assert len(shutdowns(board)) == 1  # 완료 replay 는 물리 동작을 반복하지 않는다

    found = lookup(client, UBUNTU, "stop-ubuntu-1")
    assert found.status_code == 200, found.text
    assert (found.json()["status"], found.json()["operation_id"], found.json()["action"]) == ("completed", original["operation_id"], "stop")

    conflict = keyed_stop(client, DEBIAN, "stop-ubuntu-1")  # 같은 키를 다른 VM 에
    assert conflict.status_code == 409 and conflict.json()["platform_code"] == "IDEMPOTENCY_CONFLICT"
    assert lookup(client, UBUNTU, "never-sent").status_code == 404


def test_lost_stop_response_can_be_recovered_after_retirement(client, board):
    stopped = keyed_stop(client, UBUNTU, "stop-before-archive")
    assert stopped.status_code == 200, stopped.text
    assert client.delete(f"/api/v1/vms/{UBUNTU}").status_code == 204
    operation_id = lookup(client, UBUNTU, "stop-before-archive").json()["operation_id"]
    count = len(board.xen.calls)
    replayed = keyed_stop(client, UBUNTU, "stop-before-archive")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["operation_id"] == operation_id
    assert replayed.json()["status"] == "completed"
    assert len(board.xen.calls) == count and len(shutdowns(board)) == 1


def test_completed_stop_replay_requires_verified_storage(client, board):
    stopped = keyed_stop(client, UBUNTU, "completed-before-storage-error")
    assert stopped.status_code == 200, stopped.text
    with patch.object(vm, "storage_check", side_effect=OSError("test storage unavailable")):
        replay = keyed_stop(client, UBUNTU, "completed-before-storage-error")
    assert replay.status_code == 503, replay.text
    assert replay.json()["context"]["status"] == "uncertain"
    assert replay.json()["context"]["operation_id"] == stopped.json()["operation_id"]
    assert len(shutdowns(board)) == 1


@pytest.mark.parametrize("lock_still_held", [False, True])
def test_expired_pending_stop_does_not_return_202_again(client, board, lock_still_held):
    from contextlib import nullcontext
    from xenova_runtime import operation_lock

    now = [100.]
    board.platform.clock = lambda: now[0]
    with operation_lock(board.run):
        pending = keyed_stop(client, UBUNTU, "expires-before-dispatch")
    assert pending.status_code == 202, pending.text
    now[0] += 91.
    with operation_lock(board.run) if lock_still_held else nullcontext():
        response = keyed_stop(client, UBUNTU, "expires-before-dispatch")
    assert response.status_code == 504, response.text
    body = response.json()
    assert body["platform_code"] == "DEADLINE_EXCEEDED"
    assert body["operation"]["execution_deadline_expired"] is True
    assert body["context"]["operation_id"] == pending.json()["operation_id"]
    assert lookup(client, UBUNTU, "expires-before-dispatch").json()["execution_deadline_expired"] is True
    assert shutdowns(board) == []


def test_keyed_stop_can_be_found_during_an_in_flight_http_response(client, board):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    entered, release = Event(), Event()

    def hold_command(argv, *args, **kwargs):
        if argv[:2] == ["xl", "shutdown"]:
            entered.set()
            assert release.wait(5)
        return board.xen(argv, *args, **kwargs)

    board.platform.command = hold_command
    key = "response-still-in-flight"
    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(keyed_stop, client, UBUNTU, key)
        try:
            assert entered.wait(5)
            with pytest.raises(TimeoutError):
                original.result(timeout=.01)
            pending = lookup(client, UBUNTU, key)
            assert pending.status_code == 200, pending.text
            body = pending.json()
            assert (body["phase"], body["status"]) == ("executing", "pending")
        finally:
            release.set()
        assert original.result(timeout=5).status_code == 200
    recovered = keyed_stop(client, UBUNTU, key)
    assert (recovered.status_code, recovered.json()["status"]) == (200, "completed")
    assert recovered.json()["operation_id"] == body["operation_id"]
    assert len(shutdowns(board)) == 1


def test_real_keyed_stop_is_accepted_while_a_long_operation_holds_the_execution_lock(client, board):
    """긴 start·복구가 플랫폼 실행 잠금을 쥔 동안: 정지 의도는 내구 수락(202)되고 물리 정지는 없다. 잠금이 풀린 뒤 같은 키로 이행한다."""
    from xenova_runtime import operation_lock

    with operation_lock(board.manager.p.runtime):
        pending = keyed_stop(client, UBUNTU, "stop-during-long")
        assert pending.status_code == 202, pending.text
        accepted_op = pending.json()
        assert (accepted_op["status"], accepted_op["accepted"], accepted_op["phase"]) == ("pending", True, "accepted")
        assert accepted_op["operation_id"] and accepted_op["intent_revision"]
        assert shutdowns(board) == []
        found = lookup(client, UBUNTU, "stop-during-long")  # 조회는 실행 잠금을 기다리지 않는다
        assert found.status_code == 200 and (found.json()["status"], found.json()["operation_id"]) == ("pending", accepted_op["operation_id"])
    done = keyed_stop(client, UBUNTU, "stop-during-long")
    assert done.status_code == 200, done.text
    assert (done.json()["status"], done.json()["operation_id"]) == ("completed", accepted_op["operation_id"])
    assert len(shutdowns(board)) == 1


def reboots(board):
    return [call for call in board.xen.calls if call[:2] in (["xl", "reboot"], ["xl", "destroy"], ["xl", "shutdown"])]


@pytest.mark.parametrize("flags", ["--p---", "---sc-", "---s--", "-----d"])  # paused · crashed · shutdown · dying
@pytest.mark.parametrize("key", [None, "reboot-not-running"])
def test_real_reboot_of_a_vm_that_is_not_running_is_409_with_or_without_a_key(client, board, flags, key):
    """실제 Manager reboot 는 도메인 존재만 본다. 키 유무와 관계없이 실행 중이 아니면 409 이고 물리 명령을 내지 않는다."""
    board.xen.states = {1: flags}
    headers = {"Idempotency-Key": key} if key else {}
    response = client.post(f"/api/v1/vms/{UBUNTU}/reboot", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "InvalidState"
    assert reboots(board) == []
    assert list((board.data / "control-requests").glob("*.json")) == []  # 의도 수락 기록도 없다


def test_real_keyed_reboot_starts_a_new_execution_and_replays_without_repeating(client, board):
    debian = board.records["debian-dev"]  # 플랫폼이 시작한(관리하는) 실행에서 재부팅한다
    body = restart_body(debian["vcpus"], debian["memory_mib"])
    assert client.post("/api/v1/vms", json=body).status_code == 201
    before = board.manager.call("get", {"vm_id": str(DEBIAN)})["data"]["vm"]
    assert before["execution_id"]
    first = client.post(f"/api/v1/vms/{DEBIAN}/reboot", headers={"Idempotency-Key": "reboot-debian-1"})
    assert first.status_code == 200, first.text
    original = first.json()
    assert (original["action"], original["status"], original["accepted"]) == ("reboot", "completed", True) and original["operation_id"]
    assert [c[:2] for c in reboots(board)] == [["xl", "reboot"]]
    after = board.manager.call("get", {"vm_id": str(DEBIAN)})["data"]["vm"]
    assert after["domid"] is not None and after["execution_id"] and after["execution_id"] != before["execution_id"]  # 새 실행

    again = client.post(f"/api/v1/vms/{DEBIAN}/reboot", headers={"Idempotency-Key": "reboot-debian-1"})
    assert again.status_code == 200, again.text
    assert again.json()["operation_id"] == original["operation_id"] and len(reboots(board)) == 1  # 재전달은 물리 동작을 반복하지 않는다
    found = lookup(client, DEBIAN, "reboot-debian-1")
    assert found.status_code == 200 and (found.json()["action"], found.json()["operation_id"]) == ("reboot", original["operation_id"])


def test_real_reboot_of_a_stopped_or_unregistered_vm(client, board):
    response = client.post(f"/api/v1/vms/{DEBIAN}/reboot", headers={"Idempotency-Key": "reboot-debian"})  # 등록됐지만 꺼짐
    assert response.status_code == 409, response.text
    assert client.post(f"/api/v1/vms/{NEW_VM}/reboot").status_code == 404
    assert reboots(board) == []


# ---- 자원 변경 (S15P31E110-368): 실제 update-resources·operation·control-operation ----


def resize(client, vm_id, key, vcpus, memory_mib, **headers):
    return client.put(f"/api/v1/vms/{vm_id}/resources", json={"vcpus": vcpus, "memory_mib": memory_mib},
                      headers={"Idempotency-Key": key, **headers})


def restart_body(vcpus, memory_mib):
    return {"vm_id": str(DEBIAN), "name": "debian", "template_id": DEBIAN_TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
            "vcpus": vcpus, "memory_mib": memory_mib}


def test_real_resource_update_replays_refuses_other_keys_and_restarts_with_the_new_values(client, board):
    debian = board.records["debian-dev"]
    assert client.post("/api/v1/vms", json=restart_body(debian["vcpus"], debian["memory_mib"])).status_code == 201
    running = resize(client, DEBIAN, "resize-while-running", 2, 768)
    assert (running.status_code, running.json()["code"]) == (409, "InvalidState")
    assert client.post(f"/api/v1/vms/{DEBIAN}/stop").status_code == 204
    before = board.manager.call("get", {"vm_id": str(DEBIAN)})["data"]["vm"]
    creates = sum(c[:2] == ["xl", "create"] for c in board.xen.calls)

    first = resize(client, DEBIAN, "resize-debian-1", 2, 768, **{"X-Request-Budget-Ms": "20000"})  # 하위는 budget_ms 를 받지 않는다
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["before"] == {"vcpus": debian["vcpus"], "memory_mib": debian["memory_mib"]}
    assert result["after"] == {"vcpus": 2, "memory_mib": 768} and result["operation_id"] and result["applies_on_next_start"] is True
    after = board.manager.call("get", {"vm_id": str(DEBIAN)})["data"]["vm"]
    assert (after["configuration"]["vcpus"], after["configuration"]["memory_mib"]) == (2, 768)
    assert after["configuration_sha256"] != before["configuration_sha256"]
    assert (after["generation"], after["initial_spec"], after["domid"]) == (before["generation"], before["initial_spec"], None)
    assert sum(c[:2] == ["xl", "create"] for c in board.xen.calls) == creates  # 게스트를 시작하지 않는다

    again = resize(client, DEBIAN, "resize-debian-1", 2, 768)  # 응답을 잃었다고 보고 같은 키·같은 본문
    assert again.status_code == 200, again.text
    assert again.json() == result  # 설정 해시가 바뀌었어도 원 요청으로 재전달해 원 결과
    other = resize(client, DEBIAN, "resize-debian-1", 1, 512)
    assert (other.status_code, other.json()["code"]) == (409, "VMExists")

    assert keyed_stop(client, UBUNTU, "stop-then-resize").status_code == 200
    reused = resize(client, DEBIAN, "stop-then-resize", 2, 768)  # 제어 원장의 키
    assert (reused.status_code, reused.json()["code"]) == (409, "VMExists")
    current = board.manager.call("get", {"vm_id": str(DEBIAN)})["data"]["vm"]
    assert current["configuration_sha256"] == after["configuration_sha256"]

    old = client.post("/api/v1/vms", json=restart_body(debian["vcpus"], debian["memory_mib"]))
    assert (old.status_code, old.json()["code"]) == (409, "VMSpecConflict")
    restarted = client.post("/api/v1/vms", json=restart_body(2, 768))
    assert restarted.status_code == 201, restarted.text
    assert (restarted.json()["vcpus"], restarted.json()["memory_mib"]) == (2, 768)


def test_real_resource_update_of_unregistered_or_never_stopped_vm(client, board):
    assert resize(client, NEW_VM, "resize-missing", 2, 768).status_code == 404
    legacy = resize(client, DEBIAN, "resize-no-stopped-intent", 2, 768)  # 등록만 된 옛 VM 은 stopped 의도가 없다
    assert (legacy.status_code, legacy.json()["platform_code"]) == (409, "INTENT_MISMATCH")


def test_real_budget_exhausted_submits_nothing(board):
    manager = SerializedManager(board.manager)
    driver = PlatformDriver(manager, Xl(FakeRunner({("xl", "info"): XL_INFO})))
    app = create_app(Settings(driver="platform", log_level="WARNING", response_reserve_s=0.5), driver=driver,
                     provisioner=PlatformProvisioner(manager))
    response = keyed_stop(TestClient(app), UBUNTU, "too-late", **{"X-Request-Budget-Ms": "100"})
    assert response.status_code == 504, response.text
    assert shutdowns(board) == []
    assert list((board.data / "control-requests").glob("*.json")) == []  # 의도 수락 기록도 없다

def test_real_reads_do_not_wait_for_a_long_mutation_with_read_fork(board):
    """S15P31E110-333: 같은 프로세스의 긴 변경이 공유 Manager 직렬화와 플랫폼 실행 잠금을 쥔 동안, read_fork 면 목록·상세는
    실제 Manager.fork() 로 200 이고 남은 예산을 budget_ms·timeout_ms 로 받는다. 변경(키 없는 stop)은 직렬화 대기 끝에 503 이다."""
    from xenova_runtime import operation_lock

    seen = []
    original_fork = board.manager.fork

    def fork():
        forked = original_fork()
        call = forked.call

        def recorded(action, spec=None, *, execute=False, idempotency_key=None, request_id=None, budget_ms=None):
            seen.append((action, budget_ms, (spec or {}).get("timeout_ms")))
            return call(action, spec, execute=execute, idempotency_key=idempotency_key, request_id=request_id, budget_ms=budget_ms)

        forked.call = recorded
        return forked

    board.manager.fork = fork
    manager = SerializedManager(board.manager, busy_timeout_s=0.2, read_fork=True)
    driver = PlatformDriver(manager, Xl(FakeRunner({("xl", "info"): XL_INFO})))
    client = TestClient(create_app(Settings(driver="platform", log_level="WARNING", response_reserve_s=0), driver=driver,
                                   provisioner=PlatformProvisioner(manager)))
    budget = {"X-Request-Budget-Ms": "4000"}
    assert manager._lock.acquire(timeout=1)  # 긴 create 가 공유 Manager 를 쥐고 있다
    try:
        with operation_lock(board.manager.p.runtime):  # 플랫폼 실행 잠금도 쥐고 있다
            listed = client.get("/api/v1/vms", headers=budget)
            assert listed.status_code == 200, listed.text
            assert str(UBUNTU) in {vm["vm_id"] for vm in listed.json()}
            single = client.get(f"/api/v1/vms/{UBUNTU}", headers=budget)
            assert single.status_code == 200, single.text
            stopped = client.post(f"/api/v1/vms/{DEBIAN}/stop")
            assert stopped.status_code == 503 and stopped.json()["platform_code"] == "OPERATION_IN_PROGRESS"
    finally:
        manager._lock.release()
    assert shutdowns(board) == []
    assert [action for action, _, _ in seen] == ["list", "get"]  # 변경은 fork 로 가지 않았다
    assert all(budget_ms is not None and 0 < budget_ms <= 4000 and timeout_ms == budget_ms for _, budget_ms, timeout_ms in seen)


@pytest.mark.parametrize("name", ["개발용 Ubuntu", "\u00a0VM", "VM\u3000", "\u2003주행 A VM\u202f"])
def test_spring_dto_name_uses_actual_manager_metadata_preparer(client, board, name):
    """DTO 필드·실제 HTTP·Manager 메타데이터를 연결하고 Linux 디스크 복사 경계만 대체한다."""
    import re
    import prepare_instance
    source = (REPO / "backend/src/main/java/com/xenova/backend/vm/infrastructure/Dom0LaunchRequest.java").read_text()
    record_fields = re.search(r"record Dom0LaunchRequest\((.*?)\)", source, re.S).group(1)
    names = {re.sub(r"([A-Z])", lambda m: "_" + m[1].lower(), name)
             for name in re.findall(r"(?:String|int)\s+(\w+)", record_fields)}
    body = json.loads((REPO / "platform/tests/contracts/spring-create-request.json").read_text())
    body["vm_id"] = str(NEW_VM)
    body["name"] = name
    assert set(body) == names
    board.manager.preparer = board.manager._prepare
    def copy_disk_boundary(args, **kwargs):
        record = kwargs["record_override"]
        assert record["display_name"] == body["name"]
        from test_xenova_platform import private_fixture
        assert kwargs['private'] is True
        prepared=private_fixture(board.data,record,kwargs['workspace_path'])
        board.records[record["id"]] = record
        return prepared
    with patch.object(prepare_instance, "make_instance", side_effect=copy_disk_boundary) as copy:
        response = client.post("/api/v1/vms", json=body)
    assert response.status_code == 201, response.text
    assert copy.call_count == 1
    directory = board.data / "instances" / management.internal_name(str(NEW_VM))
    persisted = json.loads((directory / "instance.json").read_text())
    assert persisted["display_name"] == body["name"]
    assert persisted["name"] == persisted["id"] == "vm-" + NEW_VM.hex
    assert persisted["uuid"] == body["vm_id"]
    view = board.manager.call("get", {"vm_id": body["vm_id"]})["data"]["vm"]
    assert view["name"] == view["configuration"]["display_name"] == body["name"]
