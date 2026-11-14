package com.xenova.backend.drive.api;

import com.xenova.backend.drive.application.PerceptionFaultOperation;
import com.xenova.backend.drive.application.PerceptionFaultService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/drive/faults/perception-block")
public class PerceptionFaultController {

    private final PerceptionFaultService service;

    public PerceptionFaultController(PerceptionFaultService service) {
        this.service = service;
    }

    @PostMapping
    public ResponseEntity<PerceptionFaultOperation> block(@Valid @RequestBody PerceptionBlockRequest request) {
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(service.block(request.durationS()));
    }

    @GetMapping("/{operationId}")
    public PerceptionFaultOperation get(@PathVariable String operationId) {
        return service.get(operationId);
    }
}
