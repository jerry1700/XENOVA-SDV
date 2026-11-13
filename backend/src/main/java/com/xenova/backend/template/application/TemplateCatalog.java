package com.xenova.backend.template.application;

import com.xenova.backend.template.domain.BoardTemplate;
import com.xenova.backend.template.domain.BoardTemplatePort;
import com.xenova.backend.template.domain.TemplateMetadata;
import com.xenova.backend.template.domain.VmTemplate;
import com.xenova.backend.template.infrastructure.TemplateCatalogProperties;
import java.math.BigInteger;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.stereotype.Service;

// Template versions come from the board catalog; display name and minimum memory come from configuration.
// - list(): the wizard. One entry per template_id, the newest version the board publishes.
// - find(): validation of create/start/resource changes. Any version the board still publishes, so a VM made from an
//   older version keeps working after a newer one is published.
// The board list is cached for refresh-interval. A failed refresh keeps the last good list; with none yet, the
// dom0-manager error (VM_CONTROL_UNAVAILABLE etc.) is passed on.
@Service
@EnableConfigurationProperties(TemplateCatalogProperties.class)
public class TemplateCatalog {

    private static final Logger log = LoggerFactory.getLogger(TemplateCatalog.class);

    private final Map<String, TemplateMetadata> metadata;
    private final BoardTemplatePort board;
    private final Duration refreshInterval;
    private final Clock clock;

    private volatile Snapshot snapshot;

    public TemplateCatalog(TemplateCatalogProperties properties, BoardTemplatePort board, Clock clock) {
        this.metadata = properties.templates().stream()
                .collect(Collectors.toMap(TemplateMetadata::templateId, Function.identity(), (a, b) -> a, LinkedHashMap::new));
        this.board = board;
        this.refreshInterval = properties.refreshInterval();
        this.clock = clock;
    }

    public List<VmTemplate> list() {
        Map<String, BoardTemplate> newest = new LinkedHashMap<>();
        for (BoardTemplate template : boardTemplates()) {
            newest.merge(template.templateId(), template,
                    (current, candidate) -> compareVersions(candidate.templateVersion(), current.templateVersion()) > 0
                            ? candidate
                            : current);
        }
        List<VmTemplate> result = new ArrayList<>();
        for (TemplateMetadata meta : metadata.values()) {
            BoardTemplate template = newest.get(meta.templateId());
            if (template != null) {
                result.add(toVmTemplate(meta, template));
            }
        }
        return result;
    }

    public Optional<VmTemplate> find(String templateId, String version) {
        TemplateMetadata meta = metadata.get(templateId);
        if (meta == null) {
            return Optional.empty();
        }
        return boardTemplates().stream()
                .filter(template -> template.templateId().equals(templateId) && template.templateVersion().equals(version))
                .findFirst()
                .map(template -> toVmTemplate(meta, template));
    }

    private List<BoardTemplate> boardTemplates() {
        Snapshot current = snapshot;
        Instant now = clock.instant();
        if (current != null && now.isBefore(current.fetchedAt().plus(refreshInterval))) {
            return current.templates();
        }
        synchronized (this) {
            current = snapshot;
            if (current != null && now.isBefore(current.fetchedAt().plus(refreshInterval))) {
                return current.templates();
            }
            try {
                List<BoardTemplate> templates = List.copyOf(board.listBoardTemplates());
                logUnknownTemplates(templates);
                snapshot = new Snapshot(templates, now);
                return templates;
            } catch (RuntimeException exception) {
                if (current == null) {
                    throw exception;
                }
                log.warn("Board template catalog refresh failed; keeping the list read at {}: {}",
                        current.fetchedAt(), exception.getMessage());
                // Retry on the next call after another interval, not on every request while the board is down.
                snapshot = new Snapshot(current.templates(), now);
                return current.templates();
            }
        }
    }

    private void logUnknownTemplates(List<BoardTemplate> templates) {
        templates.stream()
                .map(BoardTemplate::templateId)
                .distinct()
                .filter(id -> !metadata.containsKey(id))
                .forEach(id -> log.info("Board template {} has no display metadata in xenova.template-catalog; hidden", id));
    }

    private static VmTemplate toVmTemplate(TemplateMetadata meta, BoardTemplate template) {
        if (template.guestKind() != null && template.guestKind() != meta.guestKind()) {
            log.warn("Template {} is {} on the board but {} in configuration; using the board value",
                    template.templateId(), template.guestKind(), meta.guestKind());
        }
        return new VmTemplate(
                template.templateId(),
                template.templateVersion(),
                meta.displayName(),
                meta.minMemoryMib(),
                template.guestKind() != null ? template.guestKind() : meta.guestKind());
    }

    // Dot-separated numeric parts compare as numbers (1.0.10 > 1.0.9); a non-numeric part compares as text.
    static int compareVersions(String left, String right) {
        String[] a = left.split("\\.");
        String[] b = right.split("\\.");
        for (int i = 0; i < Math.max(a.length, b.length); i++) {
            String x = i < a.length ? a[i] : "0";
            String y = i < b.length ? b[i] : "0";
            int result = isNumber(x) && isNumber(y)
                    ? new BigInteger(x).compareTo(new BigInteger(y))
                    : x.compareTo(y);
            if (result != 0) {
                return result;
            }
        }
        return 0;
    }

    private static boolean isNumber(String part) {
        return !part.isEmpty() && part.chars().allMatch(Character::isDigit);
    }

    private record Snapshot(List<BoardTemplate> templates, Instant fetchedAt) {
    }
}
