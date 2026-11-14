package com.xenova.backend.display.mode;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.vm.infrastructure.Dom0ManagerClient;
import com.xenova.backend.vm.infrastructure.Dom0ManagerProperties;
import org.springframework.stereotype.Component;

// Separate from VmControlPort so VM integration tests can replace the control client without breaking this bean
// (same reason as Dom0PhoneLinkClient).
@Component
public class Dom0DisplayModeClient implements DisplayModePort {

    private final Dom0ManagerClient client;

    public Dom0DisplayModeClient(Dom0ManagerProperties properties, ObjectMapper objectMapper) {
        this.client = new Dom0ManagerClient(properties, objectMapper);
    }

    @Override
    public JsonNode status() {
        return client.displayMode();
    }

    @Override
    public JsonNode switchTo(String mode) {
        return client.setDisplayMode(mode);
    }
}
