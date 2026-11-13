package com.xenova.backend.agent.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.agent.domain.AgentHeartbeat;
import com.xenova.backend.agent.domain.AgentStatus;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.application.VmService;
import com.xenova.backend.vm.domain.Vm;
import com.xenova.backend.vm.domain.VmState;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;

class AgentStressServiceTest {

    private static final String VM_ID = "850e8400-e29b-41d4-a716-446655440001";
    private static final Instant NOW = Instant.parse("2026-11-06T01:00:00Z");

    private final VmService vmService = mock(VmService.class);
    private final AgentDataService agentDataService = mock(AgentDataService.class);
    private final AgentCommandResultStore commandResultStore = mock(AgentCommandResultStore.class);
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final List<JsonNode> sent = new ArrayList<>();
    private AgentStressService service;

    @BeforeEach
    void setUp() {
        when(vmService.get(VM_ID)).thenReturn(vm(GuestKind.LINUX));
        AgentHeartbeat heartbeat = new AgentHeartbeat(VM_ID);
        heartbeat.update("boot-123", "agent-1", 3, NOW.minusSeconds(1), NOW.minusSeconds(1));
        when(agentDataService.findHeartbeat(VM_ID)).thenReturn(Optional.of(heartbeat));
        when(agentDataService.statusOf(any(), any())).thenReturn(AgentStatus.ONLINE);
        service = new AgentStressService(vmService, agentDataService, commandResultStore,
                () -> (topic, payload, qos) -> {
            assertThat(topic).isEqualTo("xenova/v1/vms/" + VM_ID + "/command");
            assertThat(qos).isEqualTo(1);
            try {
                sent.add(objectMapper.readTree(payload));
            } catch (Exception exception) {
                throw new IllegalStateException(exception);
            }
            return true;
                }, objectMapper, Clock.fixed(NOW, ZoneOffset.UTC));
    }

    @Test
    void cpuEndpointMapsOnlyCpuLoadIntoStressCommand() {
        AgentStressCommand result = service.cpu(VM_ID, 2, null, 30);

        JsonNode envelope = sent.get(0);
        assertThat(envelope.path("type").asText()).isEqualTo("agent.command");
        assertThat(envelope.has("boot_id")).isFalse();
        JsonNode payload = envelope.path("payload");
        assertThat(payload.path("command_id").asText()).isEqualTo(result.commandId());
        assertThat(payload.path("expected_boot_id").asText()).isEqualTo("boot-123");
        assertThat(payload.path("action").asText()).isEqualTo("stress");
        assertThat(payload.path("parameters").path("cpu_workers").asInt()).isEqualTo(2);
        assertThat(payload.path("parameters").path("memory_mib").asInt()).isZero();
        assertThat(payload.path("parameters").path("duration_s").asInt()).isEqualTo(30);
        assertThat(result.loadPercent()).isNull();
        verify(commandResultStore).recordPending(
                eq(VM_ID), eq(result.commandId()), eq(result.operationId()), eq("boot-123"), eq(NOW));
    }

    @Test
    void memoryEndpointMapsOnlyMemoryLoadIntoStressCommand() {
        service.memory(VM_ID, 128, 45);

        JsonNode parameters = sent.get(0).path("payload").path("parameters");
        assertThat(parameters.path("cpu_workers").asInt()).isZero();
        assertThat(parameters.path("memory_mib").asInt()).isEqualTo(128);
        assertThat(parameters.path("duration_s").asInt()).isEqualTo(45);
    }

    @Test
    void memoryLoadUpTo70PercentOfTheVmIsSent() {
        // vm(...) has 512 MiB: 70% is 358 MiB (rounded down)
        service.memory(VM_ID, 358, 30);

        assertThat(sent).hasSize(1);
        assertThat(sent.get(0).path("payload").path("parameters").path("memory_mib").asInt()).isEqualTo(358);
    }

    @Test
    void memoryLoadAbove70PercentOfTheVmIsRejectedWithoutSending() {
        assertThatThrownBy(() -> service.memory(VM_ID, 359, 30))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.UNPROCESSABLE_ENTITY);
                    assertThat(exception.getCode()).isEqualTo("VALIDATION_ERROR");
                    assertThat(exception.getMessage()).contains("358 MiB");
                });
        assertThat(sent).isEmpty();
        verify(commandResultStore, never()).recordPending(any(), any(), any(), any(), any());
    }

    @Test
    void theMemoryCapIs70PercentOfTheVmButNeverAbove512() {
        assertThat(AgentStressService.maxMemoryStressMib(512)).isEqualTo(358);
        assertThat(AgentStressService.maxMemoryStressMib(256)).isEqualTo(179);
        // 70% of these would pass 512, which the agent refuses
        assertThat(AgentStressService.maxMemoryStressMib(832)).isEqualTo(512);
        assertThat(AgentStressService.maxMemoryStressMib(1024)).isEqualTo(512);
    }

    @Test
    void zephyrCpuIsRejectedBecauseAutoDelayIsTheOnlyFaultInjection() {
        when(vmService.get(VM_ID)).thenReturn(vm(GuestKind.ZEPHYR));

        assertThatThrownBy(() -> service.cpu(VM_ID, 1, 80, 10))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("UNSUPPORTED_GUEST_KIND"));
        assertThat(sent).isEmpty();
    }

    @Test
    void zephyrMemoryIsRejectedBecauseAutoDelayIsTheOnlyFaultInjection() {
        when(vmService.get(VM_ID)).thenReturn(vm(GuestKind.ZEPHYR));

        assertThatThrownBy(() -> service.memory(VM_ID, 8, 20))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("UNSUPPORTED_GUEST_KIND"));
        assertThat(sent).isEmpty();
    }

    @Test
    void offlineAgentIsRejectedBeforePublishing() {
        when(agentDataService.statusOf(any(), any())).thenReturn(AgentStatus.OFFLINE);

        assertThatThrownBy(() -> service.cpu(VM_ID, 1, null, 10))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("AGENT_UNAVAILABLE"));
        assertThat(sent).isEmpty();
    }

    @Test
    void unavailableBrokerReturnsServiceUnavailable() {
        service = new AgentStressService(vmService, agentDataService, commandResultStore,
                () -> null, objectMapper,
                Clock.fixed(NOW, ZoneOffset.UTC));

        assertThatThrownBy(() -> service.memory(VM_ID, 64, 10))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getCode()).isEqualTo("MQTT_UNAVAILABLE");
                    assertThat(exception.getStatus().value()).isEqualTo(503);
                });
        verify(commandResultStore).discardPending(eq(VM_ID), any());
    }

    private static Vm vm(GuestKind guestKind) {
        return new Vm(VM_ID, "stress-target", guestKind, VmState.RUNNING, "template-1", "1.0", 1, 512);
    }
}
