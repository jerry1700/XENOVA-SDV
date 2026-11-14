package com.xenova.backend.drive.application;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.common.error.ApiException;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/**
 * Jetson F02 계약의 STATUS → SET/BLOCK 흐름을 직렬화한다.
 * HTTP 요청은 STATUS 조회를 접수한 시점에 202로 끝나며, 같은 MQTT 연결로 도착한 현재 window만 SET에 쓴다.
 */
@Service
public class PerceptionFaultService {

    public static final String DEVICE = "oak-1";
    public static final String PREFIX = "xenova/v1/mapper/" + DEVICE + "/fault/";
    public static final String COMMAND_TOPIC = PREFIX + "command.json";
    public static final String QUERY_TOPIC = PREFIX + "query.json";
    public static final String RESULT_TOPIC = PREFIX + "result.json";
    public static final String STATUS_TOPIC = PREFIX + "status.json";

    private final ObjectMapper mapper;
    private final DriveStateTracker drive;
    private final Supplier<PerceptionFaultPublisher> publisher;
    private final Clock clock;
    private final String bootId;
    private final String agentInstanceId;
    private final AtomicLong seq = new AtomicLong();
    private Pending current;

    @Autowired
    public PerceptionFaultService(ObjectMapper mapper, DriveStateTracker drive,
            ObjectProvider<PerceptionFaultPublisher> publisher, Clock clock) {
        this(mapper, drive, publisher::getIfAvailable, clock, readBootId(), UUID.randomUUID().toString());
    }

    PerceptionFaultService(ObjectMapper mapper, DriveStateTracker drive,
            Supplier<PerceptionFaultPublisher> publisher, Clock clock, String bootId, String agentInstanceId) {
        this.mapper = mapper;
        this.drive = drive;
        this.publisher = publisher;
        this.clock = clock;
        this.bootId = bootId;
        this.agentInstanceId = agentInstanceId;
    }

