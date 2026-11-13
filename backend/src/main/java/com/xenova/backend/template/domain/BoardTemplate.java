package com.xenova.backend.template.domain;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

// One template version published in the board catalog (/srv/xenova/catalog), as dom0-manager GET /api/v1/templates
// returns it. The board lists every published version; choosing the newest one is Spring's job (TemplateCatalog).
// The board also sends default_resources, resource_limits and capabilities; Spring does not use them yet.
@JsonIgnoreProperties(ignoreUnknown = true)
public record BoardTemplate(String templateId, String templateVersion, GuestKind guestKind) {
}
