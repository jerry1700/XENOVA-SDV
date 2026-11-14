package com.xenova.backend.drive.application;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.Instant;

public record PerceptionFaultOperation(
        @JsonProperty("operation_id") String operationId,
        @JsonProperty("command_id") String commandId,
        @JsonProperty("duration_s") int durationS,
        String status,
        String reason,
        @JsonProperty("active_vm_id") String activeVmId,
        @JsonProperty("mode_before") String modeBefore,
        @JsonProperty("submitted_at") Instant submittedAt,
        @JsonProperty("updated_at") Instant updatedAt) {
}
