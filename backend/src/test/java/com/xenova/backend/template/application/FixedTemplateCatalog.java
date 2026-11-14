package com.xenova.backend.template.application;

import com.xenova.backend.template.domain.BoardTemplate;
import com.xenova.backend.template.domain.TemplateMetadata;
import com.xenova.backend.template.domain.VmTemplate;
import com.xenova.backend.template.infrastructure.TemplateCatalogProperties;
import java.time.Clock;
import java.util.List;

// Unit-test catalog: the board publishes exactly the given templates, each with matching display metadata.
public final class FixedTemplateCatalog {

    private FixedTemplateCatalog() {
    }

    public static TemplateCatalog of(VmTemplate... templates) {
        List<TemplateMetadata> metadata = java.util.Arrays.stream(templates)
                .map(t -> new TemplateMetadata(t.templateId(), t.displayName(), t.minMemoryMib(), t.guestKind()))
                .distinct()
                .toList();
        List<BoardTemplate> board = java.util.Arrays.stream(templates)
                .map(t -> new BoardTemplate(t.templateId(), t.version(), t.guestKind()))
                .toList();
        return new TemplateCatalog(new TemplateCatalogProperties(metadata, null), () -> board, Clock.systemUTC());
    }
}
