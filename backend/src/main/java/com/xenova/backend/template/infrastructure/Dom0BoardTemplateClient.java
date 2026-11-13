package com.xenova.backend.template.infrastructure;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.xenova.backend.template.domain.BoardTemplate;
import com.xenova.backend.template.domain.BoardTemplatePort;
import com.xenova.backend.vm.infrastructure.Dom0ManagerClient;
import com.xenova.backend.vm.infrastructure.Dom0ManagerProperties;
import java.util.List;
import org.springframework.stereotype.Component;

// Separate bean from VmControlPort for the same reason as Dom0VmRegistrationInventoryClient.
@Component
public class Dom0BoardTemplateClient implements BoardTemplatePort {

    private final Dom0ManagerClient client;

    public Dom0BoardTemplateClient(Dom0ManagerProperties properties, ObjectMapper objectMapper) {
        this.client = new Dom0ManagerClient(properties, objectMapper);
    }

    @Override
    public List<BoardTemplate> listBoardTemplates() {
        return client.listTemplates();
    }
}
