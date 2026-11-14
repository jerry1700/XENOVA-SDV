package com.xenova.backend.agent.api;

import com.xenova.backend.agent.application.AutoInputUnavailableCommand;
import com.xenova.backend.agent.application.AutoInputUnavailableService;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/vms/{vmId}/faults")
public class AutoInputUnavailableController {

    private final AutoInputUnavailableService service;

    public AutoInputUnavailableController(AutoInputUnavailableService service) {
        this.service = service;
    }

    @PostMapping("/auto-input-unavailable")
    public ResponseEntity<AutoInputUnavailableCommand> inject(
            @PathVariable String vmId, @Valid @RequestBody AutoInputUnavailableRequest request) {
        return ResponseEntity.accepted().body(service.send(vmId, request.durationS()));
    }
}
