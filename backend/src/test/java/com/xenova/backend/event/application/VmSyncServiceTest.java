package com.xenova.backend.event.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;

import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.application.VmService;
import com.xenova.backend.vm.domain.Vm;
import com.xenova.backend.vm.domain.VmState;
import java.lang.reflect.Modifier;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.scheduling.annotation.Scheduled;

class VmSyncServiceTest {

    private static final Vm VM = new Vm(
            "vm-1", "drive-1", GuestKind.LINUX, VmState.RUNNING, "debian", "1.0.0", 1, 256);

    @Test
    void checkIsNotScheduledAutomatically() throws NoSuchMethodException {
        assertThat(VmSyncService.class.getMethod("sync").getAnnotation(Scheduled.class)).isNull();
    }

    @Test
    void syncRequestsAreSerialized() throws NoSuchMethodException {
        assertThat(Modifier.isSynchronized(VmSyncService.class.getMethod("sync").getModifiers())).isTrue();
    }

    @Test
    void capturesTheOperationMarkBeforeReadingVmState() {
        VmService vmService = mock(VmService.class);
        VmStateTracker tracker = mock(VmStateTracker.class);
        when(vmService.listLive()).thenReturn(List.of());
        VmSyncService syncService = new VmSyncService(vmService, tracker);

        syncService.sync();

        InOrder order = inOrder(tracker, vmService);
        order.verify(tracker).mark();
        order.verify(vmService).listLive();
        verifyNoMoreInteractions(vmService);
    }

    @Test
    void skipsAStateReadThatOverlapsAUserOperation() {
        VmService vmService = mock(VmService.class);
        VmStateTracker tracker = mock(VmStateTracker.class);
        when(tracker.mark()).thenReturn(7L);
        when(vmService.listLive()).thenReturn(List.of(VM));
        when(tracker.touchedSince("vm-1", 7L)).thenReturn(true);

        new VmSyncService(vmService, tracker).sync();

        verify(tracker, never()).observed("vm-1", VmState.RUNNING);
    }

    @Test
    void appliesAStateReadWhenNoUserOperationOverlapsIt() {
        VmService vmService = mock(VmService.class);
        VmStateTracker tracker = mock(VmStateTracker.class);
        when(tracker.mark()).thenReturn(7L);
        when(vmService.listLive()).thenReturn(List.of(VM));

        List<Vm> result = new VmSyncService(vmService, tracker).sync();

        verify(tracker).observed("vm-1", VmState.RUNNING);
        assertThat(result).containsExactly(VM);
    }
}
