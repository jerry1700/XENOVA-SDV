"""CPU 정책 HTTP 변경: 전체 정책·revision·멱등 키를 플랫폼 cpu-policy-set에 손실 없이 전달한다."""

from fastapi.testclient import TestClient

from conftest import VM_A, VM_B
from test_control_operations import Manager, app_for, envelope, failure


KEY = "cpu-policy-1"
BODY = {
    "expected_policy_revision": 2,
    "shared_pcpus": [3, 0],
    "placements": [
        {"vm_id": str(VM_B), "mode": "exclusive", "pcpus": [2]},
        {"vm_id": str(VM_A), "mode": "shared", "pcpus": [3]},
    ],
}
POLICY = {
    "schema_version": "2.0",
    "operation_id": "33333333-3333-4333-8333-333333333333",
    "policy_revision": 3,
    "pool_name": "Pool-0",
    "shared_pcpus": [0, 3],
    "placements": [
        {"vm_id": str(VM_A), "mode": "shared", "pcpus": [3]},
        {"vm_id": str(VM_B), "mode": "exclusive", "pcpus": [2]},
    ],
}


def client_for(response):
    manager = Manager(responses={"cpu-policy-set": response})
    return TestClient(app_for(manager)), manager


def put(client, body=BODY, key=KEY):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.put("/api/v1/cpu-policy", json=body, headers=headers)


def test_policy_is_normalized_and_submitted_with_execute_and_key():
    client, manager = client_for(envelope(data={"policy": POLICY, "host_preparation_required": True}))

    response = put(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"policy": POLICY, "host_preparation_required": True}
    [call] = [call for call in manager.calls if call["action"] == "cpu-policy-set"]
    assert call["key"] == KEY
    assert call["spec"] == {
        "expected_policy_revision": 2,
        "shared_pcpus": [0, 3],
        "placements": [
            {"vm_id": str(VM_A), "mode": "shared", "pcpus": [3]},
            {"vm_id": str(VM_B), "mode": "exclusive", "pcpus": [2]},
        ],
    }


def test_completed_replay_defaults_host_preparation_required():
    client, _ = client_for(envelope(data={"policy": POLICY, "current_policy": POLICY, "historical": True}, replayed=True))
    response = put(client)
    assert response.status_code == 200
    assert response.json()["host_preparation_required"] is True


def test_policy_validation_rejects_overlaps_before_platform_call():
    client, manager = client_for(envelope(data={"policy": POLICY}))
    body = {**BODY, "placements": [{"vm_id": str(VM_A), "mode": "exclusive", "pcpus": [0]}]}
    response = put(client, body)
    assert response.status_code == 400
    assert not manager.calls


def test_missing_or_invalid_idempotency_key_is_400():
    client, manager = client_for(envelope(data={"policy": POLICY}))
    assert put(client, key=None).status_code == 400
    assert put(client, key="contains space").status_code == 400
    assert not manager.calls


def test_old_platform_and_revision_conflict_are_mapped():
    old, _ = client_for(failure("INVALID_SPEC"))
    conflict, _ = client_for(failure("TARGET_CHANGED", current_policy_revision=4))
    assert put(old).status_code == 501
    assert put(conflict).status_code == 409
