package com.xenova.backend.vm.api;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import java.util.List;

public record UpdateVcpuPinningRequest(
        @NotEmpty(message = "mappings는 비어 있을 수 없습니다.")
        List<@Valid VcpuMapping> mappings) {

    public record VcpuMapping(
            @NotNull @Min(0) @Max(3) Integer vcpu,
            @NotEmpty(message = "각 vCPU에는 하나 이상의 pCPU가 필요합니다.")
            List<@NotNull @Min(0) @Max(3) Integer> pcpus) {
    }
}
