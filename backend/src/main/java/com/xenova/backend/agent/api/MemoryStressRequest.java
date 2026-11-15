package com.xenova.backend.agent.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;

public record MemoryStressRequest(
        // Never above 512 (the agent's own limit); the tighter per-VM bound (90% of its memory) is in AgentStressService.
        @JsonProperty("memory_mib") @NotNull @Min(1) @Max(512) Integer memoryMib,
        @JsonProperty("duration_s") @NotNull @Min(1) @Max(120) Integer durationS) {
}
