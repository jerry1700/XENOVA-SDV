package com.xenova.backend.vm.domain;

import java.util.List;

public record CpuPolicyUpdate(CpuPolicy policy, boolean hostPreparationRequired) {

    public record CpuPolicy(
            String schemaVersion,
            String operationId,
            int policyRevision,
            String poolName,
            List<Integer> sharedPcpus,
            List<CpuPlacement> placements) {
    }

    public record CpuPlacement(String vmId, String mode, List<Integer> pcpus) {
    }

    public record Request(
            Integer expectedPolicyRevision,
            List<Integer> sharedPcpus,
            List<CpuPlacement> placements) {
    }
}
