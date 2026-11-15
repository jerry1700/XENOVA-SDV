package com.xenova.backend.drive.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.common.error.ApiException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class PerceptionFaultServiceTest {

    private static final Instant NOW = Instant.parse("2026-11-13T01:02:03.456Z");
    private static final String BOOT = "11111111-1111-4111-8111-111111111111";
    private static final String INSTANCE = "22222222-2222-4222-8222-222222222222";
    private static final String SERVICE = "33333333-3333-4333-8333-333333333333";
    private static final String WINDOW = "44444444-4444-4444-8444-444444444444";
    private static final String VM = "55555555-5555-4555-8555-555555555555";

    private final ObjectMapper mapper = new ObjectMapper();
    private final DriveStateTracker tracker = mock(DriveStateTracker.class);
    private final List<Published> published = new ArrayList<>();
    private final PerceptionFaultPublisher publisher = (topic, payload, qos) -> {
        published.add(new Published(topic, payload, qos));
        return true;
    };
    private final PerceptionFaultService service = new PerceptionFaultService(mapper, tracker,
            () -> publisher, Clock.fixed(NOW, ZoneOffset.UTC), BOOT, INSTANCE);

    @Test
    void statusWindowIsEchoedIntoBlockCommandAndResultsAdvanceTheOperation() throws Exception {
        when(tracker.snapshot()).thenReturn(state("AUTO", true));

        PerceptionFaultOperation operation = service.block(50);
        assertThat(operation.status()).isEqualTo("QUERYING");
        assertThat(operation.activeVmId()).isEqualTo(VM);
        assertThat(published).hasSize(1);
        assertThat(published.getFirst().topic()).isEqualTo(PerceptionFaultService.QUERY_TOPIC);
        assertThat(published.getFirst().qos()).isEqualTo(1);
        assertThat(mapper.readTree(published.getFirst().payload()).path("payload").path("action").asText())
                .isEqualTo("STATUS");

        service.accept(PerceptionFaultService.STATUS_TOPIC, response("perception.fault.status", Map.of(
                "service_instance", SERVICE, "source_bound", true, "revision", "0",
                "active_command_id", "", "mode", "PASS", "applied_known", true,
                "delay_ms", 0, "remaining_ms", 0, "window_id", WINDOW,
                "window_remaining_ms", 1800)));

        assertThat(published).hasSize(2);
        Published command = published.get(1);
        assertThat(command.topic()).isEqualTo(PerceptionFaultService.COMMAND_TOPIC);
        assertThat(command.qos()).isEqualTo(1);
        JsonNode envelope = mapper.readTree(command.payload());
        assertThat(envelope.path("source_id").asText()).isEqualTo("dom0-server");
        assertThat(envelope.path("boot_id").asText()).isEqualTo(BOOT);
        assertThat(envelope.path("agent_instance_id").asText()).isEqualTo(INSTANCE);
        JsonNode body = envelope.path("payload");
        assertThat(body.path("command_id").asText()).isEqualTo(operation.commandId());
        assertThat(body.path("service_instance").asText()).isEqualTo(SERVICE);
        assertThat(body.path("window_id").asText()).isEqualTo(WINDOW);
        assertThat(body.path("mode").asText()).isEqualTo("BLOCK");
        assertThat(body.path("delay_ms").asInt()).isZero();
        assertThat(body.path("duration_s").asInt()).isEqualTo(50);

        service.accept(PerceptionFaultService.RESULT_TOPIC, response("perception.fault.result", Map.of(
                "command_id", operation.commandId(), "status", "APPLIED", "reason", "")));
        assertThat(service.get(operation.operationId()).status()).isEqualTo("APPLIED");
        service.accept(PerceptionFaultService.RESULT_TOPIC, response("perception.fault.result", Map.of(
                "command_id", operation.commandId(), "status", "ENDED", "reason", "DURATION_ELAPSED")));
        PerceptionFaultOperation ended = service.get(operation.operationId());
        assertThat(ended.status()).isEqualTo("ENDED");
        assertThat(ended.reason()).isEqualTo("DURATION_ELAPSED");
    }

    @Test
    void blockRequiresFreshPerceptionDuringAutoDrive() {
        when(tracker.snapshot()).thenReturn(state("MANUAL", true));
        assertThatThrownBy(() -> service.block(10))
                .isInstanceOf(ApiException.class)
                .extracting(error -> ((ApiException) error).getCode())
                .isEqualTo("AUTO_DRIVE_REQUIRED");
    }

    private DriveState state(String mode, boolean perceptionValid) {
        return new DriveState(1, NOW, null,
                new DriveState.Control(mode, 7L, "autonomous", VM, "fixture", 0, null),
                null, Map.of(), new DriveState.Perception(perceptionValid, 0, 15, 0.0, 0.0, 0, false),
                null, null, Map.of());
    }

    private byte[] response(String type, Map<String, Object> payload) throws Exception {
        return mapper.writeValueAsBytes(Map.of(
                "schema_version", "1.0", "message_id", BOOT, "source_id", "jetson",
                "boot_id", BOOT, "agent_instance_id", SERVICE, "type", type,
                "seq", 1, "sent_at", NOW.toString(), "payload", payload));
    }

    private record Published(String topic, byte[] payload, int qos) {
    }
}
