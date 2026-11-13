package com.xenova.backend.vm.application;

import com.xenova.backend.agent.application.AgentInventoryCleaner;
import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.event.application.EventRecorder;
import com.xenova.backend.event.application.VmStateTracker;
import com.xenova.backend.template.application.TemplateCatalog;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.template.domain.VmTemplate;
import com.xenova.backend.vm.domain.CreateVmCommand;
import com.xenova.backend.vm.domain.CpuLayout;
import com.xenova.backend.vm.domain.CpuPolicyUpdate;
import com.xenova.backend.vm.domain.Vm;
import com.xenova.backend.vm.domain.VmControlPort;
import com.xenova.backend.vm.domain.VmControlOperation;
import com.xenova.backend.vm.domain.VmLifecycle;
import com.xenova.backend.vm.domain.VmRecord;
import com.xenova.backend.vm.domain.VmRecordRepository;
import com.xenova.backend.vm.domain.VmResourceUpdate;
import com.xenova.backend.vm.domain.VmResourceView;
import com.xenova.backend.vm.domain.VmResources;
import com.xenova.backend.vm.domain.VmRole;
import com.xenova.backend.vm.domain.VmRuntime;
import com.xenova.backend.vm.domain.VmState;
import java.time.Clock;
import java.time.Instant;
import java.util.Comparator;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

// Spring owns the public VM specification and issues the permanent vm_id. The platform owns the registry,
// disk lifecycle, and Xen runtime. A registered VM without a Xen domain is reported as stopped.
@Service
public class VmService {

    // dom0-manager rejected the request itself, so no domain or disk was created for this vm_id.
    // NOT_SUPPORTED: until the platform accepts a caller-chosen vm_id, every new create is refused this way.
    private static final Set<String> REJECTED_BY_DOM0 =
            Set.of("INVALID_ARGUMENT", "INSUFFICIENT_RESOURCES", "VM_CONTROL_NOT_SUPPORTED", "VM_NAME_CONFLICT");
    private static final String DOMAIN_EXISTS = "VM_ALREADY_EXISTS";
    private static final String INVALID_VM_STATE = "INVALID_VM_STATE";

    private final VmControlPort vmControlPort;
    private final VmRecordRepository vmRecordRepository;
    private final TemplateCatalog templateCatalog;
    private final EventRecorder eventRecorder;
    private final VmStateTracker stateTracker;
    private final VmRuntimeCache runtimeCache;
    private final AgentInventoryCleaner inventoryCleaner;
    private final Clock clock;
    // VMs with a state-changing request in flight in this instance (exclusively)
    private final Set<String> inFlight = ConcurrentHashMap.newKeySet();

    public VmService(
            VmControlPort vmControlPort,
            VmRecordRepository vmRecordRepository,
            TemplateCatalog templateCatalog,
            EventRecorder eventRecorder,
            VmStateTracker stateTracker,
            VmRuntimeCache runtimeCache,
            AgentInventoryCleaner inventoryCleaner,
            Clock clock) {
        this.vmControlPort = vmControlPort;
        this.vmRecordRepository = vmRecordRepository;
        this.templateCatalog = templateCatalog;
        this.eventRecorder = eventRecorder;
        this.stateTracker = stateTracker;
        this.runtimeCache = runtimeCache;
        this.inventoryCleaner = inventoryCleaner;
        this.clock = clock;
    }

    public record VmList(List<Vm> vms, Instant observedAt) {
    }

    // Domains unknown to Spring (e.g. started by a platform tool) are left out.
    public List<Vm> list() {
        return listObserved().vms();
    }

    // observedAt: when dom0-manager reported the runtimes, which can be a little before the response.
    public VmList listObserved() {
        return combine(runtimeCache.read());
    }

    // Always asks dom0-manager; the on-demand VM synchronization uses it.
    public List<Vm> listLive() {
        return combine(runtimeCache.refresh()).vms();
    }

    public Vm get(String vmId) {
        VmRecord record = findRecord(vmId);
        return Vm.of(record, runtimeCache.find(vmId).orElse(null));
    }

