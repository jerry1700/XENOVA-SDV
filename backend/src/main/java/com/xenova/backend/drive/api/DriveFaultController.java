package com.xenova.backend.drive.api;

import com.xenova.backend.agent.application.AutoDelayCommand;
import com.xenova.backend.agent.application.ZephyrFaultCommand;
import com.xenova.backend.drive.application.ActiveDriveAutoDelayService;
import com.xenova.backend.drive.application.ActiveDriveAppExitService;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 시연용 주행 장애 주입. 대상 VM은 검증된 최신 주행 상태에서 서버가 결정한다. */
@RestController
@RequestMapping("/api/v1/drive/faults")
public class DriveFaultController {

    private final ActiveDriveAutoDelayService service;
    private final ActiveDriveAppExitService appExitService;

    public DriveFaultController(ActiveDriveAutoDelayService service, ActiveDriveAppExitService appExitService) {
        this.service = service;
        this.appExitService = appExitService;
    }

    @PostMapping("/auto-delay")
    public ResponseEntity<AutoDelayCommand> autoDelay(@Valid @RequestBody DriveAutoDelayRequest request) {
        return ResponseEntity.accepted().body(service.send(request.delayMs(), request.durationS()));
    }

    @PostMapping("/app-exit")
    public ResponseEntity<ZephyrFaultCommand> appExit(@Valid @RequestBody DriveAppExitRequest request) {
        return ResponseEntity.accepted().body(appExitService.send());
    }
}
