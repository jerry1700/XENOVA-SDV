package com.xenova.backend.agent.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.agent.domain.TelemetryHeartbeat;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.drive.application.DriveState;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.application.VmService;
import com.xenova.backend.vm.domain.Vm;
import com.xenova.backend.vm.domain.VmState;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class AutoInputUnavailableServiceTest {

    private static final String VM_ID = "850e8400-e29b-41d4-a716-446655440001";
    private static final String OTHER_VM_ID = "850e8400-e29b-41d4-a716-446655440002";
    private static final Instant NOW = Instant.parse("2026-11-13T05:00:00Z");

    private final VmService vmService = mock(VmService.class);
    private final TelemetryHeartbeatStore heartbeatStore = mock(TelemetryHeartbeatStore.class);
    private final AgentCommandResultStore resultStore = mock(AgentCommandResultStore.class);
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final List<JsonNode> sent = new ArrayList<>();
    private DriveState.Control control;
    private AutoInputUnavailableService service;

    @BeforeEach
    void setUp() {
        when(vmService.get(VM_ID)).thenReturn(vm(GuestKind.ZEPHYR));
        when(heartbeatStore.view(VM_ID)).thenReturn(freshHeartbeat());
        control = control("AUTO", "autonomous", VM_ID, DriveState.SourceStatus.FRESH);
        service = service(() -> (topic, payload, qos) -> {
            assertThat(topic).isEqualTo("xenova/v1/vms/" + VM_ID + "/command");
            assertThat(qos).isEqualTo(1);
            try {
                sent.add(objectMapper.readTree(payload));
            } catch (Exception exception) {
                throw new IllegalStateException(exception);
            }
            return true;
        });
    }

    @Test
    void publishesOnlyToCurrentActiveAutoVm() {
        AutoInputUnavailableCommand result = service.send(VM_ID, 50);

        JsonNode envelope = sent.get(0);
        JsonNode payload = envelope.path("payload");
        assertThat(envelope.path("vm_id").asText()).isEqualTo(VM_ID);
        assertThat(payload.path("command_id").asText()).isEqualTo(result.commandId());
        assertThat(payload.path("expected_boot_id").asText()).isEqualTo("zephyr-boot");
        assertThat(payload.path("action").asText()).isEqualTo("auto_input_unavailable");
        assertThat(payload.path("parameters").fieldNames()).toIterable().containsExactly("duration_s");
        assertThat(payload.path("parameters").path("duration_s").asInt()).isEqualTo(50);
        verify(resultStore).recordPending(
                eq(VM_ID), eq(result.commandId()), eq(result.operationId()), eq("zephyr-boot"), eq(NOW));
    }

    @Test
    void rejectsStandbyVm() {
        control = control("AUTO", "autonomous", OTHER_VM_ID, DriveState.SourceStatus.FRESH);

        assertThatThrownBy(() -> service.send(VM_ID, 50))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("ACTIVE_VM_MISMATCH"));
        assertThat(sent).isEmpty();
    }

    @Test
    void rejectsNonAutoModeAndStaleControl() {
        control = control("MANUAL", "manual", VM_ID, DriveState.SourceStatus.FRESH);
        assertThatThrownBy(() -> service.send(VM_ID, 50))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("AUTO_MODE_REQUIRED"));

        control = control("AUTO", "autonomous", VM_ID, DriveState.SourceStatus.STALE);
        assertThatThrownBy(() -> service.send(VM_ID, 50))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_CONTROL_UNAVAILABLE"));
        assertThat(sent).isEmpty();
    }

    @Test
    void rejectsLinuxAndOfflineZephyrVm() {
        when(vmService.get(VM_ID)).thenReturn(vm(GuestKind.LINUX));
        assertThatThrownBy(() -> service.send(VM_ID, 50))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("UNSUPPORTED_GUEST_KIND"));

        when(vmService.get(VM_ID)).thenReturn(vm(GuestKind.ZEPHYR));
        when(heartbeatStore.view(VM_ID)).thenReturn(
                new TelemetryHeartbeatStore.View(TelemetryHeartbeatStore.Status.STALE, null, 7_000L));
        assertThatThrownBy(() -> service.send(VM_ID, 50))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("AGENT_UNAVAILABLE"));
        assertThat(sent).isEmpty();
    }

    @Test
    void discardsPendingCommandWhenBrokerIsUnavailable() {
        service = service(() -> null);

        assertThatThrownBy(() -> service.send(VM_ID, 50))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("MQTT_UNAVAILABLE"));
        verify(resultStore).discardPending(eq(VM_ID), any());
    }

    private AutoInputUnavailableService service(java.util.function.Supplier<AgentCommandPublisher> publisher) {
        return new AutoInputUnavailableService(vmService, heartbeatStore, resultStore, publisher,
                () -> control, objectMapper, Clock.fixed(NOW, ZoneOffset.UTC));
    }

    private static DriveState.Control control(
            String mode, String activeApp, String activeVmId, DriveState.SourceStatus status) {
        return new DriveState.Control(mode, 42L, activeApp, activeVmId, "test", 0L,
                new DriveState.Source(status, 0L));
    }

    private static Vm vm(GuestKind guestKind) {
        return new Vm(VM_ID, "input-fault-target", guestKind, VmState.RUNNING, "template-1", "1.0", 1, 16);
    }

    private static TelemetryHeartbeatStore.View freshHeartbeat() {
        TelemetryHeartbeat.Report heartbeat = new TelemetryHeartbeat.Report(
                "zephyr-boot", "publisher-1", "1", "1000", "running", "1000", "0", "0", "1", NOW);
        return new TelemetryHeartbeatStore.View(TelemetryHeartbeatStore.Status.FRESH, heartbeat, 0L);
    }
}
