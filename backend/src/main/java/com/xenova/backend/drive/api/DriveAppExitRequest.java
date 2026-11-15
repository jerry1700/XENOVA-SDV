package com.xenova.backend.drive.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

public record DriveAppExitRequest(
        @JsonProperty("app_id") @NotNull @Pattern(regexp = "autonomous") String appId) {
}
