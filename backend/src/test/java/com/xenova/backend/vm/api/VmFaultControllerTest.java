package com.xenova.backend.vm.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.xenova.backend.vm.application.DomainKill;
import com.xenova.backend.vm.application.VmService;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

class VmFaultControllerTest {

    @Test
    void domainKillIsAcceptedWithTheOperation() {
        VmService vmService = mock(VmService.class);
        DomainKill killed = new DomainKill("op-1", "vm-1", Instant.parse("2026-11-13T06:00:00Z"));
        when(vmService.kill("vm-1")).thenReturn(killed);

        ResponseEntity<DomainKill> response = new VmFaultController(vmService).domainKill("vm-1");

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        assertThat(response.getBody()).isEqualTo(killed);
        verify(vmService).kill("vm-1");
    }
}
