package com.xenova.backend.display.phone;

import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.auth.domain.AccessTokenRevocationStore;
import com.xenova.backend.auth.infrastructure.JwtTokenService;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.common.security.JwtAuthenticationFilter;
import com.xenova.backend.common.security.SecurityConfig;
import java.time.Instant;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(PhoneLinkController.class)
@Import({SecurityConfig.class, JwtAuthenticationFilter.class})
class PhoneLinkApiIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired ObjectMapper json;
    @MockitoBean PhoneLinkPort phoneLink;
    @MockitoBean JwtTokenService jwtTokens;
    @MockitoBean AccessTokenRevocationStore revoked;
    String token;

    @BeforeEach
    void login() {
        token = "browser-phone-token";
        when(jwtTokens.decode(token)).thenReturn(Jwt.withTokenValue(token).header("alg", "HS256")
                .subject("phone-user").claim("jti", "phone-token-id").issuedAt(Instant.now())
                .expiresAt(Instant.now().plusSeconds(300)).build());
    }

    @Test
    void loginIsRequiredAndNothingIsAskedOfTheBoardWithoutIt() throws Exception {
        mvc.perform(get("/api/v1/display/phone-link")).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/v1/display/phone-link/pairing-code")).andExpect(status().isUnauthorized());
        verify(phoneLink, never()).status();
        verify(phoneLink, never()).issuePairingCode();
    }

    @Test
    void statusIsPassedThroughWithoutCaching() throws Exception {
        when(phoneLink.status()).thenReturn(json.readTree("""
            {"configured":true,"relay":"listening","url":"http://10.117.188.25:8084/display/mobile",
             "usb":{"interface":"usb0","present":true,"ipv4":["10.117.188.25/24"],"gateway":"10.117.188.86"},
             "configured_phone_ipv4":"10.117.188.86","subnet_mismatch":false,
             "pairing":{"active":true,"expires_at":"2026-11-12T08:31:05Z"},"observed_at":"2026-11-12T08:29:10Z"}
            """));

        mvc.perform(get("/api/v1/display/phone-link").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.relay").value("listening"))
                .andExpect(jsonPath("$.url").value("http://10.117.188.25:8084/display/mobile"))
                .andExpect(jsonPath("$.usb.gateway").value("10.117.188.86"))
                .andExpect(jsonPath("$.pairing.active").value(true));
    }

    @Test
    void pairingCodeIsReturnedWithoutCaching() throws Exception {
        when(phoneLink.issuePairingCode()).thenReturn(json.readTree("""
            {"code":"123456","expires_at":"2026-11-12T08:31:05Z"}
            """));

        mvc.perform(post("/api/v1/display/phone-link/pairing-code").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.code").value("123456"))
                .andExpect(jsonPath("$.expires_at").value("2026-11-12T08:31:05Z"));
    }

    @Test
    void relayNotListeningIsItsOwnConflict() throws Exception {
        when(phoneLink.issuePairingCode()).thenThrow(
                new ApiException(HttpStatus.CONFLICT, "INVALID_VM_STATE", "phone relay is not listening"));

        mvc.perform(post("/api/v1/display/phone-link/pairing-code").header("Authorization", "Bearer " + token))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("PHONE_RELAY_NOT_LISTENING"));
    }

    @Test
    void unreachableBoardIsPhoneLinkUnavailable() throws Exception {
        when(phoneLink.status()).thenThrow(
                new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "VM_CONTROL_UNAVAILABLE", "VM 제어 서비스에 연결할 수 없습니다."));

        mvc.perform(get("/api/v1/display/phone-link").header("Authorization", "Bearer " + token))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.code").value("PHONE_LINK_UNAVAILABLE"));
    }
}
