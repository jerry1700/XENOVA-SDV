package com.xenova.backend.vm.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.xenova.backend.agent.application.AgentDataService;
import com.xenova.backend.event.application.VmSyncService;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.application.VmService;
import com.xenova.backend.vm.domain.Vm;
import com.xenova.backend.vm.domain.VmState;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import org.junit.jupiter.api.Test;

class VmControllerTest {

    @Test
    void listReturnsTheOnDemandSyncResult() {
        VmService vmService = mock(VmService.class);
        VmSyncService vmSyncService = mock(VmSyncService.class);
        AgentDataService agentDataService = mock(AgentDataService.class);
        Instant observedAt = Instant.parse("2026-11-12T00:00:00Z");
        Vm vm = new Vm(
                "vm-1", "drive-1", GuestKind.ZEPHYR, VmState.RUNNING, "zephyr", "1.0.0", 1, 16);
        when(vmSyncService.sync()).thenReturn(List.of(vm));

        VmListResponse response = new VmController(
                vmService, vmSyncService, agentDataService, Clock.fixed(observedAt, ZoneOffset.UTC)).list();

        assertThat(response.items()).extracting(VmResponse::vmId).containsExactly("vm-1");
        assertThat(response.observedAt()).isEqualTo(observedAt);
        verify(vmSyncService).sync();
        verify(vmService, never()).list();
    }
}
