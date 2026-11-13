package com.xenova.backend.display.phone;

import com.fasterxml.jackson.databind.JsonNode;
import com.xenova.backend.common.error.ApiException;
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
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * USB phone display: where the phone should connect and a pairing code for it (dom0 contract 4.13).
 * Browser JWT -> server-only dom0 service token. Nothing is stored; the pairing code only passes through the
 * response and is never logged.
 */
@RestController
@RequestMapping("/api/v1/display/phone-link")
public class PhoneLinkController {

    private static final Logger log = LoggerFactory.getLogger(PhoneLinkController.class);

    private final PhoneLinkPort phoneLink;

    public PhoneLinkController(PhoneLinkPort phoneLink) {
        this.phoneLink = phoneLink;
    }

    @GetMapping
    public ResponseEntity<JsonNode> status() {
        return noStore(call(phoneLink::status));
    }

    @PostMapping("/pairing-code")
    public ResponseEntity<JsonNode> issuePairingCode(Authentication authentication) {
        JsonNode issued = call(phoneLink::issuePairingCode);
        // Who and when only: the code itself must not reach any log.
        log.info("Phone pairing code issued by {}", username(authentication));
        return noStore(issued);
    }

    private static ResponseEntity<JsonNode> noStore(JsonNode body) {
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(body);
    }

    private static JsonNode call(Supplier<JsonNode> request) {
        try {
            return request.get();
        } catch (ApiException error) {
            // dom0-manager answers 409 InvalidState when the relay is not listening; the generic mapping calls that
            // an invalid VM state, which would confuse the console.
            if (error.getStatus() == HttpStatus.CONFLICT) {
                throw new ApiException(HttpStatus.CONFLICT, "PHONE_RELAY_NOT_LISTENING",
                        "휴대폰 화면 중계가 꺼져 있거나 휴대폰이 연결되지 않았습니다. 모바일 모드와 USB 테더링을 확인해 주세요.");
            }
            if (error.getStatus().is5xxServerError()) {
                throw new ApiException(error.getStatus(), "PHONE_LINK_UNAVAILABLE",
                        "보드의 휴대폰 화면 중계 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.");
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
