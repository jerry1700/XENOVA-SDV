package com.xenova.backend.vm.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.xenova.backend.common.error.ApiException;
import com.xenova.backend.agent.application.AgentInventoryCleaner;
import com.xenova.backend.event.application.EventRecorder;
import com.xenova.backend.event.application.VmStateTracker;
import com.xenova.backend.template.application.TemplateCatalog;
import com.xenova.backend.template.domain.GuestKind;
import com.xenova.backend.vm.domain.VmRole;
import com.xenova.backend.template.domain.VmTemplate;
import com.xenova.backend.template.infrastructure.TemplateCatalogProperties;
import com.xenova.backend.vm.application.VmService.CreateResult;
import com.xenova.backend.vm.domain.CreateVmCommand;
import com.xenova.backend.vm.domain.CpuLayout;
import com.xenova.backend.vm.domain.CpuPolicyUpdate;
import com.xenova.backend.vm.domain.Vm;
import com.xenova.backend.vm.domain.VmControlPort;
import com.xenova.backend.vm.domain.VmControlOperation;
import com.xenova.backend.vm.domain.VmLaunchSpec;
import com.xenova.backend.vm.domain.VmLifecycle;
import com.xenova.backend.vm.domain.VmRecord;
import com.xenova.backend.vm.domain.VmRecordRepository;
import com.xenova.backend.vm.domain.VmResourceUpdate;
import com.xenova.backend.vm.domain.VmResources;
import com.xenova.backend.vm.domain.VmResourceView;
import com.xenova.backend.vm.domain.VmRuntime;
import com.xenova.backend.vm.domain.VmState;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;

@ExtendWith(MockitoExtension.class)
class VmServiceTest {

    private static final Instant NOW = Instant.parse("2026-10-18T03:00:00Z");
    private static final CreateVmCommand ALPINE = new CreateVmCommand("alpine-sensor", "alpine", "1.2.0", 1, 256);
    private static final VmLaunchSpec ALPINE_SPEC = new VmLaunchSpec("alpine-sensor", "alpine", "1.2.0", 1, 256);

    @Mock
    private VmControlPort vmControlPort;

    @Mock
    private VmRecordRepository vmRecordRepository;

    @Mock
    private EventRecorder eventRecorder;

    @Mock
    private AgentInventoryCleaner inventoryCleaner;

    @Mock
    private VmStateTracker stateTracker;

    private VmService service;

    @BeforeEach
    void setUp() {
        TemplateCatalog templateCatalog = new TemplateCatalog(new TemplateCatalogProperties(
                List.of(new VmTemplate("alpine", "1.2.0", "Alpine 1.2.0", 128, GuestKind.LINUX))));
        Clock clock = Clock.fixed(NOW, ZoneOffset.UTC);
        // Disabled cache: every read goes to the port, as these cases were written for.
        VmRuntimeCache runtimeCache = new VmRuntimeCache(
                vmControlPort, new VmRuntimeCacheProperties(false, Duration.ofSeconds(1), Duration.ofSeconds(10)), clock);
        service = new VmService(vmControlPort, vmRecordRepository, templateCatalog, eventRecorder, stateTracker, runtimeCache, inventoryCleaner, clock);
    }

    @Test
    void listCombinesRecordsWithXenStateAndReportsAbsentDomainsAsStopped() {
        VmRecord running = record("vm-1", "alpine-1");
        VmRecord stopped = record("vm-2", "alpine-2");
        when(vmRecordRepository.findAllByLifecycleOrLifecycleIsNullOrderByCreatedAtAsc(VmLifecycle.REGISTERED))
                .thenReturn(List.of(running, stopped));
        when(vmControlPort.list()).thenReturn(List.of(
                new VmRuntime("vm-1", VmState.RUNNING, 2, 512),
                new VmRuntime("unknown-domain", VmState.RUNNING, 1, 128)));

        List<Vm> vms = service.list();

        assertThat(vms).extracting(Vm::vmId).containsExactly("vm-1", "vm-2");
        assertThat(vms.get(0).state()).isEqualTo(VmState.RUNNING);
        assertThat(vms.get(0).name()).isEqualTo("alpine-1");
        assertThat(vms.get(0).templateId()).isEqualTo("alpine");
        assertThat(vms.get(0).vcpus()).isEqualTo(2);
        assertThat(vms.get(0).memoryMib()).isEqualTo(512);
        assertThat(vms.get(1).state()).isEqualTo(VmState.STOPPED);
        assertThat(vms.get(1).templateId()).isEqualTo("alpine");
        assertThat(vms.get(1).vcpus()).isEqualTo(1);
        assertThat(vms.get(1).memoryMib()).isEqualTo(256);
        verify(vmRecordRepository).findAllByLifecycleOrLifecycleIsNullOrderByCreatedAtAsc(VmLifecycle.REGISTERED);
    }

