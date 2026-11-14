package com.xenova.backend.vm.application;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

// Board registry -> VM table runs only when asked (POST /api/v1/vms/synchronizations); there is no periodic loop.
// missingThreshold: consecutive synchronizations a REGISTERED row must be absent from the board before it is retired.
// creationGrace: a row younger than this is never retired; the board registers a VM only at the end of create
// (up to the dom0-manager command timeout).
@ConfigurationProperties(prefix = "xenova.vm.registration-sync")
public record VmRegistrationSyncProperties(int missingThreshold, Duration creationGrace) {
}
