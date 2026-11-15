package com.xenova.backend.vm.infrastructure;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.template.domain.BoardTemplate;
import com.xenova.backend.vm.domain.VmControlPort;
import com.xenova.backend.vm.domain.CpuLayout;
import com.xenova.backend.vm.domain.CpuPolicyUpdate;
import com.xenova.backend.vm.domain.VmControlOperation;
import com.xenova.backend.vm.domain.VmLaunchSpec;
import com.xenova.backend.vm.domain.VmRegistrationInventory;
import com.xenova.backend.vm.domain.VmResourceUpdate;
import com.xenova.backend.vm.domain.VmRuntime;
import java.io.IOException;
import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.net.http.HttpClient;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpTimeoutException;
import java.time.Duration;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import java.util.Map;
import java.util.concurrent.TimeoutException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.http.converter.json.AbstractJackson2HttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

@Component
public class Dom0ManagerClient implements VmControlPort {

    private static final Logger log = LoggerFactory.getLogger(Dom0ManagerClient.class);

    private static final int MAX_ATTEMPTS = 2;
    private static final String CPU_NOT_READY = "CPU_NOT_READY";

    private final RestClient queryClient;
    private final RestClient commandClient;
    // The board's display_mode command may take up to 240s including its rollback (dom0 contract 4.14).
    private static final Duration DISPLAY_MODE_TIMEOUT = Duration.ofSeconds(260);
    private final RestClient displayModeClient;
    private final ObjectMapper objectMapper;
    private final Duration retryBackoff;

    public Dom0ManagerClient(Dom0ManagerProperties properties, ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
        this.retryBackoff = properties.retryBackoff();
        this.queryClient = createClient(properties, properties.queryTimeout());
        this.commandClient = createClient(properties, properties.commandTimeout());
        this.displayModeClient = createClient(properties, DISPLAY_MODE_TIMEOUT);
    }

    /** Read-only execution logs; independent from the console's interactive PTY lease. */
    public JsonNode runtimeLogs(Map<String, Object> filters, boolean sources) {
        return requireBody(executeQuery(() -> queryClient.get()
                .uri(builder -> {
                    builder.path(sources ? "/api/v1/logs/sources" : "/api/v1/logs");
                    // Values go in as URI variables so they are fully encoded: as literal template text a
                    // '{' broke the build (500) and a '+' reached dom0-manager as a space.
                    filters.keySet().forEach(key -> builder.queryParam(key, "{" + key + "}"));
                    return builder.build(filters);
                })
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(JsonNode.class)));
    }

    /** USB phone display relay state on the board (dom0 contract 4.13). Read-only, so retried like other reads. */
    public JsonNode phoneLink() {
        return requireBody(executeQuery(() -> queryClient.get()
                .uri("/api/v1/display/phone-link")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(JsonNode.class)));
    }

    /**
     * Issues a new phone pairing code (dom0 contract 4.13). Each call invalidates the previous code and the paired
     * phone, so it is retried only when the request never reached dom0-manager. The code is never logged.
     */
    public JsonNode issuePhonePairingCode() {
        return requireBody(executeCommand(() -> queryClient.post()
                .uri("/api/v1/display/phone-link/pairing-code")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(JsonNode.class)));
    }