    private VmList combine(VmRuntimeCache.Snapshot snapshot) {
        Map<String, VmRuntime> runtimes = snapshot.runtimes();
        List<Vm> vms = vmRecordRepository.findAllByLifecycleOrLifecycleIsNullOrderByCreatedAtAsc(VmLifecycle.REGISTERED)
                .stream()
                .sorted(LIST_ORDER)
                .map(record -> Vm.of(record, runtimes.get(record.getVmId())))
                .toList();
        return new VmList(vms, snapshot.observedAt());
    }

    // Screens show VMs in this order whatever filled the table: primary, backup, interaction, then VMs without a role,
    // drive (Zephyr) guests before Linux among those. The stable sort keeps creation order within each group, so a VM
    // the board registry sync discovered (created_at = discovery time) does not jump ahead of the drive VMs.
    private static final Comparator<VmRecord> LIST_ORDER = Comparator
            .comparingInt((VmRecord record) -> record.getRole() == null ? VmRole.values().length : record.getRole().ordinal())
            .thenComparingInt(record -> record.getGuestKind() == GuestKind.ZEPHYR ? 0 : 1);

    // The role is a console label only: no dom0-manager call, no event, and the VM keeps running as it is.
    public Vm updateRole(String vmId, VmRole role) {
        VmRecord record = findRecord(vmId);
        record.changeRole(role);
        vmRecordRepository.save(record);
        // Same source as get(): a live dom0-manager call here could fail after the role was already saved.
        return Vm.of(record, runtimeCache.find(vmId).orElse(null));
    }

