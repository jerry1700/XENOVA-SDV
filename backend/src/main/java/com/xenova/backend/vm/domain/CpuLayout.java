package com.xenova.backend.vm.domain;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import java.util.List;

/** Platform CPU policy plus the observed Xen hard-affinity state. */
@JsonIgnoreProperties(ignoreUnknown = true)
public record CpuLayout(
        boolean configured,
        Integer policyRevision,
        String policySchemaVersion,
        CpuHost host,
        List<CpuReservation> reservations,
        List<VmCpuLayout> vms) {

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record CpuHost(
            List<Integer> onlinePcpus,
            int maxCpuId,
            List<Integer> desiredDom0Pcpus,
            boolean ready) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record CpuReservation(String vmId, List<Integer> pcpus) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record VmCpuLayout(
            String vmId,
            int vcpus,
            String allocation,
            List<VcpuAffinity> desiredHardAffinity) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record VcpuAffinity(int vcpu, List<Integer> pcpus) {
    }
}
