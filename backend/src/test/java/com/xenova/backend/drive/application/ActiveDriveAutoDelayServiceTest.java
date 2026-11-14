package com.xenova.backend.drive.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.agent.application.AutoDelayCommand;
import com.xenova.backend.agent.application.AutoDelayService;
import com.xenova.backend.common.error.ApiException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;

class ActiveDriveAutoDelayServiceTest {

    private static final String ACTIVE_VM = "850e8400-e29b-41d4-a716-446655440001";
    private final DriveStateTracker tracker = new DriveStateTracker(new ObjectMapper(),
            Clock.fixed(Instant.parse("2026-11-13T00:00:00Z"), ZoneOffset.UTC), () -> 0L);
    private final AutoDelayService autoDelayService = mock(AutoDelayService.class);
    private final ActiveDriveAutoDelayService service = new ActiveDriveAutoDelayService(tracker, autoDelayService);

    ActiveDriveAutoDelayServiceTest() {
        tracker.mqttConnected();
    }

    @Test
    void sendsDelayToTheActiveAutonomousVm() {
        control("AUTO", ACTIVE_VM);
        AutoDelayCommand expected = new AutoDelayCommand("command-1", "operation-1", ACTIVE_VM,
                50, 30, Instant.EPOCH, Instant.EPOCH.plusSeconds(30));
        when(autoDelayService.send(ACTIVE_VM, 50, 30)).thenReturn(expected);

        assertThat(service.send(50, 30)).isSameAs(expected);
        verify(autoDelayService).send(ACTIVE_VM, 50, 30);
    }

    @Test
    void rejectsDelayOutsideAutoMode() {
        control("MANUAL", ACTIVE_VM);

        assertThatThrownBy(() -> service.send(50, 30))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_MODE_NOT_AUTO"));
        verifyNoInteractions(autoDelayService);
    }

    @Test
    void rejectsMissingOrStaleDriveState() {
        assertThatThrownBy(() -> service.send(50, 30))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_STATE_UNAVAILABLE"));

        control("AUTO", ACTIVE_VM);
        tracker.mqttDisconnected();
        assertThatThrownBy(() -> service.send(50, 30))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_STATE_UNAVAILABLE"));
        verifyNoInteractions(autoDelayService);
    }

    @Test
    void rejectsAutoWithoutAnActiveVm() {
        control("AUTO", null);

        assertThatThrownBy(() -> service.send(50, 30))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("ACTIVE_DRIVE_VM_UNAVAILABLE"));
        verifyNoInteractions(autoDelayService);
    }

    private void control(String mode, String activeVmId) {
        String vm = activeVmId == null ? "null" : "\"" + activeVmId + "\"";
        tracker.accept(DriveStateTracker.CONTROL_TOPIC, DriveStateTrackerTest.envelope(
                "dom0-gateway", null, "vehicle.control", 1,
                "{\"mode\":\"%s\",\"generation\":1,\"active_app\":\"autonomous\",\"active_vm_id\":%s,\"reason\":\"start\"}"
                        .formatted(mode, vm)));
    }
}
