package com.xenova.backend.vm.domain;

import java.time.Instant;
import java.util.List;

public record VmResourceView(
        String vmId,
        int vcpus,
        int memoryMib,
        int pcpuCount,
        List<CpuLayout.VcpuAffinity> vcpuPinning,
        List<Integer> sharedPcpus,
        List<Integer> exclusivePcpus,
        List<PcpuStatus> pcpus,
        ResourceLimits limits,
        Instant observedAt,
        // Only on a pinning change response: true means this boot's host preparation must run before the VM
        // can start; false means it can start right away. null on GET /resources.
        Boolean hostPreparationRequired) {

    public record PcpuStatus(int pcpu, Double loadPercent) {
    }

    public record ResourceLimits(
            int vcpusMin,
            int vcpusMax,
            int memoryMibMin,
            int memoryMibMax,
            int memoryMibStep) {
    }
}
