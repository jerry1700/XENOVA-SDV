package com.xenova.backend.template.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.template.domain.BoardTemplate;
import com.xenova.backend.template.domain.BoardTemplatePort;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.template.domain.TemplateMetadata;
import com.xenova.backend.template.domain.VmTemplate;
import com.xenova.backend.template.infrastructure.TemplateCatalogProperties;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;

class TemplateCatalogTest {

    private static final List<TemplateMetadata> METADATA = List.of(
            new TemplateMetadata("ubuntu-24.04-arm64", "Ubuntu 24.04", 512, GuestKind.LINUX),
            new TemplateMetadata("debian-13-arm64", "Debian 13", 256, GuestKind.LINUX),
            new TemplateMetadata("zephyr-drive-lattice-lab-arm64", "Zephyr Drive Lattice Lab", 16, GuestKind.ZEPHYR),
            new TemplateMetadata("zephyr-xenvm-starter-arm64", "Zephyr Network Starter", 16, GuestKind.ZEPHYR));

    private final FakeBoard board = new FakeBoard();
    private final MutableClock clock = new MutableClock(Instant.parse("2026-11-13T00:00:00Z"));
    private TemplateCatalog catalog;

    @BeforeEach
    void setUp() {
        board.templates = List.of(
                new BoardTemplate("debian-13-arm64", "1.0.5", GuestKind.LINUX),
                new BoardTemplate("debian-13-arm64", "1.0.8", GuestKind.LINUX),
                new BoardTemplate("debian-13-arm64", "1.0.7", GuestKind.LINUX),
                new BoardTemplate("ubuntu-24.04-arm64", "1.0.5", GuestKind.LINUX),
                new BoardTemplate("zephyr-drive-lattice-lab-arm64", "1.0.8", GuestKind.ZEPHYR));
        catalog = new TemplateCatalog(new TemplateCatalogProperties(METADATA, Duration.ofSeconds(30)), board, clock);
    }

    @Test
    void listShowsOnlyTheNewestVersionPerTemplateInConfiguredOrder() {
        assertThat(catalog.list()).containsExactly(
                new VmTemplate("ubuntu-24.04-arm64", "1.0.5", "Ubuntu 24.04", 512, GuestKind.LINUX),
                new VmTemplate("debian-13-arm64", "1.0.8", "Debian 13", 256, GuestKind.LINUX),
                new VmTemplate("zephyr-drive-lattice-lab-arm64", "1.0.8", "Zephyr Drive Lattice Lab", 16, GuestKind.ZEPHYR));
    }

    @Test
    void versionsCompareNumerically() {
        board.templates = List.of(
                new BoardTemplate("debian-13-arm64", "1.0.9", GuestKind.LINUX),
                new BoardTemplate("debian-13-arm64", "1.0.10", GuestKind.LINUX));

        assertThat(catalog.list()).extracting(VmTemplate::version).containsExactly("1.0.10");
        assertThat(TemplateCatalog.compareVersions("1.0.10", "1.0.9")).isPositive();
        assertThat(TemplateCatalog.compareVersions("1.1", "1.0.9")).isPositive();
        assertThat(TemplateCatalog.compareVersions("1.0", "1.0.0")).isZero();
    }

    @Test
    void findAcceptsAnyVersionTheBoardStillPublishes() {
        assertThat(catalog.find("debian-13-arm64", "1.0.5")).hasValueSatisfying(template -> {
            assertThat(template.version()).isEqualTo("1.0.5");
            assertThat(template.minMemoryMib()).isEqualTo(256);
        });
        assertThat(catalog.find("debian-13-arm64", "1.0.6")).isEmpty();
        assertThat(catalog.find("ubuntu-24.04-arm64", "1.0.2")).isEmpty();
    }

    @Test
    void boardTemplatesWithoutMetadataAndConfiguredTemplatesMissingFromTheBoardAreHidden() {
        board.templates = List.of(
                new BoardTemplate("debian-13-arm64", "1.0.8", GuestKind.LINUX),
                new BoardTemplate("alpine-unknown", "9.9.9", GuestKind.LINUX));

        assertThat(catalog.list()).extracting(VmTemplate::templateId).containsExactly("debian-13-arm64");
        assertThat(catalog.find("alpine-unknown", "9.9.9")).isEmpty();
        assertThat(catalog.find("zephyr-xenvm-starter-arm64", "1.1.0")).isEmpty();
    }

    @Test
    void theBoardGuestKindWins() {
        board.templates = List.of(new BoardTemplate("debian-13-arm64", "1.0.8", GuestKind.ZEPHYR));

        assertThat(catalog.list()).extracting(VmTemplate::guestKind).containsExactly(GuestKind.ZEPHYR);
    }

    @Test
    void theBoardIsReadOncePerRefreshInterval() {
        catalog.list();
        catalog.find("debian-13-arm64", "1.0.8");
        clock.advance(Duration.ofSeconds(29));
        catalog.list();
        assertThat(board.calls).isEqualTo(1);

        board.templates = List.of(new BoardTemplate("debian-13-arm64", "1.0.9", GuestKind.LINUX));
        clock.advance(Duration.ofSeconds(1));
        assertThat(catalog.list()).extracting(VmTemplate::version).containsExactly("1.0.9");
        assertThat(board.calls).isEqualTo(2);
    }

    @Test
    void aFailedRefreshKeepsTheLastListAndWaitsAnotherInterval() {
        catalog.list();
        board.failure = unavailable();
        clock.advance(Duration.ofSeconds(30));

        assertThat(catalog.list()).hasSize(3);
        catalog.list();
        assertThat(board.calls).isEqualTo(2);
    }

    @Test
    void withoutAnyListTheBoardErrorIsPassedOn() {
        board.failure = unavailable();

        assertThatThrownBy(catalog::list)
                .isInstanceOf(ApiException.class)
                .extracting("code").isEqualTo("VM_CONTROL_UNAVAILABLE");
        assertThatThrownBy(() -> catalog.find("debian-13-arm64", "1.0.8")).isInstanceOf(ApiException.class);
    }

    private static ApiException unavailable() {
        return new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "VM_CONTROL_UNAVAILABLE", "VM 제어 서비스에 연결할 수 없습니다.");
    }

    private static final class FakeBoard implements BoardTemplatePort {
        private List<BoardTemplate> templates = new ArrayList<>();
        private RuntimeException failure;
        private int calls;

        @Override
        public List<BoardTemplate> listBoardTemplates() {
            calls++;
            if (failure != null) {
                throw failure;
            }
            return templates;
        }
    }

    private static final class MutableClock extends Clock {
        private Instant now;

        private MutableClock(Instant now) {
            this.now = now;
        }

        void advance(Duration duration) {
            now = now.plus(duration);
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
