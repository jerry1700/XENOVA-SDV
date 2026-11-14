package com.xenova.backend.display.mode;

import static org.mockito.ArgumentMatchers.anyString;
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
import org.springframework.http.MediaType;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(DisplayModeController.class)
@Import({SecurityConfig.class, JwtAuthenticationFilter.class})
class DisplayModeApiIntegrationTest {

    private static final String MOBILE = """
            {"mode":"mobile","boot_default":"hdmi",
             "services":{"hdmi":"inactive","mobile":"active","feed":"active"},
             "hdmi_renderer_ready":false,"observed_at":"2026-11-14T09:00:00Z"}
            """;

    @Autowired MockMvc mvc;
    @Autowired ObjectMapper json;
    @MockitoBean DisplayModePort displayMode;
    @MockitoBean JwtTokenService jwtTokens;
    @MockitoBean AccessTokenRevocationStore revoked;
    String token;

    @BeforeEach
    void login() {
        token = "browser-display-token";
        when(jwtTokens.decode(token)).thenReturn(Jwt.withTokenValue(token).header("alg", "HS256")
                .subject("display-user").claim("jti", "display-token-id").issuedAt(Instant.now())
                .expiresAt(Instant.now().plusSeconds(300)).build());
    }

    @Test
    void loginIsRequiredAndNothingIsAskedOfTheBoardWithoutIt() throws Exception {
        mvc.perform(get("/api/v1/display/mode")).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/v1/display/mode").contentType(MediaType.APPLICATION_JSON).content("{\"mode\":\"mobile\"}"))
                .andExpect(status().isUnauthorized());
        verify(displayMode, never()).status();
        verify(displayMode, never()).switchTo(anyString());
    }

    @Test
    void statusIsPassedThroughWithoutCaching() throws Exception {
        when(displayMode.status()).thenReturn(json.readTree(MOBILE));

        mvc.perform(get("/api/v1/display/mode").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.mode").value("mobile"))
                .andExpect(jsonPath("$.services.mobile").value("active"));
    }

    @Test
    void switchSendsTheModeAndReturnsTheStateAfterIt() throws Exception {
        when(displayMode.switchTo("mobile")).thenReturn(json.readTree(MOBILE));

        mvc.perform(post("/api/v1/display/mode").header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\":\"mobile\"}"))
                .andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.mode").value("mobile"));
        verify(displayMode).switchTo("mobile");
    }

    @Test
    void unknownModeIsAValidationErrorAndNotSent() throws Exception {
        mvc.perform(post("/api/v1/display/mode").header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\":\"tv\"}"))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(jsonPath("$.code").value("VALIDATION_ERROR"));
        verify(displayMode, never()).switchTo(anyString());
    }

    @Test
    void boardRefusalIsPassedOnWithItsDisplayCode() throws Exception {
        when(displayMode.switchTo("mobile")).thenThrow(new ApiException(HttpStatus.CONFLICT, "PHONE_NOT_CONNECTED",
                "휴대폰이 USB로 연결되어 있지 않습니다."));

        mvc.perform(post("/api/v1/display/mode").header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\":\"mobile\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("PHONE_NOT_CONNECTED"));
    }

    @Test
    void unreachableBoardIsDisplayModeUnavailable() throws Exception {
        when(displayMode.status()).thenThrow(
                new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "VM_CONTROL_UNAVAILABLE", "VM 제어 서비스에 연결할 수 없습니다."));

        mvc.perform(get("/api/v1/display/mode").header("Authorization", "Bearer " + token))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.code").value("DISPLAY_MODE_UNAVAILABLE"));
    }

    @Test
    void transportTimeoutIsDisplayModeTimeout() throws Exception {
        when(displayMode.switchTo("hdmi")).thenThrow(
                new ApiException(HttpStatus.GATEWAY_TIMEOUT, "VM_CONTROL_TIMEOUT", "timeout"));

        mvc.perform(post("/api/v1/display/mode").header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\":\"hdmi\"}"))
                .andExpect(status().isGatewayTimeout())
                .andExpect(jsonPath("$.code").value("DISPLAY_MODE_TIMEOUT"));
    }
}