    @Test
    void listOrdersByRoleThenDriveGuestsThenCreationWhateverFilledTheTable() {
        // Creation order as a registry sync can leave it: the Linux VM was discovered a few ms first.
        VmRecord interaction = new VmRecord("vm-d", new CreateVmCommand("debian", "alpine", "1.2.0", 1, 256),
                GuestKind.LINUX, NOW);
        VmRecord noRoleDrive = new VmRecord("vm-z", new CreateVmCommand("zephyr-c", "alpine", "1.2.0", 1, 16),
                GuestKind.ZEPHYR, NOW.plusMillis(1));
        VmRecord backup = new VmRecord("vm-b", new CreateVmCommand("zephyr-b", "alpine", "1.2.0", 1, 16),
                GuestKind.ZEPHYR, NOW.plusMillis(2));
        VmRecord primary = new VmRecord("vm-a", new CreateVmCommand("zephyr-a", "alpine", "1.2.0", 1, 16),
                GuestKind.ZEPHYR, NOW.plusMillis(3));
        VmRecord unassignedLinux = new VmRecord("vm-l", new CreateVmCommand("web-test", "alpine", "1.2.0", 1, 256),
                GuestKind.LINUX, NOW.plusMillis(4));
        backup.changeRole(VmRole.BACKUP_DRIVE);
        primary.changeRole(VmRole.PRIMARY_DRIVE);
        interaction.changeRole(VmRole.INTERACTION);
        when(vmRecordRepository.findAllByLifecycleOrLifecycleIsNullOrderByCreatedAtAsc(VmLifecycle.REGISTERED))
                .thenReturn(List.of(interaction, noRoleDrive, backup, primary, unassignedLinux));
        when(vmControlPort.list()).thenReturn(List.of());

        assertThat(service.list()).extracting(Vm::vmId).containsExactly("vm-a", "vm-b", "vm-d", "vm-z", "vm-l");
    }

    @Test
    void createSavesRecordAndLaunchesWithStoredName() {
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.empty());
        when(vmRecordRepository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(vmControlPort.launch(anyString(), eq(ALPINE_SPEC))).thenAnswer(invocation ->
                new VmRuntime(invocation.getArgument(0), VmState.RUNNING, 1, 256));

        CreateResult result = service.create(ALPINE);

        ArgumentCaptor<String> vmId = ArgumentCaptor.forClass(String.class);
        verify(vmControlPort).launch(vmId.capture(), eq(ALPINE_SPEC));
        assertThat(UUID.fromString(vmId.getValue()).toString()).isEqualTo(result.vm().vmId());
        assertThat(result.created()).isTrue();
        assertThat(result.vm().state()).isEqualTo(VmState.RUNNING);
        assertThat(result.vm().name()).isEqualTo("alpine-sensor");
        verify(eventRecorder).vmCreated(any(VmRecord.class));
        verify(stateTracker).remember(result.vm().vmId(), VmState.RUNNING);
        verify(stateTracker, never()).changedByUser(anyString(), any(), any(), anyString());
    }

