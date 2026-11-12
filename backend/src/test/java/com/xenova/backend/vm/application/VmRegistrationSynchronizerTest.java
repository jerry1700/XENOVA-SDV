package com.xenova.backend.vm.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.Duration;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

class VmRegistrationSynchronizerTest {

    private final VmSynchronizationService service = mock(VmSynchronizationService.class);
    private VmRegistrationSynchronizer synchronizer;

    @AfterEach
    void tearDown() {
        if (synchronizer != null) {
            synchronizer.stop();
        }
    }

    @Test
    void synchronizesRepeatedlyAndKeepsGoingAfterAFailure() {
        when(service.synchronize())
                .thenThrow(new IllegalStateException("board unreachable"))
                .thenReturn(null);
        synchronizer = new VmRegistrationSynchronizer(service, properties(true));

        synchronizer.start();

        assertThat(synchronizer.isRunning()).isTrue();
        verify(service, timeout(2000).atLeast(3)).synchronize();
    }

    @Test
    void disabledNeverStarts() {
        synchronizer = new VmRegistrationSynchronizer(service, properties(false));

        synchronizer.start();

        assertThat(synchronizer.isRunning()).isFalse();
        verifyNoInteractions(service);
    }

    private static VmRegistrationSyncProperties properties(boolean enabled) {
        return new VmRegistrationSyncProperties(enabled, Duration.ofMillis(50), 2, Duration.ofMinutes(20));
    }
}
