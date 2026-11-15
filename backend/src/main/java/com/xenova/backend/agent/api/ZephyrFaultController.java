package com.xenova.backend.agent.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.xenova.backend.agent.application.ZephyrFaultCommand;
import com.xenova.backend.agent.application.ZephyrFaultService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/vms/{vmId}/faults")
public class ZephyrFaultController {

    private final ZephyrFaultService service;

    public ZephyrFaultController(ZephyrFaultService service) {
        this.service = service;
    }

    @PostMapping("/app-exit")
    public ResponseEntity<ZephyrFaultCommand> appExit(
            @PathVariable String vmId, @Valid @RequestBody AppExitRequest request) {
        return ResponseEntity.accepted().body(service.appExit(vmId));
    }

    @PostMapping("/auto-tx-block")
    public ResponseEntity<ZephyrFaultCommand> blockAutoTx(
            @PathVariable String vmId, @Valid @RequestBody AutoTxBlockRequest request) {
        return ResponseEntity.accepted().body(service.blockAutoTx(vmId, request.durationS()));
    }

    public record AppExitRequest(@JsonProperty("app_id") @NotNull @Pattern(regexp = "autonomous") String appId) {
    }

    public record AutoTxBlockRequest(@JsonProperty("duration_s") @NotNull @Min(1) @Max(120) Integer durationS) {
    }
}
