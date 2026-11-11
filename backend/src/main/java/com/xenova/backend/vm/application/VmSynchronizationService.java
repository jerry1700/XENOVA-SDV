package com.xenova.backend.vm.application;

import com.xenova.backend.agent.application.AgentInventoryCleaner;
import com.xenova.backend.event.application.EventRecorder;
import com.xenova.backend.event.application.VmStateTracker;
import com.xenova.backend.vm.api.VmSynchronizationResponse;
import com.xenova.backend.vm.api.VmSynchronizationResponse.Item;
import com.xenova.backend.vm.domain.VmLifecycle;
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
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

// Board registry -> VM table. Runs periodically (VmRegistrationSynchronizer) and on POST /api/v1/vms/synchronizations,
// so VMs created or retired with a platform tool follow on the web without a Spring request.
@Service
@EnableConfigurationProperties(VmRegistrationSyncProperties.class)
public class VmSynchronizationService {

    static final String SOURCE_PLATFORM = "platform";

    private static final Logger log = LoggerFactory.getLogger(VmSynchronizationService.class);

    private final VmRegistrationInventoryPort inventoryPort;
    private final VmRecordRepository repository;
    private final VmStateTracker stateTracker;
    private final VmRuntimeCache runtimeCache;
    private final EventRecorder eventRecorder;
    private final AgentInventoryCleaner inventoryCleaner;
    private final VmRegistrationSyncProperties properties;
    private final Clock clock;
    // Consecutive synchronizations each REGISTERED row was absent from the board. A failed board read throws
    // before this is touched, so only successful reads count.
    private final Map<String, Integer> missingCounts = new ConcurrentHashMap<>();
    private final ReentrantLock lock = new ReentrantLock();

    public VmSynchronizationService(
            VmRegistrationInventoryPort inventoryPort,
            VmRecordRepository repository,
            VmStateTracker stateTracker,
            VmRuntimeCache runtimeCache,
            EventRecorder eventRecorder,
            AgentInventoryCleaner inventoryCleaner,
            VmRegistrationSyncProperties properties,
            Clock clock) {
        this.inventoryPort = inventoryPort;
        this.repository = repository;
        this.stateTracker = stateTracker;
        this.runtimeCache = runtimeCache;
        this.eventRecorder = eventRecorder;
        this.inventoryCleaner = inventoryCleaner;
        this.properties = properties;
        this.clock = clock;
    }

    // One at a time: the periodic loop and the admin endpoint must not count the same absence twice.
    @Transactional
    public VmSynchronizationResponse synchronize() {
        lock.lock();
        try {
            return synchronizeLocked();
        } finally {
            lock.unlock();
        }
    }

