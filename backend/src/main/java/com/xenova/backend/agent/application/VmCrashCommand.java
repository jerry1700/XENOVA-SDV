package com.xenova.backend.agent.application;

import java.time.Instant;

public record VmCrashCommand(
        String commandId,
        String operationId,
        String vmId,
        Instant submittedAt,
        Instant expiresAt) {
}
