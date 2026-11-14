package com.xenova.backend.drive.application;

/** Jetson F02 인지 결과방향 장애 제어 메시지를 발행한다. */
public interface PerceptionFaultPublisher {
    boolean publish(String topic, byte[] payload, int qos);
}
