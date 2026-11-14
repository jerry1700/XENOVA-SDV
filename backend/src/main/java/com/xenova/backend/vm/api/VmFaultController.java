package com.xenova.backend.vm.api;

import com.xenova.backend.vm.application.DomainKill;
import com.xenova.backend.vm.application.VmService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 장애 주입 중 dom0 에서 처리하는 것 (에이전트 명령은 agent/api 의 faults 컨트롤러) */
@RestController
@RequestMapping("/api/v1/vms/{vmId}/faults")
public class VmFaultController {

    private final VmService vmService;

    public VmFaultController(VmService vmService) {
        this.vmService = vmService;
    }

    @PostMapping("/domain-kill")
    public ResponseEntity<DomainKill> domainKill(@PathVariable String vmId) {
        return ResponseEntity.accepted().body(vmService.kill(vmId));
    }
}