    public synchronized PerceptionFaultOperation block(int durationS) {
        if (durationS < 1 || durationS > 120) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_DURATION", "장애 지속 시간은 1~120초여야 합니다.");
        }
        if (current != null && !current.terminal()) {
            throw new ApiException(HttpStatus.CONFLICT, "FAULT_OPERATION_IN_PROGRESS",
                    "이미 Jetson 인지 장애 명령을 처리하고 있습니다.");
        }
        DriveState state = drive.snapshot();
        if (state.control() == null || !"AUTO".equals(state.control().mode())) {
            throw new ApiException(HttpStatus.CONFLICT, "AUTO_DRIVE_REQUIRED",
                    "자율주행 중일 때만 Jetson 인지 단절 시연을 시작할 수 있습니다.");
        }
        if (state.perception() == null || !state.perception().valid()) {
            throw new ApiException(HttpStatus.CONFLICT, "PERCEPTION_NOT_READY",
                    "현재 인지 결과가 정상일 때만 단절 시연을 시작할 수 있습니다.");
        }
        Instant now = clock.instant();
        current = new Pending(UUID.randomUUID().toString(), UUID.randomUUID().toString(), durationS,
                "QUERYING", null, state.control().activeVmId(), state.control().mode(), now, now);
        if (!send(QUERY_TOPIC, envelope("perception.fault.query", Map.of("action", "STATUS")), 1)) {
            current.status = "FAILED";
            current.reason = "MQTT_UNAVAILABLE";
            current.updatedAt = clock.instant();
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "MQTT_UNAVAILABLE",
                    "MQTT 브로커에 연결되어 있지 않아 Jetson 상태를 조회하지 못했습니다.");
        }
        return current.view();
    }

    public synchronized PerceptionFaultOperation get(String operationId) {
        if (current == null || !current.operationId.equals(operationId)) {
            throw new ApiException(HttpStatus.NOT_FOUND, "FAULT_OPERATION_NOT_FOUND", "인지 장애 작업을 찾을 수 없습니다.");
        }
        if ((current.status.equals("QUERYING") || current.status.equals("COMMAND_SENT"))
                && current.updatedAt.plusSeconds(3).isBefore(clock.instant())) {
            current.status = "UNKNOWN";
            current.reason = "JETSON_RESPONSE_TIMEOUT";
            current.updatedAt = clock.instant();
        }
        return current.view();
    }

    /** MQTT callback thread에서만 호출된다. 형식이 맞지 않는 응답은 현재 작업을 변경하지 않는다. */
    public synchronized void accept(String topic, byte[] raw) {
        if (current == null || current.terminal() || raw.length == 0 || raw.length > 1024) {
            return;
        }
        try {
            JsonNode envelope = mapper.readTree(raw);
            if (!"1.0".equals(text(envelope, "schema_version")) || !"jetson".equals(text(envelope, "source_id"))) {
                return;
            }
            JsonNode payload = envelope.path("payload");
            if (STATUS_TOPIC.equals(topic) && "perception.fault.status".equals(text(envelope, "type"))) {
                acceptStatus(payload);
            } else if (RESULT_TOPIC.equals(topic) && "perception.fault.result".equals(text(envelope, "type"))) {
                acceptResult(payload);
            }
        } catch (IOException ignored) {
            // Jetson 제어 응답 하나가 MQTT drive 연결을 끊어서는 안 된다.
        }
    }

    private void acceptStatus(JsonNode payload) {
        if (!"QUERYING".equals(current.status)) {
            return;
        }
        String service = text(payload, "service_instance");
        String window = text(payload, "window_id");
        if (!uuid(service) || !uuid(window) || !payload.path("source_bound").asBoolean(false)
                || !payload.path("applied_known").asBoolean(false) || !"PASS".equals(text(payload, "mode"))
                || payload.path("window_remaining_ms").asLong(0) <= 0) {
            current.status = "REJECTED";
            current.reason = "JETSON_NOT_READY";
            current.updatedAt = clock.instant();
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("command_id", current.commandId);
        body.put("service_instance", service);
        body.put("window_id", window);
        body.put("action", "SET");
        body.put("mode", "BLOCK");
        body.put("delay_ms", 0);
        body.put("duration_s", current.durationS);
        if (!send(COMMAND_TOPIC, envelope("perception.fault.command", body), 1)) {
            current.status = "FAILED";
            current.reason = "MQTT_UNAVAILABLE";
        } else {
            current.status = "COMMAND_SENT";
            current.reason = null;
        }
        current.updatedAt = clock.instant();
    }

    private void acceptResult(JsonNode payload) {
        if (!current.commandId.equals(text(payload, "command_id"))) {
            return;
        }
        String status = text(payload, "status");
        if (status == null) {
            return;
        }
        current.status = switch (status) {
            case "APPLIED", "ENDED", "REJECTED", "UNKNOWN" -> status;
            default -> current.status;
        };
        current.reason = text(payload, "reason");
        current.updatedAt = clock.instant();
    }

    private byte[] envelope(String type, Map<String, Object> payload) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("schema_version", "1.0");
        body.put("message_id", UUID.randomUUID().toString());
        body.put("source_id", "dom0-server");
        body.put("boot_id", bootId);
        body.put("agent_instance_id", agentInstanceId);
        body.put("type", type);
        body.put("seq", seq.incrementAndGet());
        body.put("sent_at", clock.instant().truncatedTo(ChronoUnit.MILLIS).toString());
        body.put("payload", payload);
        try {
            return mapper.writeValueAsBytes(body);
        } catch (IOException exception) {
            throw new IllegalStateException("Cannot encode perception fault request", exception);
        }
    }

    private boolean send(String topic, byte[] payload, int qos) {
        PerceptionFaultPublisher target = publisher.get();
        return target != null && target.publish(topic, payload, qos);
    }

    private static String text(JsonNode node, String name) {
        JsonNode value = node.get(name);
        return value != null && value.isTextual() ? value.textValue() : null;
    }

    private static boolean uuid(String value) {
        try {
            return value != null && !new UUID(0, 0).equals(UUID.fromString(value));
        } catch (IllegalArgumentException exception) {
            return false;
        }
    }

    private static String readBootId() {
        try {
            String value = Files.readString(Path.of("/proc/sys/kernel/random/boot_id"), StandardCharsets.US_ASCII).trim();
            if (uuid(value)) {
                return value;
            }
        } catch (IOException ignored) {
            // 로컬 개발 환경에는 Linux boot_id가 없을 수 있다.
        }
        return UUID.randomUUID().toString();
    }

    private static final class Pending {
        private final String operationId;
        private final String commandId;
        private final int durationS;
        private String status;
        private String reason;
        private final String activeVmId;
        private final String modeBefore;
        private final Instant submittedAt;
        private Instant updatedAt;

        private Pending(String operationId, String commandId, int durationS, String status, String reason,
                String activeVmId, String modeBefore, Instant submittedAt, Instant updatedAt) {
            this.operationId = operationId;
            this.commandId = commandId;
            this.durationS = durationS;
            this.status = status;
            this.reason = reason;
            this.activeVmId = activeVmId;
            this.modeBefore = modeBefore;
            this.submittedAt = submittedAt;
            this.updatedAt = updatedAt;
        }

        private boolean terminal() {
            return switch (status) {
                case "ENDED", "REJECTED", "UNKNOWN", "FAILED" -> true;
                default -> false;
            };
        }

        private PerceptionFaultOperation view() {
            return new PerceptionFaultOperation(operationId, commandId, durationS, status, reason,
                    activeVmId, modeBefore, submittedAt, updatedAt);
        }
    }
}
