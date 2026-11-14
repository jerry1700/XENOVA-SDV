package com.xenova.backend.testsupport;

import java.util.UUID;
import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttException;

// Test MQTT clients against the embedded broker.
// - Each test gets its own client id. Reusing one id makes the broker drop the previous connection when the next
//   test connects, and that drop can land while the previous test is still tearing down.
// - Teardown never fails the test: if the broker already closed the connection, disconnect() throws
//   "Connection lost (32109)" although the test itself passed (CI MR !659, DriveRelayIntegrationTest).
public final class MqttTestClients {

    private static final long QUIESCE_MS = 500;
    private static final long DISCONNECT_TIMEOUT_MS = 1000;

    private MqttTestClients() {
    }

    public static String uniqueId(String prefix) {
        return prefix + "-" + UUID.randomUUID().toString().substring(0, 8);
    }

    public static void closeQuietly(MqttClient client) {
        if (client == null) {
            return;
        }
        try {
            if (client.isConnected()) {
                client.disconnectForcibly(QUIESCE_MS, DISCONNECT_TIMEOUT_MS);
            }
        } catch (MqttException ignored) {
            // The broker already dropped the connection: nothing left to disconnect.
        }
        try {
            client.close(true);
        } catch (MqttException ignored) {
            // Resources are released by force; a failure here cannot affect the next test.
        }
    }
}
