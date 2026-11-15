package com.xenova.backend.drive.application;

import com.xenova.backend.agent.application.AutoDelayCommand;
import com.xenova.backend.agent.application.AutoDelayService;
import com.xenova.backend.common.error.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/** 현재 AUTO 명령을 생성하는 VM에만 연산 지연을 주어 시연 대상 선택 실수를 막는다. */
@Service
public class ActiveDriveAutoDelayService {

    private final DriveStateTracker tracker;
    private final AutoDelayService autoDelayService;

    public ActiveDriveAutoDelayService(DriveStateTracker tracker, AutoDelayService autoDelayService) {
        this.tracker = tracker;
        this.autoDelayService = autoDelayService;
    }

    public AutoDelayCommand send(int delayMs, int durationS) {
        DriveState.Control control = tracker.snapshot().control();
        if (control == null || control.source().status() != DriveState.SourceStatus.FRESH) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "DRIVE_STATE_UNAVAILABLE",
                    "최신 주행 상태를 확인할 수 없어 연산 지연을 시작하지 못했습니다.");
        }
        if (!"AUTO".equals(control.mode())) {
            throw new ApiException(HttpStatus.CONFLICT, "DRIVE_MODE_NOT_AUTO",
                    "자동주행 중에만 연산 지연 시나리오를 시작할 수 있습니다.");
        }
        if (control.activeVmId() == null || control.activeVmId().isBlank()) {
            throw new ApiException(HttpStatus.CONFLICT, "ACTIVE_DRIVE_VM_UNAVAILABLE",
                    "현재 자동주행을 수행하는 VM을 확인할 수 없습니다.");
        }
        return autoDelayService.send(control.activeVmId(), delayMs, durationS);
    }
}
