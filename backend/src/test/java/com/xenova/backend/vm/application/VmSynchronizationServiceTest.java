package com.xenova.backend.vm.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.xenova.backend.agent.application.AgentInventoryCleaner;
import com.xenova.backend.event.application.EventRecorder;
import com.xenova.backend.event.application.VmStateTracker;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.api.VmSynchronizationResponse;
import com.xenova.backend.vm.domain.CreateVmCommand;
import com.xenova.backend.vm.domain.VmLifecycle;
import com.xenova.backend.vm.domain.VmRecord;
import com.xenova.backend.vm.domain.VmRecordRepository;
import com.xenova.backend.vm.domain.VmRegistrationInventory;
import com.xenova.backend.vm.domain.VmRegistrationInventoryPort;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class VmSynchronizationServiceTest {

    private static final String BOARD_ONLY_ID = "850e8400-e29b-41d4-a716-446655440001";
    private static final String EXISTING_ID = "850e8400-e29b-41d4-a716-446655440002";
    private static final String DB_ONLY_ID = "850e8400-e29b-41d4-a716-446655440003";

    @Mock
    private VmRegistrationInventoryPort inventoryPort;

    @Mock
    private VmRecordRepository repository;

    @Mock
    private VmStateTracker stateTracker;

    @Mock
    private VmRuntimeCache runtimeCache;

    @Mock
    private EventRecorder eventRecorder;

    @Mock
    private AgentInventoryCleaner inventoryCleaner;

    private VmSynchronizationService service;

    @BeforeEach
    void setUp() {
        service = new VmSynchronizationService(inventoryPort, repository, stateTracker, runtimeCache,
                eventRecorder, inventoryCleaner,
                new VmRegistrationSyncProperties(2, Duration.ofMinutes(20)),
                Clock.fixed(Instant.parse("2026-11-02T00:00:00Z"), ZoneOffset.UTC));
    }

    @Test
    void retiresAVmTheBoardNoLongerRegistersOnTheSecondConsecutiveRead() {
        VmRecord removedOnBoard = record(DB_ONLY_ID, "web-test");
        when(repository.findAll()).thenReturn(List.of(removedOnBoard));
        when(inventoryPort.listRegistrations()).thenReturn(List.of());

        VmSynchronizationResponse first = service.synchronize();

        assertThat(first.missingFromBoard()).isEqualTo(1);
        assertThat(first.retired()).isZero();
        assertThat(removedOnBoard.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        verify(repository, never()).save(any(VmRecord.class));

        VmSynchronizationResponse second = service.synchronize();

        assertThat(second.retired()).isEqualTo(1);
        assertThat(second.missingFromBoard()).isZero();
        assertThat(second.results()).singleElement().satisfies(item -> {
            assertThat(item.action()).isEqualTo("retired");
            assertThat(item.reason()).isEqualTo("missing_from_board");
        });
        assertThat(removedOnBoard.getLifecycle()).isEqualTo(VmLifecycle.RETIRED);
        verify(repository).save(removedOnBoard);
        verify(runtimeCache).remove(DB_ONLY_ID);
        verify(stateTracker).forget(DB_ONLY_ID);
        verify(eventRecorder).vmDeleted(DB_ONLY_ID, "web-test", "platform");
        verify(inventoryCleaner).vmDeleted(DB_ONLY_ID);
    }

    @Test
    void aVmBackOnTheBoardStartsCountingAgain() {
        VmRecord flapping = record(DB_ONLY_ID, "flapping");
        when(repository.findAll()).thenReturn(List.of(flapping));
        when(inventoryPort.listRegistrations())
                .thenReturn(List.of())
                .thenReturn(List.of(inventory(DB_ONLY_ID, "flapping")))
                .thenReturn(List.of());

        service.synchronize();
        service.synchronize();
        VmSynchronizationResponse third = service.synchronize();

        assertThat(third.retired()).isZero();
        assertThat(third.missingFromBoard()).isEqualTo(1);
        assertThat(flapping.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        verify(eventRecorder, never()).vmDeleted(anyString(), anyString(), anyString());
    }

    @Test
    void neverRetiresAVmInASpringOperationOrStillInItsCreationGrace() {
        VmRecord busy = record(DB_ONLY_ID, "busy");
        VmRecord justCreated = new VmRecord(
                BOARD_ONLY_ID, new CreateVmCommand("just-created", "debian-13-arm64", "1.0.2", 1, 512),
                GuestKind.LINUX, Instant.parse("2026-11-01T23:50:00Z"));
        when(repository.findAll()).thenReturn(List.of(busy, justCreated));
        when(inventoryPort.listRegistrations()).thenReturn(List.of());
        when(stateTracker.inOperation(DB_ONLY_ID)).thenReturn(true);

        service.synchronize();
        service.synchronize();
        VmSynchronizationResponse third = service.synchronize();

        assertThat(third.retired()).isZero();
        assertThat(third.missingFromBoard()).isEqualTo(2);
        assertThat(busy.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        assertThat(justCreated.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        verify(repository, never()).save(any(VmRecord.class));
    }

    @Test
    void aFailedBoardReadChangesNothing() {
        VmRecord removedOnBoard = record(DB_ONLY_ID, "web-test");
        when(repository.findAll()).thenReturn(List.of(removedOnBoard));
        when(inventoryPort.listRegistrations())
                .thenReturn(List.of())
                .thenThrow(new IllegalStateException("board unreachable"));

        service.synchronize();
        assertThatThrownBy(() -> service.synchronize()).isInstanceOf(IllegalStateException.class);

        assertThat(removedOnBoard.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        verify(repository, never()).save(any(VmRecord.class));
    }

    @Test
    void registersBoardOnlyVmUpdatesChangedVmAndReportsDatabaseOnlyVm() {
        VmRecord existing = record(EXISTING_ID, "existing");
        VmRecord databaseOnly = record(DB_ONLY_ID, "database-only");
        when(repository.findAll()).thenReturn(List.of(existing, databaseOnly));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(
                inventory(BOARD_ONLY_ID, "board-only"),
                inventory(EXISTING_ID, "stale-board-name")));
        when(repository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.discovered()).isEqualTo(2);
        assertThat(response.registered()).isEqualTo(1);
        assertThat(response.updated()).isEqualTo(1);
        assertThat(response.unchanged()).isZero();
        assertThat(response.missingFromBoard()).isEqualTo(1);
        assertThat(existing.getName()).isEqualTo("stale-board-name");
        assertThat(databaseOnly.getName()).isEqualTo("database-only");
        assertThat(response.results()).extracting(VmSynchronizationResponse.Item::action)
                .containsExactly("registered", "updated", "missing_from_board");

        ArgumentCaptor<VmRecord> saved = ArgumentCaptor.forClass(VmRecord.class);
        verify(repository, org.mockito.Mockito.times(2)).save(saved.capture());
        assertThat(saved.getAllValues()).extracting(VmRecord::getVmId)
                .containsExactly(BOARD_ONLY_ID, EXISTING_ID);
        assertThat(saved.getAllValues().getFirst().getAssignedIpv4()).isEqualTo("192.168.77.30");
    }

    @Test
    void reportsOldDom0ResponseAsIncompleteInsteadOfCreatingAPartialRow() {
        when(repository.findAll()).thenReturn(List.of());
        when(inventoryPort.listRegistrations()).thenReturn(List.of(
                new VmRegistrationInventory(BOARD_ONLY_ID, null, null, null, null, 1, 512, null)));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.registered()).isZero();
        assertThat(response.incomplete()).isEqualTo(1);
        assertThat(response.results()).singleElement().satisfies(item -> {
            assertThat(item.action()).isEqualTo("incomplete");
            assertThat(item.reason()).isEqualTo("registration_metadata_missing");
        });
    }

    private static VmRegistrationInventory inventory(String vmId, String name) {
        return new VmRegistrationInventory(
                vmId, name, GuestKind.LINUX, "debian-13-arm64", "1.0.2",
                1, 512, "192.168.77.30");
    }

    private static VmRecord record(String vmId, String name) {
        return new VmRecord(
                vmId, new CreateVmCommand(name, "debian-13-arm64", "1.0.2", 1, 512),
                GuestKind.LINUX, Instant.parse("2026-11-01T00:00:00Z"));
    }
}