    private VmSynchronizationResponse synchronizeLocked() {
        List<VmRegistrationInventory> boardVms = inventoryPort.listRegistrations();
        List<VmRecord> databaseVms = repository.findAll();
        Map<String, VmRecord> databaseById = new HashMap<>();
        Map<String, VmRecord> databaseByName = new HashMap<>();
        databaseVms.forEach(vm -> {
            databaseById.put(vm.getVmId(), vm);
            databaseByName.put(vm.getName(), vm);
        });

        List<Item> results = new ArrayList<>();
        Set<String> boardIds = new HashSet<>();
        int registered = 0;
        int updated = 0;
        int unchanged = 0;
        int conflicts = 0;
        int incomplete = 0;

        for (VmRegistrationInventory boardVm : boardVms) {
            String vmId = canonicalUuid(boardVm.vmId());
            if (vmId == null) {
                incomplete++;
                results.add(new Item(boardVm.vmId(), "incomplete", "invalid_vm_id"));
                continue;
            }
            if (!boardIds.add(vmId)) {
                conflicts++;
                results.add(new Item(vmId, "conflict", "duplicate_board_vm_id"));
                continue;
            }

            if (!hasRegistrationMetadata(boardVm)) {
                incomplete++;
                results.add(new Item(vmId, "incomplete", "registration_metadata_missing"));
                continue;
            }
            VmRecord existing = databaseById.get(vmId);
            if (existing != null && !existing.isRegistered()) {
                conflicts++;
                results.add(new Item(vmId, "conflict", "retired_vm"));
                continue;
            }
            VmRecord sameName = databaseByName.get(boardVm.name());
            if (sameName != null && !sameName.getVmId().equals(vmId)) {
                conflicts++;
                results.add(new Item(vmId, "conflict", "duplicate_name"));
                continue;
            }
            if (existing != null) {
                String previousName = existing.getName();
                boolean changed = existing.synchronizePlatformRegistration(
                        boardVm.name(), boardVm.templateId(), boardVm.templateVersion(),
                        boardVm.vcpus(), boardVm.memoryMib(), boardVm.guestKind(), boardVm.assignedIpv4());
                if (changed) {
                    repository.save(existing);
                    databaseByName.remove(previousName);
                    databaseByName.put(existing.getName(), existing);
                    updated++;
                    results.add(new Item(vmId, "updated", null));
                } else {
                    unchanged++;
                    results.add(new Item(vmId, "unchanged", null));
                }
                continue;
            }

            VmRecord created = VmRecord.registeredByPlatform(
                    vmId, boardVm.name(), boardVm.templateId(), boardVm.templateVersion(),
                    boardVm.vcpus(), boardVm.memoryMib(), boardVm.guestKind(),
                    clock.instant(), boardVm.assignedIpv4());
            repository.save(created);
            databaseById.put(vmId, created);
            databaseByName.put(created.getName(), created);
            registered++;
            results.add(new Item(vmId, "registered", null));
        }

        int missingFromBoard = 0;
        int retired = 0;
        Set<String> stillMissing = new HashSet<>();
        for (VmRecord databaseVm : databaseVms) {
            if (databaseVm.getLifecycle() != VmLifecycle.REGISTERED || boardIds.contains(databaseVm.getVmId())) {
                continue;
            }
            String vmId = databaseVm.getVmId();
            if (mayRetire(databaseVm)) {
                int count = missingCounts.getOrDefault(vmId, 0) + 1;
                if (count >= properties.missingThreshold()) {
                    retire(databaseVm);
                    retired++;
                    results.add(new Item(vmId, "retired", "missing_from_board"));
                    continue;
                }
                stillMissing.add(vmId);
                missingCounts.put(vmId, count);
            }
            missingFromBoard++;
            results.add(new Item(vmId, "missing_from_board", null));
        }
        // Back on the board, retired, busy or still in its creation grace: counting starts from zero again.
        missingCounts.keySet().retainAll(stillMissing);
        if (retired > 0) {
            log.info("Retired {} VM(s) no longer registered on the board", retired);
        }

        return new VmSynchronizationResponse(
                boardVms.size(), registered, updated, unchanged, conflicts, incomplete, missingFromBoard, retired,
                List.copyOf(results));
    }

    // A create registers the VM on the board only at its end, and a Spring start/stop/delete may be in flight,
    // so neither is read as "removed on the board".
    private boolean mayRetire(VmRecord record) {
        if (stateTracker.inOperation(record.getVmId())) {
            return false;
        }
        return record.getCreatedAt() == null
                || !record.getCreatedAt().plus(properties.creationGrace()).isAfter(clock.instant());
    }

    // Same end state as VmService.destroy, without asking dom0-manager: the board already has no registration.
    private void retire(VmRecord record) {
        String vmId = record.getVmId();
        record.retire();
        repository.save(record);
        runtimeCache.remove(vmId);
        stateTracker.forget(vmId);
        missingCounts.remove(vmId);
        eventRecorder.vmDeleted(vmId, record.getName(), SOURCE_PLATFORM);
        inventoryCleaner.vmDeleted(vmId);
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
