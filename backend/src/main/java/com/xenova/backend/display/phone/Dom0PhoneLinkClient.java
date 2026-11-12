package com.xenova.backend.display.phone;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.vm.infrastructure.Dom0ManagerClient;
import com.xenova.backend.vm.infrastructure.Dom0ManagerProperties;
import org.springframework.stereotype.Component;

// Separate from VmControlPort so VM integration tests can replace the control client without breaking this bean
// (same reason as Dom0RuntimeLogClient).
@Component
public class Dom0PhoneLinkClient implements PhoneLinkPort {

    private final Dom0ManagerClient client;

    public Dom0PhoneLinkClient(Dom0ManagerProperties properties, ObjectMapper objectMapper) {
        this.client = new Dom0ManagerClient(properties, objectMapper);
    }

    @Override
    public JsonNode status() {
        return client.phoneLink();
    }

    @Override
    public JsonNode issuePairingCode() {
        return client.issuePhonePairingCode();
    }
}
