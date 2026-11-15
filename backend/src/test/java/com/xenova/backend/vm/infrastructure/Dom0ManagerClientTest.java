package com.xenova.backend.vm.infrastructure;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.vm.domain.VmLaunchSpec;
import com.xenova.backend.vm.domain.CpuLayout;
import com.xenova.backend.vm.domain.CpuPolicyUpdate;
import com.xenova.backend.vm.domain.VmControlOperation;
import com.xenova.backend.vm.domain.VmResourceUpdate;
import com.xenova.backend.vm.domain.VmRuntime;
import com.xenova.backend.vm.domain.VmState;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;

// Stub dom0-manager responses follow docs/contracts/dom0-manager-openapi.yaml.
class Dom0ManagerClientTest {

    private static final Duration RETRY_BACKOFF = Duration.ofMillis(200);
    private static final String VM_ID = "550e8400-e29b-41d4-a716-446655440000";
    private static final VmLaunchSpec SPEC =
            new VmLaunchSpec("주행 A 개발 VM", "alpine", "1.2.0", 1, 256);

    private final ObjectMapper objectMapper = new ObjectMapper()
            .setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);

    private HttpServer server;
    private Dom0ManagerClient client;
    private final AtomicReference<String> authorization = new AtomicReference<>();

    @BeforeEach
    void setUp() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(Executors.newCachedThreadPool());
        server.start();
        client = clientFor("http://127.0.0.1:" + server.getAddress().getPort());
    }

    private Dom0ManagerClient clientFor(String baseUrl) {
        Dom0ManagerProperties properties = new Dom0ManagerProperties(
                baseUrl,
                "service-secret",
                Duration.ofSeconds(1),
                Duration.ofMillis(300),
                Duration.ofMillis(300),
                RETRY_BACKOFF);
        return new Dom0ManagerClient(properties, objectMapper);
    }

    @AfterEach
    void tearDown() {
        server.stop(0);
    }

    @Test
    void listGetsVmCollectionOnceAndSendsServiceToken() {
        AtomicInteger requests = new AtomicInteger();
        AtomicReference<String> method = new AtomicReference<>();
        server.createContext("/api/v1/vms", exchange -> {
            requests.incrementAndGet();
            method.set(exchange.getRequestMethod());
            authorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
            respond(exchange, 200, """
                    [{"vm_id":"%s","state":"running","vcpus":1,"memory_mib":256}]
                    """.formatted(VM_ID));
        });

        List<VmRuntime> result = client.list();

        assertThat(result).containsExactly(new VmRuntime(VM_ID, VmState.RUNNING, 1, 256));
        assertThat(requests).hasValue(1);
        assertThat(method).hasValue("GET");
        assertThat(authorization.get()).isEqualTo("Bearer service-secret");
    }

    @Test
    void listRegistrationsReadsTheDedicatedPlatformRegistryResponse() {
        server.createContext(
                "/api/v1/vm-registrations",
                exchange -> respond(exchange, 200, """
                        [{
                          "vm_id":"%s",
                          "vcpus":1,
                          "memory_mib":512,
                          "name":"debian-dev",
                          "guest_kind":"linux",
                          "template_id":"debian-13-arm64",
                          "template_version":"1.0.2",
                          "assigned_ipv4":"192.168.77.30"
                        }]
                        """.formatted(VM_ID)));

        var vm = client.listRegistrations().getFirst();

        assertThat(vm.name()).isEqualTo("debian-dev");
        assertThat(vm.guestKind())
                .isEqualTo(com.xenova.backend.template.domain.GuestKind.LINUX);
        assertThat(vm.templateId()).isEqualTo("debian-13-arm64");
        assertThat(vm.templateVersion()).isEqualTo("1.0.2");
        assertThat(vm.assignedIpv4()).isEqualTo("192.168.77.30");
    }

    @Test
    void listTemplatesReadsEveryPublishedVersionAndIgnoresExtraFields() {
        server.createContext(
                "/api/v1/templates",
                exchange -> respond(exchange, 200, """
                        [{
                          "template_id":"debian-13-arm64",
                          "template_version":"1.0.7",
                          "guest_kind":"linux",
                          "default_resources":{"vcpus":1,"memory_mib":512},
                          "resource_limits":{"vcpus":[1,4],"memory_mib":[128,8192]},
                          "capabilities":{"graceful_shutdown":true}
                        },{
                          "template_id":"zephyr-drive-lattice-lab-arm64",
                          "template_version":"1.0.8",
                          "guest_kind":"zephyr",
                          "default_resources":{"vcpus":1,"memory_mib":16},
                          "resource_limits":{"vcpus":[1,1],"memory_mib":[16,16]},
                          "capabilities":{"graceful_shutdown":false}
                        }]
                        """));

        var templates = client.listTemplates();

        assertThat(templates).containsExactly(
                new com.xenova.backend.template.domain.BoardTemplate(
                        "debian-13-arm64", "1.0.7", com.xenova.backend.template.domain.GuestKind.LINUX),
                new com.xenova.backend.template.domain.BoardTemplate(
                        "zephyr-drive-lattice-lab-arm64", "1.0.8", com.xenova.backend.template.domain.GuestKind.ZEPHYR));
    }

    @Test
    void runtimeLogsForwardOnlyQueryAndServerServiceToken() {
        AtomicReference<String> query = new AtomicReference<>();

        server.createContext("/api/v1/logs", exchange -> {
            authorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
            query.set(exchange.getRequestURI().getQuery());
            respond(
                    exchange,
                    200,
                    """
                    {
                      "epoch":"one",
                      "items":[],
                      "next_cursor":42,
                      "has_more":false,
                      "history_truncated":false
                    }
                    """);
        });

        JsonNode page = client.runtimeLogs(
                java.util.Map.of(
                        "after", 41,
                        "component", "api.service"),
                false);

        assertThat(page.path("next_cursor").asInt()).isEqualTo(42);
        assertThat(query.get()).contains(
                "after=41",
                "component=api.service");
        assertThat(authorization.get()).isEqualTo("Bearer service-secret");
    }

    @Test
    void findTreatsVmNotFoundAsAbsentDomain() {
        server.createContext(
                "/api/v1/vms/" + VM_ID,
                exchange -> respond(exchange, 404, """
                        {
                          "code":"VMNotFound",
                          "message":"no domain",
                          "detail":"xl: domain not found"
                        }
                        """));

        assertThat(client.find(VM_ID)).isEmpty();
    }

    @Test
    void launchPostsTheOpenApiBodyWithSpringName() throws IOException {
        AtomicReference<String> body = new AtomicReference<>();
        AtomicReference<String> contentType = new AtomicReference<>();
        AtomicReference<String> upgrade = new AtomicReference<>();

        server.createContext("/api/v1/vms", exchange -> {
            contentType.set(exchange.getRequestHeaders().getFirst("Content-Type"));
            upgrade.set(exchange.getRequestHeaders().getFirst("Upgrade"));
            body.set(new String(
                    exchange.getRequestBody().readAllBytes(),
                    StandardCharsets.UTF_8));
            respond(exchange, 201, """
                    {
                      "vm_id":"%s",
                      "state":"running",
                      "vcpus":1,
                      "memory_mib":256
                    }
                    """.formatted(VM_ID));
        });

        VmRuntime runtime = client.launch(VM_ID, SPEC);

        assertThat(runtime.state()).isEqualTo(VmState.RUNNING);
        assertThat(contentType.get()).startsWith("application/json");
        assertThat(upgrade.get()).isNull();

        JsonNode sent = objectMapper.readTree(body.get());

        assertThat(sent.fieldNames()).toIterable()
                .containsExactlyInAnyOrder(
                        "vm_id",
                        "name",
                        "template_id",
                        "template_version",
                        "vcpus",
                        "memory_mib");
        assertThat(sent.path("name").asText()).isEqualTo(SPEC.name());
        assertThat(sent.path("vm_id").asText()).isEqualTo(VM_ID);
        assertThat(sent.path("template_id").asText())
                .isEqualTo(SPEC.templateId());
        assertThat(sent.path("template_version").asText())
                .isEqualTo(SPEC.templateVersion());
    }

    @Test
    void stopAcceptsNoContent() {
        AtomicInteger requests = new AtomicInteger();

        server.createContext("/api/v1/vms/" + VM_ID + "/stop", exchange -> {
            requests.incrementAndGet();
            exchange.sendResponseHeaders(204, -1);
            exchange.close();
        });

        client.stop(VM_ID);

        assertThat(requests).hasValue(1);
    }

    @Test
    void killPostsToTheKillPathOnceAndAcceptsNoContent() {
        AtomicInteger requests = new AtomicInteger();
        java.util.concurrent.atomic.AtomicReference<String> method = new java.util.concurrent.atomic.AtomicReference<>();

        server.createContext("/api/v1/vms/" + VM_ID + "/kill", exchange -> {
            requests.incrementAndGet();
            method.set(exchange.getRequestMethod());
            exchange.sendResponseHeaders(204, -1);
            exchange.close();
        });

        client.kill(VM_ID);

        assertThat(requests).hasValue(1);
        assertThat(method.get()).isEqualTo("POST");
    }

    @Test
    void rebootPostsTheIdempotencyKeyAndReadsThePendingOperation() {
        AtomicInteger requests = new AtomicInteger();
        AtomicReference<String> method = new AtomicReference<>();
        AtomicReference<String> requestKey = new AtomicReference<>();

        server.createContext("/api/v1/vms/" + VM_ID + "/reboot", exchange -> {
            requests.incrementAndGet();
            method.set(exchange.getRequestMethod());
            requestKey.set(exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            respond(exchange, 202, controlOperation("pending"));
        });

        VmControlOperation operation = client.requestReboot(VM_ID, "reboot-1");

        assertThat(requests).hasValue(1);
        assertThat(method.get()).isEqualTo("POST");
        assertThat(requestKey.get()).isEqualTo("reboot-1");
        assertThat(operation.status()).isEqualTo("pending");
        assertThat(operation.requestKey()).isEqualTo("reboot-1");
    }

    @Test
    void controlOperationPassesTheRequestKeyAsAQueryParameter() {
        AtomicReference<String> query = new AtomicReference<>();
        server.createContext("/api/v1/vms/" + VM_ID + "/control-operations", exchange -> {
            query.set(exchange.getRequestURI().getQuery());
            respond(exchange, 200, controlOperation("completed"));
        });

        VmControlOperation operation = client.findControlOperation(VM_ID, "reboot-1");

        assertThat(query.get()).isEqualTo("request_key=reboot-1");
        assertThat(operation.status()).isEqualTo("completed");
    }

    @Test
    void resourceUpdatePutsTheBodyAndIdempotencyKey() throws IOException {
        AtomicReference<String> method = new AtomicReference<>();
        AtomicReference<String> requestKey = new AtomicReference<>();
        AtomicReference<String> body = new AtomicReference<>();
        server.createContext("/api/v1/vms/" + VM_ID + "/resources", exchange -> {
            method.set(exchange.getRequestMethod());
            requestKey.set(exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            respond(exchange, 200, """
                    {
                      "vm_id":"%s",
                      "operation_id":"resize-operation",
                      "before":{"vcpus":1,"memory_mib":256},
                      "after":{"vcpus":2,"memory_mib":512},
                      "applies_on_next_start":true
                    }
                    """.formatted(VM_ID));
        });

        VmResourceUpdate result = client.updateResources(VM_ID, 2, 512, "resize-1");

        assertThat(method.get()).isEqualTo("PUT");
        assertThat(requestKey.get()).isEqualTo("resize-1");
        assertThat(objectMapper.readTree(body.get()).toString())
                .isEqualTo("{\"vcpus\":2,\"memory_mib\":512}");
        assertThat(result.after().vcpus()).isEqualTo(2);
        assertThat(result.after().memoryMib()).isEqualTo(512);
        assertThat(result.appliesOnNextStart()).isTrue();
    }

    @Test
    void cpuLayoutReadsThePlatformPolicy() {
        server.createContext("/api/v1/cpu-layout", exchange -> respond(exchange, 200, """
                {
                  "configured":true,
                  "policy_revision":2,
                  "policy_schema_version":"2.0",
                  "host":{"online_pcpus":[0,1,2,3],"max_cpu_id":3,"desired_dom0_pcpus":[0,3],"ready":true},
                  "reservations":[{"vm_id":"%s","pcpus":[2]}],
                  "vms":[{"vm_id":"%s","vcpus":1,"allocation":"shared",
                    "desired_hard_affinity":[{"vcpu":0,"pcpus":[0,3]}]}]
                }
                """.formatted(VM_ID, VM_ID)));

        CpuLayout layout = client.cpuLayout();

        assertThat(layout.policyRevision()).isEqualTo(2);
        assertThat(layout.host().desiredDom0Pcpus()).containsExactly(0, 3);
        assertThat(layout.vms()).singleElement().satisfies(vm ->
                assertThat(vm.desiredHardAffinity().getFirst().pcpus()).containsExactly(0, 3));
    }

    @Test
    void cpuPolicyUpdatePutsFullPolicyAndIdempotencyKey() throws IOException {
        AtomicReference<String> requestKey = new AtomicReference<>();
        AtomicReference<String> body = new AtomicReference<>();
        server.createContext("/api/v1/cpu-policy", exchange -> {
            requestKey.set(exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            respond(exchange, 200, """
                    {"policy":{"schema_version":"2.0","operation_id":"operation-1","policy_revision":3,
                    "pool_name":"Pool-0","shared_pcpus":[0,3],"placements":[
                    {"vm_id":"%s","mode":"shared","pcpus":[3]}]},"host_preparation_required":true}
                    """.formatted(VM_ID));
        });
        CpuPolicyUpdate.Request request = new CpuPolicyUpdate.Request(
                2, List.of(0, 3), List.of(new CpuPolicyUpdate.CpuPlacement(VM_ID, "shared", List.of(3))));

        CpuPolicyUpdate result = client.updateCpuPolicy(request, "pin-1");

        assertThat(requestKey.get()).isEqualTo("pin-1");
        assertThat(objectMapper.readTree(body.get())).isEqualTo(objectMapper.readTree("""
                {"expected_policy_revision":2,"shared_pcpus":[0,3],"placements":[
                {"vm_id":"%s","mode":"shared","pcpus":[3]}]}
                """.formatted(VM_ID)));
        assertThat(result.policy().policyRevision()).isEqualTo(3);
        assertThat(result.hostPreparationRequired()).isTrue();
    }

    // Visible ASCII keys may hold '+', '{' or '&': they must reach dom0-manager unchanged, not as a space,
    // a broken URI template (500) or an extra parameter.
    @Test
    void controlOperationEncodesReservedCharactersInTheRequestKey() {
        AtomicReference<String> query = new AtomicReference<>();
        server.createContext("/api/v1/vms/" + VM_ID + "/control-operations", exchange -> {
            query.set(exchange.getRequestURI().getRawQuery());
            respond(exchange, 200, controlOperation("completed"));
        });

        client.findControlOperation(VM_ID, "a+b{c}&d=e");

        assertThat(query.get()).isEqualTo("request_key=a%2Bb%7Bc%7D%26d%3De");
    }

    @Test
    void runtimeLogsEncodesFilterValues() {
        AtomicReference<String> query = new AtomicReference<>();
        server.createContext("/api/v1/logs", exchange -> {
            query.set(exchange.getRequestURI().getRawQuery());
            respond(exchange, 200, """
                    {"epoch":"one","items":[],"next_cursor":0,"has_more":false,"history_truncated":false}
                    """);
        });

        client.runtimeLogs(java.util.Map.of("component", "a+b{x}"), false);

        assertThat(query.get()).isEqualTo("component=a%2Bb%7Bx%7D");
    }

    @Test
    void insufficientResourcesIsMappedToPublicError() {
        server.createContext(
                "/api/v1/vms",
                exchange -> respond(exchange, 507, """
                        {
                          "code":"InsufficientResources",
                          "message":"메모리가 부족합니다.",
                          "resource":{
                            "name":"memory_mib",
                            "requested":1024,
                            "available":512
                          }
                        }
                        """));

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> {
                            assertThat(exception.getStatus())
                                    .isEqualTo(HttpStatus.INSUFFICIENT_STORAGE);
                            assertThat(exception.getCode())
                                    .isEqualTo("INSUFFICIENT_RESOURCES");
                            assertThat(exception.getMessage())
                                    .isEqualTo("보드에 남은 메모리가 부족합니다.");
                        });
    }

    @Test
    void exhaustedIpPoolIsReportedAsInsufficientResources() {
        server.createContext(
                "/api/v1/vms",
                exchange -> respond(exchange, 507, """
                        {
                          "code":"InsufficientResources",
                          "message":"insufficient ipv4: requested 1, available 0",
                          "resource":{
                            "name":"ipv4",
                            "requested":1,
                            "available":0
                          }
                        }
                        """));

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> {
                            assertThat(exception.getStatus())
                                    .isEqualTo(HttpStatus.INSUFFICIENT_STORAGE);
                            assertThat(exception.getMessage())
                                    .isEqualTo("VM에 할당할 수 있는 IP 주소가 없습니다.");
                        });
    }

    @Test
    void notSupportedIsMappedTo501() {
        assertLaunchFailsWith(
                501,
                """
                {
                  "code":"NotSupported",
                  "message":"caller-chosen vm_id is not supported by the platform yet"
                }
                """,
                HttpStatus.NOT_IMPLEMENTED,
                "VM_CONTROL_NOT_SUPPORTED");
    }

    @Test
    void platformUnavailableIsMappedTo503() {
        assertLaunchFailsWith(
                503,
                """
                {
                  "code":"PlatformUnavailable",
                  "message":"platform operation in progress"
                }
                """,
                HttpStatus.SERVICE_UNAVAILABLE,
                "VM_CONTROL_UNAVAILABLE");
    }

    @Test
    void driverTimeoutIsMappedTo504() {
        assertLaunchFailsWith(
                504,
                """
                {
                  "code":"DriverTimeout",
                  "message":"xl create timed out"
                }
                """,
                HttpStatus.GATEWAY_TIMEOUT,
                "VM_CONTROL_TIMEOUT");
    }

    @Test
    void rejectedServiceTokenIsNotPassedOnAsAUserAuthError() {
        assertLaunchFailsWith(
                401,
                """
                {
                  "code":"Unauthorized",
                  "message":"invalid service token"
                }
                """,
                HttpStatus.BAD_GATEWAY,
                "VM_CONTROL_BAD_RESPONSE");
    }

    @Test
    void a401WithAnUnexpectedBodyIsStillTreatedAsATokenProblem() {
        assertLaunchFailsWith(
                401,
                """
                {
                  "code":"SomethingElse",
                  "message":"nope"
                }
                """,
                HttpStatus.BAD_GATEWAY,
                "VM_CONTROL_BAD_RESPONSE");
    }

    @Test
    void phoneLinkReadsRelayStateWithServiceToken() {
        AtomicReference<String> method = new AtomicReference<>();
        server.createContext("/api/v1/display/phone-link", exchange -> {
            method.set(exchange.getRequestMethod());
            authorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
            respond(exchange, 200, """
                    {"configured":true,"relay":"listening","url":"http://10.117.188.25:8084/display/mobile",
                     "usb":{"interface":"usb0","present":true,"ipv4":["10.117.188.25/24"],"gateway":"10.117.188.86"},
                     "configured_phone_ipv4":"10.117.188.86","subnet_mismatch":false,
                     "pairing":{"active":false,"expires_at":null},"observed_at":"2026-11-12T08:29:10Z"}
                    """);
        });

        JsonNode status = client.phoneLink();

        assertThat(method).hasValue("GET");
        assertThat(authorization.get()).isEqualTo("Bearer service-secret");
        assertThat(status.path("url").asText()).isEqualTo("http://10.117.188.25:8084/display/mobile");
        assertThat(status.path("usb").path("gateway").asText()).isEqualTo("10.117.188.86");
    }

    @Test
    void phonePairingCodeIsPostedAndNotRetriedAfterTheRequestWasSent() {
        AtomicInteger requests = new AtomicInteger();
        server.createContext("/api/v1/display/phone-link/pairing-code", exchange -> {
            requests.incrementAndGet();
            try {
                Thread.sleep(800); // longer than the 300 ms query read timeout
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            respond(exchange, 200, """
                    {"code":"123456","expires_at":"2026-11-12T08:31:05Z"}
                    """);
        });

        assertThatThrownBy(() -> client.issuePhonePairingCode())
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getStatus()).isEqualTo(HttpStatus.GATEWAY_TIMEOUT));
        assertThat(requests).hasValue(1);
    }

    @Test
    void phonePairingCodeReturnsTheIssuedCode() {
        AtomicReference<String> method = new AtomicReference<>();
        server.createContext("/api/v1/display/phone-link/pairing-code", exchange -> {
            method.set(exchange.getRequestMethod());
            respond(exchange, 200, """
                    {"code":"123456","expires_at":"2026-11-12T08:31:05Z"}
                    """);
        });

        JsonNode issued = client.issuePhonePairingCode();

        assertThat(method).hasValue("POST");
        assertThat(issued.path("code").asText()).isEqualTo("123456");
    }

    @Test
    void displayModeSwitchPostsTheModeAndWaitsLongerThanTheQueryTimeout() {
        AtomicReference<String> sent = new AtomicReference<>();
        AtomicInteger requests = new AtomicInteger();
        server.createContext("/api/v1/display/mode", exchange -> {
            requests.incrementAndGet();
            sent.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            try {
                Thread.sleep(800); // longer than the 300 ms query read timeout: a switch may take minutes
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            respond(exchange, 200, """
                    {"mode":"mobile","boot_default":"hdmi",
                     "services":{"hdmi":"inactive","mobile":"active","feed":"active"},
                     "hdmi_renderer_ready":false,"observed_at":"2026-11-14T09:00:00Z"}
                    """);
        });

        JsonNode result = client.setDisplayMode("mobile");

        assertThat(sent.get()).contains("\"mode\":\"mobile\"");
        assertThat(requests).hasValue(1);
        assertThat(result.path("mode").asText()).isEqualTo("mobile");
        assertThat(result.path("services").path("mobile").asText()).isEqualTo("active");
    }

    @Test
    void displayModeRefusalsUseTheBoardReason() {
        server.createContext("/api/v1/display/mode", exchange -> respond(exchange, 409, """
                {"code":"InvalidState","message":"no USB link","platform_code":"USB_LINK_ABSENT"}
                """));

        assertThatThrownBy(() -> client.setDisplayMode("mobile"))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(exception.getCode()).isEqualTo("PHONE_NOT_CONNECTED");
                });
    }

    @Test
    void displayModeErrorsMapToDisplayCodes() {
        assertThat(code("InvalidState", "DISPLAY_MODE_BUSY", 409)).isEqualTo("DISPLAY_MODE_BUSY");
        assertThat(code("InvalidState", "MOBILE_ENDPOINT_NOT_READY", 409)).isEqualTo("DISPLAY_MODE_NOT_APPLIED");
        assertThat(code("InvalidState", "DISPLAY_MODE_FAILED_RESTORED", 409)).isEqualTo("DISPLAY_MODE_NOT_APPLIED");
        assertThat(code("InvalidState", "MODE_GUARD_NOT_LOADED", 409)).isEqualTo("DISPLAY_SETUP_REQUIRED");
        assertThat(code("DriverError", "ROLLBACK_INCOMPLETE_CHECK_DISPLAY_SERVICES", 500))
                .isEqualTo("DISPLAY_MODE_ROLLBACK_INCOMPLETE");
        assertThat(code("DriverTimeout", "DISPLAY_MODE_TIMEOUT", 504)).isEqualTo("DISPLAY_MODE_TIMEOUT");
        assertThat(code("PlatformUnavailable", null, 503)).isEqualTo("DISPLAY_MODE_UNAVAILABLE");
        assertThat(code("Unauthorized", null, 401)).isEqualTo("VM_CONTROL_BAD_RESPONSE");
    }

    private static String code(String code, String platformCode, int status) {
        return Dom0ManagerClient.mapDisplayModeError(
                new Dom0ErrorResponse(code, "m", null, null, platformCode), status).getCode();
    }

    @Test
    void driverErrorsAreNotRetried() {
        AtomicInteger requests = new AtomicInteger();

        server.createContext("/api/v1/vms", exchange -> {
            requests.incrementAndGet();
            respond(exchange, 503, """
                    {
                      "code":"PlatformUnavailable",
                      "message":"busy"
                    }
                    """);
        });

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOf(ApiException.class);
        assertThat(requests).hasValue(1);
    }

    // QA 1-B1: the board's English message (with internal ids) must not reach the API; Korean text per code instead.
    @org.junit.jupiter.params.ParameterizedTest(name = "{0} {1} -> {3}")
    @org.junit.jupiter.params.provider.CsvSource(delimiter = '|', textBlock = """
            VMNotFound        |                            | 404 | VM_NOT_FOUND        | 보드에서 VM을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.
            VMExists          |                            | 409 | VM_ALREADY_EXISTS   | 보드에 같은 VM이 이미 있습니다.
            InvalidState      |                            | 409 | INVALID_VM_STATE    | 지금 VM 상태에서는 할 수 없는 작업입니다. VM 상태를 확인한 뒤 다시 시도하세요.
            InvalidState      | VM_NOT_STOPPED             | 409 | INVALID_VM_STATE    | VM을 정지한 뒤 다시 시도하세요.
            InvalidState      | VM_NOT_RUNNING             | 409 | INVALID_VM_STATE    | 실행 중인 VM에서만 할 수 있는 작업입니다.
            InvalidState      | HOST_QUIESCING             | 409 | INVALID_VM_STATE    | 보드가 종료 중이라 새 작업을 받을 수 없습니다.
            InvalidArgument   |                            | 400 | INVALID_ARGUMENT    | 보드가 요청 값을 받아들이지 않았습니다. 입력값을 확인하세요.
            InvalidArgument   | TEMPLATE_VERSION_NOT_FOUND | 400 | INVALID_ARGUMENT    | 보드에 해당 템플릿 버전이 없습니다. 템플릿을 다시 선택하세요.
            OperationNotFound |                            | 404 | OPERATION_NOT_FOUND | 보드에서 요청한 작업 기록을 찾을 수 없습니다.
            DriverError       |                            | 500 | VM_DRIVER_ERROR     | 보드에서 VM 작업을 처리하지 못했습니다. 잠시 후 다시 시도하세요.
            """)
    void boardErrorsAreReturnedInKoreanWithoutTheBoardsOwnWording(
            String dom0Code, String platformCode, int status, String code, String message) {
        String platform = platformCode == null ? "" : ",\"platform_code\":\"" + platformCode + "\"";
        server.createContext(
                "/api/v1/vms",
                exchange -> respond(exchange, status, """
                        {"code":"%s","message":"domain 6f0c-internal is running; stop it before delete"%s}
                        """.formatted(dom0Code, platform)));

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus().value()).isEqualTo(status);
                    assertThat(exception.getCode()).isEqualTo(code);
                    assertThat(exception.getMessage()).isEqualTo(message).doesNotContain("domain", "6f0c");
                });
    }

    @Test
    void startRefusedUntilHostPreparationIsAClear409() {
        // dom0-manager has no mapping for the platform's CPU_NOT_READY and sends it as DriverError
        server.createContext(
                "/api/v1/vms",
                exchange -> respond(exchange, 500, """
                        {"code":"DriverError","message":"CPU_NOT_READY: prepare this host boot and policy revision",
                         "platform_code":"CPU_NOT_READY"}
                        """));

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(exception.getCode()).isEqualTo("CPU_NOT_READY");
                    assertThat(exception.getMessage()).contains("호스트 준비").doesNotContain("prepare this host");
                });
    }

    private void assertLaunchFailsWith(
            int dom0Status,
            String body,
            HttpStatus status,
            String code) {

        server.createContext(
                "/api/v1/vms",
                exchange -> respond(exchange, dom0Status, body));

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> {
                            assertThat(exception.getStatus()).isEqualTo(status);
                            assertThat(exception.getCode()).isEqualTo(code);
                        });
    }

    @Test
    void queryTimeoutIsRetriedOnceThenReportedAsTimeout() {
        AtomicInteger requests = new AtomicInteger();

        server.createContext("/api/v1/vms", exchange -> {
            requests.incrementAndGet();
            sleep(Duration.ofSeconds(1));
            respond(exchange, 200, "[]");
        });

        assertThatThrownBy(client::list)
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> assertThat(exception.getCode())
                                .isEqualTo("VM_CONTROL_TIMEOUT"));
        assertThat(requests).hasValue(2);
    }

    @Test
    void commandTimeoutIsNotRetried() {
        AtomicInteger requests = new AtomicInteger();

        server.createContext("/api/v1/vms/" + VM_ID + "/stop", exchange -> {
            requests.incrementAndGet();
            sleep(Duration.ofSeconds(1));
            exchange.sendResponseHeaders(204, -1);
            exchange.close();
        });

        assertThatThrownBy(() -> client.stop(VM_ID))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> {
                            assertThat(exception.getStatus().value()).isEqualTo(504);
                            assertThat(exception.getCode())
                                    .isEqualTo("VM_CONTROL_TIMEOUT");
                        });
        assertThat(requests).hasValue(1);
    }

    @Test
    void commandConnectFailureIsRetriedOnceThenReportedAsUnavailable()
            throws IOException {

        int closedPort;
        try (ServerSocket socket = new ServerSocket(0)) {
            closedPort = socket.getLocalPort();
        }

        Dom0ManagerClient unreachable =
                clientFor("http://127.0.0.1:" + closedPort);

        long startedAt = System.nanoTime();

        assertThatThrownBy(() -> unreachable.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> {
                            assertThat(exception.getStatus().value()).isEqualTo(503);
                            assertThat(exception.getCode())
                                    .isEqualTo("VM_CONTROL_UNAVAILABLE");
                        });

        assertThat(Duration.ofNanos(System.nanoTime() - startedAt))
                .isGreaterThanOrEqualTo(RETRY_BACKOFF);
    }

    @Test
    void vmExistsIsNotRetried() {
        AtomicInteger requests = new AtomicInteger();

        server.createContext("/api/v1/vms", exchange -> {
            requests.incrementAndGet();
            respond(exchange, 409, """
                    {
                      "code":"VMExists",
                      "message":"vm_id already exists"
                    }
                    """);
        });

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> assertThat(exception.getCode())
                                .isEqualTo("VM_ALREADY_EXISTS"));
        assertThat(requests).hasValue(1);
    }

    @Test
    void vmNameConflictIsAPublicNameConflictNotAnExistingVm() {
        server.createContext("/api/v1/vms", exchange -> respond(exchange, 409, """
                {
                  "code":"VMNameConflict",
                  "message":"platform name already used by another vm_id"
                }
                """));

        assertThatThrownBy(() -> client.launch(VM_ID, SPEC))
                .isInstanceOfSatisfying(
                        ApiException.class,
                        exception -> {
                            assertThat(exception.getStatus().value()).isEqualTo(409);
                            assertThat(exception.getCode()).isEqualTo("VM_NAME_CONFLICT");
                        });
    }

    private static String controlOperation(String status) {
        return """
                {
                  "vm_id":"%s",
                  "action":"reboot",
                  "request_key":"reboot-1",
                  "operation_id":"650e8400-e29b-41d4-a716-446655440000",
                  "status":"%s",
                  "accepted":true,
                  "phase":"%s",
                  "warnings":[]
                }
                """.formatted(VM_ID, status, status);
    }

    private void sleep(Duration duration) {
        try {
            Thread.sleep(duration);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
        }
    }

    private void respond(
            HttpExchange exchange,
            int status,
            String body) throws IOException {

        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set(
                "Content-Type",
                "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }
}
