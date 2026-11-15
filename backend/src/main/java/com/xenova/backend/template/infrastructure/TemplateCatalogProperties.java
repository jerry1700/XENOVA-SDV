package com.xenova.backend.template.infrastructure;

import com.xenova.backend.template.domain.TemplateMetadata;
import java.time.Duration;
import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;

// Display metadata per template_id. Template versions are not configured here: they come from the board catalog.
// The list order is the order the wizard shows templates in.
@ConfigurationProperties(prefix = "xenova.template-catalog")
public record TemplateCatalogProperties(List<TemplateMetadata> templates, Duration refreshInterval) {

    private static final Duration DEFAULT_REFRESH_INTERVAL = Duration.ofSeconds(30);

    public TemplateCatalogProperties {
        templates = templates == null ? List.of() : List.copyOf(templates);
        refreshInterval = refreshInterval == null ? DEFAULT_REFRESH_INTERVAL : refreshInterval;
    }
}
