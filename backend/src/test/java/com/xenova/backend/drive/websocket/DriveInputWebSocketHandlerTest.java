package com.xenova.backend.drive.websocket;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.auth.application.AccessTokenVerifier;
import com.xenova.backend.auth.application.AccessTokenVerifier.VerifiedToken;
import com.xenova.backend.drive.application.ManualInputRelay;
import com.xenova.backend.drive.application.ManualInputRelay.Result;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

// The relay repeats the held keys at 20 Hz until it is told to release, so every way an authenticated
// input socket ends must release them — including the closes the server itself starts.
class DriveInputWebSocketHandlerTest {

    private static final Instant START = Instant.parse("2026-11-12T00:00:00Z");

    private final MutableClock clock = new MutableClock(START);
    private final ManualInputRelay relay = mock(ManualInputRelay.class);
    private final AccessTokenVerifier verifier = mock(AccessTokenVerifier.class);
    private final WebSocketSession session = mock(WebSocketSession.class);
    private DriveInputWebSocketHandler handler;

    @BeforeEach
    void setUp() {
        when(session.getId()).thenReturn("s1");
        when(session.isOpen()).thenReturn(true);
        when(verifier.verify("token")).thenReturn(new VerifiedToken("operator", START.plusSeconds(900)));
        when(relay.update(any())).thenReturn(Result.SENT);
        when(relay.held()).thenReturn(List.of("up"));
        handler = new DriveInputWebSocketHandler(new ObjectMapper(), verifier, clock, relay);
        handler.afterConnectionEstablished(session);
    }

    @Test
    void closingForAnExpiredTokenReleasesTheHeldKeys() throws Exception {
        send("{\"type\":\"auth\",\"access_token\":\"token\"}");
        send("{\"type\":\"input\",\"keys\":[\"up\"]}");
        clock.now = START.plusSeconds(901);

        send("{\"type\":\"input\",\"keys\":[]}"); // the key-up frame arrives after expiry and is refused
        handler.afterConnectionClosed(session, AuthenticatedSocketHandler.UNAUTHORIZED);

        verify(session).close(AuthenticatedSocketHandler.UNAUTHORIZED);
        verify(relay).release();
    }

    @Test
    void closingForAnOversizedFrameReleasesTheHeldKeys() throws Exception {
        send("{\"type\":\"auth\",\"access_token\":\"token\"}");
        send("{\"type\":\"input\",\"keys\":[\"up\"]}");

        send("x".repeat(AuthenticatedSocketHandler.MAX_FRAME_BYTES + 1));
        handler.afterConnectionClosed(session, CloseStatus.TOO_BIG_TO_PROCESS);

        verify(relay).release();
    }

    @Test
    void aSocketThatNeverAuthenticatedDoesNotTouchTheKeys() throws Exception {
        handler.afterConnectionClosed(session, CloseStatus.NORMAL);

        verify(relay, never()).release();
    }

    private void send(String text) throws Exception {
        handler.handleMessage(session, new TextMessage(text));
    }

    private static final class MutableClock extends Clock {
        private Instant now;

        private MutableClock(Instant now) {
            this.now = now;
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }
}
