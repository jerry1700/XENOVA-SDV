package com.xenova.backend.event.application;

import com.xenova.backend.common.error.ApiException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

// Xen does not tell anyone when a VM crashes or shuts itself down, so the VM state is read every 5 s
// (same work as GET /api/v1/vms) and changes no user operation explains are recorded as vm.state.changed.
// This reads run state only; adding or retiring VMs (board registry -> VM table) is POST /api/v1/vms/synchronizations.
@Component
public class VmStateWatcher {

    private static final Logger log = LoggerFactory.getLogger(VmStateWatcher.class);

    private final VmSyncService vmSyncService;

    public VmStateWatcher(VmSyncService vmSyncService) {
        this.vmSyncService = vmSyncService;
    }

    @Scheduled(
            initialDelayString = "${xenova.event.vm-state-check-interval-ms:5000}",
            fixedDelayString = "${xenova.event.vm-state-check-interval-ms:5000}")
    public void check() {
        try {
            vmSyncService.sync();
        } catch (ApiException exception) {
            // Without dom0-manager the real state is unknown; reporting every VM as stopped would be wrong.
            log.debug("VM state check skipped: {}", exception.getMessage());
        }
    }
}
