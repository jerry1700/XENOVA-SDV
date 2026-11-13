package com.xenova.backend.vm.application;

import java.time.Instant;

/** POST /api/v1/vms/{vmId}/faults/domain-kill 응답. 결과(꺼짐·복구)는 VM 상태와 이벤트로 본다 */
public record DomainKill(String operationId, String vmId, Instant submittedAt) {
}
