package com.xenova.backend.event.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.xenova.backend.common.error.ApiException;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.scheduling.annotation.Scheduled;

class VmStateWatcherTest {

    @Test
    void checksVmStateEveryFiveSecondsByDefault() throws NoSuchMethodException {
        Scheduled scheduled = VmStateWatcher.class.getMethod("check").getAnnotation(Scheduled.class);
        assertThat(scheduled).isNotNull();
        assertThat(scheduled.fixedDelayString()).isEqualTo("${xenova.event.vm-state-check-interval-ms:5000}");
    }

    @Test
    void readsStateThroughTheSameSyncAsTheVmList() {
        VmSyncService sync = mock(VmSyncService.class);
        new VmStateWatcher(sync).check();
        verify(sync).sync();
    }

    @Test
    void unreachableDom0ManagerSkipsTheRound() {
        VmSyncService sync = mock(VmSyncService.class);
        when(sync.sync()).thenThrow(new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "VM_CONTROL_UNAVAILABLE", "down"));
        assertThatCode(() -> new VmStateWatcher(sync).check()).doesNotThrowAnyException();
    }
}