    /** Current display mode (HDMI / phone) and the display services on the board (dom0 contract 4.14). */
    public JsonNode displayMode() {
        return requireBody(executeQuery(() -> queryClient.get()
                .uri("/api/v1/display/mode")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleDisplayModeError)
                .body(JsonNode.class)));
    }

    /**
     * Switches the display mode (dom0 contract 4.14). The board runs its display_mode command to the end, which is
     * usually a few seconds but up to 240s with its own rollback, so this uses a client that waits longer than that.
     * Not retried once sent: a second call would race the first switch.
     */
    public JsonNode setDisplayMode(String mode) {
        return requireBody(executeCommand(() -> displayModeClient.post()
                .uri("/api/v1/display/mode")
                .body(Map.of("mode", mode))
                .retrieve()
                .onStatus(status -> status.isError(), this::handleDisplayModeError)
                .body(JsonNode.class)));
    }

    @Override
    public List<VmRuntime> list() {
        VmRuntime[] response = executeQuery(() -> queryClient.get()
                .uri("/api/v1/vms")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(VmRuntime[].class));
        return response == null ? List.of() : Arrays.asList(response);
    }

    public List<VmRegistrationInventory> listRegistrations() {
        VmRegistrationInventory[] response = executeQuery(() -> queryClient.get()
                .uri("/api/v1/vm-registrations")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(VmRegistrationInventory[].class));
        return response == null ? List.of() : Arrays.asList(response);
    }

    /** Every template version published in the board catalog, unfiltered (dom0-manager GET /api/v1/templates). */
    public List<BoardTemplate> listTemplates() {
        BoardTemplate[] response = executeQuery(() -> queryClient.get()
                .uri("/api/v1/templates")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(BoardTemplate[].class));
        return response == null ? List.of() : Arrays.asList(response);
    }

    // 404 VMNotFound means the domain is absent in Xen, which is how a stopped VM looks to dom0-manager.
    @Override
    public Optional<VmRuntime> find(String vmId) {
        try {
            return Optional.of(requireBody(executeQuery(() -> queryClient.get()
                    .uri("/api/v1/vms/{vmId}", vmId)
                    .retrieve()
                    .onStatus(status -> status.isError(), this::handleError)
                    .body(VmRuntime.class))));
        } catch (ApiException exception) {
            if ("VM_NOT_FOUND".equals(exception.getCode())) {
                return Optional.empty();
            }
            throw exception;
        }
    }

    @Override
    public VmRuntime launch(String vmId, VmLaunchSpec spec) {
        return requireBody(executeCommand(() -> commandClient.post()
                .uri("/api/v1/vms")
                .body(Dom0LaunchRequest.of(vmId, spec))
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(VmRuntime.class)));
    }

    @Override
    public void stop(String vmId) {
        executeCommand(() -> {
            commandClient.post()
                    .uri("/api/v1/vms/{vmId}/stop", vmId)
                    .retrieve()
                    .onStatus(status -> status.isError(), this::handleError)
                    .toBodilessEntity();
            return null;
        });
    }

    @Override
    public VmControlOperation requestReboot(String vmId, String requestKey) {
        return requireBody(executeCommand(() -> commandClient.post()
                    .uri("/api/v1/vms/{vmId}/reboot", vmId)
                    .header("Idempotency-Key", requestKey)
                    .retrieve()
                    .onStatus(status -> status.isError(), this::handleError)
                    .body(VmControlOperation.class)));
    }

    @Override
    public VmControlOperation findControlOperation(String vmId, String requestKey) {
        return requireBody(executeQuery(() -> queryClient.get()
                .uri(builder -> builder
                        .path("/api/v1/vms/{vmId}/control-operations")
                        .queryParam("request_key", "{requestKey}")
                        .build(vmId, requestKey))
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(VmControlOperation.class)));
    }

    @Override
    public VmResourceUpdate updateResources(String vmId, int vcpus, int memoryMib, String requestKey) {
        return requireBody(executeCommand(() -> commandClient.put()
                .uri("/api/v1/vms/{vmId}/resources", vmId)
                .header("Idempotency-Key", requestKey)
                .body(new Dom0ResourceUpdateRequest(vcpus, memoryMib))
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(VmResourceUpdate.class)));
    }

    @Override
    public CpuLayout cpuLayout() {
        return requireBody(executeQuery(() -> queryClient.get()
                .uri("/api/v1/cpu-layout")
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(CpuLayout.class)));
    }

    @Override
    public CpuPolicyUpdate updateCpuPolicy(CpuPolicyUpdate.Request request, String requestKey) {
        return requireBody(executeCommand(() -> commandClient.put()
                .uri("/api/v1/cpu-policy")
                .header("Idempotency-Key", requestKey)
                .body(request)
                .retrieve()
                .onStatus(status -> status.isError(), this::handleError)
                .body(CpuPolicyUpdate.class)));
    }

    @Override
    public void kill(String vmId) {
        executeCommand(() -> {
            commandClient.post()
                    .uri("/api/v1/vms/{vmId}/kill", vmId)
                    .retrieve()
                    .onStatus(status -> status.isError(), this::handleError)
                    .toBodilessEntity();
            return null;
        });
    }

    @Override
    public void destroy(String vmId) {
        executeCommand(() -> {
            commandClient.delete()
                    .uri("/api/v1/vms/{vmId}", vmId)
                    .retrieve()
                    .onStatus(status -> status.isError(), this::handleError)
                    .toBodilessEntity();
            return null;
        });
    }

    private RestClient createClient(Dom0ManagerProperties properties, Duration readTimeout) {
        HttpClient httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(properties.connectTimeout())
                .build();
        JdkClientHttpRequestFactory requestFactory = new JdkClientHttpRequestFactory(httpClient);
        requestFactory.setReadTimeout(readTimeout);

        RestClient.Builder builder = RestClient.builder()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory)
                // dom0-manager only speaks snake_case JSON. Without this, other Jackson converters on the
                // classpath (e.g. YAML from springdoc) are picked first and the body is not JSON at all.
                .messageConverters(converters -> {
                    converters.removeIf(AbstractJackson2HttpMessageConverter.class::isInstance);
                    converters.add(0, new MappingJackson2HttpMessageConverter(objectMapper));
                })
                .defaultHeader(HttpHeaders.ACCEPT, "application/json");
        if (StringUtils.hasText(properties.serviceToken())) {
            builder.defaultHeader(HttpHeaders.AUTHORIZATION, "Bearer " + properties.serviceToken());
        }
        return builder.build();
    }

    private void handleError(
            org.springframework.http.HttpRequest request,
            org.springframework.http.client.ClientHttpResponse response) throws IOException {
        Dom0ErrorResponse error;
        try {
            error = objectMapper.readValue(response.getBody(), Dom0ErrorResponse.class);
        } catch (Exception exception) {
            throw new ApiException(
                    HttpStatus.BAD_GATEWAY,
                    "VM_CONTROL_BAD_RESPONSE",
                    "VM 제어 서비스의 오류 응답을 해석할 수 없습니다.");
        }

        // The board's own wording (English, with internal ids) goes to the log only; the API returns Korean text per code.
        log.warn("dom0-manager error: status={}, code={}, platform_code={}, message={}, detail={}",
                response.getStatusCode().value(), error.code(), error.platformCode(), error.message(), error.detail());
        throw mapError(error, response.getStatusCode().value());
    }

    private void handleDisplayModeError(
            org.springframework.http.HttpRequest request,
            org.springframework.http.client.ClientHttpResponse response) throws IOException {
        Dom0ErrorResponse error;
        try {
            error = objectMapper.readValue(response.getBody(), Dom0ErrorResponse.class);
        } catch (Exception exception) {
            throw new ApiException(
                    HttpStatus.BAD_GATEWAY, "DISPLAY_MODE_UNAVAILABLE", "보드의 화면 전환 응답을 해석할 수 없습니다.");
        }
        log.warn("dom0-manager display mode error: status={}, code={}, platform_code={}, message={}",
                response.getStatusCode().value(), error.code(), error.platformCode(), error.message());
        throw mapDisplayModeError(error, response.getStatusCode().value());
    }

    // dom0 contract 4.14: the board's reason is in platform_code. 409 means the mode did not change (or was restored).
    static ApiException mapDisplayModeError(Dom0ErrorResponse error, int upstreamStatus) {
        String platformCode = error.platformCode() == null ? "" : error.platformCode();
        return switch (error.code() == null ? "" : error.code()) {
            case "InvalidState" -> switch (platformCode) {
                case "USB_LINK_ABSENT" -> new ApiException(HttpStatus.CONFLICT, "PHONE_NOT_CONNECTED",
                        "휴대폰이 USB로 연결되어 있지 않습니다. 케이블을 연결하고 USB 테더링을 켠 뒤 다시 시도해 주세요.");
                case "DISPLAY_MODE_BUSY", "DISPLAY_SERVICE_NOT_SETTLED" -> new ApiException(HttpStatus.CONFLICT,
                        "DISPLAY_MODE_BUSY", "다른 화면 전환이 진행 중입니다. 잠시 후 다시 시도해 주세요.");
                case "MOBILE_ENDPOINT_NOT_READY" -> new ApiException(HttpStatus.CONFLICT, "DISPLAY_MODE_NOT_APPLIED",
                        "휴대폰 중계가 USB 연결을 찾지 못해 이전 화면을 유지했습니다. USB 테더링을 확인해 주세요.");
                case "HDMI_RENDERER_NOT_READY", "DISPLAY_SERVICE_TRANSITION_FAILED", "DISPLAY_MODE_FAILED_RESTORED" ->
                        new ApiException(HttpStatus.CONFLICT, "DISPLAY_MODE_NOT_APPLIED",
                                "화면 전환에 실패해 이전 화면을 유지했습니다. 잠시 후 다시 시도해 주세요.");
                default -> new ApiException(HttpStatus.CONFLICT, "DISPLAY_SETUP_REQUIRED",
                        "보드의 화면 설정이 준비되지 않아 전환할 수 없습니다. 보드의 표시 서비스 설정을 확인해 주세요.");
            };
            case "InvalidArgument" -> new ApiException(
                    HttpStatus.BAD_REQUEST, "INVALID_ARGUMENT", "보드가 화면 전환 요청을 거절했습니다.");
            case "DriverError" -> new ApiException(HttpStatus.INTERNAL_SERVER_ERROR, "DISPLAY_MODE_ROLLBACK_INCOMPLETE",
                    "화면 전환에 실패했고 이전 화면으로 완전히 되돌리지 못했습니다. 보드에서 화면 서비스를 확인해 주세요.");
            case "DriverTimeout" -> new ApiException(HttpStatus.GATEWAY_TIMEOUT, "DISPLAY_MODE_TIMEOUT",
                    "보드의 화면 전환이 시간 안에 끝나지 않았습니다. 상태를 새로 고쳐 확인해 주세요.");
            case "Unauthorized" -> serviceTokenRejected(upstreamStatus);
            default -> {
                if (upstreamStatus == HttpStatus.UNAUTHORIZED.value()) {
                    yield serviceTokenRejected(upstreamStatus);
                }
                yield new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "DISPLAY_MODE_UNAVAILABLE",
                        "보드의 화면 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.");
            }
        };
    }

    private ApiException mapError(Dom0ErrorResponse error, int upstreamStatus) {
        // The platform refuses to start a guest until this boot's host preparation matches the CPU policy
        // (a pinning change that returned host_preparation_required=true). dom0-manager has no mapping for it and
        // sends it as DriverError, so the platform code is checked first.
        if (CPU_NOT_READY.equals(error.platformCode())) {
            return new ApiException(HttpStatus.CONFLICT, CPU_NOT_READY,
                    "CPU 배치를 바꾼 뒤 보드 호스트 준비가 끝나지 않아 VM을 시작할 수 없습니다. "
                            + "보드에서 호스트 준비를 실행하거나 보드를 재부팅해 주세요.");
        }
        return switch (error.code() == null ? "" : error.code()) {
            case "VMNotFound" -> new ApiException(
                    HttpStatus.NOT_FOUND, "VM_NOT_FOUND", "보드에서 VM을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.");
            // Usually turned into success by VmService (a retried create of the same VM); shown only if it is not.
            case "VMExists" -> new ApiException(
                    HttpStatus.CONFLICT, "VM_ALREADY_EXISTS", "보드에 같은 VM이 이미 있습니다.");
            // The board already uses this platform (domain) name for a different vm_id. Unlike VMExists it is never
            // our own earlier launch, so it must not be read as success: it is the same public code as a Spring-side
            // name clash.
            case "VMNameConflict" -> new ApiException(
                    HttpStatus.CONFLICT, "VM_NAME_CONFLICT", "보드에 같은 이름의 VM이 이미 있습니다. 다른 이름을 사용하세요.");
            case "InvalidState" -> new ApiException(
                    HttpStatus.CONFLICT, "INVALID_VM_STATE", invalidStateMessage(error.platformCode()));
            case "InvalidArgument" -> new ApiException(
                    HttpStatus.BAD_REQUEST, "INVALID_ARGUMENT", invalidArgumentMessage(error.platformCode()));
            case "OperationNotFound" -> new ApiException(
                    HttpStatus.NOT_FOUND, "OPERATION_NOT_FOUND", "보드에서 요청한 작업 기록을 찾을 수 없습니다.");
            // Passed on as 507 like dom0-manager: retrying the same request cannot succeed until resources free up.
            case "InsufficientResources" -> new ApiException(
                    HttpStatus.INSUFFICIENT_STORAGE, "INSUFFICIENT_RESOURCES", insufficientResourceMessage(error));
            case "DriverError" -> new ApiException(
                    HttpStatus.INTERNAL_SERVER_ERROR, "VM_DRIVER_ERROR", "보드에서 VM 작업을 처리하지 못했습니다. 잠시 후 다시 시도하세요.");
            // The driver or platform does not offer this operation yet (e.g. caller-chosen vm_id before P2 supports it).
            case "NotSupported" -> new ApiException(
                    HttpStatus.NOT_IMPLEMENTED, "VM_CONTROL_NOT_SUPPORTED", "보드가 아직 지원하지 않는 작업입니다.");
            // Temporary registry/storage/Xen trouble or a platform lock held by another operation: retrying later can work,
            // so it uses the same public code as a connection failure.
            case "PlatformUnavailable" -> new ApiException(
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "VM_CONTROL_UNAVAILABLE",
                    "VM 제어 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도하세요.");
            // dom0-manager ran out of time itself (xl or platform call, including the 90s stop wait).
            case "DriverTimeout" -> new ApiException(
                    HttpStatus.GATEWAY_TIMEOUT, "VM_CONTROL_TIMEOUT", "VM 제어 서비스의 응답 시간이 초과되었습니다.");
            // The service token does not match: a deployment setting, not the user's login, so it is not passed on as 401.
            case "Unauthorized" -> serviceTokenRejected(upstreamStatus);
            default -> {
                if (upstreamStatus == HttpStatus.UNAUTHORIZED.value()) {
                    yield serviceTokenRejected(upstreamStatus);
                }
                log.warn("Unknown dom0-manager error code={}, status={}", error.code(), upstreamStatus);
                yield new ApiException(
                        HttpStatus.BAD_GATEWAY,
                        "VM_CONTROL_BAD_RESPONSE",
                        "VM 제어 서비스가 알 수 없는 오류를 반환했습니다.");
            }
        };
    }

    private static ApiException serviceTokenRejected(int upstreamStatus) {
        log.warn("dom0-manager rejected the service token (status={}); check DOM0_MANAGER_SERVICE_TOKEN "
                + "against XENOVA_SERVICE_TOKEN on the board", upstreamStatus);
        return new ApiException(
                HttpStatus.BAD_GATEWAY,
                "VM_CONTROL_BAD_RESPONSE",
                "VM 제어 서비스와의 연결 설정이 올바르지 않습니다.");
    }

    // platform_code says why the board refused (contract 8.1 mapping table). Errors dom0-manager raises itself carry
    // none; the caller that knows the operation (e.g. VmService.destroy) can replace the generic text.
    private static String invalidStateMessage(String platformCode) {
        return switch (platformCode == null ? "" : platformCode) {
            case "VM_NOT_STOPPED" -> "VM을 정지한 뒤 다시 시도하세요.";
            case "VM_NOT_RUNNING" -> "실행 중인 VM에서만 할 수 있는 작업입니다.";
            case "HOST_QUIESCING" -> "보드가 종료 중이라 새 작업을 받을 수 없습니다.";
            case "BOOT_ADMISSION_REQUIRED" -> "보드의 VM 실행 정책이 확정되지 않아 지금은 시작할 수 없습니다.";
            case "TARGET_CHANGED", "INTENT_MISMATCH" -> "그사이 VM 상태가 바뀌었습니다. 최신 상태를 확인한 뒤 다시 시도하세요.";
            case "ASSET_IN_USE" -> "VM 디스크를 다른 작업이 사용 중입니다. 잠시 후 다시 시도하세요.";
            case "VM_PURGED" -> "이미 완전히 삭제된 VM입니다.";
            default -> "지금 VM 상태에서는 할 수 없는 작업입니다. VM 상태를 확인한 뒤 다시 시도하세요.";
        };
    }

    private static String invalidArgumentMessage(String platformCode) {
        return switch (platformCode == null ? "" : platformCode) {
            case "TEMPLATE_NOT_FOUND", "TEMPLATE_VERSION_NOT_FOUND" ->
                    "보드에 해당 템플릿 버전이 없습니다. 템플릿을 다시 선택하세요.";
            case "RESOURCE_UNSUPPORTED" -> "이 템플릿이 허용하지 않는 vCPU·메모리 값입니다.";
            default -> "보드가 요청 값을 받아들이지 않았습니다. 입력값을 확인하세요.";
        };
    }

    // dom0-manager's message is English and names internal fields, so the user sees which resource ran out instead.
    private static String insufficientResourceMessage(Dom0ErrorResponse error) {
        String name = error.resource() == null ? null : error.resource().name();
        if (name == null) {
            return "보드의 자원이 부족합니다.";
        }
        return switch (name) {
            case "memory_mib" -> "보드에 남은 메모리가 부족합니다.";
            case "vcpus" -> "보드에 남은 CPU가 부족합니다.";
            case "ipv4" -> "VM에 할당할 수 있는 IP 주소가 없습니다.";
            default -> "보드의 자원이 부족합니다.";
        };
    }

    // Reads are safe to repeat, so any transport failure is retried once.
    private <T> T executeQuery(ClientCall<T> call) {
        return execute(call, true);
    }

    // A command that timed out may still be running in dom0-manager (single worker),
    // so only failures where the request never arrived are retried.
    private <T> T executeCommand(ClientCall<T> call) {
        return execute(call, false);
    }

    private <T> T execute(ClientCall<T> call, boolean retryAfterSend) {
        for (int attempt = 1; ; attempt++) {
            try {
                return call.execute();
            } catch (ApiException exception) {
                throw exception;
            } catch (ResourceAccessException exception) {
                TransportFailure failure = classify(exception);
                boolean retryable = failure == TransportFailure.CONNECT || retryAfterSend;
                if (!retryable || attempt >= MAX_ATTEMPTS) {
                    throw failure.toApiException();
                }
                log.warn("dom0-manager call failed ({}), retrying once", failure);
                pauseBeforeRetry();
            } catch (RestClientException exception) {
                throw new ApiException(
                        HttpStatus.BAD_GATEWAY,
                        "VM_CONTROL_BAD_RESPONSE",
                        "VM 제어 서비스의 응답을 처리할 수 없습니다.");
            }
        }
    }

    private TransportFailure classify(Throwable throwable) {
        Throwable current = throwable;
        while (current != null) {
            if (current instanceof HttpConnectTimeoutException || current instanceof ConnectException) {
                return TransportFailure.CONNECT;
            }
            if (current instanceof HttpTimeoutException
                    || current instanceof SocketTimeoutException
                    || current instanceof TimeoutException) {
                return TransportFailure.TIMEOUT;
            }
            current = current.getCause();
        }
        return TransportFailure.INTERRUPTED;
    }

    private void pauseBeforeRetry() {
        try {
            Thread.sleep(retryBackoff);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw TransportFailure.CONNECT.toApiException();
        }
    }

    private <T> T requireBody(T body) {
        if (body == null) {
            throw new ApiException(
                    HttpStatus.BAD_GATEWAY,
                    "VM_CONTROL_BAD_RESPONSE",
                    "VM 제어 서비스가 응답 본문을 반환하지 않았습니다.");
        }
        return body;
    }

    private enum TransportFailure {
        // The request never reached dom0-manager.
        CONNECT,
        // dom0-manager accepted the request but did not answer in time.
        TIMEOUT,
        // The connection broke after the request may have been sent.
        INTERRUPTED;

        ApiException toApiException() {
            if (this == TIMEOUT) {
                return new ApiException(
                        HttpStatus.GATEWAY_TIMEOUT,
                        "VM_CONTROL_TIMEOUT",
                        "VM 제어 서비스의 응답 시간이 초과되었습니다.");
            }
            return new ApiException(
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "VM_CONTROL_UNAVAILABLE",
                    "VM 제어 서비스에 연결할 수 없습니다.");
        }
    }

    @FunctionalInterface
    private interface ClientCall<T> {
        T execute();
    }
}
