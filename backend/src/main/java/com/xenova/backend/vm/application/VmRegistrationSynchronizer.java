package com.xenova.backend.vm.application;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.SmartLifecycle;
import org.springframework.stereotype.Component;

// Runs the board registration synchronization on its own thread: a registration
// read crosses Tailscale and must not hold up the shared @Scheduled thread.
@Component
@EnableConfigurationProperties(VmRegistrationSyncProperties.class)
public class VmRegistrationSynchronizer implements SmartLifecycle {

    private static final Logger log = LoggerFactory.getLogger(VmRegistrationSynchronizer.class);

    private final VmSynchronizationService service;
    private final VmRegistrationSyncProperties properties;
    private volatile Thread thread;
    private volatile boolean running;

    public VmRegistrationSynchronizer(VmSynchronizationService service, VmRegistrationSyncProperties properties) {
        this.service = service;
        this.properties = properties;
    }

    @Override
    public synchronized void start() {
        if (running || !properties.enabled()) {
            return;
        }
        running = true;
        thread = new Thread(this::loop, "vm-registration-sync");
        thread.setDaemon(true);
        thread.start();
    }

    @Override
    public synchronized void stop() {
        running = false;
        Thread current = thread;
        thread = null;
        if (current != null) {
            current.interrupt();
        }
    }

    @Override
    public boolean isRunning() {
        return running;
    }

    private void loop() {
        while (running) {
            try {
                service.synchronize();
            } catch (RuntimeException exception) {
                // An unreachable board changes nothing; the next round tries again.
                log.warn("VM registration synchronization failed: {}", exception.getMessage());
            }
            try {
                Thread.sleep(properties.interval().toMillis());
            } catch (InterruptedException exception) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }
}
