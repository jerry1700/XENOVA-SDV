package com.xenova.backend.agent.api;

import com.xenova.backend.agent.application.VmCrashCommand;
import com.xenova.backend.agent.application.VmCrashService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/vms/{vmId}/faults")
public class VmCrashController {

    private final VmCrashService service;

    public VmCrashController(VmCrashService service) {
        this.service = service;
    }

    @PostMapping("/vm-crash")
    public ResponseEntity<VmCrashCommand> vmCrash(@PathVariable String vmId) {
        return ResponseEntity.accepted().body(service.send(vmId));
    }
}
