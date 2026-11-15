package com.xenova.backend.drive.application;

import com.xenova.backend.agent.application.VmCrashCommand;
import com.xenova.backend.agent.application.VmCrashService;
import com.xenova.backend.common.error.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/** 최신 AUTO 제어 주체만 crash 대상으로 선택해 오래된 프론트 대상 선택을 막는다. */
@Service
public class ActiveDriveVmCrashService {

    private final DriveStateTracker tracker;
    private final VmCrashService vmCrashService;

    public ActiveDriveVmCrashService(DriveStateTracker tracker, VmCrashService vmCrashService) {
        this.tracker = tracker;
        this.vmCrashService = vmCrashService;
    }

    public VmCrashCommand send() {
        DriveState.Control control = tracker.snapshot().control();
        if (control == null || control.source().status() != DriveState.SourceStatus.FRESH) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "DRIVE_STATE_UNAVAILABLE",
                    "최신 주행 상태를 확인할 수 없어 VM 장애 시연을 시작하지 못했습니다.");
        }
        if (!"AUTO".equals(control.mode()) || !"autonomous".equals(control.activeApp())) {
            throw new ApiException(HttpStatus.CONFLICT, "DRIVE_MODE_NOT_AUTO",
                    "자율주행 앱이 AUTO 제어 중일 때만 VM 장애 시연을 시작할 수 있습니다.");
        }
        if (control.activeVmId() == null || control.activeVmId().isBlank()) {
            throw new ApiException(HttpStatus.CONFLICT, "ACTIVE_DRIVE_VM_UNAVAILABLE",
                    "현재 자율주행을 수행하는 VM을 확인할 수 없습니다.");
        }
        return vmCrashService.send(control.activeVmId());
    }
}
