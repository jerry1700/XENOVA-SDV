package com.xenova.backend.vm.domain;

public record VmResourceUpdate(
        String vmId,
        String operationId,
        VmResources before,
        VmResources after,
        boolean appliesOnNextStart) {
}
