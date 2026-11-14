package com.xenova.backend.vm.application;

import com.xenova.backend.agent.application.AgentInventoryCleaner;
import com.xenova.backend.event.application.EventRecorder;
import com.xenova.backend.event.application.VmStateTracker;
import com.xenova.backend.vm.api.VmSynchronizationResponse;
import com.xenova.backend.vm.api.VmSynchronizationResponse.Item;
import com.xenova.backend.vm.domain.VmRecord;
import com.xenova.backend.vm.domain.VmRecordRepository;
import com.xenova.backend.vm.domain.VmRegistrationInventory;
import com.xenova.backend.vm.domain.VmRegistrationInventoryPort;
import java.time.Clock;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.locks.ReentrantLock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionOperations;

// Board registry -> VM table. Runs only on POST /api/v1/vms/synchronizations (no periodic loop): one request leaves
// the table matching the board. The board is the source of truth and VMs are matched by vm_id only.
// - on the board, not in the table: registered
// - on the board, retired in the table (same vm_id): restored
// - in the table, not on the board: retired at once, unless a Spring create/start/stop/delete is in flight
@Service
public class VmSynchronizationService {

    static final String SOURCE_PLATFORM = "platform";

    private static final Logger log = LoggerFactory.getLogger(VmSynchronizationService.class);

    private final VmRegistrationInventoryPort inventoryPort;
    private final VmRecordRepository repository;
    private final VmStateTracker stateTracker;
    private final VmRuntimeCache runtimeCache;
    private final EventRecorder eventRecorder;
    private final AgentInventoryCleaner inventoryCleaner;
    private final TransactionOperations transaction;
    private final Clock clock;
    private final ReentrantLock lock = new ReentrantLock();

    public VmSynchronizationService(
            VmRegistrationInventoryPort inventoryPort,
            VmRecordRepository repository,
            VmStateTracker stateTracker,
            VmRuntimeCache runtimeCache,
            EventRecorder eventRecorder,
            AgentInventoryCleaner inventoryCleaner,
            TransactionOperations transaction,
            Clock clock) {
        this.inventoryPort = inventoryPort;
        this.repository = repository;
        this.stateTracker = stateTracker;
        this.runtimeCache = runtimeCache;
        this.eventRecorder = eventRecorder;
        this.inventoryCleaner = inventoryCleaner;
        this.transaction = transaction;
        this.clock = clock;
    }

    // One at a time, and the transaction commits before the lock is released, so the next request reads the
    // committed table. The board is read outside the transaction; a failed read changes nothing.
    public VmSynchronizationResponse synchronize() {
        lock.lock();
        try {
            List<VmRegistrationInventory> boardVms = inventoryPort.listRegistrations();
            Outcome outcome = transaction.execute(status -> reconcile(boardVms));
            // In-memory state follows only a committed retirement.
            outcome.retiredIds().forEach(this::forgetRetired);
            if (!outcome.retiredIds().isEmpty()) {
                log.info("Retired {} VM(s) no longer registered on the board", outcome.retiredIds().size());
            }
            return outcome.response();
        } finally {
            lock.unlock();
        }
    }

