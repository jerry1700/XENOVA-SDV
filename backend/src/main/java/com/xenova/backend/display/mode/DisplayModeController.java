package com.xenova.backend.display.mode;

import com.fasterxml.jackson.databind.JsonNode;
import com.xenova.backend.common.error.ApiException;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Vehicle display mode: HDMI monitor or USB phone (dom0 contract 4.14). Switching to the phone turns the phone relay
 * on, so the console no longer needs SSH for it. The board's answer is passed through; nothing is stored.
 */
@RestController
@RequestMapping("/api/v1/display/mode")
public class DisplayModeController {

    private static final Logger log = LoggerFactory.getLogger(DisplayModeController.class);

    private final DisplayModePort displayMode;

    public DisplayModeController(DisplayModePort displayMode) {
        this.displayMode = displayMode;
    }

    public record SwitchRequest(
            @NotNull(message = "mode는 필수입니다.")
            @Pattern(regexp = "hdmi|mobile", message = "mode는 hdmi 또는 mobile 이어야 합니다.")
            String mode) {
    }

    @GetMapping
    public ResponseEntity<JsonNode> status() {
        return noStore(call(displayMode::status));
    }

    @PostMapping
    public ResponseEntity<JsonNode> switchTo(@Valid @RequestBody SwitchRequest request, Authentication authentication) {
        log.info("Display mode switch to {} requested by {}", request.mode(), username(authentication));
        JsonNode result = call(() -> displayMode.switchTo(request.mode()));
        log.info("Display mode is now {}", result.path("mode").asText("unknown"));
        return noStore(result);
    }

    private static ResponseEntity<JsonNode> noStore(JsonNode body) {
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(body);
    }

    private static JsonNode call(Supplier<JsonNode> request) {
        try {
            return request.get();
        } catch (ApiException error) {
            // Transport failures come out of the shared client with VM wording; the console shows display wording.
            if ("VM_CONTROL_TIMEOUT".equals(error.getCode())) {
                throw new ApiException(HttpStatus.GATEWAY_TIMEOUT, "DISPLAY_MODE_TIMEOUT",
                        "보드의 화면 전환 응답 시간이 초과되었습니다. 상태를 새로 고쳐 확인해 주세요.");
            }
            if (error.getCode() != null && error.getCode().startsWith("VM_CONTROL_")) {
                throw new ApiException(error.getStatus(), "DISPLAY_MODE_UNAVAILABLE",
                        "보드의 화면 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.");
            }
            throw error;
        }
    }

    // The JWT carries the login name as the "username" claim; the principal itself is the user id.
    private static String username(Authentication authentication) {
        if (authentication == null) {
            return null;
        }
        if (authentication.getDetails() instanceof Jwt jwt && jwt.getClaimAsString("username") != null) {
            return jwt.getClaimAsString("username");
        }
        return authentication.getName();
    }
}
