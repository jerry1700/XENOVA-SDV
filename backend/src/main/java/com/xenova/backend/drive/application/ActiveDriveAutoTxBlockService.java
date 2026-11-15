package com.xenova.backend.drive.application;

import com.xenova.backend.agent.application.ZephyrFaultCommand;
import com.xenova.backend.agent.application.ZephyrFaultService;
import com.xenova.backend.common.error.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/** 최신 AUTO 제어 주체의 drive.command 송신만 차단한다. */
@Service
public class ActiveDriveAutoTxBlockService {

    private final DriveStateTracker tracker;
    private final ZephyrFaultService faultService;

    public ActiveDriveAutoTxBlockService(DriveStateTracker tracker, ZephyrFaultService faultService) {
        this.tracker = tracker;
        this.faultService = faultService;
    }

    public ZephyrFaultCommand send(int durationS) {
        DriveState.Control control = tracker.snapshot().control();
        if (control == null || control.source().status() != DriveState.SourceStatus.FRESH) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "DRIVE_STATE_UNAVAILABLE",
                    "최신 주행 상태를 확인할 수 없어 결과 송신 장애 시연을 시작하지 못했습니다.");
        }
        if (!"AUTO".equals(control.mode()) || !"autonomous".equals(control.activeApp())) {
            throw new ApiException(HttpStatus.CONFLICT, "DRIVE_MODE_NOT_AUTO",
                    "자율주행 앱이 AUTO 제어 중일 때만 결과 송신 장애 시연을 시작할 수 있습니다.");
        }
        if (control.activeVmId() == null || control.activeVmId().isBlank()) {
            throw new ApiException(HttpStatus.CONFLICT, "ACTIVE_DRIVE_VM_UNAVAILABLE",
                    "현재 자율주행을 수행하는 VM을 확인할 수 없습니다.");
        }
        return faultService.blockAutoTx(control.activeVmId(), durationS);
    }
}
