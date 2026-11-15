package com.xenova.backend.agent.application;

import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public class AutoDelayService {

    private final ZephyrFaultService faults;

    public AutoDelayService(ZephyrFaultService faults) {
        this.faults = faults;
    }

    public AutoDelayCommand send(String vmId, int delayMs, int durationS) {
        Map<String, Object> parameters = new LinkedHashMap<>();
        parameters.put("delay_ms", delayMs);
        parameters.put("duration_s", durationS);
        ZephyrFaultCommand command = faults.submit(vmId, "auto_delay", parameters, false);
        return new AutoDelayCommand(command.commandId(), command.operationId(), command.vmId(),
                delayMs, durationS, command.submittedAt(), command.expiresAt());
    }
}
