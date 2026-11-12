package com.xenova.backend.event.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.xenova.backend.vm.application.VmService;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.scheduling.annotation.Scheduled;

class VmStateWatcherTest {

    @Test
    void checkIsNotScheduledAutomatically() throws NoSuchMethodException {
        assertThat(VmStateWatcher.class.getMethod("check").getAnnotation(Scheduled.class)).isNull();
    }

    @Test
    void capturesTheOperationMarkBeforeReadingVmState() {
        VmService vmService = mock(VmService.class);
        VmStateTracker tracker = mock(VmStateTracker.class);
        when(vmService.list()).thenReturn(List.of());
        VmStateWatcher watcher = new VmStateWatcher(vmService, tracker);

        watcher.check();

        InOrder order = inOrder(tracker, vmService);
        order.verify(tracker).mark();
        order.verify(vmService).list();
    }
}
