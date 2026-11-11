package com.xenova.backend.vm.api;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;

public record UpdateVmResourcesRequest(
        @NotNull(message = "vcpus는 필수입니다.")
        @Min(value = 1, message = "vcpus는 1~4여야 합니다.")
        @Max(value = 4, message = "vcpus는 1~4여야 합니다.")
        Integer vcpus,
        @NotNull(message = "memory_mib는 필수입니다.")
        @Min(value = 128, message = "memory_mib는 128~1024여야 합니다.")
        @Max(value = 1024, message = "memory_mib는 128~1024여야 합니다.")
        Integer memoryMib) {
}
