package com.xenova.backend.vm.application;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

// Keeps the VM table in step with the board registry, so a VM created or retired with a platform tool
// (console, terminal) shows up or disappears without anyone calling POST /api/v1/vms/synchronizations.
// interval: pause after each synchronization.
// missingThreshold: consecutive synchronizations a REGISTERED row must be absent from the board before it is retired.
// creationGrace: a row younger than this is never retired; the board registers a VM only at the end of create
// (up to the dom0-manager command timeout).
@ConfigurationProperties(prefix = "xenova.vm.registration-sync")
public record VmRegistrationSyncProperties(
        boolean enabled, Duration interval, int missingThreshold, Duration creationGrace) {
}
