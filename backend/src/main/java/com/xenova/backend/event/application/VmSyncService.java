package com.xenova.backend.event.application;

import com.xenova.backend.vm.application.VmService;
import com.xenova.backend.vm.domain.Vm;
import java.util.List;
import org.springframework.stereotype.Service;

// Reads the VM list on demand and records state changes that no user operation explains as
// vm.state.changed (cause detected).
@Service
public class VmSyncService {

    private final VmService vmService;
    private final VmStateTracker tracker;

    public VmSyncService(VmService vmService, VmStateTracker tracker) {
        this.vmService = vmService;
        this.tracker = tracker;
    }

    public synchronized List<Vm> sync() {
        long mark = tracker.mark();
        List<Vm> vms = vmService.listLive();
        for (Vm vm : vms) {
            if (!tracker.touchedSince(vm.vmId(), mark)) {
                tracker.observed(vm.vmId(), vm.state());
            }
        }
        return vms;
    }
}
