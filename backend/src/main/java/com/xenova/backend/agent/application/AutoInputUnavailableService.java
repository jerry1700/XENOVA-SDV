package com.xenova.backend.agent.application;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.agent.domain.TelemetryHeartbeat;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.drive.application.DriveState;
import com.xenova.backend.drive.application.DriveStateTracker;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.application.VmService;
import com.xenova.backend.vm.domain.Vm;
import java.time.Clock;
import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

@Service
public class AutoInputUnavailableService {

    private static final int COMMAND_TTL_SECONDS = 30;
    private static final String SCHEMA_VERSION = "1.0";

    private final VmService vmService;
    private final TelemetryHeartbeatStore telemetryHeartbeatStore;
    private final AgentCommandResultStore commandResultStore;
    private final Supplier<AgentCommandPublisher> publisher;
    private final Supplier<DriveState.Control> currentControl;
    private final ObjectMapper objectMapper;
    private final Clock clock;

    @Autowired
    public AutoInputUnavailableService(
            VmService vmService,
            TelemetryHeartbeatStore telemetryHeartbeatStore,
            AgentCommandResultStore commandResultStore,
            ObjectProvider<AgentCommandPublisher> publisher,
            DriveStateTracker driveStateTracker,
            ObjectMapper objectMapper,
            Clock clock) {
        this(vmService, telemetryHeartbeatStore, commandResultStore, publisher::getIfAvailable,
                () -> driveStateTracker.snapshot().control(), objectMapper, clock);
    }

    AutoInputUnavailableService(
            VmService vmService,
            TelemetryHeartbeatStore telemetryHeartbeatStore,
            AgentCommandResultStore commandResultStore,
            Supplier<AgentCommandPublisher> publisher,
            Supplier<DriveState.Control> currentControl,
            ObjectMapper objectMapper,
            Clock clock) {
        this.vmService = vmService;
        this.telemetryHeartbeatStore = telemetryHeartbeatStore;
        this.commandResultStore = commandResultStore;
        this.publisher = publisher;
        this.currentControl = currentControl;
        this.objectMapper = objectMapper;
        this.clock = clock;
    }

    public AutoInputUnavailableCommand send(String vmId, int durationS) {
        Vm vm = vmService.get(vmId);
        if (vm.guestKind() != GuestKind.ZEPHYR) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "UNSUPPORTED_GUEST_KIND",
                    "AUTO 입력 장애는 Zephyr VM에서만 지원합니다.");
        }
        requireActiveAutoVm(vmId);

        TelemetryHeartbeatStore.View view = telemetryHeartbeatStore.view(vmId);
        TelemetryHeartbeat.Report heartbeat = view.heartbeat();
        if (view.status() != TelemetryHeartbeatStore.Status.FRESH || heartbeat == null
                || heartbeat.bootId() == null || heartbeat.bootId().isBlank()) {
            throw new ApiException(HttpStatus.CONFLICT, "AGENT_UNAVAILABLE",
                    "Zephyr telemetry publisher가 온라인 상태가 아닙니다: " + vmId);
        }

        Instant now = clock.instant();
        Instant expiresAt = now.plusSeconds(COMMAND_TTL_SECONDS);
        String commandId = UUID.randomUUID().toString();
        String operationId = UUID.randomUUID().toString();

        Map<String, Object> parameters = new LinkedHashMap<>();
        parameters.put("duration_s", durationS);

        Map<String, Object> command = new LinkedHashMap<>();
        command.put("command_id", commandId);
        command.put("operation_id", operationId);
        command.put("expected_boot_id", heartbeat.bootId());
        command.put("expires_at", timestamp(expiresAt));
        command.put("action", "auto_input_unavailable");
        command.put("parameters", parameters);

        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("schema_version", SCHEMA_VERSION);
        envelope.put("message_id", UUID.randomUUID().toString());
        envelope.put("vm_id", vmId);
        envelope.put("type", "agent.command");
        envelope.put("sent_at", timestamp(now));
        envelope.put("payload", command);

        byte[] message = serialize(envelope);
        commandResultStore.recordPending(vmId, commandId, operationId, heartbeat.bootId(), now);
        AgentCommandPublisher target = publisher.get();
        if (target == null || !target.publish("xenova/v1/vms/" + vmId + "/command", message, 1)) {
            commandResultStore.discardPending(vmId, commandId);
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "MQTT_UNAVAILABLE",
                    "MQTT 브로커에 연결되어 있지 않아 AUTO 입력 장애 명령을 보내지 못했습니다.");
        }
        return new AutoInputUnavailableCommand(commandId, operationId, vmId, durationS, now, expiresAt);
    }

    private void requireActiveAutoVm(String vmId) {
        DriveState.Control control = currentControl.get();
        if (control == null || control.source() == null
                || control.source().status() != DriveState.SourceStatus.FRESH) {
            throw new ApiException(HttpStatus.CONFLICT, "DRIVE_CONTROL_UNAVAILABLE",
                    "현재 주행 제어 상태를 확인할 수 없습니다.");
        }
        if (!"AUTO".equals(control.mode()) || !"autonomous".equals(control.activeApp())) {
            throw new ApiException(HttpStatus.CONFLICT, "AUTO_MODE_REQUIRED",
                    "AUTO 모드의 활성 주행 VM에만 입력 장애를 주입할 수 있습니다.");
        }
        if (!vmId.equals(control.activeVmId())) {
            throw new ApiException(HttpStatus.CONFLICT, "ACTIVE_VM_MISMATCH",
                    "요청한 VM이 현재 활성 주행 VM이 아닙니다: " + control.activeVmId());
        }
    }

    private byte[] serialize(Map<String, Object> envelope) {
        try {
            return objectMapper.writeValueAsBytes(envelope);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException(exception);
        }
    }

    private static String timestamp(Instant instant) {
        return DateTimeFormatter.ISO_INSTANT.format(instant.truncatedTo(ChronoUnit.MILLIS));
    }
}
