package com.xenova.backend.drive.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.agent.application.VmCrashCommand;
import com.xenova.backend.agent.application.VmCrashService;
import com.xenova.backend.common.error.ApiException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;

class ActiveDriveVmCrashServiceTest {

    private static final String ACTIVE_VM = "850e8400-e29b-41d4-a716-446655440001";
    private final DriveStateTracker tracker = new DriveStateTracker(new ObjectMapper(),
            Clock.fixed(Instant.parse("2026-11-13T00:00:00Z"), ZoneOffset.UTC), () -> 0L);
    private final VmCrashService vmCrashService = mock(VmCrashService.class);
    private final ActiveDriveVmCrashService service = new ActiveDriveVmCrashService(tracker, vmCrashService);

    ActiveDriveVmCrashServiceTest() {
        tracker.mqttConnected();
    }

    @Test
    void crashesTheActiveAutonomousVm() {
        control("AUTO", "autonomous", ACTIVE_VM);
        VmCrashCommand expected = new VmCrashCommand(
                "command-1", "operation-1", ACTIVE_VM, Instant.EPOCH, Instant.EPOCH.plusSeconds(30));
        when(vmCrashService.send(ACTIVE_VM)).thenReturn(expected);

        assertThat(service.send()).isSameAs(expected);
        verify(vmCrashService).send(ACTIVE_VM);
    }

    @Test
    void rejectsOutsideActiveAutoControl() {
        control("MANUAL", "manual", ACTIVE_VM);

        assertThatThrownBy(service::send)
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_MODE_NOT_AUTO"));
        verifyNoInteractions(vmCrashService);
    }

    @Test
    void rejectsMissingOrStaleDriveState() {
        assertThatThrownBy(service::send)
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_STATE_UNAVAILABLE"));

        control("AUTO", "autonomous", ACTIVE_VM);
        tracker.mqttDisconnected();
        assertThatThrownBy(service::send)
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_STATE_UNAVAILABLE"));
        verifyNoInteractions(vmCrashService);
    }

    @Test
    void rejectsAutoWithoutAnActiveVm() {
        control("AUTO", "autonomous", null);

        assertThatThrownBy(service::send)
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("ACTIVE_DRIVE_VM_UNAVAILABLE"));
        verifyNoInteractions(vmCrashService);
    }

    private void control(String mode, String app, String activeVmId) {
        String vm = activeVmId == null ? "null" : "\"" + activeVmId + "\"";
        tracker.accept(DriveStateTracker.CONTROL_TOPIC, DriveStateTrackerTest.envelope(
                "dom0-gateway", null, "vehicle.control", 1,
                "{\"mode\":\"%s\",\"generation\":1,\"active_app\":\"%s\",\"active_vm_id\":%s,\"reason\":\"start\"}"
                        .formatted(mode, app, vm)));
    }
}