    @Test
    void retriedCreateWithSameSpecReturnsExistingVm() {
        VmRecord existing = record("vm-1", "alpine-sensor");
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.of(existing));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.of(new VmRuntime("vm-1", VmState.RUNNING, 1, 256)));

        CreateResult result = service.create(ALPINE);

        assertThat(result.created()).isFalse();
        assertThat(result.vm().vmId()).isEqualTo("vm-1");
        verify(vmRecordRepository, never()).save(any());
        verify(vmControlPort, never()).launch(anyString(), any());
    }

    @Test
    void createWithTakenNameAndDifferentSpecIsRejected() {
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.of(record("vm-1", "alpine-sensor")));

        assertThatThrownBy(() -> service.create(new CreateVmCommand("alpine-sensor", "alpine", "1.2.0", 2, 256)))
                .isInstanceOfSatisfying(ApiException.class, exception ->
                        assertThat(exception.getCode()).isEqualTo("VM_NAME_CONFLICT"));
        verify(vmControlPort, never()).launch(anyString(), any());
    }

    @Test
    void createRejectedByDom0RemovesTheRecord() {
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.empty());
        when(vmRecordRepository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(vmControlPort.launch(anyString(), any())).thenThrow(
                new ApiException(HttpStatus.INSUFFICIENT_STORAGE, "INSUFFICIENT_RESOURCES", "메모리 부족"));

        assertThatThrownBy(() -> service.create(ALPINE)).isInstanceOf(ApiException.class);
        verify(vmRecordRepository).delete(any(VmRecord.class));
    }

    @Test
    void createRefusedAsNotSupportedRemovesTheRecord() {
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.empty());
        when(vmRecordRepository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(vmControlPort.launch(anyString(), any())).thenThrow(
                new ApiException(HttpStatus.NOT_IMPLEMENTED, "VM_CONTROL_NOT_SUPPORTED", "지원하지 않음"));

        assertThatThrownBy(() -> service.create(ALPINE)).isInstanceOf(ApiException.class);
        verify(vmRecordRepository).delete(any(VmRecord.class));
    }

    @Test
    void createRefusedForAPlatformNameConflictRemovesTheRecordAndIsNotASuccess() {
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.empty());
        when(vmRecordRepository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(vmControlPort.launch(anyString(), any())).thenThrow(
                new ApiException(HttpStatus.CONFLICT, "VM_NAME_CONFLICT", "이름 충돌"));

        assertThatThrownBy(() -> service.create(ALPINE))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(exception.getCode()).isEqualTo("VM_NAME_CONFLICT");
                });
        verify(vmRecordRepository).delete(any(VmRecord.class));
        verify(vmControlPort, never()).find(anyString());
        verify(eventRecorder, never()).vmCreated(any());
    }

    @Test
    void startRefusedForAPlatformNameConflictIsNotTreatedAsStarted() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.empty());
        when(vmControlPort.launch("vm-1", new VmLaunchSpec("alpine-1", "alpine", "1.2.0", 1, 256))).thenThrow(
                new ApiException(HttpStatus.CONFLICT, "VM_NAME_CONFLICT", "이름 충돌"));

        assertThatThrownBy(() -> service.start("vm-1"))
                .isInstanceOfSatisfying(ApiException.class,
                        exception -> assertThat(exception.getCode()).isEqualTo("VM_NAME_CONFLICT"));
        verify(stateTracker, never()).changedByUser(anyString(), any(), any(), anyString());
        verify(vmRecordRepository, never()).delete(any(VmRecord.class));
    }

    @Test
    void createThatTimedOutKeepsTheRecordForARetry() {
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.empty());
        when(vmRecordRepository.save(any(VmRecord.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(vmControlPort.launch(anyString(), any())).thenThrow(
                new ApiException(HttpStatus.GATEWAY_TIMEOUT, "VM_CONTROL_TIMEOUT", "시간 초과"));

        assertThatThrownBy(() -> service.create(ALPINE)).isInstanceOf(ApiException.class);
        verify(vmRecordRepository, never()).delete(any(VmRecord.class));
    }

    @Test
    void startLaunchesTheRecordedSpecAgainWhenTheDomainIsAbsent() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.empty());
        when(vmControlPort.launch("vm-1", new VmLaunchSpec("alpine-1", "alpine", "1.2.0", 1, 256)))
                .thenReturn(new VmRuntime("vm-1", VmState.RUNNING, 1, 256));

        assertThat(service.start("vm-1").state()).isEqualTo(VmState.RUNNING);
        verify(stateTracker).changedByUser("vm-1", VmState.STOPPED, VmState.RUNNING, "user_start");
    }

    @Test
    void startOfARunningVmDoesNotLaunchAgain() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.of(new VmRuntime("vm-1", VmState.RUNNING, 1, 256)));

        assertThat(service.start("vm-1").state()).isEqualTo(VmState.RUNNING);
        verify(vmControlPort, never()).launch(anyString(), any());
        verify(stateTracker, never()).changedByUser(anyString(), any(), any(), anyString());
    }

    @Test
    void startTreatsVmExistsAsAlreadyStarted() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1"))
                .thenReturn(Optional.empty())
                .thenReturn(Optional.of(new VmRuntime("vm-1", VmState.RUNNING, 1, 256)));
        when(vmControlPort.launch("vm-1", new VmLaunchSpec("alpine-1", "alpine", "1.2.0", 1, 256))).thenThrow(
                new ApiException(HttpStatus.CONFLICT, "VM_ALREADY_EXISTS", "exists"));

        assertThat(service.start("vm-1").state()).isEqualTo(VmState.RUNNING);
    }

    @ParameterizedTest
    @EnumSource(value = VmState.class, names = {"CRASHED", "PAUSED", "STOPPING"})
    void startOfADomainThatIsNotRunningIsRefused(VmState state) {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.of(new VmRuntime("vm-1", state, 1, 256)));

        assertThatThrownBy(() -> service.start("vm-1"))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(exception.getCode()).isEqualTo("INVALID_VM_STATE");
                });
        verify(vmControlPort, never()).launch(anyString(), any());
        verify(stateTracker, never()).changedByUser(anyString(), any(), any(), anyString());
    }

    @Test
    void stopRecordsTheStateItStoppedFrom() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.of(new VmRuntime("vm-1", VmState.RUNNING, 1, 256)));

        assertThat(service.stop("vm-1").state()).isEqualTo(VmState.STOPPED);
        verify(vmControlPort).stop("vm-1");
        verify(stateTracker).changedByUser("vm-1", VmState.RUNNING, VmState.STOPPED, "user_stop");
    }

    @Test
    void stopOfACrashedVmRecordsCrashedAsTheFormerState() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.of(new VmRuntime("vm-1", VmState.CRASHED, 1, 256)));

        service.stop("vm-1");

        verify(vmControlPort).stop("vm-1");
        verify(stateTracker).changedByUser("vm-1", VmState.CRASHED, VmState.STOPPED, "user_stop");
    }

    @Test
    void stopWithoutRuntimeStillForwardsTheStopIntent() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.empty());

        assertThat(service.stop("vm-1").state()).isEqualTo(VmState.STOPPED);
        verify(vmControlPort).stop("vm-1");
        verify(stateTracker, never()).changedByUser(anyString(), any(), any(), anyString());
    }

    @Test
    void rebootReturnsTheTrackedOperationWithoutWaitingForTheNewExecution() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        VmControlOperation pending = operation("vm-1", "reboot-1", "pending");
        when(vmControlPort.requestReboot("vm-1", "reboot-1")).thenReturn(pending);

        VmControlOperation result = service.reboot("vm-1", "reboot-1");

        assertThat(result).isEqualTo(pending);
        verify(vmControlPort).requestReboot("vm-1", "reboot-1");
        verify(vmControlPort, never()).find("vm-1");
    }

    @Test
    void controlOperationReadsTheOriginalRequestWithoutRebootingAgain() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        VmControlOperation completed = operation("vm-1", "reboot-1", "completed");
        when(vmControlPort.findControlOperation("vm-1", "reboot-1")).thenReturn(completed);

        assertThat(service.controlOperation("vm-1", "reboot-1")).isEqualTo(completed);
        verify(vmControlPort).findControlOperation("vm-1", "reboot-1");
        verify(vmControlPort, never()).requestReboot(anyString(), anyString());
    }

    @Test
    void rebootRejectsAnInvalidIdempotencyKeyBeforeCallingDom0() {
        assertThatThrownBy(() -> service.reboot("vm-1", "contains space"))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.BAD_REQUEST);
                    assertThat(exception.getCode()).isEqualTo("INVALID_ARGUMENT");
                });
        verify(vmControlPort, never()).requestReboot(anyString(), anyString());
    }

    @Test
    void linuxResourcesAreUpdatedOnThePlatformAndSavedForTheNextStart() {
        VmRecord record = record("vm-1", "alpine-1");
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record));
        VmResourceUpdate update = new VmResourceUpdate(
                "vm-1", "operation-1", new VmResources(1, 256), new VmResources(2, 512), true);
        when(vmControlPort.updateResources("vm-1", 2, 512, "resize-1")).thenReturn(update);

        assertThat(service.updateResources("vm-1", 2, 512, "resize-1")).isEqualTo(update);

        assertThat(record.getVcpus()).isEqualTo(2);
        assertThat(record.getMemoryMib()).isEqualTo(512);
        verify(vmRecordRepository).save(record);
        verify(stateTracker).beginOperation("vm-1");
        verify(stateTracker).endOperation("vm-1");
    }

    @Test
    void zephyrResourcesAreRejectedBeforeCallingThePlatform() {
        VmRecord record = new VmRecord(
                "vm-z", new CreateVmCommand("zephyr", "alpine", "1.2.0", 1, 256), GuestKind.ZEPHYR, NOW);
        when(vmRecordRepository.findById("vm-z")).thenReturn(Optional.of(record));

        assertThatThrownBy(() -> service.updateResources("vm-z", 2, 512, "resize-1"))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.BAD_REQUEST);
                    assertThat(exception.getCode()).isEqualTo("RESOURCE_UPDATE_UNSUPPORTED");
                });
        verify(vmControlPort, never()).updateResources(anyString(), any(Integer.class), any(Integer.class), anyString());
    }

    @Test
    void resourceUpdateRejectsMemoryBelowTheTemplateMinimum() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));

        assertThatThrownBy(() -> service.updateResources("vm-1", 1, 127, "resize-1"))
                .isInstanceOfSatisfying(ApiException.class, exception ->
                        assertThat(exception.getCode()).isEqualTo("VALIDATION_ERROR"));
        verify(vmControlPort, never()).updateResources(anyString(), any(Integer.class), any(Integer.class), anyString());
    }

    @Test
    void resourcesExposeThePlatformHardAffinity() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.cpuLayout()).thenReturn(cpuLayout());

        VmResourceView result = service.resources("vm-1");

        assertThat(result.pcpuCount()).isEqualTo(4);
        assertThat(result.vcpuPinning()).containsExactly(new CpuLayout.VcpuAffinity(0, List.of(0, 3)));
        assertThat(result.exclusivePcpus()).containsExactly(1);
        assertThat(result.pcpus()).allSatisfy(pcpu -> assertThat(pcpu.loadPercent()).isNull());
    }

    @Test
    void pinningUpdateRebuildsTheFullPolicyAndPreservesOtherVmPlacements() {
        VmRecord record = record("vm-1", "alpine-1");
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record));
        when(vmControlPort.cpuLayout()).thenReturn(cpuLayout());
        CpuPolicyUpdate update = new CpuPolicyUpdate(
                new CpuPolicyUpdate.CpuPolicy(
                        "2.0", "operation-1", 3, "Pool-0", List.of(0, 3), List.of(
                        new CpuPolicyUpdate.CpuPlacement("vm-1", "shared", List.of(3)),
                        new CpuPolicyUpdate.CpuPlacement("vm-z", "exclusive", List.of(1)))),
                true);
        when(vmControlPort.updateCpuPolicy(any(), eq("pin-1"))).thenReturn(update);

        VmResourceView result = service.updateVcpuPinning(
                "vm-1", List.of(new CpuLayout.VcpuAffinity(0, List.of(3))), "pin-1");

        ArgumentCaptor<CpuPolicyUpdate.Request> request = ArgumentCaptor.forClass(CpuPolicyUpdate.Request.class);
        verify(vmControlPort).updateCpuPolicy(request.capture(), eq("pin-1"));
        assertThat(request.getValue().expectedPolicyRevision()).isEqualTo(2);
        assertThat(request.getValue().sharedPcpus()).containsExactly(0, 3);
        assertThat(request.getValue().placements()).containsExactly(
                new CpuPolicyUpdate.CpuPlacement("vm-1", "shared", List.of(3)),
                new CpuPolicyUpdate.CpuPlacement("vm-z", "exclusive", List.of(1)));
        assertThat(result.vcpuPinning()).containsExactly(new CpuLayout.VcpuAffinity(0, List.of(3)));
        assertThat(result.exclusivePcpus()).containsExactly(1);
    }

    @Test
    void differentMasksPerVcpuAreRejectedBeforeCallingThePlatform() {
        VmRecord record = new VmRecord(
                "vm-2", new CreateVmCommand("linux-2", "alpine", "1.2.0", 2, 256), GuestKind.LINUX, NOW);
        when(vmRecordRepository.findById("vm-2")).thenReturn(Optional.of(record));

        assertThatThrownBy(() -> service.updateVcpuPinning("vm-2", List.of(
                new CpuLayout.VcpuAffinity(0, List.of(0)),
                new CpuLayout.VcpuAffinity(1, List.of(3))), "pin-1"))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.BAD_REQUEST);
                    assertThat(exception.getCode()).isEqualTo("RESOURCE_UNSUPPORTED");
                });
        verify(vmControlPort, never()).cpuLayout();
        verify(vmControlPort, never()).updateCpuPolicy(any(), anyString());
    }

    @Test
    void exclusivePinningRejectsCoresReservedByDom0OrAnotherVmBeforeCallingThePlatform() {
        VmRecord record = record("vm-1", "alpine-1");
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record));
        when(vmControlPort.cpuLayout()).thenReturn(exclusiveCpuLayout());

        assertThatThrownBy(() -> service.updateVcpuPinning(
                "vm-1", List.of(new CpuLayout.VcpuAffinity(0, List.of(0, 1))), "pin-conflict"))
                .isInstanceOfSatisfying(ApiException.class, exception -> {
                    assertThat(exception.getStatus()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(exception.getCode()).isEqualTo("CPU_PLACEMENT_CONFLICT");
                    assertThat(exception.getMessage()).contains("0", "1");
                });
        verify(vmControlPort, never()).updateCpuPolicy(any(), anyString());
    }

    @Test
    void destroyRetiresDomainAndKeepsTheRecordForRestore() {
        VmRecord record = record("vm-1", "alpine-1");
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record));

        service.destroy("vm-1");

        verify(vmControlPort).destroy("vm-1");
        verify(vmRecordRepository).save(record);
        assertThat(record.getLifecycle()).isEqualTo(VmLifecycle.RETIRED);
        verify(eventRecorder).vmDeleted("vm-1", "alpine-1");
        verify(stateTracker).forget("vm-1");
        // Contract mqtt §3: the deleted VM's retained and stored inventory goes after dom0 confirmed the delete.
        verify(inventoryCleaner).vmDeleted("vm-1");
    }

    @Test
    void failedDestroyKeepsTheInventory() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        org.mockito.Mockito.doThrow(new ApiException(HttpStatus.CONFLICT, "INVALID_VM_STATE", "실행 중"))
                .when(vmControlPort).destroy("vm-1");

        assertThatThrownBy(() -> service.destroy("vm-1")).isInstanceOf(ApiException.class);
        verify(inventoryCleaner, never()).vmDeleted(anyString());
    }

    @Test
    void destroyOfAVmTheBoardNoLongerRegistersRetiresTheRow() {
        VmRecord record = record("vm-1", "web-test");
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record));
        org.mockito.Mockito.doThrow(new ApiException(HttpStatus.NOT_FOUND, "VM_NOT_FOUND", "instance vm-1 not registered"))
                .when(vmControlPort).destroy("vm-1");

        service.destroy("vm-1");

        assertThat(record.getLifecycle()).isEqualTo(VmLifecycle.RETIRED);
        verify(vmRecordRepository).save(record);
        verify(stateTracker).forget("vm-1");
        verify(eventRecorder).vmDeleted("vm-1", "web-test", "platform");
        verify(inventoryCleaner).vmDeleted("vm-1");
    }

    @Test
    void retiredVmIsHiddenAndItsNameRemainsReserved() {
        VmRecord retired = record("vm-1", "alpine-sensor");
        retired.retire();
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(retired));
        when(vmRecordRepository.findByName("alpine-sensor")).thenReturn(Optional.of(retired));

        assertThatThrownBy(() -> service.get("vm-1"))
                .isInstanceOfSatisfying(ApiException.class, exception ->
                        assertThat(exception.getCode()).isEqualTo("VM_NOT_FOUND"));
        assertThatThrownBy(() -> service.create(ALPINE))
                .isInstanceOfSatisfying(ApiException.class, exception ->
                        assertThat(exception.getCode()).isEqualTo("VM_NAME_CONFLICT"));
        verify(vmControlPort, never()).launch(anyString(), any());
    }

    @Test
    void failedStopRecordsNoEvent() {
        when(vmRecordRepository.findById("vm-1")).thenReturn(Optional.of(record("vm-1", "alpine-1")));
        when(vmControlPort.find("vm-1")).thenReturn(Optional.of(new VmRuntime("vm-1", VmState.RUNNING, 1, 256)));
        org.mockito.Mockito.doThrow(new ApiException(HttpStatus.GATEWAY_TIMEOUT, "VM_CONTROL_TIMEOUT", "시간 초과"))
                .when(vmControlPort).stop("vm-1");

        assertThatThrownBy(() -> service.stop("vm-1")).isInstanceOf(ApiException.class);
        verify(stateTracker, never()).changedByUser(anyString(), any(), any(), anyString());
    }

    @Test
    void unknownVmIsNotFound() {
        when(vmRecordRepository.findById("missing")).thenReturn(Optional.empty());

        assertThatThrownBy(() -> service.get("missing"))
                .isInstanceOfSatisfying(ApiException.class, exception ->
                        assertThat(exception.getCode()).isEqualTo("VM_NOT_FOUND"));
    }

    private static VmRecord record(String vmId, String name) {
        return new VmRecord(vmId, new CreateVmCommand(name, "alpine", "1.2.0", 1, 256), NOW);
    }

    private static CpuLayout cpuLayout() {
        return new CpuLayout(
                true,
                2,
                "2.0",
                new CpuLayout.CpuHost(List.of(0, 1, 2, 3), 3, List.of(0, 3), true),
                List.of(new CpuLayout.CpuReservation("vm-z", List.of(1))),
                List.of(
                        new CpuLayout.VmCpuLayout(
                                "vm-1", 1, "shared", List.of(new CpuLayout.VcpuAffinity(0, List.of(0, 3)))),
                        new CpuLayout.VmCpuLayout(
                                "vm-z", 1, "exclusive", List.of(new CpuLayout.VcpuAffinity(0, List.of(1))))));
    }

    private static CpuLayout exclusiveCpuLayout() {
        return new CpuLayout(
                true,
                2,
                "2.0",
                new CpuLayout.CpuHost(List.of(0, 1, 2, 3), 3, List.of(0, 3), true),
                List.of(
                        new CpuLayout.CpuReservation("vm-1", List.of(2)),
                        new CpuLayout.CpuReservation("vm-z", List.of(1))),
                List.of(
                        new CpuLayout.VmCpuLayout(
                                "vm-1", 1, "exclusive", List.of(new CpuLayout.VcpuAffinity(0, List.of(2)))),
                        new CpuLayout.VmCpuLayout(
                                "vm-z", 1, "exclusive", List.of(new CpuLayout.VcpuAffinity(0, List.of(1))))));
    }

    private static VmControlOperation operation(String vmId, String requestKey, String status) {
        return new VmControlOperation(
                vmId, "reboot", requestKey, UUID.randomUUID().toString(), status, true,
                status, 1L, UUID.randomUUID().toString(), 2L, null, null, null, null, List.of());
    }
}