    // A retried request with the same name and spec returns the existing VM (200) instead of a duplicate.
    public CreateResult create(CreateVmCommand command) {
        VmTemplate template = templateCatalog.find(command.templateId(), command.templateVersion())
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "TEMPLATE_NOT_FOUND", "존재하지 않는 템플릿입니다."));
        if (command.memoryMib() < template.minMemoryMib()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "VALIDATION_ERROR",
                    "memory_mib는 이 템플릿의 최소값(" + template.minMemoryMib() + ") 이상이어야 합니다.");
        }

        VmRecord existing = vmRecordRepository.findByName(command.name()).orElse(null);
        if (existing != null) {
            if (!existing.isRegistered()) {
                throw new ApiException(HttpStatus.CONFLICT, "VM_NAME_CONFLICT", "보관 중인 VM이 사용하는 이름입니다.");
            }
            if (!existing.hasSameSpec(command)) {
                throw new ApiException(HttpStatus.CONFLICT, "VM_NAME_CONFLICT", "이미 사용 중인 VM 이름입니다.");
            }
            return new CreateResult(underOperation(existing.getVmId(), () -> ensureRunning(existing)), false);
        }

        VmRecord record;
        try {
            record = vmRecordRepository.save(
                    new VmRecord(UUID.randomUUID().toString(), command, template.guestKind(), clock.instant()));
        } catch (DataIntegrityViolationException exception) {
            // Another request took the name between the check above and this insert (uk_vms_name).
            throw new ApiException(HttpStatus.CONFLICT, "VM_NAME_CONFLICT", "이미 사용 중인 VM 이름입니다.");
        }
        return new CreateResult(underOperation(record.getVmId(), () -> launchNew(record)), true);
    }

    private Vm launchNew(VmRecord record) {
        Vm vm;
        try {
            vm = launchDomain(record);
        } catch (ApiException exception) {
            // On a timeout the domain may still be coming up, so the record is kept and a retry can recover it.
            if (REJECTED_BY_DOM0.contains(exception.getCode())) {
                vmRecordRepository.delete(record);
            }
            throw exception;
        }
        eventRecorder.vmCreated(record);
        // vm.created already tells the story; the state is only remembered so a later sync does not repeat it.
        stateTracker.remember(record.getVmId(), vm.state());
        return vm;
    }

    // There is no start endpoint: the same vm_id is launched again and its preserved disk is reused.
    // A domain that is still in Xen but not running is refused, as the platform CLI does: there is no automatic
    // recovery, so the user stops it first and starts again.
    public Vm start(String vmId) {
        VmRecord record = findRecord(vmId);
        return underOperation(vmId, () -> {
            VmRuntime runtime = vmControlPort.find(vmId).orElse(null);
            if (runtime == null) {
                return launch(record);
            }
            rejectUnstartable(runtime.state());
            return Vm.of(record, runtime);
        });
    }

    // The state is read first so the event says what was stopped. Always forward stop even without a runtime:
    // dom0-manager persists desired_state=stopped, which prevents recovery from relaunching the VM.
    public Vm stop(String vmId) {
        VmRecord record = findRecord(vmId);
        return underOperation(vmId, () -> {
            VmState before = vmControlPort.find(vmId).map(VmRuntime::state).orElse(null);
            vmControlPort.stop(vmId);
            runtimeCache.remove(vmId);
            if (before != null) {
                stateTracker.changedByUser(vmId, before, VmState.STOPPED, VmStateTracker.CAUSE_USER_STOP);
            }
            return Vm.of(record, null);
        });
    }

    public VmControlOperation reboot(String vmId, String requestKey) {
        String validatedKey = validateRequestKey(requestKey);
        findRecord(vmId);
        try {
            return exclusively(vmId, () -> vmControlPort.requestReboot(vmId, validatedKey));
        } catch (ApiException exception) {
            // Reboot needs a running domain: a stopped VM is started instead, a crashed one is stopped first.
            if (INVALID_VM_STATE.equals(exception.getCode())) {
                throw new ApiException(HttpStatus.CONFLICT, INVALID_VM_STATE,
                        "실행 중인 VM만 재부팅할 수 있습니다. 꺼진 VM은 시작하고, 비정상 상태면 정지한 뒤 시작하세요.");
            }
            throw exception;
        }
    }

    public VmControlOperation controlOperation(String vmId, String requestKey) {
        String validatedKey = validateRequestKey(requestKey);
        findRecord(vmId);
        return vmControlPort.findControlOperation(vmId, validatedKey);
    }

    public VmResourceUpdate updateResources(String vmId, int vcpus, int memoryMib, String requestKey) {
        String validatedKey = validateRequestKey(requestKey);
        VmRecord record = findRecord(vmId);
        if (record.getGuestKind() != GuestKind.LINUX) {
            throw new ApiException(
                    HttpStatus.BAD_REQUEST,
                    "RESOURCE_UPDATE_UNSUPPORTED",
                    "자원 변경은 Linux VM만 지원합니다.");
        }
        VmTemplate template = templateCatalog.find(record.getTemplateId(), record.getTemplateVersion())
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "TEMPLATE_NOT_FOUND", "존재하지 않는 템플릿입니다."));
        if (memoryMib < template.minMemoryMib()) {
            throw new ApiException(
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "VALIDATION_ERROR",
                    "memory_mib는 이 템플릿의 최소값(" + template.minMemoryMib() + ") 이상이어야 합니다.");
        }

        return underOperation(vmId, () -> {
            VmResourceUpdate result = vmControlPort.updateResources(vmId, vcpus, memoryMib, validatedKey);
            validateResourceUpdateResponse(vmId, vcpus, memoryMib, result);
            record.changeResources(result.after().vcpus(), result.after().memoryMib());
            vmRecordRepository.save(record);
            runtimeCache.remove(vmId);
            return result;
        });
    }

    public VmResourceView resources(String vmId) {
        VmRecord record = findRecord(vmId);
        CpuLayout layout = requireConfiguredCpuLayout(vmControlPort.cpuLayout());
        CpuLayout.VmCpuLayout vmLayout = findVmLayout(layout, vmId);
        return resourceView(record, layout, requireUniformAffinity(vmLayout), exclusivePcpus(layout));
    }

    public VmResourceView updateVcpuPinning(
            String vmId, List<CpuLayout.VcpuAffinity> mappings, String requestKey) {
        String validatedKey = validateRequestKey(requestKey);
        VmRecord record = findRecord(vmId);
        List<Integer> requestedMask = validatePinningMappings(record, mappings);
        CpuLayout layout = requireConfiguredCpuLayout(vmControlPort.cpuLayout());
        CpuLayout.VmCpuLayout target = findVmLayout(layout, vmId);
        if (target.allocation() == null) {
            throw new ApiException(HttpStatus.CONFLICT, "CPU_POLICY_NOT_CONFIGURED", "VM의 CPU 배치 정책이 없습니다.");
        }
        if (layout.host() == null || layout.host().onlinePcpus() == null
                || !layout.host().onlinePcpus().containsAll(requestedMask)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "RESOURCE_UNSUPPORTED", "온라인 상태인 pCPU만 선택할 수 있습니다.");
        }
        List<Integer> shared = layout.host().desiredDom0Pcpus();
        if (shared == null || shared.isEmpty()) {
            throw new ApiException(HttpStatus.CONFLICT, "CPU_POLICY_NOT_CONFIGURED", "공유 CPU 정책이 설정되지 않았습니다.");
        }
        if ("shared".equals(target.allocation()) && !shared.containsAll(requestedMask)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "RESOURCE_UNSUPPORTED", "공유 VM은 공유 pCPU 집합 안에서만 배치할 수 있습니다.");
        }
        if ("exclusive".equals(target.allocation())) {
            Set<Integer> unavailable = new java.util.HashSet<>(shared);
            if (layout.reservations() != null) {
                layout.reservations().stream()
                        .filter(reservation -> !vmId.equals(reservation.vmId()))
                        .filter(reservation -> reservation.pcpus() != null)
                        .forEach(reservation -> unavailable.addAll(reservation.pcpus()));
            }
            List<Integer> conflicts = requestedMask.stream().filter(unavailable::contains).toList();
            if (!conflicts.isEmpty()) {
                throw new ApiException(
                        HttpStatus.CONFLICT,
                        "CPU_PLACEMENT_CONFLICT",
                        "선택한 pCPU " + conflicts + "는 dom0 공유 영역이거나 다른 VM이 전용으로 사용 중입니다.");
            }
        }

        List<CpuPolicyUpdate.CpuPlacement> placements = new ArrayList<>();
        for (CpuLayout.VmCpuLayout item : layout.vms()) {
            if (item.allocation() == null) {
                continue;
            }
            List<Integer> mask = item.vmId().equals(vmId) ? requestedMask : uniformMask(item);
            placements.add(new CpuPolicyUpdate.CpuPlacement(item.vmId(), item.allocation(), mask));
        }
        placements.sort(Comparator.comparing(CpuPolicyUpdate.CpuPlacement::vmId));
        CpuPolicyUpdate update = exclusively(vmId, () -> vmControlPort.updateCpuPolicy(
                new CpuPolicyUpdate.Request(layout.policyRevision(), sortedUnique(shared), placements), validatedKey));
        validateCpuPolicyResponse(update, vmId, target.allocation(), requestedMask, layout.policyRevision());

        List<CpuLayout.VcpuAffinity> applied = new ArrayList<>();
        for (int vcpu = 0; vcpu < record.getVcpus(); vcpu++) {
            applied.add(new CpuLayout.VcpuAffinity(vcpu, requestedMask));
        }
        List<Integer> exclusive = update.policy().placements().stream()
                .filter(placement -> "exclusive".equals(placement.mode()))
                .flatMap(placement -> placement.pcpus().stream())
                .distinct().sorted().toList();
        return resourceView(record, layout, applied, exclusive);
    }

    private CpuLayout requireConfiguredCpuLayout(CpuLayout layout) {
        if (layout == null || !layout.configured() || layout.policyRevision() == null
                || layout.host() == null || layout.host().desiredDom0Pcpus() == null || layout.vms() == null) {
            throw new ApiException(HttpStatus.CONFLICT, "CPU_POLICY_NOT_CONFIGURED", "CPU 배치 정책이 설정되지 않았습니다.");
        }
        return layout;
    }

    private CpuLayout.VmCpuLayout findVmLayout(CpuLayout layout, String vmId) {
        return layout.vms().stream()
                .filter(item -> vmId.equals(item.vmId()))
                .findFirst()
                .orElseThrow(() -> new ApiException(
                        HttpStatus.BAD_GATEWAY, "VM_CONTROL_BAD_RESPONSE", "VM의 CPU 배치 정보를 찾을 수 없습니다."));
    }

    private List<Integer> validatePinningMappings(VmRecord record, List<CpuLayout.VcpuAffinity> mappings) {
        if (mappings == null || mappings.size() != record.getVcpus()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_ARGUMENT", "모든 vCPU의 매핑을 하나씩 보내야 합니다.");
        }
        List<CpuLayout.VcpuAffinity> sorted = mappings.stream()
                .sorted(Comparator.comparingInt(CpuLayout.VcpuAffinity::vcpu)).toList();
        List<Integer> common = null;
        for (int index = 0; index < sorted.size(); index++) {
            CpuLayout.VcpuAffinity mapping = sorted.get(index);
            if (mapping.vcpu() != index || mapping.pcpus() == null || mapping.pcpus().isEmpty()) {
                throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_ARGUMENT", "vCPU 번호와 pCPU 매핑이 올바르지 않습니다.");
            }
            List<Integer> mask = sortedUnique(mapping.pcpus());
            if (mask.size() != mapping.pcpus().size() || mask.stream().anyMatch(cpu -> cpu < 0 || cpu > 3)) {
                throw new ApiException(HttpStatus.BAD_REQUEST, "INVALID_ARGUMENT", "pCPU는 중복 없이 0~3 범위로 지정해야 합니다.");
            }
            if (common == null) {
                common = mask;
            } else if (!common.equals(mask)) {
                throw new ApiException(
                        HttpStatus.BAD_REQUEST,
                        "RESOURCE_UNSUPPORTED",
                        "현재 플랫폼은 한 VM의 모든 vCPU에 동일한 pCPU 집합만 적용할 수 있습니다.");
            }
        }
        return common;
    }

    private List<CpuLayout.VcpuAffinity> requireUniformAffinity(CpuLayout.VmCpuLayout layout) {
        if (layout.desiredHardAffinity() == null || layout.desiredHardAffinity().size() != layout.vcpus()) {
            throw malformedCpuLayout();
        }
        List<CpuLayout.VcpuAffinity> sorted = layout.desiredHardAffinity().stream()
                .sorted(Comparator.comparingInt(CpuLayout.VcpuAffinity::vcpu)).toList();
        List<Integer> mask = null;
        for (int index = 0; index < sorted.size(); index++) {
            CpuLayout.VcpuAffinity item = sorted.get(index);
            List<Integer> current = item.pcpus() == null ? List.of() : sortedUnique(item.pcpus());
            if (item.vcpu() != index || current.isEmpty() || (mask != null && !mask.equals(current))) {
                throw malformedCpuLayout();
            }
            mask = current;
        }
        List<Integer> finalMask = mask;
        return sorted.stream().map(item -> new CpuLayout.VcpuAffinity(item.vcpu(), finalMask)).toList();
    }

    private List<Integer> uniformMask(CpuLayout.VmCpuLayout layout) {
        List<CpuLayout.VcpuAffinity> affinities = requireUniformAffinity(layout);
        return affinities.getFirst().pcpus();
    }

    private ApiException malformedCpuLayout() {
        return new ApiException(
                HttpStatus.BAD_GATEWAY, "VM_CONTROL_BAD_RESPONSE", "플랫폼의 CPU 배치 정보가 올바르지 않습니다.");
    }

    private List<Integer> exclusivePcpus(CpuLayout layout) {
        if (layout.reservations() == null) {
            return List.of();
        }
        return layout.reservations().stream().flatMap(item -> item.pcpus().stream()).distinct().sorted().toList();
    }

    private VmResourceView resourceView(
            VmRecord record,
            CpuLayout layout,
            List<CpuLayout.VcpuAffinity> pinning,
            List<Integer> exclusive) {
        VmTemplate template = templateCatalog.find(record.getTemplateId(), record.getTemplateVersion())
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "TEMPLATE_NOT_FOUND", "존재하지 않는 템플릿입니다."));
        boolean fixed = record.getGuestKind() == GuestKind.ZEPHYR;
        int pcpuCount = layout.host().maxCpuId() + 1;
        List<VmResourceView.PcpuStatus> statuses = new ArrayList<>();
        for (int pcpu = 0; pcpu < pcpuCount; pcpu++) {
            statuses.add(new VmResourceView.PcpuStatus(pcpu, null));
        }
        return new VmResourceView(
                record.getVmId(), record.getVcpus(), record.getMemoryMib(), pcpuCount, pinning,
                List.copyOf(layout.host().desiredDom0Pcpus()), exclusive, statuses,
                new VmResourceView.ResourceLimits(
                        fixed ? record.getVcpus() : 1,
                        fixed ? record.getVcpus() : 4,
                        fixed ? record.getMemoryMib() : template.minMemoryMib(),
                        fixed ? record.getMemoryMib() : 1024,
                        fixed ? 1 : 128),
                clock.instant());
    }

    private static List<Integer> sortedUnique(List<Integer> values) {
        return values.stream().distinct().sorted().toList();
    }

    private void validateCpuPolicyResponse(
            CpuPolicyUpdate update,
            String vmId,
            String mode,
            List<Integer> mask,
            int previousRevision) {
        if (update == null || update.policy() == null || !update.hostPreparationRequired()
                || update.policy().policyRevision() != previousRevision + 1
                || update.policy().placements() == null) {
            throw malformedCpuLayout();
        }
        CpuPolicyUpdate.CpuPlacement target = update.policy().placements().stream()
                .filter(item -> vmId.equals(item.vmId())).findFirst().orElse(null);
        if (target == null || !mode.equals(target.mode()) || !mask.equals(sortedUnique(target.pcpus()))) {
            throw malformedCpuLayout();
        }
    }

    private static void validateResourceUpdateResponse(
            String vmId, int vcpus, int memoryMib, VmResourceUpdate result) {
        VmResources after = result == null ? null : result.after();
        if (result == null || !vmId.equals(result.vmId()) || after == null
                || after.vcpus() != vcpus || after.memoryMib() != memoryMib
                || !result.appliesOnNextStart()) {
            throw new ApiException(
                    HttpStatus.BAD_GATEWAY,
                    "VM_CONTROL_BAD_RESPONSE",
                    "VM 제어 서비스가 올바른 자원 변경 결과를 반환하지 않았습니다.");
        }
    }

    // The platform retires the disk instead of deleting it. Keep the specification and vm_id for a future restore,
    // but hide the retired VM from the current v1 list/get/start/stop surface.
    // dom0-manager answers 404 VMNotFound only when the board has no registration for the VM (contract v1.1):
    // it was already retired or removed with a platform tool, so the delete is complete and the row is retired too.
    public void destroy(String vmId) {
        VmRecord record = findRecord(vmId);
        boolean alreadyGone = underOperation(vmId, () -> {
            boolean gone = false;
            try {
                vmControlPort.destroy(vmId);
            } catch (ApiException exception) {
                // dom0-manager refuses a delete while the domain exists ("stop it before delete").
                if (INVALID_VM_STATE.equals(exception.getCode())) {
                    throw new ApiException(HttpStatus.CONFLICT, INVALID_VM_STATE, "실행 중인 VM은 정지한 뒤 삭제할 수 있습니다.");
                }
                if (!"VM_NOT_FOUND".equals(exception.getCode())) {
                    throw exception;
                }
                gone = true;
            }
            runtimeCache.remove(vmId);
            record.retire();
            vmRecordRepository.save(record);
            stateTracker.forget(vmId);
            return gone;
        });
        if (alreadyGone) {
            eventRecorder.vmDeleted(vmId, record.getName(), "platform");
        } else {
            eventRecorder.vmDeleted(vmId, record.getName());
        }
        // Only after dom0-manager confirmed the delete. Failures are logged there and never fail the delete.
        inventoryCleaner.vmDeleted(vmId);
    }

    // Marks the VM busy so an overlapping on-demand state sync does not report this operation's
    // intermediate states as detected changes. Only one such operation runs per VM (see exclusively).
    private <T> T underOperation(String vmId, Supplier<T> operation) {
        return exclusively(vmId, () -> {
            stateTracker.beginOperation(vmId);
            try {
                return operation.get();
            } finally {
                stateTracker.endOperation(vmId);
            }
        });
    }

    // QA 1-B2: a second create/start/stop/delete/resource/pinning/reboot request for a VM that already has one in
    // flight (another tab or user) is refused at once instead of queueing behind it in dom0-manager, where it would
    // wait silently and then run against whatever state the first one left (e.g. stop then delete).
    // The set is per Spring instance, which is enough while the backend runs as a single instance.
    private <T> T exclusively(String vmId, Supplier<T> operation) {
        if (!inFlight.add(vmId)) {
            throw new ApiException(HttpStatus.CONFLICT, "VM_OPERATION_IN_PROGRESS",
                    "이 VM에 다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요.");
        }
        try {
            return operation.get();
        } finally {
            inFlight.remove(vmId);
        }
    }

    private Vm ensureRunning(VmRecord record) {
        VmRuntime runtime = vmControlPort.find(record.getVmId()).orElse(null);
        if (runtime != null) {
            return Vm.of(record, runtime);
        }
        return launch(record);
    }

    private static void rejectUnstartable(VmState state) {
        String message = switch (state) {
            case RUNNING -> null;
            case CRASHED -> "VM이 크래시 상태입니다. 정지한 뒤 다시 시작하세요.";
            case PAUSED -> "VM이 일시정지 상태입니다. 정지한 뒤 다시 시작하세요.";
            case STOPPING -> "VM이 종료되는 중입니다. 종료된 뒤 다시 시작하세요.";
            case STOPPED -> null;
        };
        if (message != null) {
            throw new ApiException(HttpStatus.CONFLICT, "INVALID_VM_STATE", message);
        }
    }

    private Vm launch(VmRecord record) {
        Vm vm = launchDomain(record);
        stateTracker.changedByUser(record.getVmId(), VmState.STOPPED, vm.state(), VmStateTracker.CAUSE_USER_START);
        return vm;
    }

    // 409 VMExists for our own vm_id means an earlier launch already succeeded (contract 10.1), so the live
    // domain is read back. Both create and start use this: the cached list may not have the domain yet.
    private Vm launchDomain(VmRecord record) {
        VmRuntime runtime;
        try {
            runtime = vmControlPort.launch(record.getVmId(), record.launchSpec());
        } catch (ApiException exception) {
            if (!DOMAIN_EXISTS.equals(exception.getCode())) {
                throw exception;
            }
            runtime = vmControlPort.find(record.getVmId()).orElse(null);
        }
        if (runtime != null) {
            runtimeCache.put(record.getVmId(), runtime);
        }
        return Vm.of(record, runtime);
    }

    /**
     * 장애 주입 강제 종료. dom0-manager kill 로 도메인만 없애고 등록·의도(running)는 남겨 복구 워커가 다시 띄우게 한다.
     * 사용자 정지가 아니므로 stateTracker.changedByUser 로 기록하지 않는다 — 꺼짐은 다음 상태 동기화가 장애로 감지한다.
     */
    public DomainKill kill(String vmId) {
        VmRecord record = findRecord(vmId);
        VmState state = vmControlPort.find(vmId).map(VmRuntime::state).orElse(VmState.STOPPED);
        if (state != VmState.RUNNING) {
            throw new ApiException(HttpStatus.CONFLICT, "INVALID_VM_STATE", "실행 중인 VM만 강제 종료할 수 있습니다.");
        }
        vmControlPort.kill(vmId);
        eventRecorder.faultInjected(vmId, record.getName(), "force_kill");
        return new DomainKill(UUID.randomUUID().toString(), vmId, clock.instant());
    }

    private VmRecord findRecord(String vmId) {
        return vmRecordRepository.findById(vmId)
                .filter(VmRecord::isRegistered)
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "VM_NOT_FOUND", "존재하지 않는 VM입니다."));
    }

    private static String validateRequestKey(String requestKey) {
        if (requestKey == null || requestKey.isEmpty() || requestKey.length() > 200
                || requestKey.chars().anyMatch(character -> character < '!' || character > '~')) {
            throw new ApiException(
                    HttpStatus.BAD_REQUEST,
                    "INVALID_ARGUMENT",
                    "Idempotency-Key는 1~200자의 표시 가능한 ASCII 문자여야 합니다.");
        }
        return requestKey;
    }

    public record CreateResult(Vm vm, boolean created) {
    }
}
