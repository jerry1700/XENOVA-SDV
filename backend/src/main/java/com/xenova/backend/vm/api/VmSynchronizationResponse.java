package com.xenova.backend.vm.api;

import java.util.List;

// Result of one board registry -> VM table synchronization. One item per board VM and per row retired or skipped.
public record VmSynchronizationResponse(
        // VMs the board registry returned (valid or not).
        int discovered,
        int registered,
        int updated,
        int unchanged,
        // Retired rows whose vm_id is on the board again.
        int restored,
        // Rows retired because the board no longer registers them.
        int retired,
        // Rows absent from the board but left alone because a create/start/stop/delete is in flight.
        int skipped,
        int conflicts,
        int incomplete,
        List<Item> results) {

    // name: the board's name for board VMs; for a retired row, the name it had before it was retired.
    public record Item(String vmId, String name, String action, String reason) {
    }
}
