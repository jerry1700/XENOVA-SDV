package com.xenova.backend.vm.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.xenova.backend.agent.application.AgentInventoryCleaner;
import com.xenova.backend.event.application.EventRecorder;
import com.xenova.backend.event.application.VmStateTracker;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.api.VmSynchronizationResponse;
import com.xenova.backend.vm.api.VmSynchronizationResponse.Item;
import com.xenova.backend.vm.domain.CreateVmCommand;
import com.xenova.backend.vm.domain.VmLifecycle;
import com.xenova.backend.vm.domain.VmRecord;
import com.xenova.backend.vm.domain.VmRecordRepository;
import com.xenova.backend.vm.domain.VmRegistrationInventory;
import com.xenova.backend.vm.domain.VmRegistrationInventoryPort;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.InOrder;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.transaction.support.TransactionOperations;

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
                eventRecorder, inventoryCleaner, TransactionOperations.withoutTransaction(),
                Clock.fixed(Instant.parse("2026-11-02T00:00:00Z"), ZoneOffset.UTC));
    }

    @Test
    void retiresAVmTheBoardNoLongerRegistersOnTheFirstRequest() {
        VmRecord removedOnBoard = record(DB_ONLY_ID, "web-test");
        when(repository.findAll()).thenReturn(List.of(removedOnBoard));
        when(inventoryPort.listRegistrations()).thenReturn(List.of());

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.retired()).isEqualTo(1);
        assertThat(response.results()).singleElement().isEqualTo(
                new Item(DB_ONLY_ID, "web-test", "retired", "missing_from_board"));
        assertThat(removedOnBoard.getLifecycle()).isEqualTo(VmLifecycle.RETIRED);
        assertThat(removedOnBoard.getName()).isEqualTo("web-test~850e8400");
        verify(repository).save(removedOnBoard);
        verify(repository).flush();
        verify(eventRecorder).vmDeleted(DB_ONLY_ID, "web-test", "platform");
        verify(runtimeCache).remove(DB_ONLY_ID);
        verify(stateTracker).forget(DB_ONLY_ID);
        verify(inventoryCleaner).vmDeleted(DB_ONLY_ID);
    }

    // The deployed case: the platform re-created the VMs, so the board has new vm_ids under the old names.
    @Test
    void aVmReCreatedOnTheBoardUnderTheSameNameReplacesTheOldRowInOneRequest() {
        VmRecord old = record(DB_ONLY_ID, "debian");
        when(repository.findAll()).thenReturn(List.of(old));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(inventory(BOARD_ONLY_ID, "debian")));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.retired()).isEqualTo(1);
        assertThat(response.registered()).isEqualTo(1);
        assertThat(response.conflicts()).isZero();
        assertThat(response.results()).containsExactly(
                new Item(DB_ONLY_ID, "debian", "retired", "missing_from_board"),
                new Item(BOARD_ONLY_ID, "debian", "registered", null));
        assertThat(old.getName()).isEqualTo("debian~850e8400");

        // The freed name reaches the table before the new row is inserted under it.
        ArgumentCaptor<VmRecord> saved = ArgumentCaptor.forClass(VmRecord.class);
        InOrder order = inOrder(repository);
        order.verify(repository).save(old);
        order.verify(repository).flush();
        order.verify(repository).save(saved.capture());
        assertThat(saved.getValue().getVmId()).isEqualTo(BOARD_ONLY_ID);
        assertThat(saved.getValue().getName()).isEqualTo("debian");
    }

    @Test
    void restoresARetiredVmWhoseVmIdIsOnTheBoardAgain() {
        VmRecord retired = record(EXISTING_ID, "zephyr-a");
        retired.retire();
        when(repository.findAll()).thenReturn(List.of(retired));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(inventory(EXISTING_ID, "zephyr-a")));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.restored()).isEqualTo(1);
        assertThat(response.conflicts()).isZero();
        assertThat(response.results()).singleElement().isEqualTo(
                new Item(EXISTING_ID, "zephyr-a", "restored", null));
        assertThat(retired.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        assertThat(retired.getName()).isEqualTo("zephyr-a");
        verify(repository).save(retired);
        verify(repository, never()).flush();
    }

    @Test
    void skipsAVmInASpringOperation() {
        VmRecord busy = record(DB_ONLY_ID, "busy");
        when(repository.findAll()).thenReturn(List.of(busy));
        when(inventoryPort.listRegistrations()).thenReturn(List.of());
        when(stateTracker.inOperation(DB_ONLY_ID)).thenReturn(true);

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.retired()).isZero();
        assertThat(response.skipped()).isEqualTo(1);
        assertThat(response.results()).singleElement().isEqualTo(
                new Item(DB_ONLY_ID, "busy", "skipped", "in_operation"));
        assertThat(busy.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
        assertThat(busy.getName()).isEqualTo("busy");
        verify(repository, never()).save(any(VmRecord.class));
    }

    @Test
    void aVmOnTheBoardWithIncompleteMetadataIsNotRetired() {
        VmRecord existing = record(EXISTING_ID, "existing");
        when(repository.findAll()).thenReturn(List.of(existing));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(
                new VmRegistrationInventory(EXISTING_ID, null, null, null, null, 1, 512, null)));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.retired()).isZero();
        assertThat(response.incomplete()).isEqualTo(1);
        assertThat(response.results()).singleElement().isEqualTo(
                new Item(EXISTING_ID, null, "incomplete", "registration_metadata_missing"));
        assertThat(existing.getLifecycle()).isEqualTo(VmLifecycle.REGISTERED);
    }

    @Test
    void reportsANameTakenByAnotherRegisteredVm() {
        VmRecord existing = record(EXISTING_ID, "debian");
        when(repository.findAll()).thenReturn(List.of(existing));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(
                inventory(EXISTING_ID, "debian"), inventory(BOARD_ONLY_ID, "debian")));

        VmSynchronizationResponse response = service.synchronize();

        // The existing row only picks up the board's IP; the second VM under its name is refused.
        assertThat(response.updated()).isEqualTo(1);
        assertThat(response.conflicts()).isEqualTo(1);
        assertThat(response.results()).containsExactly(
                new Item(EXISTING_ID, "debian", "updated", null),
                new Item(BOARD_ONLY_ID, "debian", "conflict", "duplicate_name"));
        verify(repository, times(1)).save(any(VmRecord.class));
        verify(repository).save(existing);
    }

    @Test
    void aFailedBoardReadChangesNothing() {
        when(inventoryPort.listRegistrations()).thenThrow(new IllegalStateException("board unreachable"));

        assertThatThrownBy(() -> service.synchronize()).isInstanceOf(IllegalStateException.class);

        verify(repository, never()).findAll();
        verify(repository, never()).save(any(VmRecord.class));
        verify(runtimeCache, never()).remove(anyString());
    }

    @Test
    void aFailedWriteLeavesInMemoryStateAlone() {
        VmRecord removedOnBoard = record(DB_ONLY_ID, "web-test");
        when(repository.findAll()).thenReturn(List.of(removedOnBoard));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(inventory(BOARD_ONLY_ID, "board-only")));
        when(repository.save(any(VmRecord.class)))
                .thenAnswer(invocation -> invocation.getArgument(0))
                .thenThrow(new IllegalStateException("insert failed"));

        assertThatThrownBy(() -> service.synchronize()).isInstanceOf(IllegalStateException.class);

        verify(runtimeCache, never()).remove(anyString());
        verify(stateTracker, never()).forget(anyString());
        verify(inventoryCleaner, never()).vmDeleted(anyString());
    }

    @Test
    void registersBoardOnlyVmAndUpdatesChangedVm() {
        VmRecord existing = record(EXISTING_ID, "existing");
        when(repository.findAll()).thenReturn(List.of(existing));
        when(inventoryPort.listRegistrations()).thenReturn(List.of(
                inventory(BOARD_ONLY_ID, "board-only"),
                inventory(EXISTING_ID, "renamed-on-board")));
        when(repository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.discovered()).isEqualTo(2);
        assertThat(response.registered()).isEqualTo(1);
        assertThat(response.updated()).isEqualTo(1);
        assertThat(response.unchanged()).isZero();
        assertThat(existing.getName()).isEqualTo("renamed-on-board");
        assertThat(response.results()).containsExactly(
                new Item(BOARD_ONLY_ID, "board-only", "registered", null),
                new Item(EXISTING_ID, "renamed-on-board", "updated", null));

        ArgumentCaptor<VmRecord> saved = ArgumentCaptor.forClass(VmRecord.class);
        verify(repository, times(2)).save(saved.capture());
        assertThat(saved.getAllValues()).extracting(VmRecord::getVmId)
                .containsExactly(BOARD_ONLY_ID, EXISTING_ID);
        assertThat(saved.getAllValues().getFirst().getAssignedIpv4()).isEqualTo("192.168.77.30");
        verify(eventRecorder, never()).vmDeleted(anyString(), anyString(), anyString());
    }

    @Test
    void reportsAnInvalidVmIdAsIncomplete() {
        when(repository.findAll()).thenReturn(List.of());
        when(inventoryPort.listRegistrations()).thenReturn(List.of(inventory("not-a-uuid", "odd")));

        VmSynchronizationResponse response = service.synchronize();

        assertThat(response.registered()).isZero();
        assertThat(response.results()).singleElement().isEqualTo(
                new Item("not-a-uuid", "odd", "incomplete", "invalid_vm_id"));
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
