package com.xenova.backend.display.phone;

import com.fasterxml.jackson.databind.JsonNode;

/** USB phone display relay on the board, read through dom0-manager (dom0 contract 4.13). */
public interface PhoneLinkPort {

    /** Relay state, phone URL and USB link. Never contains the pairing code. */
    JsonNode status();

    /** A new pairing code and its expiry. The caller must not store or log the code. */
    JsonNode issuePairingCode();
}
