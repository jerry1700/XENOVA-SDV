package com.xenova.backend.template;

import com.xenova.backend.template.domain.BoardTemplate;
import com.xenova.backend.template.domain.BoardTemplatePort;
import com.xenova.backend.template.domain.GuestKind;
import java.util.List;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

// Integration tests have no board: this stands in for dom0-manager GET /api/v1/templates.
// Picked up by component scanning from the test classpath and preferred over Dom0BoardTemplateClient.
@Component
@Primary
public class TestBoardTemplates implements BoardTemplatePort {

    public static final List<BoardTemplate> PUBLISHED = List.of(
            new BoardTemplate("debian-13-arm64", "1.0.7", GuestKind.LINUX),
            new BoardTemplate("debian-13-arm64", "1.0.8", GuestKind.LINUX),
            new BoardTemplate("ubuntu-24.04-arm64", "1.0.2", GuestKind.LINUX),
            new BoardTemplate("zephyr-xenvm-starter-arm64", "1.1.0", GuestKind.ZEPHYR));

    @Override
    public List<BoardTemplate> listBoardTemplates() {
        return PUBLISHED;
    }
}