    private Outcome reconcile(List<VmRegistrationInventory> boardVms) {
        List<VmRecord> databaseVms = repository.findAll();
        Map<String, VmRecord> databaseById = new HashMap<>();
        Map<String, VmRecord> databaseByName = new HashMap<>();
        databaseVms.forEach(vm -> {
            databaseById.put(vm.getVmId(), vm);
            databaseByName.put(vm.getName(), vm);
        });
        // Every valid vm_id on the board counts as present, even one whose metadata is incomplete.
        Set<String> boardIds = new HashSet<>();
        boardVms.forEach(boardVm -> {
            String vmId = canonicalUuid(boardVm.vmId());
            if (vmId != null) {
                boardIds.add(vmId);
            }
        });

        List<Item> results = new ArrayList<>();
        List<String> retiredIds = new ArrayList<>();
        int skipped = 0;

        // Retire first: a VM re-created on the board under the same name has a new vm_id and needs the old row's
        // name, which retire() gives back.
        for (VmRecord databaseVm : databaseVms) {
            String vmId = databaseVm.getVmId();
            if (!databaseVm.isRegistered() || boardIds.contains(vmId)) {
                continue;
            }
            // A create registers the VM on the board only at its end; start/stop/delete may be mid-way.
            if (stateTracker.inOperation(vmId)) {
                skipped++;
                results.add(new Item(vmId, databaseVm.getName(), "skipped", "in_operation"));
                continue;
            }
            String name = databaseVm.getName();
            databaseVm.retire();
            repository.save(databaseVm);
            databaseByName.remove(name);
            databaseByName.put(databaseVm.getName(), databaseVm);
            eventRecorder.vmDeleted(vmId, name, SOURCE_PLATFORM);
            retiredIds.add(vmId);
            results.add(new Item(vmId, name, "retired", "missing_from_board"));
        }
        // Hibernate flushes inserts before updates; the freed names must be in the table before new rows take them.
        if (!retiredIds.isEmpty()) {
            repository.flush();
        }

        Set<String> seenIds = new HashSet<>();
        int registered = 0;
        int updated = 0;
        int unchanged = 0;
        int restored = 0;
        int conflicts = 0;
        int incomplete = 0;

        for (VmRegistrationInventory boardVm : boardVms) {
            String vmId = canonicalUuid(boardVm.vmId());
            if (vmId == null) {
                incomplete++;
                results.add(new Item(boardVm.vmId(), boardVm.name(), "incomplete", "invalid_vm_id"));
                continue;
            }
            if (!seenIds.add(vmId)) {
                conflicts++;
                results.add(new Item(vmId, boardVm.name(), "conflict", "duplicate_board_vm_id"));
                continue;
            }
            if (!hasRegistrationMetadata(boardVm)) {
                incomplete++;
                results.add(new Item(vmId, boardVm.name(), "incomplete", "registration_metadata_missing"));
                continue;
            }
            VmRecord existing = databaseById.get(vmId);
            VmRecord sameName = databaseByName.get(boardVm.name());
            if (sameName != null && sameName != existing) {
                conflicts++;
                results.add(new Item(vmId, boardVm.name(), "conflict", "duplicate_name"));
                continue;
            }

            if (existing == null) {
                VmRecord created = VmRecord.registeredByPlatform(
                        vmId, boardVm.name(), boardVm.templateId(), boardVm.templateVersion(),
                        boardVm.vcpus(), boardVm.memoryMib(), boardVm.guestKind(),
                        clock.instant(), boardVm.assignedIpv4());
                repository.save(created);
                databaseById.put(vmId, created);
                databaseByName.put(created.getName(), created);
                registered++;
                results.add(new Item(vmId, created.getName(), "registered", null));
                continue;
            }

            boolean wasRetired = !existing.isRegistered();
            String previousName = existing.getName();
            if (wasRetired) {
                existing.restore();
            }
            boolean changed = existing.synchronizePlatformRegistration(
                    boardVm.name(), boardVm.templateId(), boardVm.templateVersion(),
                    boardVm.vcpus(), boardVm.memoryMib(), boardVm.guestKind(), boardVm.assignedIpv4());
            if (wasRetired || changed) {
                repository.save(existing);
                databaseByName.remove(previousName);
                databaseByName.put(existing.getName(), existing);
            }
            if (wasRetired) {
                restored++;
                results.add(new Item(vmId, existing.getName(), "restored", null));
            } else if (changed) {
                updated++;
                results.add(new Item(vmId, existing.getName(), "updated", null));
            } else {
                unchanged++;
                results.add(new Item(vmId, existing.getName(), "unchanged", null));
            }
        }

        VmSynchronizationResponse response = new VmSynchronizationResponse(
                boardVms.size(), registered, updated, unchanged, restored, retiredIds.size(), skipped,
                conflicts, incomplete, List.copyOf(results));
        return new Outcome(response, List.copyOf(retiredIds));
    }

    // Same end state as VmService.destroy, without asking dom0-manager: the board already has no registration.
    private void forgetRetired(String vmId) {
        runtimeCache.remove(vmId);
        stateTracker.forget(vmId);
        inventoryCleaner.vmDeleted(vmId);
    }

    private record Outcome(VmSynchronizationResponse response, List<String> retiredIds) {
    }

    private static boolean hasRegistrationMetadata(VmRegistrationInventory vm) {
        return validText(vm.name(), 63)
                && vm.guestKind() != null
                && validText(vm.templateId(), 64)
                && (vm.templateVersion() == null || validText(vm.templateVersion(), 64))
                && vm.vcpus() >= 1 && vm.vcpus() <= 4
                && vm.memoryMib() >= 16 && vm.memoryMib() <= 8192
                && validIpv4(vm.assignedIpv4());
    }

    private static boolean validText(String value, int maxLength) {
        return value != null && !value.isBlank() && value.equals(value.strip()) && value.length() <= maxLength
                && value.chars().noneMatch(Character::isISOControl);
    }

    private static boolean validIpv4(String value) {
        if (value == null) {
            return true;
        }
        String[] parts = value.split("\\.", -1);
        if (parts.length != 4) {
            return false;
        }
        for (String part : parts) {
            if (part.isEmpty() || part.length() > 3 || !part.chars().allMatch(Character::isDigit)
                    || (part.length() > 1 && part.startsWith("0")) || Integer.parseInt(part) > 255) {
                return false;
            }
        }
        return true;
    }

    private static String canonicalUuid(String value) {
        try {
            return UUID.fromString(value).toString();
        } catch (RuntimeException exception) {
            return null;
        }
    }
}
