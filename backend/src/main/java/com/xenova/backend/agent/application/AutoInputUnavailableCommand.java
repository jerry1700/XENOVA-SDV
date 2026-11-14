package com.xenova.backend.agent.application;

import java.time.Instant;

public record AutoInputUnavailableCommand(
        String commandId,
        String operationId,
        String vmId,
        int durationS,
        Instant submittedAt,
        Instant expiresAt) {
}
