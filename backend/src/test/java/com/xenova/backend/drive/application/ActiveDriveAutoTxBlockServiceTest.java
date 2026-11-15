package com.xenova.backend.drive.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.agent.application.ZephyrFaultCommand;
import com.xenova.backend.agent.application.ZephyrFaultService;
import com.xenova.backend.common.error.ApiException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;

class ActiveDriveAutoTxBlockServiceTest {

    private static final String ACTIVE_VM = "850e8400-e29b-41d4-a716-446655440001";
    private final DriveStateTracker tracker = new DriveStateTracker(new ObjectMapper(),
            Clock.fixed(Instant.parse("2026-11-13T00:00:00Z"), ZoneOffset.UTC), () -> 0L);
    private final ZephyrFaultService faultService = mock(ZephyrFaultService.class);
    private final ActiveDriveAutoTxBlockService service = new ActiveDriveAutoTxBlockService(tracker, faultService);

    ActiveDriveAutoTxBlockServiceTest() {
        tracker.mqttConnected();
    }

    @Test
    void blocksTheActiveAutoVmsCommandPublication() {
        control("AUTO", "autonomous", ACTIVE_VM);
        ZephyrFaultCommand expected = new ZephyrFaultCommand(
                "command-1", "operation-1", ACTIVE_VM, Instant.EPOCH, Instant.EPOCH.plusSeconds(30));
        when(faultService.blockAutoTx(ACTIVE_VM, 10)).thenReturn(expected);

        assertThat(service.send(10)).isSameAs(expected);
        verify(faultService).blockAutoTx(ACTIVE_VM, 10);
    }

    @Test
    void rejectsOutsideAutoMode() {
        control("MANUAL", "manual", ACTIVE_VM);
        assertThatThrownBy(() -> service.send(10))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_MODE_NOT_AUTO"));
        verifyNoInteractions(faultService);
    }

    @Test
    void rejectsMissingDriveState() {
        assertThatThrownBy(() -> service.send(10))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("DRIVE_STATE_UNAVAILABLE"));
        verifyNoInteractions(faultService);
    }

    private void control(String mode, String app, String activeVmId) {
        tracker.accept(DriveStateTracker.CONTROL_TOPIC, DriveStateTrackerTest.envelope(
                "dom0-gateway", null, "vehicle.control", 1,
                "{\"mode\":\"%s\",\"generation\":1,\"active_app\":\"%s\",\"active_vm_id\":\"%s\",\"reason\":\"start\"}"
                        .formatted(mode, app, activeVmId)));
    }
}
